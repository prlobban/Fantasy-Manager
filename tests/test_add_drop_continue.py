"""2026-10-21 — the add/drop lane died on a Continue that was merely late.

Tuesday's week-7 sweep tried two free-agent swaps, Titans D/ST for the Ravens
D/ST and Blake Corum for Emanuel Wilson, and both came back:

    Continue is still disabled after selecting the drop — ESPN has not
    accepted the transaction, so nothing was committed.

The exception screenshots show the DROP had been clicked on the right row both
times. `add_drop` read ESPN's Continue button EXACTLY ONCE, 800 ms after that
click, and failed the write closed — the shortest wait in the module, guarding
the one state transition that decides whether a write happens at all. The draft
room learned on 2026-09-04 that an ESPN button is not live the moment it
renders and waits for it (`_wait_for_draft_button`); the waiver path never did.

~5.6 projected starting points sat on the wire for the week because of it.
"""

from __future__ import annotations

import re

import pytest

from core.browser import actions as A
from core.browser import selectors as S

# ── fakes ────────────────────────────────────────────────────────────────────


class FakeEl:
    """A button, row or input — everything the action layer asks of one.

    `disabled_for` is ESPN taking its time: the element reports disabled for
    that many reads and is enabled from then on.
    """

    def __init__(self, *, text="", cls="", html=None, enabled=True,
                 disabled_for=0, children=None):
        self.text, self.cls = text, cls
        self.html = text if html is None else html
        self._enabled, self._disabled_for = enabled, disabled_for
        self.children = children or {}
        self.clicks = 0
        self.filled: str | None = None
        self.pressed: list[str] = []

    def is_disabled(self):
        if self._disabled_for > 0:
            self._disabled_for -= 1
            return True
        return not self._enabled

    def is_enabled(self):
        return not self.is_disabled()

    def is_visible(self):
        return True

    def evaluate(self, _script):
        return {"id": "", "cls": self.cls, "aria": "", "name": "", "visible": True}

    def get_attribute(self, name):
        return self.cls if name == "class" else None

    def inner_text(self):
        return self.text

    def inner_html(self):
        return self.html

    def click(self, **_):
        self.clicks += 1

    def fill(self, value):
        self.filled = value

    def press(self, key):
        self.pressed.append(key)

    def locator(self, sel):
        return FakeLoc(self.children.get(sel, []))


class FakeLoc:
    def __init__(self, items):
        self._items = list(items)

    def count(self):
        return len(self._items)

    def nth(self, i):
        return self._items[i]

    @property
    def first(self):
        return self._items[0]

    def filter(self, has_text=None):
        return FakeLoc([e for e in self._items
                        if has_text is None or has_text.search(e.inner_text())])

    def locator(self, sel):
        out: list[FakeEl] = []
        for e in self._items:
            out.extend(e.children.get(sel, []))
        return FakeLoc(out)


class FakePage:
    def __init__(self, *, body="", matches=None):
        self.body = body
        self.matches = matches or {}
        self.waits: list[int] = []

    def inner_text(self, _sel):
        return self.body

    def locator(self, sel):
        return FakeLoc(self.matches.get(sel, []))

    def wait_for_timeout(self, ms):
        self.waits.append(ms)


class FakeSession:
    def __init__(self, page):
        self.page = page
        self.urls: list[str] = []

    def goto(self, url):
        self.urls.append(url)
        return self.page

    def dismiss_overlays(self):
        pass

    def screenshot(self, _label):
        return None


def _row(slot_and_name: str, espn_id: int | None, *, drop: FakeEl | None = None):
    children = {S.DROP_PLAYER_BUTTON: [drop]} if drop is not None else {}
    return FakeEl(text=slot_and_name,
                  html=f"{slot_and_name} headshot/{espn_id}.png" if espn_id else
                       slot_and_name,
                  children=children)


