"""
List an account's LinkedIn connections, newest first (fork-only).

A port of the msgkit Chrome extension's "List my connections" (connections_sync_content.js): open
/mynetwork/invite-connect/connections/, read the rows, scroll, repeat, and stop at the first profile the
caller already has (``stop_at_profile``), at the end of the list, or when scrolling loads nothing new.

The scroll loop is plain Python over a ``page`` so it can be tested with a scripted page; the three small
browser programs below are ports of the extension's readRows / totalConnections / clickLoadMore.

One difference on purpose: the extension reports an empty page (logged out, blocked, layout changed) as
"Already up to date". Here an empty first read is its own outcome (``stopped_reason == "empty"``) so the
caller can fail the run instead of recording a false success.
"""

from __future__ import annotations

import random
import re
import time
from typing import Any, Awaitable, Callable

CONNECTIONS_URL = "https://www.linkedin.com/mynetwork/invite-connect/connections/"

#: Scrolls that load nothing new before the list is taken to have ended (the extension's MAX_STALE_SCROLLS).
MAX_STALE_SCROLLS = 3

# Every <p> that starts "Connected on " is one row; the row is the nearest ancestor (up to 8 levels) that also
# holds the profile link and the Message (compose) link. Two links point at the profile: the avatar (no <p>) and
# the name/tagline one. Other msgkit scripts inject a button into that anchor, so only its <p> elements are
# read: first the name, second the tagline. The message link is whatever LinkedIn put in the anchor's href.
ROWS_JS = r"""() => {
  const MONTHS = {January:1,February:2,March:3,April:4,May:5,June:6,July:7,August:8,September:9,October:10,November:11,December:12};
  const norm = (href) => {
    try { const m = new URL(href, location.origin).pathname.match(/^\/in\/([^/]+)/); return m ? 'https://www.linkedin.com/in/' + m[1] + '/' : ''; }
    catch (_) { return ''; }
  };
  const when = (text) => {
    const m = text.trim().match(/^Connected on (\w+) (\d{1,2}), (\d{4})$/);
    if (!m || !MONTHS[m[1]]) return text.trim();
    return m[3] + '-' + String(MONTHS[m[1]]).padStart(2, '0') + '-' + String(Number(m[2])).padStart(2, '0');
  };
  const rows = [];
  for (const p of document.querySelectorAll('p')) {
    const text = (p.innerText || '').trim();
    if (!text.startsWith('Connected on ')) continue;
    let row = p.parentElement;
    for (let i = 0; i < 8 && row; i++) {
      if (row.querySelector('a[href*="/in/"]') && row.querySelector('a[href*="messaging/compose"]')) break;
      row = row.parentElement;
    }
    if (!row) continue;
    const link = Array.from(row.querySelectorAll('a[href*="/in/"]')).find((a) => a.querySelector('p'));
    if (!link) continue;
    const profile_link = norm(link.href);
    if (!profile_link) continue;
    const ps = Array.from(link.querySelectorAll('p'));
    rows.push({
      profile_link,
      name: ((ps[0] && ps[0].innerText) || '').trim(),
      tagline: ((ps[1] && ps[1].innerText) || '').trim(),
      connected_on: when(text),
      message_link: row.querySelector('a[href*="messaging/compose"]').href,
    });
  }
  return rows;
}"""

# "1,234 connections" near the top of the page: how many the list will hold.
TOTAL_JS = r"""() => {
  for (const p of document.querySelectorAll('p')) {
    const m = (p.innerText || '').trim().match(/^([\d,]+)\s*connections?$/i);
    if (m) return parseInt(m[1].replace(/,/g, ''), 10);
  }
  return null;
}"""

# The list scrolls inside #workspace when that is scrollable, else the page itself.
SCROLL_JS = r"""() => {
  const w = document.getElementById('workspace');
  const s = (w && w.scrollHeight > w.clientHeight) ? w : (document.scrollingElement || document.documentElement);
  s.scrollTo({ top: s.scrollHeight, behavior: 'smooth' });
}"""

LOAD_MORE_JS = r"""() => {
  const b = Array.from(document.querySelectorAll('button')).find((x) => /^(load|show) more/i.test((x.innerText || '').trim()));
  if (b) b.click();
  return !!b;
}"""

PAGE_TEXT_JS = "() => (document.querySelector('main') || document.body || {}).innerText || ''"

_SLUG_RE = re.compile(r"/in/([^/?#]+)", re.I)


def profile_key(link: str | None) -> str:
    """The comparable form of a profile link: its lower-cased slug ("" when it is not a /in/ link)."""
    m = _SLUG_RE.search(link or "")
    return m.group(1).strip().lower() if m else ""


async def _random_pause(lo: float, hi: float) -> None:
    import asyncio

    await asyncio.sleep(random.uniform(lo, hi))


async def list_connections(
    page: Any,
    *,
    stop_at_profile: str = "",
    max_rows: int = 5000,
    max_seconds: float = 2400,
    pause_min_s: float = 5.0,
    pause_max_s: float = 10.0,
    pause: Callable[[float, float], Awaitable[None]] = _random_pause,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Read connections from the open connections page until a stop rule fires.

    Returns {rows, stopped_reason, total_visible, scrolls, seconds}. ``rows`` are in page order (newest first) and
    never include ``stop_at_profile`` or anything after it. ``stopped_reason`` is one of:
      checkpoint  the stop profile was reached (a normal incremental run)
      end         every connection the page reports (its total) was read
      stale       three scrolls in a row loaded nothing new (the list ended)
      limit       max_rows or max_seconds was hit before the end: the rows are good but the list is not finished
      empty       the first read found no rows at all (not signed in, blocked, or the page changed)
    """
    started = clock()
    stop_key = profile_key(stop_at_profile)
    seen: dict[str, dict[str, Any]] = {}
    rows: list[dict[str, Any]] = []
    total = await page.evaluate(TOTAL_JS)
    scrolls = 0
    stale = 0
    reason = ""

    def read(batch: list[dict[str, Any]]) -> bool:
        """Take the unseen rows of a read; True when the stop profile was reached."""
        for r in batch:
            key = profile_key(r.get("profile_link"))
            if not key or key in seen:
                continue
            if stop_key and key == stop_key:
                return True
            seen[key] = r
            rows.append(r)
        return False

    batch = await page.evaluate(ROWS_JS)
    if not batch:
        text = ""
        try:
            text = (await page.evaluate(PAGE_TEXT_JS)) or ""
        except Exception:
            pass
        return {
            "rows": [],
            "stopped_reason": "empty",
            "total_visible": total,
            "scrolls": 0,
            "seconds": round(clock() - started, 1),
            "empty_text": text[:600],
        }

    hit = read(batch)
    while True:
        if hit:
            reason = "checkpoint"
            break
        if total and len(rows) >= total:
            reason = "end"
            break
        if len(rows) >= max_rows or clock() - started >= max_seconds:
            reason = "limit"
            break
        await page.evaluate(SCROLL_JS)
        scrolls += 1
        await pause(pause_min_s, pause_max_s)
        await page.evaluate(LOAD_MORE_JS)
        before = len(rows)
        batch = await page.evaluate(ROWS_JS)
        hit = read(batch)
        stale = 0 if (len(rows) > before or hit) else stale + 1
        if stale >= MAX_STALE_SCROLLS:
            reason = "stale"
            break

    return {
        "rows": rows,
        "stopped_reason": reason,
        "total_visible": total,
        "scrolls": scrolls,
        "seconds": round(clock() - started, 1),
    }
