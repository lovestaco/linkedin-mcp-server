"""
msgkit lead-enrichment tools (fork-only: github.com/lovestaco/linkedin-mcp-server).

Built for msgkit-server's workers, which call them directly over stdio with no
LLM in between. Each tool opens exactly one LinkedIn page and returns small,
structured data instead of the whole page text:

- msgkit_person_activity: /in/<user>/recent-activity/all/ only (no main profile
  load), the newest posts with their text, and each post's share link read
  through its own "Copy link to post" menu item.
- msgkit_person_company: /in/<user>/details/experience/, for the current company link.
- msgkit_account: who this browser is logged in as, and whether it is Premium.
- msgkit_dm_check: read-only check of one person before a DM is prepared (degree, URN,
  existing conversation); sends nothing.
- msgkit_connect: send one connection request (no note unless given) on a single
  profile view, and read the profile-tracking fields off that same page.

Copying a link never touches the machine's clipboard: the page's own
navigator.clipboard.writeText is replaced in the page's main world before the
menu item is clicked, and the copied text is read back from there.
"""

import asyncio
import logging
import random
import re
from typing import Annotated, Any
from urllib.parse import quote_plus

from fastmcp import Context, FastMCP
from pydantic import Field

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.dependencies import get_ready_extractor, tool_errors
from linkedin_mcp_server.linkedin import connection
from linkedin_mcp_server.linkedin.identifiers import normalize_person_identifier
from linkedin_mcp_server.tools.msgkit_connections import CONNECTIONS_URL, list_connections
from linkedin_mcp_server.tools.msgkit_profile import build_fields

logger = logging.getLogger(__name__)

_CARD = 'main [componentkey^="update-card-focus"]'

# Per card: visible lines (for author/age/counts), the post text, and the link
# to an embedded original post (present on reposts).
_CARDS_JS = r"""(limit) => {
  const cards = [...document.querySelectorAll('main [componentkey^="update-card-focus"]')].slice(0, limit);
  return cards.map((c, i) => {
    const lines = (c.innerText || '').split('\n').map(s => s.trim())
      .filter(s => s && s !== '​');
    const box = c.querySelector('[data-testid="expandable-text-box"]');
    const orig = c.querySelector('a[href*="/feed/update/"]');
    const actors = [...c.querySelectorAll('a[href*="/in/"], a[href*="/company/"]')]
      .map(a => (a.getAttribute('href') || '').split('?')[0])
      .filter((h, j, all) => h && all.indexOf(h) === j).slice(0, 4);
    return {
      index: i,
      lines: lines.slice(0, 60),
      text: box ? (box.innerText || box.textContent || '').replace(/\s*…\s*more\s*$/, '').trim() : '',
      original_post_url: orig ? (orig.getAttribute('href') || '').split('?')[0] : null,
      actor_links: actors,
    };
  });
}"""

# The person's follower count, from the profile card beside their activity
# ("4,321 followers"): the first such line before the first post card. Plain
# text, no selector: None when the page does not show it.
_FOLLOWERS_JS = r"""() => {
  const lines = (document.body ? document.body.innerText : '').split('\n').map(s => s.trim());
  for (const l of lines) {
    if (l === 'Feed post') break;
    if (/^[\d.,]+\s*[KkMm]?\s+followers?$/.test(l)) return l;
  }
  return null;
}"""

_INSTALL_COPY_HOOK_JS = r"""() => {
  if (window.__msgkitCopyHook) { window.__msgkitCopied = []; return true; }
  window.__msgkitCopied = [];
  const keep = t => { window.__msgkitCopied.push(String(t)); return Promise.resolve(); };
  try { navigator.clipboard.writeText = keep; } catch (e) {}
  try { Object.defineProperty(navigator.clipboard, 'writeText', { value: keep, configurable: true }); } catch (e) {}
  window.__msgkitCopyHook = true;
  return true;
}"""

_TAKE_COPIED_JS = r"""() => { const v = window.__msgkitCopied || []; window.__msgkitCopied = []; return v; }"""

_EXPERIENCE_JS = r"""() => {
  const main = document.querySelector('main');
  if (!main) return { entries: [] };
  // Experience entries in page order: one per company link (newest first),
  // with the visible lines of that entry (company, title, dates).
  const byHref = new Map();
  for (const a of main.querySelectorAll('a[href*="/company/"], a[href*="/school/"]')) {
    const href = (a.getAttribute('href') || '').split('?')[0];
    if (!href) continue;
    const lines = (a.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
    if (!byHref.has(href)) byHref.set(href, []);
    byHref.get(href).push(...lines);
  }
  const entries = [...byHref.entries()].slice(0, 6).map(([url, lines]) => ({
    url, lines: [...new Set(lines)].slice(0, 12),
  }));
  return { entries };
}"""

_ACCOUNT_JS = r"""() => {
  const body = document.body ? document.body.innerText : '';
  const notPremium = /Try Premium|Reactivate Premium|Retry Premium|Get Premium/i.test(body);
  const premiumIcon = document.querySelectorAll('[aria-label="Premium" i], [alt="Premium" i]').length > 0;
  const main = document.querySelector('main');
  const nameEl = main && (main.querySelector('h1') || main.querySelector('h2'));
  return {
    name: nameEl ? nameEl.innerText.trim() : null,
    profile_url: location.href.split('?')[0],
    premium: notPremium ? false : (premiumIcon ? true : null),
  };
}"""


_PEOPLE_JS = r"""() => {
  const m = document.querySelector('main');
  const text = m ? m.innerText : '';
  // The company's numeric id: its own "<size> employees" link searches
  // currentCompany=["<id>"] (a "connections work here" link may name another).
  let id = null;
  for (const a of document.querySelectorAll('a[href*="currentCompany"]')) {
    const mm = decodeURIComponent(a.href).match(/currentCompany=\["?(\d+)/);
    if (mm && /employees/i.test(a.innerText || '')) { id = mm[1]; break; }
  }
  const slug = (location.pathname.match(/\/company\/([^/]+)/) || [])[1] || null;
  return {
    text,
    company_id: id,
    slug,
    ready: /associated members?/i.test(text) && /What they do/i.test(text),
  };
}"""

