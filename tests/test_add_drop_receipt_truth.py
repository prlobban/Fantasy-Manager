"""2026-10-28 — a swap that committed came back saying it hadn't.

Week 8's Wednesday sweep made three add/drops. The third, Brenton Strange in
for C.J. Stroud, committed: its receipt screenshot
(.repair/20261028T123448-add-drop.png) is ESPN's team page with the green bar
reading "Move saved - C.J. Stroud dropped to Waivers, Brenton Strange added".
The sweep was told otherwise twice over:

  1. `add_drop`'s modal path never read that bar. It returned
     `verified=False` unconditionally — the only one of the three roster-write
     paths that proves nothing, while the bench-had-room path beside it and
     `drop_player` both prove themselves off exactly this banner.
  2. The tool left the run's cached snapshot alone, so the roster read the
     agent made straight afterwards was the one taken BEFORE the write and
     still showed Stroud and no Strange.

So the run escalated "the write did not execute" about a transaction ESPN had
already made, and handed off a Thursday retry that would have spent a second of
the week's seven adds re-doing it, with six already gone.
"""

from __future__ import annotations

import json

import pytest

from core.browser import actions as A
from core.browser import selectors as S
from tests.test_add_drop_continue import FakeEl, FakePage, FakeSession, _row
from tests.test_add_drop_guards import FakeState, wr

# ── the page ESPN actually showed ────────────────────────────────────────────

BANNER = "Move saved - C.J. Stroud dropped to Waivers, Brenton Strange added"

STRANGE_ID, STROUD_ID = 4430539, 4432577


class CommittingPage(FakePage):
    """The claim page, and then the team page ESPN redirects to.

    Before Continue is clicked the body is the modal's "you must drop a player"
    text; after it, the green `Move saved` bar — which is what the 12:34:48
    screenshot caught.
    """

    def __init__(self, *, banner: str | None, commit: FakeEl, **kw):
        super().__init__(**kw)
        self._banner, self._commit = banner, commit

    def inner_text(self, sel):
        if self._commit.clicks and self._banner is not None:
            return self._banner
        return super().inner_text(sel)


def _strange_page(*, banner: str | None = BANNER):
    add = FakeEl(text="Add", cls="add-action-btn")
    drop = FakeEl(text="DROP", cls="drop-action-btn")
    cont = FakeEl(text="Continue", cls="Button")
    search = FakeEl(text="", cls="player-search")

    fa_row = FakeEl(text="Brenton Strange JAX TE",
                    children={"button.add-action-btn": [add]})
    roster = [
        _row("QB Josh Allen BUF QB", 3918298),
        _row("QB C.J. Stroud HOU QB", STROUD_ID, drop=drop),
        _row("TE Travis Kelce KC TE", 15847,
             drop=FakeEl(text="DROP", cls="drop-action-btn")),
    ]
    page = CommittingPage(
        banner=banner, commit=cont,
        body="You must drop a player to clear your roster for the incoming players.",
        matches={
            "input[placeholder='Player Name']": [search],
            S.ANY_ROW: [fa_row, *roster],
            S.PLAYER_TABLE_ROW: roster,
            "button:has-text('Continue')": [cont],
            S.CONFIRM_BUTTON: [cont],
        },
    )
    return page, {"add": add, "drop": drop, "continue": cont}


def _strange_for_stroud(page):
    return A.add_drop(FakeSession(page), 12345, 2026,
                      STRANGE_ID, "Brenton Strange", STROUD_ID, "C.J. Stroud",
                      team_id=7)


# ── 1. the receipt must carry ESPN's own proof ───────────────────────────────


def test_an_add_drop_espn_confirms_comes_back_verified():
    """🔴 2026-10-28: ESPN painted "Move saved - C.J. Stroud dropped to
    Waivers, Brenton Strange added" and the receipt still said UNVERIFIED."""
    page, part = _strange_page()

    receipt = _strange_for_stroud(page)

    assert part["continue"].clicks >= 1
    assert receipt.verified is True, "the Move saved bar is the proof it committed"
    assert "UNVERIFIED" not in str(receipt)


def test_an_add_drop_espn_does_not_confirm_stays_unverified():
    """Fail-closed (§10.6) is unchanged: no banner, no claim that it landed."""
    page, _ = _strange_page(banner=None)

    receipt = _strange_for_stroud(page)

    assert receipt.verified is False
    assert "UNVERIFIED" in str(receipt)


def test_the_banner_must_name_the_player_we_added():
    """A stale bar from the previous swap cannot vouch for this one."""
    page, _ = _strange_page(banner="Move saved - Titans D/ST dropped, Jaguars D/ST added")

    receipt = _strange_for_stroud(page)

    assert receipt.verified is False


# ── 2. the roster read that follows the write ────────────────────────────────


class ReadableState(FakeState):
    """FakeState plus the few team fields `get_roster` prints."""

    def __init__(self, roster, pool):
        super().__init__(roster, pool)
        self.me.name = "big P"
        self.me.wins, self.me.losses = 7, 0
        self.me.points_for = 835.9
        self.me.waiver_priority = 5


@pytest.fixture
def server(monkeypatch):
    from core import mcp_server as M
    from core.gates import rate_limits
    from core.model.schema import GateResult

    monkeypatch.setattr(M, "_run_write",
                        lambda action, perform: (GateResult(allowed=True, reason="ok"),
                                                 "add drop: add Brenton Strange [verified]",
                                                 None))
    monkeypatch.setattr(rate_limits, "record_add", lambda *a, **kw: None)
    monkeypatch.setattr(rate_limits, "reconcile", lambda *a, **kw: [])
    monkeypatch.setattr(M, "_vals", lambda state, **kw: {})
    monkeypatch.setattr(M, "_state", None)
    return M


def test_a_roster_read_after_an_add_does_not_serve_the_pre_write_snapshot(
        server, monkeypatch):
    """🔴 2026-10-28: the Strange write landed, and `get_roster` straight after
    it still returned the snapshot taken before the write — Stroud on the
    roster, no Strange — which is what the sweep escalated as a failed write."""
    M = server
    kelce = wr(15847, "Travis Kelce")
    stroud, strange = wr(STROUD_ID, "C.J. Stroud"), wr(STRANGE_ID, "Brenton Strange")
    before = ReadableState(roster=[kelce, stroud], pool=[strange])
    after = ReadableState(roster=[kelce, strange], pool=[stroud])
    reads = iter([before, after, after, after])
    monkeypatch.setattr(M.ls_mod, "snapshot", lambda: next(reads))

    out = json.loads(M.add_drop(STRANGE_ID, STROUD_ID, "because", ["§5.7"]))
    assert out["allowed"] is True

    names = [p["name"] for p in json.loads(M.get_roster())["players"]]
    assert "Brenton Strange" in names, "the read must see the write that just happened"
    assert "C.J. Stroud" not in names
