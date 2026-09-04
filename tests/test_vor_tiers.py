"""§2.3–2.4 — VOR and tier breaks."""

from __future__ import annotations

from core.model.schema import Pos
from core.model.vor import compute_tiers, compute_vor, tiers_for_position


def test_vor_is_points_minus_replacement(settings, pool):
    points = {p.espn_id: p.proj_season for p in pool}
    vors = compute_vor(pool, settings, points_of=points)

    qb1 = next(p for p in pool if p.espn_id == 1000)
    # QB1 = 380, replacement QB13 = 380 - 72 = 308
    assert vors[qb1.espn_id] == 72.0


def test_top_qb_vor_is_smaller_than_top_rb_vor(settings, pool):
    """The point of the whole model: QBs project highest and are worth least."""
    points = {p.espn_id: p.proj_season for p in pool}
    vors = compute_vor(pool, settings, points_of=points)

    qb1_pts = next(p.proj_season for p in pool if p.espn_id == 1000)
    rb1_pts = next(p.proj_season for p in pool if p.espn_id == 2000)
    assert qb1_pts > rb1_pts          # QB projects more...
    assert vors[1000] < vors[2000]    # ...and is worth less.


def test_replacement_level_player_has_zero_vor(settings, pool):
    points = {p.espn_id: p.proj_season for p in pool}
    vors = compute_vor(pool, settings, points_of=points)
    assert vors[1000 + 12] == 0.0  # QB13


def test_evenly_spaced_players_form_one_tier():
    values = [(i, 100.0 - i * 5) for i in range(10)]
    tiers = tiers_for_position(values, gap_multiple=1.5)
    assert set(tiers.values()) == {1}


def test_a_cliff_creates_a_tier_break():
    # Four close players, then a chasm, then four more.
    values = [(i, 100.0 - i * 2) for i in range(4)]
    values += [(10 + i, 50.0 - i * 2) for i in range(4)]
    tiers = tiers_for_position(values, gap_multiple=1.5)
    assert tiers[0] == 1
    assert tiers[13] == 2
    assert len(set(tiers.values())) == 2


def test_identical_projections_do_not_split_into_one_tier_each():
    values = [(i, 50.0) for i in range(8)]
    tiers = tiers_for_position(values, gap_multiple=1.5)
    assert set(tiers.values()) == {1}


def test_tiers_are_computed_per_position(settings, pool):
    points = {p.espn_id: p.proj_season for p in pool}
    vors = compute_vor(pool, settings, points_of=points)
    tiers = compute_tiers(pool, vors)

    # Every position has its own tier 1.
    for pos, start in ((Pos.QB, 1000), (Pos.RB, 2000), (Pos.WR, 3000), (Pos.TE, 4000)):
        assert tiers[start] == 1, pos


def test_empty_input_is_not_an_error():
    assert tiers_for_position([]) == {}


def test_a_player_who_cannot_play_never_scores_replacement_level():
    """availability x surplus is 0 when availability is 0 — which reads as
    'exactly replacement level'. In the dead rounds every real player is
    negative, so a 0 would float an unplayable player to the top of the
    board. He must come out clearly worse (2026-09-04)."""
    from core.model.schema import LeagueSettings, Player, Pos, RosterSlot
    from core.model.vor import compute_vor

    settings = LeagueSettings(
        league_id=1, season=2026, name="t", team_count=10, draft_type="SNAKE",
        starting_slots=[RosterSlot(name="RB", count=2, eligible=(Pos.RB,))],
        bench_count=4, ir_count=1, scoring={53: 0.5}, waiver_type="X",
        faab_budget=None, trade_deadline=None, playoff_team_count=6,
        playoff_weeks=[15], regular_season_weeks=14, keeper_count=0,
    )
    pool = [Player(espn_id=i, name=f"RB{i}", pos=Pos.RB, pro_team="A",
                   proj_season=300.0 - i * 5.0) for i in range(1, 40)]
    points = {p.espn_id: p.proj_season for p in pool}
    avail = dict.fromkeys(points, 1.0)
    avail[pool[0].espn_id] = 0.0          # the best player, but out for the season

    vors = compute_vor(pool, settings, points_of=points, availability_of=avail)
    worst_real = min(v for pid, v in vors.items() if avail[pid] > 0)
    assert vors[pool[0].espn_id] < worst_real, (
        "an unplayable player outranked every real one"
    )
