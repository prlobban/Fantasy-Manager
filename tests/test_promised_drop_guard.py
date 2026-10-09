"""§6.1/§6.1a — a player promised away in an open offer is not ours to drop.

2026-10-09 (week 5 daily sweep). Our 10-05 offer to team 6 was Travis Kelce +
Jordan Addison for Isaiah Likely. Addison then went out as the drop behind a
later add, and that killed the offer: ESPN voids a proposal whose proposer no
longer owns a piece of it, and §6.1a closes an offer on exactly one signal —
every player we asked to GET arriving on our roster. Likely never can, so the
offer can never settle, and §6.1's one-open-offer-per-manager rule refused
every proposal to team 6 until 10-19. What it refused included a Kenneth
Walker III deal core graded at +103.7 ROS starting points on a 0.84 market
ratio that clears §6.3's floor — the deal that manager had asked for in chat.

Nothing in core knew a rostered player was already spoken for: `choose_drop`
ranked Addison as the cheapest bench body and handed him to the agent as the
drop behind every candidate, and `add_drop` wrote it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from core.gates import rate_limits
from core.manager import trades_out, waivers
from core.model.schema import (
    GateResult,
    LeagueSettings,
    Player,
    Pos,
    RosterSlot,
    Valuation,
)

KELCE, ADDISON, LIKELY, SPARE = 15847, 4036348, 3139477, 4241389
THEM = 6


def _settings() -> LeagueSettings:
    return LeagueSettings(
        league_id=1, season=2026, name="t", team_count=10, draft_type="SNAKE",
        starting_slots=[
            RosterSlot(name="QB", count=1, eligible=(Pos.QB,)),
            RosterSlot(name="RB", count=2, eligible=(Pos.RB,)),
            RosterSlot(name="WR", count=2, eligible=(Pos.WR,)),
            RosterSlot(name="TE", count=1, eligible=(Pos.TE,)),
        ],
        bench_count=4, ir_count=1, scoring={53: 0.5},
        waiver_type="WAIVERS_TRADITIONAL", faab_budget=None, trade_deadline=None,
        playoff_team_count=6, playoff_weeks=[15, 16, 17],
        regular_season_weeks=14, keeper_count=0,
    )


def pl(pid, pos, proj, *, name=None) -> Player:
    return Player(espn_id=pid, name=name or f"{pos.value}{pid}", pos=pos,
                  pro_team="XX", proj_season=proj * 14, proj_week={1: proj})


def _vv(pid, pts) -> Valuation:
    return Valuation(espn_id=pid, window="week", points=pts, vor=float(pts),
                     tier=1, availability=1.0, components={"base": pts})


def _our_roster() -> tuple[list[Player], dict[int, Valuation]]:
    """Week 5's shape: six starters and two spare receivers, the cheaper of
    whom is the one we have already offered to team 6."""
    roster = [
        pl(1, Pos.QB, 19.6), pl(3, Pos.RB, 18.0), pl(4, Pos.RB, 14.0),
        pl(6, Pos.WR, 17.0), pl(7, Pos.WR, 13.0), pl(9, Pos.TE, 11.0),
        pl(ADDISON, Pos.WR, 2.0, name="Jordan Addison"),
        pl(SPARE, Pos.WR, 3.0, name="Spare WR"),
    ]
    return roster, {p.espn_id: _vv(p.espn_id, p.proj_week[1]) for p in roster}


def _open_offer(store, *, days: float, give=(KELCE, ADDISON), get=(LIKELY,)) -> None:
    """The 10-05 offer, `days` old: past §6.1's 1/day and 3/week counters,
    still inside the 14-day window that blocks the manager."""
    store.append("trade_proposals", {
        "at": (datetime.now(UTC) - timedelta(days=days)).isoformat(),
        "to_team": THEM,
        "offer_hash": rate_limits.offer_hash(list(give), list(get), THEM),
        "give": list(give), "get": list(get)})


@pytest.fixture
def state(tmp_path, monkeypatch):
    from core.state import store

    monkeypatch.setattr(store, "_path", lambda: tmp_path / "state.json")
    return store


# ── what core offers as the drop ─────────────────────────────────────────────


def test_a_player_promised_in_an_open_offer_is_not_offered_as_the_drop(state):
    """🔴 2026-10-09: Jordan Addison was half of the open 10-05 offer to team 6
    and also the cheapest body on the bench, so core named him as the drop."""
    _open_offer(state, days=4)
    roster, vals = _our_roster()

    drop, _, reason, _ = waivers.choose_drop(
        roster, vals, _settings(), current_week=1,
        promised=trades_out.promised_ids(),
    )

    assert drop is not None and drop.name == "Spare WR", (
        "a player we have promised to another manager cannot be the cut, "
        "however cheap he is — dropping him kills the offer and mortgages "
        "that manager for the rest of the fortnight (§6.1a)"
    )
    assert "Jordan Addison" in reason, "say who the open offer took off the table"


def test_without_an_open_offer_the_cheapest_body_is_still_the_drop(state):
    """The guard is per promise, not a blanket freeze on the bench."""
    roster, vals = _our_roster()

    drop, _, _, _ = waivers.choose_drop(
        roster, vals, _settings(), current_week=1,
        promised=trades_out.promised_ids(),
    )

    assert drop is not None and drop.name == "Jordan Addison"


def test_a_promise_expires_with_the_block_it_created(state):
    """§6.1 blocks the manager for 14 days and no longer; past that the offer
    is dead on both sides and its pieces are ordinary bench bodies again."""
    _open_offer(state, days=15)
    assert trades_out.promised_ids() == {}


def test_a_settled_offer_promises_nobody(state):
    """§6.1a — once the GET side lands the offer is closed, and a player we
    offered is free to cut again."""
    _open_offer(state, days=4)
    assert rate_limits.settle_proposals({LIKELY, 1, 3}), "the offer was accepted"
    assert trades_out.promised_ids() == {}


def test_a_legacy_offer_with_no_ids_promises_nobody(state):
    """Entries written before 2026-09-16 carry no player ids; they are waited
    out and surfaced as unverifiable, not turned into a bench-wide freeze."""
    _open_offer(state, days=4, give=(), get=())
    state.append("trade_proposals", {"at": state.now_iso(), "to_team": THEM,
                                     "offer_hash": "deadbeef"})
    assert trades_out.promised_ids() == {}


# ── and what the gate does with it ───────────────────────────────────────────


class FakeFacts:
    def __init__(self):
        self.settings = _settings()


class FakeTeam:
    def __init__(self, roster: list[Player]):
        self.roster = roster
        self.slots = {p.espn_id: "BE" for p in roster}


class FakeState:
    """The attributes `add_drop` reads before it writes."""

    def __init__(self, roster: list[Player], pool: list[Player]):
        self.facts = FakeFacts()
        self.week = 5
        self.decision_week = 5
        self.my_team_id = 1
        self.me = FakeTeam(roster)
        self.on_waivers: set[int] = set()
        self.bench_open = 0
        self._pool = pool

    def all_players(self) -> list[Player]:
        return [*self.me.roster, *self._pool]


@pytest.fixture
def harness(monkeypatch):
    """Patch out the snapshot, the browser write and the §5.7 counter."""
    from core import mcp_server as M

    writes: list[object] = []
    counted: list[tuple[int, int | None, bool]] = []

    def fake_run_write(action, perform):
        writes.append(action)
        return GateResult(allowed=True, reason="ok"), "receipt", None

    monkeypatch.setattr(M, "_run_write", fake_run_write)
    monkeypatch.setattr(rate_limits, "record_add",
                        lambda a, d, *, pending=False: counted.append((a, d, pending)))
    monkeypatch.setattr(M, "_vals", lambda state, **kw: {})
    return M, writes, counted


def call(M, monkeypatch, roster, *, add_id, drop_id):
    monkeypatch.setattr(M, "_snap", lambda refresh=False: FakeState(
        roster=roster, pool=[pl(99, Pos.TE, 9.0, name="T.J. Hockenson")]))
    return json.loads(M.add_drop(add_id, drop_id, "because", ["§5.3.2"]))


def test_add_drop_refuses_a_drop_promised_in_an_open_offer(state, harness, monkeypatch):
    """🔴 2026-10-09: the add that cost us team 6. Dropping Addison could not
    be undone, and it turned a live offer into one that can never settle."""
    M, writes, counted = harness
    _open_offer(state, days=4)
    roster, _ = _our_roster()

    out = call(M, monkeypatch, roster, add_id=99, drop_id=ADDISON)

    assert out["allowed"] is False
    assert out["refused_by"] == "§6.1"
    assert "Jordan Addison" in out["reason"] and "6" in out["reason"]
    assert writes == [], "a drop that kills an open offer never reaches the browser"
    assert counted == [], "and §5.7 is not charged for it"


def test_add_drop_still_allows_a_drop_nobody_was_promised(state, harness, monkeypatch):
    """Narrow by construction: the same open offer, a different drop."""
    M, writes, counted = harness
    _open_offer(state, days=4)
    roster, _ = _our_roster()

    out = call(M, monkeypatch, roster, add_id=99, drop_id=SPARE)

    assert out["allowed"] is True
    assert len(writes) == 1
    assert counted == [(99, SPARE, False)]