def _add_page(*, continue_disabled_for=0, continue_enabled=True):
    """The 2026-10-21 page: Blake Corum searched, roster full, Emanuel Wilson
    the intended drop."""
    add = FakeEl(text="Add", cls="add-action-btn")
    drop = FakeEl(text="DROP", cls="drop-action-btn")
    cont = FakeEl(text="Continue", cls="Button",
                  enabled=continue_enabled, disabled_for=continue_disabled_for)
    search = FakeEl(text="", cls="player-search")

    fa_row = FakeEl(text="Blake Corum LAR RB",
                    children={"button.add-action-btn": [add]})
    roster = [
        _row("QB Josh Allen BUF QB", 3918298),
        _row("RB Jaylen Warren PIT RB", 3121422,
             drop=FakeEl(text="DROP", cls="drop-action-btn")),
        _row("RB Emanuel Wilson SEA RB", 4887558, drop=drop),
        _row("D/ST Ravens BAL D/ST", None,
             drop=FakeEl(text="DROP", cls="drop-action-btn")),
    ]
    page = FakePage(
        body="You must drop a player to clear your roster for the incoming players.",
        matches={
            "input[placeholder='Player Name']": [search],
            S.ANY_ROW: [fa_row, *roster],
            S.PLAYER_TABLE_ROW: roster,
            "button:has-text('Continue')": [cont],
            S.CONFIRM_BUTTON: [cont],
        },
    )
    return page, {"add": add, "drop": drop, "continue": cont, "search": search}


def _corum_for_wilson(page):
    return A.add_drop(FakeSession(page), 12345, 2026,
                      4429096, "Blake Corum", 4887558, "Emanuel Wilson",
                      team_id=7)


# ── the fault ────────────────────────────────────────────────────────────────


def test_a_continue_espn_enables_a_beat_late_still_commits_the_add():
    """2026-10-21: Continue was read once, 800 ms after the drop click, and both
    of the week's free-agent swaps were thrown away on it."""
    page, part = _add_page(continue_disabled_for=3)

    receipt = _corum_for_wilson(page)

    assert part["drop"].clicks == 1, "the drop must be selected exactly once"
    assert part["continue"].clicks >= 1, "ESPN enabled Continue; it must be clicked"
    assert "drop Emanuel Wilson" in receipt.detail


def test_the_drop_selected_is_the_row_the_plan_named():
    """The wait must not turn into clicking whatever row comes first."""
    page, part = _add_page(continue_disabled_for=2)
    others = [r.children[S.DROP_PLAYER_BUTTON][0]
              for r in page.matches[S.PLAYER_TABLE_ROW]
              if r.children.get(S.DROP_PLAYER_BUTTON)
              and r.children[S.DROP_PLAYER_BUTTON][0] is not part["drop"]]

    _corum_for_wilson(page)

    assert part["drop"].clicks == 1
    assert all(d.clicks == 0 for d in others)


def test_a_continue_that_never_enables_is_still_a_refusal(monkeypatch):
    """Fail-closed (§10.6) survives the wait: no Continue, no write."""
    monkeypatch.setattr(A, "_CONFIRM_ENABLE_MS", 50)
    page, part = _add_page(continue_enabled=False)

    with pytest.raises(A.ActionFailed, match="still disabled"):
        _corum_for_wilson(page)

    assert part["continue"].clicks == 0


# ── the same wait on the other half of a stream ──────────────────────────────


def _drop_page(*, continue_disabled_for=0):
    toolbar = FakeEl(text="Drop", cls="")
    drop = FakeEl(text="DROP", cls="drop-action-btn")
    cont = FakeEl(text="Continue", cls="Button", disabled_for=continue_disabled_for)
    roster = [_row("D/ST Ravens BAL D/ST", None, drop=drop)]
    page = FakePage(
        body="",
        matches={
            "button:text-is('Drop'):not(.drop-action-btn)": [toolbar],
            S.PLAYER_TABLE_ROW: roster,
            "button:has-text('Continue')": [cont],
            S.CONFIRM_BUTTON: [cont],
        },
    )
    return page, {"drop": drop, "continue": cont}


def test_drop_player_also_waits_for_continue():
    """`drop_player` finishes an add that committed on its own (2026-09-12), so
    it cannot fail closed on the same late button."""
    page, part = _drop_page(continue_disabled_for=3)

    A.drop_player(FakeSession(page), 12345, 7, 2026, -16033, "Ravens D/ST")

    assert part["drop"].clicks == 1
    assert part["continue"].clicks >= 1


def test_the_fake_page_matches_the_selectors_in_use():
    """A fake that drifts from selectors.py proves nothing."""
    assert re.search(r"has-text\('Continue'\)", S.CONFIRM_BUTTON)
    assert "drop-action-btn" in S.DROP_PLAYER_BUTTON
