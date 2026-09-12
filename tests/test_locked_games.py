"""§4.8 — a game that has kicked off is settled, and the system must know it.

Until 2026-09-12 nothing in the codebase knew a game had a clock. Every
projection read as live and every player as movable, which produced three
separate wrong answers on the same day:

  - a kicker whose game was final (Rams 7, 49ers 27, Thursday) was still
    carried at his pre-game 8.0, and a waiver add was built on the "gain";
  - with that fixed, the same phantom reappeared as a +4.25/wk claim on a
    quarterback whose game had also already finished;
  - and the lineup optimiser would happily plan a move ESPN refuses, because
    a locked player's row reads LOCKED instead of MOVE.

The rule: once his game starts, a player's points are his ACTUAL points, he
holds whatever slot he is in, and no add can improve that slot this week.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from core.manager import lineup, waivers
from core.model.schema import (
    LeagueSettings,
    Player,
    Pos,
    ProGame,
    RosterSlot,
    Valuation,
)
from core.model.value import value_pool

NOW = datetime(2026, 9, 12, 19, 0, tzinfo=UTC)


def settings_k() -> LeagueSettings:
    return LeagueSettings(
        league_id=1, season=2026, name="t", team_count=10, draft_type="SNAKE",
        starting_slots=[
            RosterSlot(name="QB", count=1, eligible=(Pos.QB,)),
            RosterSlot(name="RB", count=2, eligible=(Pos.RB,)),
            RosterSlot(name="WR", count=2, eligible=(Pos.WR,)),
            RosterSlot(name="TE", count=1, eligible=(Pos.TE,)),
            RosterSlot(name="K", count=1, eligible=(Pos.K,)),
        ],
        bench_count=4, ir_count=1, scoring={53: 0.5},
        waiver_type="WAIVERS_TRADITIONAL", faab_budget=None, trade_deadline=None,
        playoff_team_count=6, playoff_weeks=[15, 16, 17],
        regular_season_weeks=14, keeper_count=0,
    )


def pl(pid, pos, proj, *, name=None, actual=None, game=None) -> Player:
    p = Player(
        espn_id=pid, name=name or f"{pos.value}{pid}", pos=pos, pro_team="XX",
        proj_season=proj * 14, proj_week={1: proj},
        actual_week=({1: actual} if actual is not None else {}),
    )
    if game is not None:
        p.games = {1: game}
    return p


def played(official=True):
    return ProGame(week=1, kickoff=NOW - timedelta(hours=20), stats_official=official)


def upcoming():
    return ProGame(week=1, kickoff=NOW + timedelta(hours=18), stats_official=False)


# ── the signal itself ────────────────────────────────────────────────────────


def test_a_final_game_is_locked():
    assert pl(1, Pos.K, 9.0, game=played()).game_locked(1, NOW) is True


def test_a_kicked_off_game_is_locked_even_before_stats_are_official():
    p = pl(1, Pos.K, 9.0, game=ProGame(
        week=1, kickoff=NOW - timedelta(minutes=5), stats_official=False))
    assert p.game_locked(1, NOW) is True


def test_an_upcoming_game_is_not_locked():
    assert pl(1, Pos.K, 9.0, game=upcoming()).game_locked(1, NOW) is False


def test_an_unknown_schedule_fails_open():
    """No schedule data must not freeze the lineup — it degrades to the old
    behaviour rather than refusing to act."""
    assert pl(1, Pos.K, 9.0).game_locked(1, NOW) is False


# ── valuation ────────────────────────────────────────────────────────────────


def test_a_locked_player_is_worth_what_he_actually_scored():
    """🔴 Mevis: carried at 9.4 projected while his game was already final and
    he had scored 1.0."""
    s = settings_k()
    mevis = pl(10, Pos.K, 9.4, name="Mevis", actual=1.0, game=played())
    roster = [pl(1, Pos.QB, 20), pl(3, Pos.RB, 18), pl(4, Pos.RB, 14),
              pl(6, Pos.WR, 17), pl(7, Pos.WR, 13), pl(9, Pos.TE, 11), mevis]
    v = value_pool(roster, s, window="week", week=1, current_week=1)

    assert v[mevis.espn_id].locked is True
    assert v[mevis.espn_id].points == 1.0


def test_an_unplayed_player_still_uses_his_projection():
    s = settings_k()
    k = pl(10, Pos.K, 9.4, name="K", actual=None, game=upcoming())
    roster = [pl(1, Pos.QB, 20), pl(3, Pos.RB, 18), pl(4, Pos.RB, 14),
              pl(6, Pos.WR, 17), pl(7, Pos.WR, 13), pl(9, Pos.TE, 11), k]
    v = value_pool(roster, s, window="week", week=1, current_week=1)

    assert v[k.espn_id].locked is False
    assert v[k.espn_id].points == 9.4


# ── the lineup ───────────────────────────────────────────────────────────────


def test_a_locked_starter_keeps_his_slot_even_when_someone_better_is_free():
    """ESPN will not move him — his row reads LOCKED, not MOVE. Planning
    around him produces a write that fails at the browser."""
    s = settings_k()
    mevis = pl(10, Pos.K, 9.4, name="Mevis", actual=1.0, game=played())
    better = pl(11, Pos.K, 12.0, name="Boswell", game=upcoming())
    roster = [pl(1, Pos.QB, 20), pl(3, Pos.RB, 18), pl(4, Pos.RB, 14),
              pl(6, Pos.WR, 17), pl(7, Pos.WR, 13), pl(9, Pos.TE, 11),
              mevis, better]
    v = value_pool(roster, s, window="week", week=1, current_week=1)

    plan = lineup.build(roster, v, s, week=1, current_starters={mevis.espn_id: "K"})

    k = next(a for a in plan.assignments if a.slot == "K")
    assert k.player.name == "Mevis", "a locked player cannot be benched"


def test_a_locked_bench_player_is_never_promoted():
    s = settings_k()
    benched = pl(11, Pos.K, 30.0, name="Locked", actual=30.0, game=played())
    starter = pl(10, Pos.K, 9.4, name="Playing", game=upcoming())
    roster = [pl(1, Pos.QB, 20), pl(3, Pos.RB, 18), pl(4, Pos.RB, 14),
              pl(6, Pos.WR, 17), pl(7, Pos.WR, 13), pl(9, Pos.TE, 11),
              starter, benched]
    v = value_pool(roster, s, window="week", week=1, current_week=1)

    plan = lineup.build(roster, v, s, week=1, current_starters={starter.espn_id: "K"})

    k = next(a for a in plan.assignments if a.slot == "K")
    assert k.player.name == "Playing", (
        "a player whose game has finished cannot be moved into the lineup, "
        "however many points he scored"
    )


# ── waivers ──────────────────────────────────────────────────────────────────


def _val(pid, pts, *, locked=False) -> Valuation:
    return Valuation(espn_id=pid, window="week", points=pts, vor=0.0, tier=1,
                     availability=1.0, locked=locked, components={"base": pts})


def test_adding_a_player_whose_game_is_over_gains_nothing_this_week():
    """🔴 Brock Purdy, +4.25/wk on a game that finished 27-7 the night before."""
    s = settings_k()
    cand = pl(99, Pos.QB, 24.0, name="Purdy", actual=24.0, game=played())
    roster = [pl(1, Pos.QB, 19.6), pl(3, Pos.RB, 18), pl(4, Pos.RB, 14),
              pl(6, Pos.WR, 17), pl(7, Pos.WR, 13), pl(9, Pos.TE, 11)]
    vals = {p.espn_id: _val(p.espn_id, p.proj_week[1]) for p in roster}

    gain, replaced = waivers.weekly_gain_for(
        cand, _val(cand.espn_id, 24.0, locked=True), roster, vals, s
    )

    assert gain == 0.0
    assert replaced is None


def test_an_add_cannot_gain_at_a_slot_that_is_already_locked():
    """🔴 The kicker phantom: +8.2/wk into a K slot that was already settled."""
    s = settings_k()
    mevis = pl(10, Pos.K, 9.4, name="Mevis", actual=1.0, game=played())
    roster = [pl(1, Pos.QB, 19.6), pl(3, Pos.RB, 18), pl(4, Pos.RB, 14),
              pl(6, Pos.WR, 17), pl(7, Pos.WR, 13), pl(9, Pos.TE, 11), mevis]
    vals = {p.espn_id: _val(p.espn_id, p.proj_week[1]) for p in roster}
    vals[mevis.espn_id] = _val(mevis.espn_id, 1.0, locked=True)

    cand = pl(99, Pos.K, 9.2, name="Boswell", game=upcoming())

    gain, _ = waivers.weekly_gain_for(
        cand, _val(cand.espn_id, 9.2), roster, vals, s,
        {mevis.espn_id: "K"},
    )

    assert gain == 0.0, (
        "the K slot is held by a player ESPN has locked; no add can score there "
        "this week, however big the projection gap looks"
    )
