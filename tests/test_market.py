"""§2.2b — the consensus blend.

This is the largest single change the engine has taken: it moves mean finish
against ESPN autopick from 6.80 of 10 to 3.65. It also rewrites `proj_season`
in place, before valuation, which means a mistake here is invisible everywhere
downstream — every consumer just sees a number.
"""

from __future__ import annotations

import pytest

from core.model import market
from core.model.schema import Pos
from tests.conftest import make_player


@pytest.fixture
def pool():
    # projections 100, 90, 80, 70, 60 — a clean ladder to index into.
    return [make_player(i, Pos.RB, 100.0 - 10 * i, name=f"p{i}") for i in range(5)]


def test_weight_zero_changes_nothing(pool):
    before = [p.proj_season for p in pool]
    n = market.blend(pool, {0: 5, 1: 1}, 0.0)
    assert n == 0
    assert [p.proj_season for p in pool] == before


def test_weight_one_replaces_with_the_consensus_value(pool):
    """A player ESPN ranks 1st is worth what OUR best player is worth."""
    market.blend(pool, {4: 1}, 1.0)
    assert pool[4].proj_season == pytest.approx(100.0)


def test_the_blend_is_a_weighted_average(pool):
    market.blend(pool, {4: 1}, 0.75)      # ours 60, consensus-implied 100
    assert pool[4].proj_season == pytest.approx(0.25 * 60 + 0.75 * 100)


def test_an_unranked_player_is_left_alone(pool):
    """No opinion from ESPN is not the same as a bad opinion."""
    market.blend(pool, {0: 1}, 1.0)
    assert pool[3].proj_season == pytest.approx(70.0)
    assert pool[4].proj_season == pytest.approx(60.0)


def test_a_rank_past_the_end_of_the_pool_clamps(pool):
    """Deep ranks are common — the pool is 450 and ESPN ranks thousands."""
    market.blend(pool, {0: 9999}, 1.0)
    assert pool[0].proj_season == pytest.approx(60.0)   # the worst on the ladder


def test_a_weight_outside_zero_to_one_is_refused(pool):
    with pytest.raises(ValueError):
        market.blend(pool, {0: 1}, 1.5)


def test_ranks_are_parsed_from_the_espn_payload():
    raw = [{"player": {"id": 4, "draftRanksByRankType": {
                "PPR": {"rank": 12}, "STANDARD": {"rank": 3}}}},
           {"player": {"id": 5, "draftRanksByRankType": {"PPR": {}}}},
           {"player": {"id": 6}}]
    assert market.ranks_from_raw(raw, "PPR") == {4: 12}
    assert market.ranks_from_raw(raw, "STANDARD") == {4: 3}


def test_blending_preserves_the_ladder_it_reads_from(pool):
    """The ladder is snapshotted before any write, so a player blended early
    cannot change the consensus value of one blended later."""
    market.blend(pool, {i: 1 for i in range(5)}, 1.0)
    assert all(p.proj_season == pytest.approx(100.0) for p in pool)