# One profile page: everything msgkit_connect needs to fill the profile-tracking
# fields. The Experience card is lazy, so it is scrolled into view and polled
# until it has text (the extension's PT button does the same), then the page is
# scrolled back to the top where the action buttons are.
_PROFILE_JS = r"""async () => {
  const sections = [...document.querySelectorAll('[componentkey^="com.linkedin.sdui.profile.card"]')]
    .map(e => e.closest('section')).filter(Boolean);
  const h2 = sections[1] && sections[1].querySelector('h2');
  let exp = document.querySelector('[id^="profileCardsExperienceOnly"]');
  let expText = '', expUrl = null;
  if (exp) {
    exp.scrollIntoView({ block: 'center' });
    for (let i = 0; i < 18; i++) {
      exp = document.querySelector('[id^="profileCardsExperienceOnly"]');
      if (exp && (exp.innerText || '').trim()) break;
      await new Promise(r => setTimeout(r, 400));
    }
    if (exp) {
      expText = exp.innerText || '';
      const a = exp.querySelector('a[href*="/company/"]');
      expUrl = a ? a.href.split('?')[0] : null;
    }
    window.scrollTo(0, 0);
  }
  // The top card's Message button: the first visible messaging link in the page's main part
  // (the people-also-viewed cards come later in the page).
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const msg = [...document.querySelectorAll('main a[href*="/messaging/thread/"], main a[href*="/messaging/compose/"]')].filter(vis)[0];
  return {
    name: h2 ? h2.innerText : null,
    message_url: msg ? msg.href : null,
    title: document.title,
    text: document.body ? document.body.innerText : '',
    experience_text: expText,
    experience_company_url: expUrl,
  };
}"""

# The profile's own Connect control (checked on live pages, 2026-11):
#   * visible in the top card's button row:
#       <a componentkey="ConnectButtonstate:invitation:urn:li:member:<id>_connect"
#          aria-label="Invite <Name> to connect" href="https://www.linkedin.com/">
#   * or, on profiles whose primary buttons are Follow / Message, a menu item in
#     the three-dots menu: <a role="menuitem" componentkey="ConnectButton...">.
# Neither has a /preload/custom-invite/ address, so the stock invite-anchor signal
# misses both (and its Pending signal, a labelled link in the row, fires on the
# Connect link itself). The page draws the top card twice (one copy hidden), and
# the people-also-viewed cards carry the same control for other people, so only
# VISIBLE controls in the top card's row count. The match is tagged so the click
# targets exactly it.
#   mode "top":  Connect in the top card's row
#   mode "more": tag the top card's More button
#   mode "menu": Connect inside the open More menu
_CONNECT_JS = r"""(mode) => {
  document.querySelectorAll('[data-msgkit-connect],[data-msgkit-more]').forEach(e => {
    e.removeAttribute('data-msgkit-connect'); e.removeAttribute('data-msgkit-more');
  });
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const main = document.querySelector('main');
  if (!main) return { found: false };
  const sections = [...document.querySelectorAll('[componentkey^="com.linkedin.sdui.profile.card"]')]
    .map(e => e.closest('section')).filter(Boolean);
  const h2 = sections[1] && sections[1].querySelector('h2');
  const name = h2 ? h2.innerText.trim().toLowerCase() : '';
  // The top card's button row: the smallest ancestor of its Message link that holds the More button.
  let row = null;
  const compose = [...main.querySelectorAll('a[href*="/messaging/compose/"]')].filter(vis)[0];
  for (let el = compose, i = 0; el && i < 6; el = el.parentElement, i++) {
    if ([...el.querySelectorAll('button[aria-label="More"]')].some(vis)) { row = el; break; }
  }
  const tag = (e, attr) => { e.setAttribute(attr, '1'); return e; };
  // The same ConnectButton control turns into "Pending" once a request is out, and clicking it then
  // opens LinkedIn's Withdraw dialog (on 2026-11-21 a retry withdrew a pending invitation that way).
  // Only a control that says Connect counts; a Pending / Withdraw one means a request is pending.
  const label = e => ((e.getAttribute('aria-label') || '') + ' ' + (e.innerText || '')).trim();
  const isPending = e => /\bpending\b|withdraw/i.test(label(e)) || /_pending$|withdraw/i.test(e.getAttribute('componentkey') || '');
  const isConnect = e => !isPending(e) && (
    /_connect$/.test(e.getAttribute('componentkey') || '') ||
    /^invite .+ to connect$/i.test((e.getAttribute('aria-label') || '').trim()) ||
    /^connect$/i.test((e.innerText || '').trim()) ||
    /\/preload\/custom-invite\//.test(e.getAttribute('href') || ''));
  const controls = root => [...root.querySelectorAll('[componentkey^="ConnectButton"], a[href*="/preload/custom-invite/"]')];
  if (mode === 'menu') {
    for (const m of [...document.querySelectorAll('[role="menu"]')].filter(vis)) {
      const all = controls(m);
      if (all.some(isPending) || [...m.querySelectorAll('[role="menuitem"], a, button')].some(isPending)) return { found: false, pending: true, where: 'menu' };
      const c = all.find(isConnect);
      if (c) { tag(c, 'data-msgkit-connect'); return { found: true, where: 'menu', tag: c.tagName, key: c.getAttribute('componentkey') }; }
    }
    return { found: false, menu_open: [...document.querySelectorAll('[role="menu"]')].some(vis) };
  }
  if (mode === 'more') {
    const more = row && [...row.querySelectorAll('button[aria-label="More"]')].filter(vis)[0];
    if (!more) return { found: false };
    tag(more, 'data-msgkit-more');
    return { found: true };
  }
  let pool = [...main.querySelectorAll('[componentkey^="ConnectButton"]')].filter(vis);
  if (row) pool = pool.filter(e => row.contains(e));
  else if (name) pool = pool.filter(e => (e.getAttribute('aria-label') || '').toLowerCase().includes(name));
  else pool = [];
  const rowPending = row && [...row.querySelectorAll('button, a')].filter(vis).some(isPending);
  if (pool.some(isPending) || rowPending) return { found: false, pending: true, where: 'top' };
  pool = pool.filter(isConnect);
  if (!pool.length) return { found: false, row: !!row };
  const hit = tag(pool[0], 'data-msgkit-connect');
  return { found: true, where: 'top', tag: hit.tagName, label: hit.getAttribute('aria-label'), key: hit.getAttribute('componentkey') };
}"""

_CONNECT_TARGET = '[data-msgkit-connect="1"]'
_MORE_TARGET = '[data-msgkit-more="1"]'

