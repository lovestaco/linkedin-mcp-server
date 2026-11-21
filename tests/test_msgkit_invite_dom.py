"""msgkit_connect's invite Send button, in headless chromium: the chat window LinkedIn keeps open is a dialog too.

On 2026-11-21 ten requests failed because the tagger picked the chat window's last button ("Open send options", one
of 87) instead of the invite's "Send without a note". Skipped automatically when chromium is not installed.
"""

from __future__ import annotations

import pytest
from patchright.async_api import async_playwright

from linkedin_mcp_server.tools import msgkit as mk

pytestmark = [pytest.mark.browser_dom, pytest.mark.xdist_group("browser_runtime")]

CHAT = """<aside class="msg-overlay-container"><div role="dialog" class="msg-overlay-conversation-bubble">
  <button>Close</button><button>Minimize</button>
  <div contenteditable="true" role="textbox"></div>
  <button>Open GIF Keyboard</button><button>Open Emoji Keyboard</button><button>Send</button><button>Open send options</button>
</div></aside>"""
INVITE = """<dialog open><p>Add a note to your invitation?</p>
  <button aria-label="Dismiss">x</button><a href="#">Add a note</a><button>Send without a note</button>
</dialog>"""


@pytest.fixture
async def page():
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(channel="chromium", headless=True)
            pg = await browser.new_page()
        except Exception as exc:  # browser binary missing
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            yield pg
        finally:
            await browser.close()


async def test_the_invite_send_button_is_picked_over_an_open_chat(page):
    await page.set_content(f"<main>profile</main>{CHAT}{INVITE}")
    tagged = await page.evaluate(mk._TAG_SEND_JS)
    assert tagged == {"found": True, "text": "Send without a note", "buttons": 2}
    assert await page.locator(mk._SEND_TARGET).inner_text() == "Send without a note"


async def test_an_open_chat_alone_is_not_an_invite(page):
    await page.set_content(f"<main>profile</main>{CHAT}")
    assert await page.evaluate(mk._TAG_SEND_JS) == {"found": False}
    assert await page.locator(mk._SEND_TARGET).count() == 0
