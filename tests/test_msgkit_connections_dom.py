"""msgkit_list_connections: the browser programs in headless chromium, on a page shaped like the connections list.

The row structure is the one the msgkit Chrome extension reads (connections_sync_content.js): a "Connected on" <p>,
an avatar link with no text, a name/tagline link holding two <p>, and a Message (compose) link, all inside one row.
Skipped automatically when chromium is not installed.
"""

from __future__ import annotations

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.tools import msgkit_connections as mc

pytestmark = [pytest.mark.browser_dom, pytest.mark.xdist_group("browser_runtime")]

HTML = """<!doctype html><html><body><main id="workspace" style="height:300px;overflow:auto">
<p>1,234 connections</p>
<ul>
 <li class="row"><a href="/in/ada-lovelace/"><img alt=""></a>
   <a href="/in/ada-lovelace/?miniProfileUrn=x"><p>Ada Lovelace</p><p>Analytical Engine | Math</p></a>
   <p>Connected on November 5, 2026</p>
   <a href="/messaging/compose/?profileUrn=urn%3Ali%3Afsd_profile%3AACoAAB&amp;recipient=ACoAAB&amp;interop=msgOverlay">Message</a></li>
 <li class="row"><a href="/in/Grace-Hopper"><img alt=""></a>
   <a href="/in/Grace-Hopper"><p>Grace Hopper</p><p></p></a>
   <p>Connected on March 12, 2025</p>
   <a href="/messaging/compose/?recipient=ACoAAC">Message</a></li>
</ul>
<button id="more">Show more results</button>
</main></body></html>"""


async def serve(page, html: str) -> None:
    """Show `html` at the real connections address, so relative links resolve against linkedin.com."""
    await page.unroute_all()
    await page.route(mc.CONNECTIONS_URL, lambda route: route.fulfill(status=200, content_type="text/html", body=html))
    await page.goto(mc.CONNECTIONS_URL)


@pytest.fixture
async def dom_page():
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chromium", headless=True)
            page = await browser.new_page()
        except Exception as exc:  # browser binary missing
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            await serve(page, HTML)
            yield page
        finally:
            await browser.close()


async def test_rows_are_read_the_way_the_extension_reads_them(dom_page):
    rows = await dom_page.evaluate(mc.ROWS_JS)
    assert [r["profile_link"] for r in rows] == [
        "https://www.linkedin.com/in/ada-lovelace/",
        "https://www.linkedin.com/in/Grace-Hopper/",
    ]
    ada, grace = rows
    assert ada["name"] == "Ada Lovelace" and ada["tagline"] == "Analytical Engine | Math"
    assert ada["connected_on"] == "2026-11-05"
    assert ada["message_link"].startswith("https://www.linkedin.com/messaging/compose/")
    assert "profileUrn=urn%3Ali%3Afsd_profile%3AACoAAB" in ada["message_link"]
    assert grace["name"] == "Grace Hopper" and grace["tagline"] == "" and grace["connected_on"] == "2025-03-12"


async def test_total_and_load_more(dom_page):
    assert await dom_page.evaluate(mc.TOTAL_JS) == 1234
    assert await dom_page.evaluate(mc.LOAD_MORE_JS) is True


async def test_the_whole_loop_on_a_real_page(dom_page):
    async def no_pause(lo, hi):
        return None

    out = await mc.list_connections(dom_page, stop_at_profile="https://www.linkedin.com/in/grace-hopper", pause=no_pause)
    assert [mc.profile_key(r["profile_link"]) for r in out["rows"]] == ["ada-lovelace"]
    assert out["stopped_reason"] == "checkpoint" and out["total_visible"] == 1234


async def test_a_page_without_connections_is_empty(dom_page):
    await serve(dom_page, "<html><body><main><p>Sign in to see your connections</p></main></body></html>")

    async def no_pause(lo, hi):
        return None

    out = await mc.list_connections(dom_page, pause=no_pause)
    assert out["stopped_reason"] == "empty" and "Sign in" in out["empty_text"]