# --- direct messages: read-only check of one person before a DM is ever prepared ---
#
# The profile's own "Message" link (checked on live pages, 2026-11): a compose link
# /messaging/compose/?profileUrn=urn:li:fsd_profile:ACoAA...&recipient=... (the second
# compose link, "Say hello", carries a `body` parameter and is skipped). The compose
# page keeps its address but shows an existing conversation inline: the same page held
# 102 msg-s-event nodes for a person with a long thread and 0 for people without one.
_DM_PROFILE_JS = r"""() => {
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const main = document.querySelector('main');
  if (!main) return { ok: false };
  const sections = [...document.querySelectorAll('[componentkey^="com.linkedin.sdui.profile.card"]')]
    .map(e => e.closest('section')).filter(Boolean);
  const h2 = sections[1] && sections[1].querySelector('h2');
  const text = main.innerText || '';
  const d = text.match(/[·•]\s*(1st|2nd|3rd)\b/);
  const links = [...main.querySelectorAll('a[href*="/messaging/compose/"]')].filter(vis)
    .filter(a => { try { return !new URL(a.href).searchParams.has('body'); } catch (e) { return true; } });
  const a = links[0] || null;
  let urn = null;
  if (a) { try { urn = new URL(a.href).searchParams.get('profileUrn'); } catch (e) {} }
  return {
    ok: true,
    name: h2 ? h2.innerText.trim() : null,
    title: document.title,
    degree: d ? d[1] : null,
    compose_url: a ? a.href : null,
    profile_urn: urn,
  };
}"""

# The compose page after it settled: the conversation shown with the composer.
_DM_THREAD_JS = r"""() => {
  const events = document.querySelectorAll('[class*="msg-s-event"]').length;
  const list = document.querySelector('[class*="msg-s-message-list-content"], [class*="msg-s-message-list"]');
  const senders = [...new Set([...document.querySelectorAll('[class*="msg-s-message-group__name"], [class*="msg-s-event-listitem__name"]')]
    .map(e => (e.innerText || '').trim()).filter(Boolean))].slice(0, 6);
  const text = list ? (list.innerText || '').replace(/\n{3,}/g, '\n\n').trim() : '';
  return {
    events,
    composer: !!document.querySelector('[contenteditable="true"][role="textbox"], .msg-form__contenteditable'),
    senders,
    text: text.slice(-3500),
    path: location.pathname,
  };
}"""


_INVITE_ANCHOR = 'a[href*="/preload/custom-invite/"][href*="vanityName={u}"]'


def _parts(extractor: Any) -> tuple[Any, Any]:
    capture = extractor._capture
    return capture._session, capture._navigator


async def _open(extractor: Any, url: str) -> Any:
    """Open one LinkedIn page with the server's own auth/rate-limit checks."""
    session, navigator = _parts(extractor)
    await navigator._navigate_to_page(url)
    await session.check_rate_limit()
    try:
        await session.page.wait_for_selector("main", timeout=15000)
    except Exception:
        pass
    await session.dismiss_modal()
    return session


# The Message button's link on a profile or activity page (the first visible messaging link in
# the page's main part; the people-also-viewed cards come later). A conversation
# (/messaging/thread/...) when one exists, otherwise the compose link.
_MESSAGE_URL_JS = r"""() => {
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  // The person's name from the tab title ("Activity | Name | LinkedIn", "(3) Name | LinkedIn").
  const parts = document.title.replace(/^\(\d+\+?\)\s*/, '').split('|').map(t => t.trim()).filter(Boolean);
  const name = (parts.find(t => !/^(activity|linkedin)$/i.test(t)) || '').toLowerCase();
  const links = [...document.querySelectorAll('main a[href*="/messaging/thread/"], main a[href*="/messaging/compose/"]')].filter(vis);
  for (const a of links) {
    if (!name) return a.href.split('#')[0];
    // only the person's own header card: the card around the button names them
    let card = a;
    for (let i = 0; i < 8 && card.parentElement; i++) {
      card = card.parentElement;
      if ((card.innerText || '').toLowerCase().includes(name)) return a.href.split('#')[0];
    }
  }
  return null;
}"""


async def _message_url(page: Any) -> str | None:
    try:
        return await page.evaluate(_MESSAGE_URL_JS)
    except Exception:  # never lose the posts over the message link
        return None


async def _followers(page: Any) -> str | None:
    try:
        return await page.evaluate(_FOLLOWERS_JS)
    except Exception:  # never lose the posts over the follower count
        return None


async def _connect_control(page: Any, mode: str = "top") -> dict[str, Any]:
    try:
        return await page.evaluate(_CONNECT_JS, mode) or {"found": False}
    except Exception:
        return {"found": False}


# What the top card offered, for the failure message when no Connect control is found: the
# visible button/link labels near the profile header and any open menu's items (text only).
_CONNECT_DIAG_JS = r"""() => {
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const lab = e => ((e.getAttribute('aria-label') || e.innerText || '').trim().replace(/\s+/g, ' ')).slice(0, 28);
  const main = document.querySelector('main');
  const compose = main && [...main.querySelectorAll('a[href*="/messaging/compose/"]')].filter(vis)[0];
  let row = compose;
  for (let i = 0; row && i < 4; i++) row = row.parentElement;
  const near = row ? [...row.querySelectorAll('button, a')].filter(vis).map(lab).filter(Boolean) : [];
  const menus = [...document.querySelectorAll('[role="menu"]')].filter(vis).map(m => (m.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 160));
  return { compose: !!compose, w: window.innerWidth, h: window.innerHeight, near: [...new Set(near)].slice(0, 12), menus };
}"""


async def _connect_diag(page: Any) -> str:
    try:
        return str(await page.evaluate(_CONNECT_DIAG_JS))[:700]
    except Exception:
        return "diag unavailable"


async def _open_more_for_connect(page: Any, conn: Any) -> dict[str, Any]:
    """Open the top card's More menu and find Connect in it ({found: False} if none)."""
    if not (await _connect_control(page, "more")).get("found"):
        return {"found": False, "more": "not found"}
    more = page.locator(_MORE_TARGET).first
    # A real click first; when something covers the button (or it is still settling), the
    # same click forced, then the page's own element.click().
    clicked, errors = None, []
    for name, act in (
        ("click", lambda: more.click(timeout=4000)),
        ("forced", lambda: more.click(timeout=3000, force=True)),
        ("js", lambda: more.evaluate("e => e.click()")),
    ):
        try:
            await act()
            clicked = name
            break
        except Exception as exc:
            errors.append(f"{name}:{type(exc).__name__}")
    if clicked is None:
        return {"found": False, "more": "click failed " + ",".join(errors)}
    await asyncio.sleep(1.0)
    ctl = await _connect_control(page, "menu")
    if clicked != "click":
        ctl["more"] = clicked
    if not ctl.get("found"):
        try:
            await conn._press_escape()
        except Exception:
            logger.debug("Escape after More menu failed", exc_info=True)
    return ctl


