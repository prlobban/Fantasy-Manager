"""§6.1/§6.1a — a player promised away in an open offer is not ours to OFFER
to a second manager either.

2026-10-12 (week 5 daily sweep). Our 10-08 offer to team 9 was Mark Andrews +
Garrett Wilson for Chris Olave, still open and unsettleable until 10-22. The
trade board that morning then put Garrett Wilson on the give side of three MORE
ideas — to teams 7, 6 and Purdy Mouths — and the sweep spent its one escalation
slot asking Pearce for a read on the Purdy Mouths one (James Cook III for
Garrett Wilson + Jaylen Warren, +23.5 ROS starting points at a 0.98 market
ratio). That idea was never ours to send: if Purdy Mouths had accepted it,
Garrett Wilson leaves our roster, ESPN voids the Olave offer whose proposer no
longer owns a piece of it, §6.1a can then never see Olave arrive, and §6.1
keeps team 9 blocked for the rest of the fortnight for nothing.

2026-10-09 fixed exactly this hole in the DROP path (`choose_drop` and
`add_drop`, tests/test_promised_drop_guard.py). The trade path kept it:
`trades_out.build` and `value_check` had no §6.1a term, and
`rate_limits.can_propose` only knows about a second offer to the SAME manager.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from core.gates import rate_limits
from core.manager import trades_out
from core.model.schema import GateResult, Pos
from core.model.value import value_pool
from tests.test_lineup import pl, settings_10

#: The counterparty of the escalated idea, and the manager we already owe.
PURDY, GLOBO = 2, 9

#: Our roster ids, named for the bodies they stand in for on 2026-10-12.
JEFFERSON, GWILSON, GOLDEN = 6, 7, 8
SWIFT, WARREN = 3, 4
ANDREWS = 11
COOK = 23


def _ours():
    """Week 5's roster: three WRs, three RBs, three TEs for one slot."""
    return [
        pl(1, Pos.QB, 20, name="Josh Allen"),
        pl(SWIFT, Pos.RB, 14, name="D'Andre Swift"),
        pl(WARREN, Pos.RB, 11, name="Jaylen Warren"),
        pl(5, Pos.RB, 8, name="Chuba Hubbard"),
        pl(JEFFERSON, Pos.WR, 15, name="Justin Jefferson"),
        pl(GWILSON, Pos.WR, 12, name="Garrett Wilson"),
        pl(GOLDEN, Pos.WR, 9, name="Matthew Golden"),
        pl(9, Pos.TE, 10, name="Travis Kelce"),
        pl(10, Pos.TE, 8, name="T.J. Hockenson"),
        pl(ANDREWS, Pos.TE, 7, name="Mark Andrews"),
    ]


def _theirs():
    """Purdy Mouths: a running back we want, and a tight end they would start."""
    return [
        pl(21, Pos.QB, 19), pl(COOK, Pos.RB, 26, name="James Cook III"),
        pl(24, Pos.RB, 12), pl(25, Pos.RB, 12),
        pl(26, Pos.WR, 14), pl(27, Pos.WR, 11),
        pl(28, Pos.TE, 4, name="their bad TE"), pl(29, Pos.RB, 10),
    ]


def _vals(pool):
    return value_pool(pool, settings_10(), window="ros",
                      weeks_remaining=6, current_week=9)


def _open_offer(store, *, to_team=GLOBO, give=(ANDREWS, GWILSON), get=(99,),
                days=4.0) -> None:
    """The 10-08 Andrews + Garrett Wilson offer to team 9, `days` old: past
    §6.1's 1/day and 3/week counters, still inside the 14-day window."""
    store.append("trade_proposals", {
        "at": (datetime.now(UTC) - timedelta(days=days)).isoformat(),
        "to_team": to_team,
        "offer_hash": rate_limits.offer_hash(list(give), list(get), to_team),
        "give": list(give), "get": list(get)})


@pytest.fixture
def state(tmp_path, monkeypatch):
    from core.state import store

    monkeypatch.setattr(store, "_path", lambda: tmp_path / "state.json")
    return store


# ── the board (§6.1a, D4) ────────────────────────────────────────────────────


def test_the_board_does_not_offer_a_second_manager_a_player_already_promised(state):
    """🔴 2026-10-12: Garrett Wilson was half of the open 10-08 offer to team 9
    and still appeared on the give side of the ideas to three other managers."""
    ours, theirs = _ours(), _theirs()
    v = _vals(ours + theirs)
    others = {PURDY: ("Purdy Mouths", theirs)}

    # What core wants to send Purdy Mouths with nothing outstanding anywhere.
    base = trades_out.build(ours, others, v, settings_10(), week=5, promised={})
    assert base, "the fixture has to produce an idea, or there is nothing to suppress"
    spoken_for = {x.espn_id for x in base[0].give}

    # The same bodies, now committed to team 9 in an offer that is still open.
    _open_offer(state, give=tuple(spoken_for))
    after = trades_out.build(ours, others, v, settings_10(), week=5,
                             promised=trades_out.promised_ids())

    for p in after:
        assert not (spoken_for & {x.espn_id for x in p.give}), (
            f"{p.describe()} re-offers a body already promised to team {GLOBO} — "
            "if it is accepted ESPN voids that offer and §6.1 blocks that "
            "manager for the rest of the fortnight (§6.1a)"
        )


