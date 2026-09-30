"""§5.4 / §5.7 — add_drop must refuse the claims it already knows are wasted.

2026-09-29 (Tuesday) the sweep placed three waiver claims that could not do
anything, and every one of them still spent an add:

  1. A SECOND claim for Matthew Golden, who the 09-27 claim had already landed
     on our roster. Nothing checked whether the player being added was already
     ours, so the claim went to the browser, came back with a receipt, and was
     recorded against §5.7.
  2. A Kyler Murray claim whose drop was Colston Loveland — already gone, and
     with him no longer in the 200-deep free-agent read he was not in the
     snapshot pool at all. `drop_p` came back None, which made the whole §5.4 /
     §5.5 block skip rather than refuse, and the write went out naming a player
     we do not own as the drop.

None of the three executed. All three were counted, which is why 2026-09-30
opened at 2 of 7 adds instead of 5.
"""

from __future__ import annotations

import json

import pytest

from core.gates import rate_limits
from core.model.schema import GateResult, Player, Pos
from tests.conftest import make_settings

# ── just enough league to drive the tool ─────────────────────────────────────


class FakeFacts:
    def __init__(self):
        self.settings = make_settings()


class FakeTeam:
    def __init__(self, roster: list[Player]):
        self.roster = roster
        self.slots = {p.espn_id: "BE" for p in roster}


class FakeState:
    """The attributes `add_drop` reads before it writes."""

    def __init__(self, roster: list[Player], pool: list[Player]):
        self.facts = FakeFacts()
        self.week = 4
        self.decision_week = 4
        self.my_team_id = 1
        self.me = FakeTeam(roster)
        self.on_waivers: set[int] = set()
        self.bench_open = 1
        self._pool = pool

    def all_players(self) -> list[Player]:
        return [*self.me.roster, *self._pool]


def wr(pid: int, name: str) -> Player:
    return Player(espn_id=pid, name=name, pos=Pos.WR, pro_team="GB")


@pytest.fixture
def harness(monkeypatch):
    """Patch out the snapshot, the browser write and the add counter."""
    from core import mcp_server as M

    writes: list[object] = []
    counted: list[tuple[int, int | None, bool]] = []

    def fake_run_write(action, perform):
        writes.append(action)
        return GateResult(allowed=True, reason="ok"), "receipt", None

    def fake_record_add(add_id, drop_id, *, pending=False):
        counted.append((add_id, drop_id, pending))

    monkeypatch.setattr(M, "_run_write", fake_run_write)
    monkeypatch.setattr(rate_limits, "record_add", fake_record_add)
    # §5.5's top-N check needs ROS valuations; nobody here is protected.
    monkeypatch.setattr(M, "_vals", lambda state, **kw: {})
    return M, writes, counted


def call(M, state, monkeypatch, *, add_id, drop_id):
    monkeypatch.setattr(M, "_snap", lambda refresh=False: state)
    return json.loads(M.add_drop(add_id, drop_id, "because", ["§5.7"]))


# ── 1. the duplicate Golden claim ────────────────────────────────────────────


def test_an_add_for_a_player_already_on_our_roster_is_refused(harness, monkeypatch):
    """🔴 2026-09-29: a second claim for Matthew Golden, already ours."""
    M, writes, counted = harness
    golden = wr(4001, "Matthew Golden")
    state = FakeState(roster=[golden, wr(4002, "Ladd McConkey")], pool=[])

    out = call(M, state, monkeypatch, add_id=4001, drop_id=4002)

    assert out["allowed"] is False
    assert out["refused_by"] == "§5.4"
    assert "Matthew Golden" in out["reason"]
    assert writes == [], "no browser write for a player we already have"
    assert counted == [], "§5.7 must not be charged for a duplicate claim"


def test_an_add_for_a_player_we_do_not_have_still_goes_through(harness, monkeypatch):
    """The guard is about OUR roster only — a free agent still adds."""
    M, writes, counted = harness
    state = FakeState(roster=[wr(4002, "Ladd McConkey")], pool=[wr(4001, "Golden")])

    out = call(M, state, monkeypatch, add_id=4001, drop_id=4002)

    assert out["allowed"] is True
    assert len(writes) == 1
    assert counted == [(4001, 4002, False)]


# ── 2. the drop that was already gone ────────────────────────────────────────


def test_a_drop_missing_from_the_pool_is_refused_not_skipped(harness, monkeypatch):
    """🔴 2026-09-29: dropping Loveland, who was already off our roster and out
    of the 200-deep free-agent read, so `by_id` had no row for him at all."""
    M, writes, counted = harness
    state = FakeState(roster=[wr(4002, "Ladd McConkey")], pool=[wr(4001, "Golden")])

    out = call(M, state, monkeypatch, add_id=4001, drop_id=9999)

    assert out["allowed"] is False
    assert out["refused_by"] == "§5.4"
    assert "9999" in out["reason"]
    assert writes == [], "never name a drop we cannot see on our own roster"
    assert counted == []


def test_a_drop_we_can_see_but_do_not_own_is_still_refused(harness, monkeypatch):
    """The pre-existing §5.4 refusal, pinned so the new branch cannot swallow it."""
    M, writes, counted = harness
    state = FakeState(roster=[wr(4002, "Ladd McConkey")],
                      pool=[wr(4001, "Golden"), wr(4003, "Someone Else's WR")])

    out = call(M, state, monkeypatch, add_id=4001, drop_id=4003)

    assert out["allowed"] is False
    assert out["refused_by"] == "§5.4"
    assert "Someone Else's WR" in out["reason"]
    assert writes == [] and counted == []