# The invite dialog (checked on live pages, 2026-11): a native <dialog open> holding a
# Dismiss button and a "Send without a note" button ("Add a note" is a link). The
# primary action is the LAST button; no label text is relied on. Older layouts used
# the extension's ".send-invite" web component, which this also covers.
_SEND_TARGET = '[data-msgkit-send="1"]'
_TAG_SEND_JS = r"""() => {
  document.querySelectorAll('[data-msgkit-send]').forEach(e => e.removeAttribute('data-msgkit-send'));
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const deep = (root, sel, acc = []) => {
    root.querySelectorAll(sel).forEach(e => acc.push(e));
    root.querySelectorAll('*').forEach(e => { if (e.shadowRoot) deep(e.shadowRoot, sel, acc); });
    return acc;
  };
  for (const d of deep(document, 'dialog[open], [role="dialog"], [aria-modal="true"], .artdeco-modal, .send-invite').filter(vis)) {
    if (d.querySelector('textarea')) continue;            // the note editor, not the send step
    // A chat window LinkedIn keeps open across pages is a dialog too (it has a composer and
    // dozens of buttons); its last button is "Open send options", never the invite's Send.
    if (d.querySelector('[contenteditable="true"]') || d.closest('[class*="msg-overlay"]')) continue;
    const btns = [...d.querySelectorAll('button')].filter(vis);
    if (btns.length < 2 || btns.length > 6) continue;     // dismiss + primary (+ add a note)
    const says = (d.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 300);
    // A pending request's dialog offers Withdraw: never click anything in it.
    if (btns.some(b => /withdraw/i.test(b.innerText || b.getAttribute('aria-label') || '')) || /\bwithdraw/i.test(says)) {
      return { found: false, withdraw: true, says };
    }
    const last = btns[btns.length - 1];
    if (!/\bsend\b/i.test((last.innerText || last.getAttribute('aria-label') || '').trim())) {
      return { found: false, other: true, says, text: (last.innerText || '').trim() };
    }
    last.setAttribute('data-msgkit-send', '1');
    // Some people can only be invited with their email address: the dialog then holds an input and
    // keeps Send disabled until it is filled.
    const inputs = [...d.querySelectorAll('input:not([type="hidden"]):not([type="checkbox"]):not([type="radio"])')].filter(vis).length;
    return {
      found: true, text: (last.innerText || '').trim(), buttons: btns.length, inputs,
      disabled: last.disabled || last.getAttribute('aria-disabled') === 'true',
      says,
    };
  }
  return { found: false };
}"""
# What dialogs/modals are on the page, for the failure message when no send button is found.
_DIALOG_DIAG_JS = r"""() => {
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const deep = (root, sel, acc = []) => {
    root.querySelectorAll(sel).forEach(e => acc.push(e));
    root.querySelectorAll('*').forEach(e => { if (e.shadowRoot) deep(e.shadowRoot, sel, acc); });
    return acc;
  };
  return deep(document, 'dialog, [role="dialog"], [aria-modal="true"], .artdeco-modal, .send-invite')
    .filter(vis).slice(0, 3).map(d => d.tagName.toLowerCase() + (d.hasAttribute('open') ? '[open]' : '') +
      ' buttons=[' + [...d.querySelectorAll('button, a[role="button"]')].map(b => (b.innerText || b.getAttribute('aria-label') || '').trim().slice(0, 24) + (vis(b) ? '' : '(hidden)')).join(' | ') + ']' +
      (d.querySelector('textarea') ? ' has-textarea' : '')).join(' ;; ') || 'no dialog on the page';
}"""
_DIALOG_OPEN_JS = r"""() => {
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  const deep = (root, sel, acc = []) => {
    root.querySelectorAll(sel).forEach(e => acc.push(e));
    root.querySelectorAll('*').forEach(e => { if (e.shadowRoot) deep(e.shadowRoot, sel, acc); });
    return acc;
  };
  return deep(document, 'dialog[open], .send-invite').filter(vis).length;
}"""
# What LinkedIn says after a send (a toast or alert), for the result and the log.
# A video on the page has its own status line ("Muted", "Play"...): never a notice from LinkedIn
# (on 2026-11-21 "Muted" was taken for the send's answer and a sent invitation was reported failed).
_NOTICES_JS = r"""() => [...document.querySelectorAll('[role="alert"], [role="status"], [class*="toast" i]')]
  .filter(e => !e.closest('video, audio, [class*="video" i], [class*="media-player" i], [class*="vjs" i]'))
  .map(e => (e.innerText || '').trim().replace(/\s+/g, ' '))
  .filter(t => t && !/^(un)?muted$|^(play|pause|playing|paused|replay|loaded:?.*)$/i.test(t))
  .slice(0, 5).map(t => t.slice(0, 160))"""


async def _invite_dialog_open(page: Any, conn: Any, timeout_s: float = 5.0) -> bool:
    """The invite dialog is up (native <dialog open>, a role=dialog, or .send-invite)."""
    end = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < end:
        try:
            if await page.evaluate(_DIALOG_OPEN_JS) or await conn._dialog_is_open(
                timeout=300
            ):
                return True
        except Exception:
            pass
        await asyncio.sleep(0.3)
    return False


async def _dialog_closed_within(page: Any, seconds: float) -> bool:
    end = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < end:
        await asyncio.sleep(0.3)
        try:
            if not await page.evaluate(_DIALOG_OPEN_JS):
                return True
        except Exception:
            return True
    return False


# What _send_without_note answers when the dialog is LinkedIn's Withdraw dialog of a pending request.
PENDING_DIALOG = "\x00pending"


