"""Answering an incoming offer — the 2026-09-30 reject that could never land.

Every reject before this date navigated to `/football/tradeoffers`, which is a
404, so the Lion Family counter (`ede95b6e`) stayed open after the agent
declined it. The real page is `/football/tradereview`, and it is the more
dangerous of the two: it loads with "Accept Trade" PRESELECTED and a single
button reading "Accept". A decline that clicks the button without moving the
radio accepts the trade. These tests pin that it cannot.
"""

from __future__ import annotations

import pytest

from core.browser import actions as A
from core.browser import selectors as S

NAMES = ["Xavier Worthy", "Omarion Hampton", "Matthew Golden", "D'Andre Swift"]


class _Loc:
    def __init__(self, items):
        self._items = items

    def count(self):
        return len(self._items)

    @property
    def first(self):
        return self._items[0]


class _Radio:
    def __init__(self, page, value, sticky=True):
        self.page, self.value, self.sticky = page, value, sticky

    def click(self, **_):
        if self.sticky:
            self.page.choice = self.value

    def is_checked(self):
        return self.page.choice == self.value


class _Button:
    def __init__(self, page, text):
        self.page, self.text = page, text

    def click(self, **_):
        self.page.clicked.append(self.text)

    def is_disabled(self):
        return False


class ReviewPage:
    """ESPN's /football/tradereview as observed live on 2026-09-30."""

    def __init__(self, *, names=NAMES, radio_sticks=True):
        self.body = "Review Trade | Respond to Trade | " + " | ".join(names)
        self.choice = "accept"  # preselected on load
        self.clicked: list[str] = []
        self.accept = _Radio(self, "accept", radio_sticks)
        self.decline = _Radio(self, "decline", radio_sticks)

    def inner_text(self, _sel):
        return self.body

    def wait_for_timeout(self, _ms):
        pass

    def _buttons(self):
        if self.choice == "accept":
            return ["Accept"]
        return ["Decline", "Decline & Counter"]

    def locator(self, sel):
        radio = {S.TRADE_ACCEPT_RADIO: self.accept, S.TRADE_DECLINE_RADIO: self.decline}
        if sel in radio:
            return _Loc([radio[sel]])
        for label, r in radio.items():
            if sel == label + " input":
                return _Loc([r])
        exact = {S.TRADE_ACCEPT_BUTTON: "Accept", S.TRADE_REJECT_BUTTON: "Decline"}
        if sel in exact:
            return _Loc([_Button(self, t) for t in self._buttons() if t == exact[sel]])
        return _Loc([])


def test_review_path_is_the_live_page_with_our_team_id():
    p = A.trade_review_path(1526991210, 8, "ede95b6e")
    assert p.startswith("/football/tradereview?")
    assert "transactionId=ede95b6e" in p and "fromTeamId=8" in p
    assert "tradeoffers" not in p


def test_decline_moves_the_radio_then_clicks_decline_only():
    page = ReviewPage()
    A.respond_to_trade(page, NAMES, "decline")
    assert page.choice == "decline"
    assert page.clicked == ["Decline"]  # never Accept, never Decline & Counter


def test_decline_whose_radio_does_not_take_submits_nothing():
    """The failure that would accept the trade: the radio stays on Accept."""
    page = ReviewPage(radio_sticks=False)
    with pytest.raises(A.ActionFailed, match="did not take"):
        A.respond_to_trade(page, NAMES, "decline")
    assert page.clicked == []


def test_wrong_offer_on_the_page_clicks_nothing():
    page = ReviewPage(names=["Xavier Worthy", "Somebody Else"])
    with pytest.raises(A.ActionFailed, match="not this offer"):
        A.respond_to_trade(page, NAMES, "decline")
    assert page.clicked == [] and page.choice == "accept"


def test_accept_clicks_accept():
    page = ReviewPage()
    A.respond_to_trade(page, NAMES, "accept")
    assert page.clicked == ["Accept"]


def test_reject_selector_cannot_match_decline_and_counter():
    """§6.8.13: countering is never authorised. `has-text` matched both."""
    assert S.TRADE_REJECT_BUTTON == "button[data-trade-type='TRADE_DECLINE']"
    assert "has-text" not in S.TRADE_REJECT_BUTTON + S.TRADE_ACCEPT_BUTTON
