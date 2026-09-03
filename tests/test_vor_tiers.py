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
