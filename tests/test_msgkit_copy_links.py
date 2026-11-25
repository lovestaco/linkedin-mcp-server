"""msgkit_person_activity: each card gets its own link, however late LinkedIn is (no browser)."""

import asyncio

import pytest

from linkedin_mcp_server.tools import msgkit


class Card:
    """One scripted card: its menu shows after `menu` seconds, its link is copied after `copy` seconds."""

    def __init__(self, n: int, menu: float = 0.0, copy: float | None = 0.0):
        self.n, self.menu, self.copy = n, menu, copy


class ScriptedPage:
    def __init__(self, cards: list[Card]):
        self.cards = cards
        self.copied: list[str] = []
        self.open: Card | None = None
        self.keyboard = self
        self.escapes = 0

    async def evaluate(self, js: str, *args, **kwargs):
        if js is msgkit._TAKE_COPIED_JS:
            out, self.copied = self.copied, []
            return out
        return True

    async def press(self, key: str) -> None:
        self.escapes += 1
        self.open = None

    def locator(self, selector: str):
        return CardList(self)

    def get_by_role(self, role: str, name: str):
        return MenuItem(self)


class CardList:
    def __init__(self, page: ScriptedPage):
        self.page = page

    def nth(self, i: int):
        return CardHandle(self.page, self.page.cards[i])


class CardHandle:
    def __init__(self, page: ScriptedPage, card: Card):
        self.page, self.card = page, card

    async def scroll_into_view_if_needed(self, timeout: int) -> None: ...

    def locator(self, selector: str):
        return self

    @property
    def first(self):
        return self

    async def click(self, timeout: int) -> None:
        self.page.open = self.card


class MenuItem:
    def __init__(self, page: ScriptedPage):
        self.page = page

    @property
    def first(self):
        return self

    async def wait_for(self, state: str, timeout: int) -> None:
        if self.page.open.menu * 1000 > timeout:
            raise TimeoutError(f"Timeout {timeout}ms exceeded")
        await asyncio.sleep(self.page.open.menu)

    async def click(self, timeout: int) -> None:
        card = self.page.open
        if card.copy is not None:
            loop = asyncio.get_running_loop()
            loop.call_later(
                card.copy, lambda: self.page.copied.append(f"link-{card.n}")
            )


@pytest.fixture(autouse=True)
def quick(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_pause(lo: float, hi: float) -> None: ...

    monkeypatch.setattr(msgkit, "_pause", no_pause)
    monkeypatch.setattr(msgkit, "_COPY_WAIT_S", 0.8)
    monkeypatch.setattr(msgkit, "_MENU_ITEM_TIMEOUT_MS", 500)


def make(cards: list[Card]) -> tuple[ScriptedPage, list[dict]]:
    return ScriptedPage(cards), [{"index": c.n} for c in cards]


async def test_each_card_gets_its_own_link_when_links_come_late() -> None:
    page, posts = make([Card(0, copy=0.4), Card(1, copy=0.0), Card(2, copy=0.3)])
    errors = await msgkit._copy_card_links(page, posts)
    assert [p["share_url"] for p in posts] == ["link-0", "link-1", "link-2"]
    assert errors == []


async def test_a_card_without_a_link_stops_the_rest_instead_of_passing_on_the_wrong_one() -> (
    None
):
    page, posts = make([Card(0), Card(1, copy=None), Card(2), Card(3)])
    errors = await msgkit._copy_card_links(page, posts)
    assert [p["share_url"] for p in posts] == ["link-0", None, None, None]
    assert errors == [
        "card 1: nothing copied",
        "card 2: skipped, card 1 never gave its link",
        "card 3: skipped, card 1 never gave its link",
    ]


async def test_a_menu_that_never_opens_is_reported_and_the_next_card_still_works() -> (
    None
):
    page, posts = make([Card(0), Card(1, menu=9), Card(2)])
    errors = await msgkit._copy_card_links(page, posts)
    assert [p["share_url"] for p in posts] == ["link-0", None, "link-2"]
    assert len(errors) == 1 and errors[0].startswith("card 1: TimeoutError")
    assert page.escapes == 1


async def test_a_slow_menu_is_waited_for() -> None:
    page, posts = make([Card(0, menu=0.3)])
    assert await msgkit._copy_card_links(page, posts) == []
    assert posts[0]["share_url"] == "link-0"


async def test_the_loop_stops_when_it_runs_out_of_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(msgkit, "_COPY_BUDGET_S", -1.0)
    page, posts = make([Card(0), Card(1)])
    assert await msgkit._copy_card_links(page, posts) == [
        "card 0: skipped, out of time",
        "card 1: skipped, out of time",
    ]