async def _send_without_note(page: Any, trace: list[str]) -> tuple[bool, str | None]:
    """Click the invite dialog's primary button ("Send without a note"); (True, None) once the dialog closed.

    (False, what the dialog says) when LinkedIn will not take the request from this dialog at all:
    Send stays disabled, or the dialog asks for more (the person's email address). Nothing is
    clicked then, and the caller must not try another way.

    A real click first; if the dialog is still up, the same click forced past any overlay, then
    the page's own element.click() (what the msgkit extension does), then Enter on the focused
    button. A step is only tried when the previous one left the dialog open, so a request that
    went out is never sent twice.
    """
    tagged: dict[str, Any] = {"found": False}
    for _ in range(16):  # the dialog may still be rendering: wait up to ~8 s
        try:
            tagged = await page.evaluate(_TAG_SEND_JS)
        except Exception as exc:
            trace.append(f"tag failed: {exc}")
            return False, None
        if tagged.get("withdraw"):
            trace.append(f"the dialog offers Withdraw (a request is pending); nothing clicked: {str(tagged.get('says'))[:160]}")
            return False, PENDING_DIALOG
        if tagged.get("found") and not tagged.get("disabled") and not tagged.get("inputs"):
            break
        await asyncio.sleep(0.5)
    if tagged.get("other"):
        says = str(tagged.get("says") or "")
        trace.append(f"the dialog's main button is {tagged.get('text')!r}, not Send; nothing clicked: {says[:160]}")
        return False, says or "an unexpected dialog"
    if not tagged.get("found"):
        try:
            seen = await page.evaluate(_DIALOG_DIAG_JS)
        except Exception as exc:
            seen = f"diag failed: {exc}"
        trace.append(f"send button not found; page has: {seen}")
        return False, None
    trace.append(f"send button {tagged.get('text')!r} of {tagged.get('buttons')}")
    if tagged.get("disabled") or tagged.get("inputs"):
        says = str(tagged.get("says") or "")
        trace.append(("send button disabled" if tagged.get("disabled") else "dialog asks for more") + f"; it says: {says[:200]}")
        return False, says or "the Send button stayed disabled"
    btn = page.locator(_SEND_TARGET).first
    await _pause(0.5, 1.2)
    steps = [
        ("click", lambda: btn.click(timeout=4000)),
        ("forced click", lambda: btn.click(timeout=3000, force=True)),
        ("js click", lambda: btn.evaluate("e => e.click()")),
        ("enter", lambda: btn.press("Enter", timeout=3000)),
    ]
    for name, act in steps:
        try:
            await act()
        except Exception as exc:
            trace.append(f"{name} raised {type(exc).__name__}")
            continue
        if await _dialog_closed_within(page, 5.0):
            trace.append(f"{name}: dialog closed")
            return True, None
        trace.append(f"{name}: dialog still open")
    try:  # what the dialog that would not close says, for the failure message
        again = await page.evaluate(_TAG_SEND_JS)
        if again.get("says"):
            trace.append(f"dialog says: {str(again['says'])[:200]}")
    except Exception:
        pass
    return False, None


def _says_sent(text: str) -> bool:
    """A notice that says the invitation went out. "Invitation to Mehul withdrawn." names the person
    too, and was once taken for a send: only a notice that says sent counts, never a withdrawal."""
    if re.search(r"withdr", text, re.I):
        return False
    return bool(re.search(r"\binvitation sent\b|\bsent\b", text, re.I))


async def _page_notices(page: Any) -> list[str]:
    try:
        return list(await page.evaluate(_NOTICES_JS) or [])
    except Exception:
        return []


async def _pause(lo: float, hi: float) -> None:
    await asyncio.sleep(random.uniform(lo, hi))


async def _scroll_round_trip(page: Any) -> None:
    """Scroll to the bottom of the page and back to the top (never fails the caller)."""
    try:
        await page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        await _pause(0.8, 1.4)
        await page.evaluate("() => window.scrollTo(0, 0)")
    except Exception:
        logger.debug("Scroll round trip failed", exc_info=True)


