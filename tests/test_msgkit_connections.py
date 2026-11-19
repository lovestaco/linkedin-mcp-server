"""msgkit_list_connections: the scroll loop and its stop rules, over a scripted page (no browser)."""

import pytest

from linkedin_mcp_server.tools import msgkit_connections as mc


def row(slug: str, name: str = "", on: str = "2026-11-01") -> dict:
    return {
        "profile_link": f"https://www.linkedin.com/in/{slug}/",
        "name": name or slug.title(),
        "tagline": "Engineer",
        "connected_on": on,
        "message_link": f"https://www.linkedin.com/messaging/compose/?recipient={slug}",
    }


class ScriptedPage:
    """A connections page that shows `visible[0]` rows first and `visible[n]` after the n-th scroll."""

    def __init__(self, visible: list[list[dict]], total: int | None = None, text: str = ""):
        self.visible, self.total, self.text = visible, total, text
        self.step = 0
        self.scrolls = 0

    async def evaluate(self, js: str, *args):
        if js is mc.TOTAL_JS:
            return self.total
        if js is mc.ROWS_JS:
            return self.visible[min(self.step, len(self.visible) - 1)]
        if js is mc.SCROLL_JS:
            self.step += 1
            self.scrolls += 1
            return None
        if js is mc.LOAD_MORE_JS:
            return False
        if js is mc.PAGE_TEXT_JS:
            return self.text
        raise AssertionError("unexpected program")


async def no_pause(lo: float, hi: float) -> None:
    return None


def run(page, **kw):
    return mc.list_connections(page, pause=no_pause, **kw)


def links(out) -> list[str]:
    return [mc.profile_key(r["profile_link"]) for r in out["rows"]]


def test_profile_key_ignores_case_slash_and_query():
    assert mc.profile_key("https://www.linkedin.com/in/Ada-Lovelace/?x=1") == "ada-lovelace"
    assert mc.profile_key("https://linkedin.com/in/ada-lovelace") == "ada-lovelace"
    assert mc.profile_key("https://www.linkedin.com/company/acme") == ""
    assert mc.profile_key("") == ""


@pytest.mark.asyncio
async def test_stops_at_the_checkpoint_and_leaves_it_and_older_rows_out():
    page = ScriptedPage([[row("a"), row("b")], [row("a"), row("b"), row("c"), row("d"), row("e")]])
    out = await run(page, stop_at_profile="https://www.linkedin.com/in/D")
    assert links(out) == ["a", "b", "c"]
    assert out["stopped_reason"] == "checkpoint"
    assert out["scrolls"] == 1


@pytest.mark.asyncio
async def test_checkpoint_on_the_first_row_means_nothing_new():
    page = ScriptedPage([[row("a"), row("b")]])
    out = await run(page, stop_at_profile="https://www.linkedin.com/in/a")
    assert out["rows"] == [] and out["stopped_reason"] == "checkpoint" and out["scrolls"] == 0


@pytest.mark.asyncio
async def test_stops_when_every_reported_connection_was_read():
    page = ScriptedPage([[row("a"), row("b")], [row("a"), row("b"), row("c")]], total=3)
    out = await run(page)
    assert links(out) == ["a", "b", "c"] and out["stopped_reason"] == "end" and out["total_visible"] == 3


@pytest.mark.asyncio
async def test_three_scrolls_with_nothing_new_end_the_list():
    page = ScriptedPage([[row("a")], [row("a"), row("b")]])  # then the page stops growing
    out = await run(page)
    assert links(out) == ["a", "b"] and out["stopped_reason"] == "stale"
    assert out["scrolls"] == 1 + mc.MAX_STALE_SCROLLS  # one scroll that loaded b, then three that loaded nothing


@pytest.mark.asyncio
async def test_row_limit_is_reported_as_limit_not_end():
    page = ScriptedPage([[row(str(i)) for i in range(n)] for n in (2, 4, 6, 8)], total=100)
    out = await run(page, max_rows=4)
    assert out["stopped_reason"] == "limit" and len(out["rows"]) >= 4


@pytest.mark.asyncio
async def test_time_limit_is_reported_as_limit():
    t = {"now": 0.0}

    def clock() -> float:
        t["now"] += 400.0
        return t["now"]

    page = ScriptedPage([[row("a")], [row("a"), row("b")], [row("a"), row("b"), row("c")]], total=50)
    out = await mc.list_connections(page, pause=no_pause, clock=clock, max_seconds=1000)
    assert out["stopped_reason"] == "limit"


@pytest.mark.asyncio
async def test_an_empty_page_is_not_up_to_date():
    page = ScriptedPage([[]], text="Sign in to see your connections")
    out = await run(page, stop_at_profile="https://www.linkedin.com/in/a")
    assert out["rows"] == [] and out["stopped_reason"] == "empty"
    assert "Sign in" in out["empty_text"]


@pytest.mark.asyncio
async def test_duplicates_and_non_profile_rows_are_skipped():
    bad = {"profile_link": "", "name": "x", "tagline": "", "connected_on": "", "message_link": ""}
    page = ScriptedPage([[row("a"), row("A"), bad, row("b")]], total=2)
    out = await run(page)
    assert links(out) == ["a", "b"]
