"""§4 — start / sit."""

from __future__ import annotations

from core.manager import lineup
from core.model.schema import InjuryStatus, LeagueSettings, Player, Pos, RosterSlot
from core.model.value import value_pool


def settings_10() -> LeagueSettings:
    return LeagueSettings(
        league_id=1, season=2026, name="t", team_count=10, draft_type="SNAKE",
        starting_slots=[
            RosterSlot(name="QB", count=1, eligible=(Pos.QB,)),
            RosterSlot(name="RB", count=2, eligible=(Pos.RB,)),
            RosterSlot(name="WR", count=2, eligible=(Pos.WR,)),
            RosterSlot(name="TE", count=1, eligible=(Pos.TE,)),
            RosterSlot(name="RB/WR/TE", count=1, eligible=(Pos.RB, Pos.WR, Pos.TE)),
        ],
        bench_count=4, ir_count=1, scoring={53: 0.5},
        waiver_type="WAIVERS_TRADITIONAL", faab_budget=None, trade_deadline=None,
        playoff_team_count=6, playoff_weeks=[15, 16, 17],
        regular_season_weeks=14, keeper_count=0,
    )


def pl(pid, pos, wk_pts, *, name=None, weeks=8, spread=0.0, **kw) -> Player:
    """A player with `weeks` of history at wk_pts +/- spread (alternating)."""
    actual = {}
    proj = {}
    for w in range(1, weeks + 1):
        actual[w] = max(0.1, wk_pts + (spread if w % 2 else -spread))
        proj[w] = wk_pts
    proj[weeks + 1] = wk_pts
    return Player(
        espn_id=pid, name=name or f"{pos.value}{pid}", pos=pos, pro_team="XX",
        proj_season=wk_pts * 14, proj_week=proj, actual_week=actual, **kw,
    )


def build(roster, *, week=9, opponent=None, current=None):
    s = settings_10()
    vals = value_pool(roster, s, window="week", week=week)
    return lineup.build(roster, vals, s, opponent_projected=opponent,
                        current_starters=current, week=week), vals


def base_roster():
    return [
        pl(1, Pos.QB, 20), pl(2, Pos.QB, 12),
        pl(3, Pos.RB, 18), pl(4, Pos.RB, 14), pl(5, Pos.RB, 9),
        pl(6, Pos.WR, 17), pl(7, Pos.WR, 13), pl(8, Pos.WR, 8),
        pl(9, Pos.TE, 11), pl(10, Pos.TE, 5),
    ]


def test_fills_every_slot_with_the_best_eligible_player():
    plan, _ = build(base_roster())
    by_slot = {a.slot: (a.player.name if a.player else None) for a in plan.assignments}
    assert by_slot["QB"] == "QB1"
    assert by_slot["TE"] == "TE9"
    assert not [a for a in plan.assignments if a.player is None]


def test_flex_takes_the_best_leftover_skill_player():
    plan, _ = build(base_roster())
    flex = next(a for a in plan.assignments if a.slot == "RB/WR/TE")
    # RB5 (9), WR8 (8) and TE10 (5) are the leftovers; RB5 is best.
    assert flex.player.name == "RB5"


def test_out_players_never_start():
    """§4.3."""
    roster = base_roster()
    roster[3] = pl(4, Pos.RB, 14, injury_status=InjuryStatus.OUT)
    plan, _ = build(roster)
    starters = {a.player.espn_id for a in plan.assignments if a.player}
    assert 4 not in starters


def test_doubtful_players_never_start():
    roster = base_roster()
    roster[5] = pl(6, Pos.WR, 17, injury_status=InjuryStatus.DOUBTFUL)
    plan, _ = build(roster)
    starters = {a.player.espn_id for a in plan.assignments if a.player}
    assert 6 not in starters


def test_an_unfillable_slot_is_reported_not_hidden():
    roster = [pl(1, Pos.QB, 20), pl(3, Pos.RB, 18)]
    plan, _ = build(roster)
    empty = [a.slot for a in plan.assignments if a.player is None]
    assert empty
    assert any("could not be filled" in n for n in plan.notes)


# ── §4.2 — variance chosen by the matchup ────────────────────────────────────


def variance_roster():
    """A steady starter and a volatile bench player at the same position, close
    enough in projection that §4.2's budget can swap them.

    RB5 is bumped so the flex takes him — otherwise the volatile receiver slides
    into the flex on merit and there is no swap to test.
    """
    r = base_roster()
    r[4] = pl(5, Pos.RB, 12.5)              # takes the flex
    r[6] = pl(7, Pos.WR, 13, spread=1.0)    # steady starter
    r[7] = pl(8, Pos.WR, 12, spread=9.0)    # volatile, on the bench
    return r


def test_heavy_favourite_plays_the_floor():
    plan, _ = build(variance_roster(), opponent=80.0)
    assert plan.variance_mode == "floor"


def test_heavy_underdog_plays_the_ceiling_and_takes_the_volatile_player():
    plan, _ = build(variance_roster(), opponent=200.0)
    assert plan.variance_mode == "ceiling"
    starters = {a.player.espn_id for a in plan.assignments if a.player}
    assert 8 in starters, "should buy the tail when losing by 30 costs the same as 5"
    assert any("§4.2 ceiling" in why for _, _, _, why in plan.changes)


def test_close_matchup_just_maximises_points():
    plan, _ = build(variance_roster(), opponent=None)
    assert plan.variance_mode in ("neutral", "expected points")
    starters = {a.player.espn_id for a in plan.assignments if a.player}
    assert 7 in starters


def test_variance_swap_respects_its_points_budget():
    """§4.2 buys variance, but not at any price."""
    r = base_roster()
    r[6] = pl(7, Pos.WR, 13, spread=1.0)
    r[7] = pl(8, Pos.WR, 2, spread=9.0)   # far too weak, 11 pts worse
    plan, _ = build(r, opponent=200.0)
    starters = {a.player.espn_id for a in plan.assignments if a.player}
    assert 8 not in starters, "gave up more points than the ceiling budget allows"


def test_favourite_does_not_bench_points_wholesale():
    """Playoff seeding here is TOTAL_POINTS_SCORED, so 'play the floor' must
    never become 'leave points on the bench'."""
    plan, _ = build(variance_roster(), opponent=80.0)
    best, _ = build(variance_roster(), opponent=None)
    budget = 1.5  # priors: lineup.floor_max_points_sacrificed
    assert plan.projected_points >= best.projected_points - budget - 0.01


# ── diffing against what's currently set ─────────────────────────────────────


def test_diff_reports_only_real_changes():
    roster = base_roster()
    plan, _ = build(roster)
    current = {a.player.espn_id: a.slot for a in plan.assignments if a.player}
    plan2, _ = build(roster, current=current)
    assert plan2.changes == [], "an already-optimal lineup should need no moves"


def test_diff_spots_a_benched_starter():
    roster = base_roster()
    plan, _ = build(roster)
    current = {a.player.espn_id: a.slot for a in plan.assignments if a.player}
    qb = next(a for a in plan.assignments if a.slot == "QB")
    del current[qb.player.espn_id]   # pretend our QB is on the bench
    plan2, _ = build(roster, current=current)
    assert any(pl_.espn_id == qb.player.espn_id for pl_, _, _, _ in plan2.changes)