def test_the_board_still_offers_him_to_the_manager_he_is_promised_to(state):
    """Narrow by construction: §6.1's one-open-offer rule owns the second offer
    to the SAME manager, and it already reports it as a rate-limit warning."""
    ours, theirs = _ours(), _theirs()
    v = _vals(ours + theirs)
    others = {GLOBO: ("GLOBO GYM PURPLE COBRAS", theirs)}

    base = trades_out.build(ours, others, v, settings_10(), week=5, promised={})
    assert base
    spoken_for = {x.espn_id for x in base[0].give}
    _open_offer(state, give=tuple(spoken_for))

    after = trades_out.build(ours, others, v, settings_10(), week=5,
                             promised=trades_out.promised_ids())
    assert after and {x.espn_id for x in after[0].give} == spoken_for
    assert any("rate limit" in w for w in after[0].warnings)


# ── and the gate propose_trade re-runs in code ───────────────────────────────


def test_value_check_refuses_an_offer_that_re_promises_an_open_offers_player(state):
    """🔴 2026-10-12, the escalated idea itself: James Cook III to us for
    Garrett Wilson + Jaylen Warren, while Garrett Wilson was already half of
    the open 10-08 offer to team 9."""
    ours, theirs = _ours(), _theirs()
    v = _vals(ours + theirs)
    _open_offer(state)
    give = [p for p in ours if p.espn_id in (GWILSON, WARREN)]
    get = [p for p in theirs if p.espn_id == COOK]

    ok, why, ours_gain, _ = trades_out.value_check(
        ours, theirs, give, get, v, settings_10(), week=5, to_team=PURDY,
        promised=trades_out.promised_ids())

    assert ours_gain > 0, "the §6.2 gain is real — that is why this one got escalated"
    assert not ok, "a body promised to team 9 is not ours to send to team 2"
    assert "§6.1a" in why and "Garrett Wilson" in why and str(GLOBO) in why


def test_value_check_allows_the_same_offer_to_the_manager_he_is_promised_to(state):
    """The promise is per manager. Re-offering him to team 9 is §6.1's
    one-open-offer business, checked at the rate limit, not a value refusal."""
    ours, theirs = _ours(), _theirs()
    v = _vals(ours + theirs)
    _open_offer(state)
    give = [p for p in ours if p.espn_id in (GWILSON, WARREN)]
    get = [p for p in theirs if p.espn_id == COOK]

    ok, why, _, _ = trades_out.value_check(
        ours, theirs, give, get, v, settings_10(), week=5, to_team=GLOBO,
        promised=trades_out.promised_ids())
    assert ok, why


def test_value_check_allows_the_offer_once_the_promise_has_aged_out(state):
    """§6.1 blocks the manager for 14 days and no longer; past that the offer
    is dead on both sides and its pieces are ordinary trade capital again."""
    ours, theirs = _ours(), _theirs()
    v = _vals(ours + theirs)
    _open_offer(state, days=15)
    give = [p for p in ours if p.espn_id in (GWILSON, WARREN)]
    get = [p for p in theirs if p.espn_id == COOK]

    ok, why, _, _ = trades_out.value_check(
        ours, theirs, give, get, v, settings_10(), week=5, to_team=PURDY,
        promised=trades_out.promised_ids())
    assert ok, why


# ── the write ────────────────────────────────────────────────────────────────


class FakeFacts:
    def __init__(self):
        self.settings = settings_10()


class FakeTeam:
    def __init__(self, name, roster):
        self.name = name
        self.roster = roster


class FakeState:
    """The attributes `propose_trade` reads before it writes."""

    def __init__(self, ours, theirs):
        self.facts = FakeFacts()
        self.week = 5
        self.decision_week = 5
        self.my_team_id = 1
        self.me = FakeTeam("us", ours)
        self.teams = {1: self.me, PURDY: FakeTeam("Purdy Mouths", theirs)}

    def all_players(self):
        return [*self.me.roster, *self.teams[PURDY].roster]


def test_propose_trade_refuses_to_send_a_player_promised_in_an_open_offer(
        state, monkeypatch):
    """🔴 2026-10-12: had the sweep taken the consolidation it was escalating,
    the write would have gone through and killed the team 9 offer."""
    from core import mcp_server as M

    ours, theirs = _ours(), _theirs()
    v = _vals(ours + theirs)
    _open_offer(state)
    executed: list[object] = []

    monkeypatch.setattr(M, "_snap", lambda refresh=False: FakeState(ours, theirs))
    monkeypatch.setattr(M, "_vals", lambda s, **kw: v)
    monkeypatch.setattr(M.write_gate, "execute",
                        lambda *a, **kw: (executed.append(a),
                                          GateResult(allowed=True, reason="ok"), None)[1:])

    out = json.loads(M.propose_trade(
        PURDY, [GWILSON, WARREN], [COOK],
        reason="consolidation at RB", cites=["§6.2"],
        why_they_accept="they are thin at receiver and need two startable bodies now"))

    assert out["allowed"] is False
    assert "§6.1" in (out["refused_by"] or "")
    assert "Garrett Wilson" in out["reason"] and str(GLOBO) in out["reason"]
    assert executed == [], "an offer that kills an open offer never reaches the browser"