def register_msgkit_tools(
    mcp: FastMCP, *, tool_timeout: float = DEFAULT_TOOL_TIMEOUT_SECONDS
) -> None:
    """Register the msgkit enrichment tools."""

    @mcp.tool(
        timeout=max(tool_timeout, 600),
        title="msgkit: Person Activity",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"person", "msgkit"},
    )
    @tool_errors
    async def msgkit_person_activity(
        linkedin_username: str,
        ctx: Context,
        limit: Annotated[int, Field(ge=1, le=30)] = 10,
        copy_links: bool = True,
        max_scrolls: Annotated[int, Field(ge=0, le=20)] = 6,
    ) -> dict[str, Any]:
        """
        Read a person's newest activity from /in/<user>/recent-activity/all/
        (one page load; the main profile page is not opened).

        Returns {url, posts: [{index, lines, text, original_post_url,
        actor_links, share_url}], cards_seen, copy_errors, followers_text, message_url}.
        share_url is the link LinkedIn's "Copy link to post" gives (an lnkd.in
        short link); followers_text is the person's "N followers" line or null;
        message_url is the Message button's link (a conversation or the compose link) or null.
        """
        username = normalize_person_identifier(linkedin_username)
        extractor = await get_ready_extractor(ctx, tool_name="msgkit_person_activity")
        url = f"https://www.linkedin.com/in/{username}/recent-activity/all/"
        session = await _open(extractor, url)
        page = session.page

        try:
            await page.wait_for_selector(_CARD, timeout=12000)
        except Exception:
            text = await page.evaluate(
                "() => (document.querySelector('main')||{}).innerText || ''"
            )
            return {
                "url": url,
                "posts": [],
                "cards_seen": 0,
                "copy_errors": [],
                "empty_text": text[:600],
                "followers_text": await _followers(page),
                "message_url": await _message_url(page),
            }

        for _ in range(max_scrolls):
            if await page.locator(_CARD).count() >= limit:
                break
            await page.mouse.wheel(0, random.randint(1400, 2200))
            await _pause(1.0, 2.2)

        posts: list[dict[str, Any]] = await page.evaluate(_CARDS_JS, limit)
        copy_errors: list[str] = []

        if copy_links and posts:
            await page.evaluate(_INSTALL_COPY_HOOK_JS, isolated_context=False)
            cards = page.locator(_CARD)
            for post in posts:
                i = post["index"]
                try:
                    card = cards.nth(i)
                    await card.scroll_into_view_if_needed(timeout=5000)
                    await _pause(0.6, 1.4)
                    await card.locator(
                        'button[aria-label^="Open control menu"]'
                    ).first.click(timeout=5000)
                    item = page.get_by_role("menuitem", name="Copy link to post")
                    await item.first.click(timeout=5000)
                    await _pause(0.5, 1.0)
                    copied = await page.evaluate(
                        _TAKE_COPIED_JS, isolated_context=False
                    )
                    post["share_url"] = copied[-1] if copied else None
                    if not copied:
                        copy_errors.append(f"card {i}: nothing copied")
                except Exception as e:  # one stubborn card must not lose the rest
                    copy_errors.append(f"card {i}: {type(e).__name__}: {str(e)[:120]}")
                    post["share_url"] = None
                    try:
                        await page.keyboard.press("Escape")
                    except Exception:
                        pass
                await _pause(0.8, 2.0)

        return {
            "url": url,
            "posts": posts,
            "cards_seen": await page.locator(_CARD).count(),
            "copy_errors": copy_errors,
            "followers_text": await _followers(page),
            "message_url": await _message_url(page),
        }

    @mcp.tool(
        timeout=tool_timeout,
        title="msgkit: Person Current Company",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"person", "msgkit"},
    )
    @tool_errors
    async def msgkit_person_company(
        linkedin_username: str, ctx: Context
    ) -> dict[str, Any]:
        """
        Open /in/<user>/details/experience/ (one page load) and return the
        experience entries in page order: {url, entries: [{url, lines}]}.
        The first entry whose lines contain "Present" is the current job;
        company links there are numeric (/company/<id>/).
        """
        username = normalize_person_identifier(linkedin_username)
        extractor = await get_ready_extractor(ctx, tool_name="msgkit_person_company")
        url = f"https://www.linkedin.com/in/{username}/details/experience/"
        session = await _open(extractor, url)
        await _pause(1.5, 3.0)
        info = await session.page.evaluate(_EXPERIENCE_JS)
        info["url"] = url
        return info

    @mcp.tool(
        timeout=tool_timeout,
        title="msgkit: Company People (one function)",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"company", "msgkit"},
    )
    @tool_errors
    async def msgkit_company_people(
        company_slug: str,
        ctx: Context,
        function_id: Annotated[int, Field(ge=1, le=100)] = 8,
    ) -> dict[str, Any]:
        """
        Open /company/<slug>/people/?facetCurrentFunction=<id> (one page load;
        8 = Engineering) and wait until the insights have rendered. With the
        filter, "N associated members" and the pinned first row of "What they
        do" are that function's headcount, the same for every viewer (the
        "People highlights" line is personalised and is not used).

        Returns {url, text, company_id, slug, ready}; ready=false means the
        insights never appeared (retry later rather than trusting the text).
        """
        slug = company_slug.strip().strip("/").split("/")[-1].lower()
        extractor = await get_ready_extractor(ctx, tool_name="msgkit_company_people")
        url = f"https://www.linkedin.com/company/{slug}/people/?facetCurrentFunction={function_id}"
        session = await _open(extractor, url)
        page = session.page
        info: dict[str, Any] = {}
        deadline = asyncio.get_running_loop().time() + 20
        while True:
            await _pause(1.2, 2.0)
            info = await page.evaluate(_PEOPLE_JS)
            if info.get("ready") or asyncio.get_running_loop().time() > deadline:
                break
            # The insights cards render lazily: nudge the page.
            await page.mouse.wheel(0, random.randint(500, 900))
        info["url"] = url
        return info

    @mcp.tool(
        timeout=max(tool_timeout, 180),
        title="msgkit: Idle Like a Reader",
        annotations={"readOnlyHint": True, "openWorldHint": False},
        tags={"msgkit"},
    )
    @tool_errors
    async def msgkit_idle(
        seconds: Annotated[float, Field(ge=1, le=150)],
        ctx: Context,
    ) -> dict[str, Any]:
        """
        Spend `seconds` on the page that is already open the way a person
        reading it would: small uneven wheel scrolls (mostly down, sometimes
        back up), smooth mouse moves and reading pauses. Never clicks, types
        or navigates, so it opens no new page. On a blank or non-LinkedIn page
        it only waits. Returns {seconds, scrolls, moves, url}.
        """
        extractor = await get_ready_extractor(ctx, tool_name="msgkit_idle")
        session, _ = _parts(extractor)
        page = session.page
        loop = asyncio.get_running_loop()
        deadline = loop.time() + seconds
        url = page.url or ""
        scrolls = moves = 0
        if "linkedin.com" not in url:
            await asyncio.sleep(seconds)
            return {"seconds": seconds, "scrolls": 0, "moves": 0, "url": url}

        vp = page.viewport_size or {"width": 1280, "height": 800}
        w, h = vp["width"], vp["height"]
        x, y = random.uniform(0.3, 0.7) * w, random.uniform(0.3, 0.7) * h

        async def rest(lo: float, hi: float) -> None:
            left = deadline - loop.time()
            if left > 0:
                await asyncio.sleep(min(left, random.uniform(lo, hi)))

        while loop.time() < deadline:
            r = random.random()
            try:
                if r < 0.55:  # scroll a little, in uneven wheel ticks
                    direction = -1 if random.random() < 0.18 else 1
                    for _ in range(random.randint(1, 4)):
                        await page.mouse.wheel(0, direction * random.randint(60, 260))
                        await asyncio.sleep(random.uniform(0.05, 0.25))
                    scrolls += 1
                elif r < 0.85:  # drift the pointer somewhere else on the page
                    nx = min(max(x + random.gauss(0, w * 0.18), 20), w - 20)
                    ny = min(max(y + random.gauss(0, h * 0.15), 80), h - 20)
                    await page.mouse.move(nx, ny, steps=random.randint(8, 30))
                    x, y = nx, ny
                    moves += 1
                # else: just keep reading
            except (
                Exception
            ):  # a detached page or closed overlay: stop moving, keep waiting
                await rest(deadline - loop.time(), deadline - loop.time())
                break
            await rest(1.5, 6.0)
        return {"seconds": seconds, "scrolls": scrolls, "moves": moves, "url": url}

    @mcp.tool(
        timeout=tool_timeout,
        title="msgkit: Logged-in Account",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"msgkit"},
    )
    @tool_errors
    async def msgkit_account(ctx: Context) -> dict[str, Any]:
        """
        Open the logged-in member's own profile (/in/me/) and return {name,
        profile_url, premium} (premium is null when it cannot be told).
        """
        extractor = await get_ready_extractor(ctx, tool_name="msgkit_account")
        session = await _open(extractor, "https://www.linkedin.com/in/me/")
        await _pause(1.5, 3.0)
        return await session.page.evaluate(_ACCOUNT_JS)

    @mcp.tool(
        timeout=max(tool_timeout, 3600),
        title="msgkit: List connections",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"msgkit"},
    )
    @tool_errors
    async def msgkit_list_connections(
        ctx: Context,
        stop_at_profile: str = "",
        max_rows: Annotated[int, Field(ge=1, le=20000)] = 5000,
        max_seconds: Annotated[int, Field(ge=30, le=3000)] = 2400,
        pause_min_s: Annotated[float, Field(ge=1, le=60)] = 5.0,
        pause_max_s: Annotated[float, Field(ge=1, le=120)] = 10.0,
    ) -> dict[str, Any]:
        """
        List the logged-in member's connections, newest first, from
        /mynetwork/invite-connect/connections/ (one page load, then scrolling).

        Stops at ``stop_at_profile`` (a profile link already stored: it and everything
        after it are left out), at the end of the list, when three scrolls load
        nothing new, or at max_rows / max_seconds.

        Returns {rows: [{profile_link, name, tagline, connected_on, message_link}],
        stopped_reason, total_visible, scrolls, seconds}. stopped_reason is
        "checkpoint", "end", "stale", "limit" (rows are good, the list is not finished)
        or "empty" (the page showed no connections: not signed in, blocked, or the
        page changed; empty_text holds what the page said).
        """
        extractor = await get_ready_extractor(ctx, tool_name="msgkit_list_connections")
        session = await _open(extractor, CONNECTIONS_URL)
        page = session.page
        try:
            await page.wait_for_function(
                "() => Array.from(document.querySelectorAll('p')).some(p => (p.innerText || '').trim().startsWith('Connected on '))",
                timeout=20000,
            )
        except Exception:
            pass  # list_connections reports an empty first read as stopped_reason "empty"
        out = await list_connections(
            page,
            stop_at_profile=stop_at_profile,
            max_rows=max_rows,
            max_seconds=max_seconds,
            pause_min_s=pause_min_s,
            pause_max_s=max(pause_max_s, pause_min_s),
            pause=_pause,
        )
        out["url"] = CONNECTIONS_URL
        return out

    @mcp.tool(
        timeout=tool_timeout,
        title="msgkit: DM check (read-only)",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"person", "msgkit"},
    )
    @tool_errors
    async def msgkit_dm_check(linkedin_username: str, ctx: Context) -> dict[str, Any]:
        """
        Read-only check of one person before a direct message is prepared. Sends nothing
        and types nothing.

        Opens /in/<user>/ once (degree, name, the Message link and the profile URN it
        carries), then opens that Message link's compose page, which shows any existing
        conversation inline, and reads it. Returns {url, name, degree, has_message,
        profile_urn, compose_url, conversation: {exists, events, senders, text, composer}}.
        ``text`` is the tail of the visible conversation (up to ~3500 characters).
        """
        username = normalize_person_identifier(linkedin_username)
        extractor = await get_ready_extractor(ctx, tool_name="msgkit_dm_check")
        url = f"https://www.linkedin.com/in/{username}/"
        session = await _open(extractor, url)
        await _pause(1.5, 3.0)
        try:
            prof = await session.page.evaluate(_DM_PROFILE_JS)
        except Exception:
            prof = {"ok": False}
        out: dict[str, Any] = {
            "url": url,
            "name": (prof or {}).get("name"),
            "degree": (prof or {}).get("degree"),
            "has_message": bool((prof or {}).get("compose_url")),
            "profile_urn": (prof or {}).get("profile_urn"),
            "compose_url": (prof or {}).get("compose_url"),
            "conversation": {
                "exists": False,
                "events": 0,
                "senders": [],
                "text": "",
                "composer": False,
            },
            "read": bool((prof or {}).get("ok")),
        }
        compose = out["compose_url"]
        if not compose or out["degree"] != "1st":
            return out  # nothing to read: not connected here, or no Message action
        await _pause(1.0, 2.0)
        session = await _open(extractor, compose)
        info: dict[str, Any] = {}
        for _ in range(16):  # the thread loads after the page: wait up to ~8 s
            await asyncio.sleep(0.5)
            try:
                info = await session.page.evaluate(_DM_THREAD_JS)
            except Exception:
                info = {}
            if info.get("events") or (info.get("composer") and _ >= 6):
                break
        out["conversation"] = {
            "exists": bool(info.get("events")),
            "events": int(info.get("events") or 0),
            "senders": info.get("senders") or [],
            "text": info.get("text") or "",
            "composer": bool(info.get("composer")),
            "path": info.get("path"),
        }
        return out

    @mcp.tool(
        timeout=tool_timeout,
        title="msgkit: Connect",
        annotations={"destructiveHint": True, "openWorldHint": True},
        tags={"person", "msgkit"},
    )
    @tool_errors
    async def msgkit_connect(
        linkedin_username: str,
        ctx: Context,
        note: Annotated[
            str | None,
            Field(description="Invitation note. Omit to send without a note."),
        ] = None,
        dry_run: Annotated[
            bool,
            Field(
                description="Read the profile and report what would happen; send nothing."
            ),
        ] = False,
    ) -> dict[str, Any]:
        """
        Send one LinkedIn connection request on a single profile view and read
        the profile-tracking fields off that same page.

        Opens /in/<user>/ once, reads followers, place, country, company,
        company_position and headline, then (unless dry_run) opens the invite
        dialog from the Connect button and sends it without a note (or with
        ``note``). The result is checked on the same page, not by reloading.

        Returns {url, status, message, via, note_sent, would_send, fields,
        extra_profile_view}. Statuses: sent, pending (already pending),
        already_connected, follow_only, connect_unavailable, self,
        custom_note_limit_reached, dry_run, sent_unverified (the invite went out but the
        page never showed it: do not retry), send_failed, unavailable.
        """
        username = normalize_person_identifier(linkedin_username)
        extractor = await get_ready_extractor(ctx, tool_name="msgkit_connect")
        conn = extractor._connection
        url = f"https://www.linkedin.com/in/{username}/"
        out: dict[str, Any] = {
            "url": url,
            "status": "unavailable",
            "message": "",
            "via": "button",
            "note_sent": False,
            "would_send": False,
            "extra_profile_view": False,
            "fields": {},
        }

        session = await _open(extractor, url)
        await _pause(1.5, 3.0)
        try:
            raw = await session.page.evaluate(_PROFILE_JS)
        except Exception:
            raw = {}
        fields = build_fields(raw or {})
        out["fields"] = fields
        if not (raw or {}).get("text"):
            out["message"] = "Could not read the profile page."
            return out

        signals = await conn._read_action_signals(username)
        state = connection.detect_connection_state(signals)
        page = session.page
        blocked = state in ("self_profile", "incoming_request")
        # Connect is either a visible button in the top card's row or, on
        # profiles led by Follow / Message, an item in the three-dots menu.
        ctl = await _connect_control(page, "top")
        if ctl.get("pending") and not blocked:
            state = "pending"  # the top card shows Pending: never click it (that is Withdraw)
        via_menu = False
        tries = 1
        if (
            not ctl.get("found")
            and not blocked
            and state != "pending"
            and fields.get("degree") != "1st"
        ):
            ctl = await _open_more_for_connect(page, conn)  # left open when found
            via_menu = bool(ctl.get("found"))
            if ctl.get("pending"):
                state = "pending"
            # About a third of the profiles led by Follow / Message failed here although
            # Connect sat in their More menu: the top card's button row had not finished
            # drawing yet (or the menu opened late). Look again, up to twice, with a pause.
            while not ctl.get("found") and not ctl.get("pending") and tries < 3:
                tries += 1
                # Scroll to the bottom and back to the top: LinkedIn draws parts of the top
                # card (and its menu) lazily, and the scroll makes it finish.
                await _scroll_round_trip(page)
                await _pause(1.5, 2.5)
                ctl = await _connect_control(page, "top")
                if not ctl.get("found") and not ctl.get("pending"):
                    ctl = await _open_more_for_connect(page, conn)
                    via_menu = bool(ctl.get("found"))
                else:
                    via_menu = False
                if ctl.get("pending"):
                    state = "pending"
                    break
                if ctl.get("found"):
                    signals = await conn._read_action_signals(username)
                    state = connection.detect_connection_state(signals)
                    break
        out["connect_control"] = ctl
        if ctl.get("found") and not blocked:
            # The profile's own Connect control is there: whatever the stock
            # signals made of the labelled link next to it, it is not pending.
            state = "connectable"
        logger.info(
            "msgkit_connect %s: state=%s connect_control=%s", username, state, ctl
        )

        if state == "self_profile":
            out.update(status="self", message="This is your own profile.")
            return out
        if state == "incoming_request":
            out.update(
                status="connect_unavailable",
                message="They sent you a request; accept it on LinkedIn.",
            )
            return out
        if state == "pending":
            out.update(status="pending", message="A request is already pending.")
            return out
        if state == "already_connected":
            if fields.get("degree") in ("2nd", "3rd"):
                # Message as the primary action but not a 1st-degree link:
                # the invitation is pending with its control under More.
                out.update(status="pending", message="A request is already pending.")
            else:
                out.update(status="already_connected", message="Already connected.")
            return out
        if state != "connectable":
            out.update(
                status="follow_only" if state == "follow_only" else "unavailable",
                message="LinkedIn did not offer a Connect action for this profile."
                f" (state={state}, looked {tries}x, control={ctl}, page={await _connect_diag(page)})",
            )
            return out

        out["would_send"] = True
        if dry_run:
            if via_menu:
                await conn._press_escape()
            out.update(status="dry_run", message="Dry run: nothing sent.")
            return out

        # Open the dialog from the page's own Connect control (no second page).
        if ctl.get("found"):
            anchor = page.locator(_CONNECT_TARGET).first
        else:
            anchor = page.locator(_INVITE_ANCHOR.replace("{u}", username)).first
        opened = False
        try:
            await _pause(0.6, 1.4)
            await anchor.click(timeout=5000)
            opened = await _invite_dialog_open(page, conn)
        except Exception:
            logger.debug("Connect link click failed", exc_info=True)
        if not opened:
            # The link was in the More menu or did not open in place: use the
            # invite page (one navigation, then the profile again to verify).
            invite = (
                "https://www.linkedin.com/preload/custom-invite/"
                f"?vanityName={quote_plus(username)}"
            )
            out["via"] = "deeplink"
            await conn._navigator._navigate_to_page(invite)
            await _invite_dialog_open(page, conn)

        trace: list[str] = []
        submitted, note_sent, limit_msg = False, False, None
        # Notices already on the page are not the answer to this send.
        notices_before = set(await _page_notices(page))
        if not note:
            # The dialog's primary (last) button is "Send without a note".
            submitted, blocked = await _send_without_note(page, trace)
            if blocked is not None:
                try:
                    await conn._press_escape()
                except Exception:
                    logger.debug("Escape after a blocked invite dialog failed", exc_info=True)
                if blocked == PENDING_DIALOG:
                    out.update(status="pending", message="A request is already pending (LinkedIn offered to withdraw it; nothing was clicked).")
                    out["trace"] = trace
                    logger.warning("msgkit_connect %s: pending | steps: %s", username, "; ".join(trace))
                    return out
                who = str(fields.get("name") or "this person").split(" ")[0]
                asks_email = bool(re.search(r"e-?mail", blocked, re.I))
                out.update(
                    status="connect_unavailable",
                    message=(
                        f"LinkedIn asks for {who}'s email address before it lets you connect; nothing was sent."
                        if asks_email
                        else "LinkedIn's invite dialog would not let us send (Send stayed disabled); nothing was sent."
                    )
                    + f" The dialog said: {blocked[:200]}",
                )
                out["trace"] = trace
                logger.warning("msgkit_connect %s: blocked | steps: %s", username, "; ".join(trace))
                return out
        if not submitted:
            submitted, note_sent, limit_msg = await conn._submit_invite_dialog(note)
        if limit_msg is not None:
            out.update(status="custom_note_limit_reached", message=limit_msg)
            return out
        if not submitted:
            out.update(
                status="connect_unavailable",
                message="The invite dialog did not accept the request."
                + (f" Steps: {'; '.join(trace)}." if trace else ""),
            )
            return out
        out["note_sent"] = bool(note_sent)

        if out["via"] == "deeplink":
            session = await _open(extractor, url)  # a second profile view
            out["extra_profile_view"] = True

        # LinkedIn repaints the button a moment after sending; check for a while.
        async def still_connectable() -> bool:
            after = await conn._read_action_signals(username)
            ctl_now = await _connect_control(session.page, "top")
            if not ctl_now.get("found") and via_menu and out["via"] == "button":
                ctl_now = await _open_more_for_connect(session.page, conn)
                if ctl_now.get("found"):
                    await conn._press_escape()
            return bool(after.has_invite_anchor or ctl_now.get("found"))

        # LinkedIn confirms with a toast ("Invitation sent to <First name>") within a second or
        # two, and keeps drawing Connect on the profile for a while after: the toast is the
        # proof (checked on a live send: the toast appeared, Sent invitations listed the
        # person, and the profile still showed Connect after a reload).

        # Wait up to ~4 s for a new notice that says the invitation went out; any other new
        # notice is kept for the message but does not end the wait.
        notices = ""
        for _ in range(8):
            fresh = [n for n in await _page_notices(session.page) if n not in notices_before]
            if fresh:
                notices = " | ".join(fresh)[:300]
                if _says_sent(notices):
                    break
            await asyncio.sleep(0.5)
        toast_says_sent = bool(notices and _says_sent(notices))
        if notices:
            out["notice"] = notices
            trace.append(f"toast: {notices[:80]}")
        sent = toast_says_sent
        if not sent:
            for _ in range(4):
                await asyncio.sleep(3.0)
                if not await still_connectable():
                    sent = True
                    break
        if not sent:
            # Still Connect after ~12 s: read the profile fresh (one more view) so the
            # answer is sent or not sent, never "unknown".
            session = await _open(extractor, url)
            out["extra_profile_view"] = True
            await _pause(1.5, 2.5)
            sent = not await still_connectable()
        if sent:
            out.update(status="sent", message="Invitation sent.")
        else:
            out.update(
                status="send_failed",
                message="LinkedIn did not accept the invitation: the profile still shows "
                "Connect after reloading."
                + (f" LinkedIn said: {notices}." if notices else "")
                + (f" Steps: {'; '.join(trace)}." if trace else ""),
            )
        out["trace"] = trace
        logger.warning(
            "msgkit_connect %s: %s | steps: %s",
            username,
            out["status"],
            "; ".join(trace),
        )
        return out
