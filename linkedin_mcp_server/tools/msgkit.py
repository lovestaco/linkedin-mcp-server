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

Copying a link never touches the machine's clipboard: the page's own
navigator.clipboard.writeText is replaced in the page's main world before the
menu item is clicked, and the copied text is read back from there.
"""

import asyncio
import logging
import random
from typing import Annotated, Any

from fastmcp import Context, FastMCP
from pydantic import Field

from linkedin_mcp_server.config.schema import DEFAULT_TOOL_TIMEOUT_SECONDS
from linkedin_mcp_server.dependencies import get_ready_extractor, tool_errors
from linkedin_mcp_server.linkedin.identifiers import normalize_person_identifier

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


async def _pause(lo: float, hi: float) -> None:
    await asyncio.sleep(random.uniform(lo, hi))


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
        actor_links, share_url}], cards_seen, copy_errors}. share_url is the
        link LinkedIn's "Copy link to post" gives (an lnkd.in short link).
        """
        username = normalize_person_identifier(linkedin_username)
        extractor = await get_ready_extractor(ctx, tool_name="msgkit_person_activity")
        url = f"https://www.linkedin.com/in/{username}/recent-activity/all/"
        session = await _open(extractor, url)
        page = session.page

        try:
            await page.wait_for_selector(_CARD, timeout=12000)
        except Exception:
            text = await page.evaluate("() => (document.querySelector('main')||{}).innerText || ''")
            return {"url": url, "posts": [], "cards_seen": 0, "copy_errors": [],
                    "empty_text": text[:600]}

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
                    await card.locator('button[aria-label^="Open control menu"]').first.click(timeout=5000)
                    item = page.get_by_role("menuitem", name="Copy link to post")
                    await item.first.click(timeout=5000)
                    await _pause(0.5, 1.0)
                    copied = await page.evaluate(_TAKE_COPIED_JS, isolated_context=False)
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

        return {"url": url, "posts": posts, "cards_seen": await page.locator(_CARD).count(),
                "copy_errors": copy_errors}

    @mcp.tool(
        timeout=tool_timeout,
        title="msgkit: Person Current Company",
        annotations={"readOnlyHint": True, "openWorldHint": True},
        tags={"person", "msgkit"},
    )
    @tool_errors
    async def msgkit_person_company(linkedin_username: str, ctx: Context) -> dict[str, Any]:
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
