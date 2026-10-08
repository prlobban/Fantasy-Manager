"""§2 — rest-of-season value for a player whose role changed in-season.

ESPN's season row is refreshed in-season on a rest-of-season scale, but it
lags a role change by weeks. Found 2026-10-08: Emanuel Wilson, promoted to
lead back, carried a 24.9 season row against weekly projections of 11.1 and
12.7, so his ROS VOR read -109.2 and §4.7 benched him. `value.ros_base`
rebuilds ROS from the weekly projections when the season row cannot cover the
fantasy weeks left at his lowest recent weekly projection.
"""

from __future__ import annotations

from datetime import UTC, datetime

from core.manager import lineup
from core.model.priors import priors
from core.model.schema import InjuryStatus, LeagueSettings, Player, Pos, ProGame, RosterSlot
from core.model.value import ros_base, value_pool

WEEK = 5
WEEKS_LEFT = 10  # 14-week regular season, valued in week 5
_FAR = datetime(2100, 1, 1, tzinfo=UTC)  # nothing reads as kicked off


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


def schedule(bye: int) -> dict[int, ProGame]:
    """An 18-week NFL schedule with one bye."""
    return {w: ProGame(week=w, kickoff=_FAR) for w in range(1, 19) if w != bye}


def games_left(bye: int) -> int:
    return sum(1 for w in schedule(bye) if w >= WEEK)


def player(pid: int, pos: Pos, season: float, weekly: dict[int, float], *,
           name: str | None = None, bye: int = 11, **kw) -> Player:
    return Player(
        espn_id=pid, name=name or f"{pos.value}{pid}", pos=pos, pro_team="XX",
        bye_week=bye, games=schedule(bye), proj_season=season,
        proj_week=dict(weekly), **kw,
    )


def established(pid: int, pos: Pos, rate: float, *, scale: float = 1.0, **kw) -> Player:
    """A player whose season row agrees with his weekly projections: `rate`
    per week over the games left on his schedule, times `scale` (the live pool
    ran 0.86-1.10 p10-p90 in week 5 of 2026)."""
    bye = kw.pop("bye", 11)
    return player(pid, pos, rate * games_left(bye) * scale,
                  {WEEK - 1: rate, WEEK: rate}, bye=bye, **kw)


def rb_room() -> list[Player]:
    """Thirty established backs, 20.0 down to 5.5 a week. The RB replacement
    rank in a 10-team half-PPR league is 25, so replacement is the 8.0 back."""
    return [established(100 + i, Pos.RB, 20.0 - 0.5 * i) for i in range(30)]


def ros(pool: list[Player]):
    return value_pool(pool, settings_10(), window="ros",
                      weeks_remaining=WEEKS_LEFT, current_week=WEEK)


# ── (a) a role the season row has not caught up to ─────────────────────────


def test_a_promoted_backup_is_valued_off_his_role_not_his_stale_season_row():
    """🔴 2026-10-08: Emanuel Wilson's ROS VOR read -109.2 — a 24.9 season
    row for a third-stringer, against weekly projections of 11.1 and 12.7 as
    the lead back. A projection that lags his role must never by itself price
    a startable back a hundred points under replacement."""
    wilson = player(1, Pos.RB, 24.87, {4: 11.08, 5: 12.68}, name="Emanuel Wilson")
    vals = ros(rb_room() + [wilson])

    v = vals[1]
    # 11.08 a week beats the 8.0 replacement back, so he is above replacement.
    assert v.vor > 0, f"ROS VOR {v.vor} for a back projecting 11+ a week"
    assert v.components["role.weekly_rate"] == 11.08, "the LOWER of the two weeks"
    assert v.components["espn_season_projection"] == 24.87
    assert v.components["base_projection"] == round(11.08 * games_left(11), 3)


def test_no_season_projection_at_all_does_not_make_a_starter_worthless():
    """The same, with ESPN's season row missing entirely (0.0)."""
    nobody = player(1, Pos.RB, 0.0, {4: 11.08, 5: 12.68})
    v = ros(rb_room() + [nobody])[1]
    assert v.vor > 0
    assert "no ESPN season projection" not in v.missing


def test_the_rebuild_works_at_every_position():
    """General, not a running-back patch: a backup quarterback who has
    started two weeks is rebuilt the same way."""
    qb = player(1, Pos.QB, 12.4, {4: 14.8, 5: 14.9})
    base, comps = ros_base(qb, current_week=WEEK, weeks_remaining=WEEKS_LEFT)
    assert base == 14.8 * games_left(11)
    assert comps["role.weekly_rate"] == 14.8


def test_without_a_schedule_the_rebuild_counts_only_the_fantasy_weeks_left():
    """No schedule loaded: the conservative horizon, never a longer one."""
    p = player(1, Pos.RB, 20.0, {4: 11.0, 5: 12.0})
    p.games = {}
    base, _ = ros_base(p, current_week=WEEK, weeks_remaining=WEEKS_LEFT)
    assert base == 11.0 * WEEKS_LEFT


# ── (b) everyone else is untouched ─────────────────────────────────────────


def test_an_established_players_ros_value_is_unchanged():
    """The fix must not move a player whose season row explains his weekly
    projections — including one at the low edge of the live pool (0.86x),
    one on a hot matchup week, and one whose last week was a 0.0 (OUT). The
    test: identical valuations with every weekly projection stripped, i.e.
    with no in-season evidence for the rebuild to read."""
    pool = rb_room() + [
        established(1, Pos.RB, 13.0, scale=0.86),
        player(2, Pos.RB, 13.0 * games_left(11), {4: 13.0, 5: 19.0}),  # matchup spike
        player(3, Pos.WR, 15.0 * games_left(11), {4: 0.0, 5: 15.0}),    # back from OUT
        established(4, Pos.WR, 14.0), established(5, Pos.QB, 18.0),
        established(6, Pos.TE, 9.0, scale=0.9),
    ]
    with_evidence = ros(pool)
    for p in pool:
        p.proj_week = {}
    without = ros(pool)

    for p in pool:
        a, b = with_evidence[p.espn_id], without[p.espn_id]
        assert (a.points, a.vor, a.tier) == (b.points, b.vor, b.tier), p.name
        assert "role.weekly_rate" not in a.components, p.name


def test_one_spot_start_is_not_a_role():
    """ESPN prices a one-game fill-in into the season row and nothing more
    (a backup quarterback's season row equals that week's projection). One
    weekly projection is not two weeks of evidence."""
    spot = player(1, Pos.QB, 16.7, {5: 16.7})
    assert ros_base(spot, current_week=WEEK, weeks_remaining=WEEKS_LEFT)[0] == 16.7


def test_a_bye_week_is_not_evidence():
    """The week after his bye there is only one game week to read."""
    p = player(1, Pos.RB, 20.0, {4: 0.0, 5: 12.0}, bye=4)
    assert ros_base(p, current_week=WEEK, weeks_remaining=WEEKS_LEFT)[0] == 20.0


def test_preseason_valuation_is_unchanged():
    """Draft day has one weekly projection at most: the board is ESPN's row."""
    p = player(1, Pos.RB, 20.0, {1: 15.0})
    assert ros_base(p, current_week=1, weeks_remaining=14)[0] == 20.0


# ── (c) the lineup comes out right without the designation guard ───────────


def wilson_swift(swift_status: InjuryStatus):
    """The week-5 2026 RB room with live numbers: Wilson 24.87 season row,
    11.08 / 12.68 weekly; Swift 158.92 season row, 13.40 then 12.24 this week
    (his number before the QUESTIONABLE cut). One RB2 slot between them: the
    stud takes RB1, and the flex goes to a receiver projecting more than
    `stud_bench_margin` above either back, out of §4.7's reach."""
    s = settings_10()
    wilson = player(30, Pos.RB, 24.87, {4: 11.08, 5: 12.68}, name="Emanuel Wilson")
    swift = player(31, Pos.RB, 158.92, {4: 13.40, 5: 12.24}, name="D'Andre Swift",
                   bye=10, injury_status=swift_status)
    roster = [
        established(1, Pos.QB, 20.0), established(3, Pos.RB, 18.0),
        established(6, Pos.WR, 22.0), established(7, Pos.WR, 21.0),
        established(8, Pos.WR, 21.0), established(9, Pos.TE, 11.0),
        wilson, swift,
    ]
    wk = value_pool(roster, s, window="week", week=WEEK)
    ros_vals = ros(rb_room() + roster)
    return s, roster, wk, ros_vals


def test_a_healthy_swift_does_not_bench_a_higher_projected_wilson_on_a_stale_row():
    """🔴 The half the 2026-10-08 guard could not cover: if Swift clears to
    ACTIVE, neither back is designated, the guard is inert, and on the old
    numbers §4.7 benched Wilson again (a 114-point ROS gap off his stale row,
    a 0.44-point weekly gap inside `stud_bench_margin`). With his ROS rebuilt
    the two are close rest-of-season, so the weekly projection decides."""
    s, roster, wk, ros_vals = wilson_swift(InjuryStatus.ACTIVE)
    gap = ros_vals[31].vor - ros_vals[30].vor
    assert gap < float(priors().get("lineup.stud_ros_gap")), (
        f"Swift still {gap:.1f} ROS VOR ahead — §4.7 would fire off the artifact"
    )

    plan = lineup.build(roster, wk, s, week=WEEK, ros_valuations=ros_vals)

    starters = {a.player.name for a in plan.assignments if a.player}
    assert "Emanuel Wilson" in starters
    assert "D'Andre Swift" not in starters


def test_the_questionable_case_no_longer_leans_on_the_guard():
    """The original escalation, QUESTIONABLE Swift: the ROS gap that made the
    guard necessary is gone, so the answer comes out of §4.7's own arithmetic."""
    s, roster, wk, ros_vals = wilson_swift(InjuryStatus.QUESTIONABLE)
    assert ros_vals[31].vor - ros_vals[30].vor < float(priors().get("lineup.stud_ros_gap"))

    plan = lineup.build(roster, wk, s, week=WEEK, ros_valuations=ros_vals)

    starters = {a.player.name for a in plan.assignments if a.player}
    assert "Emanuel Wilson" in starters
