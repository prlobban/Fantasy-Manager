"""§2.3 — replacement level. Known-answer tests.

If these drift, every downstream valuation is wrong, so they are written as
arithmetic anyone can check by hand rather than as golden numbers.
"""

from __future__ import annotations

from core.model.replacement import flex_share, replacement_points, replacement_rank
from core.model.schema import Pos
from tests.conftest import make_settings


def test_qb_replacement_is_shallow_in_1qb_league(settings):
    # 12 teams x 1 QB starter, no flex accepts QB -> QB13 is replacement.
    assert replacement_rank(Pos.QB, settings) == 13


def test_rb_replacement_includes_flex_share(settings):
    # 12 x 2 dedicated = 24, plus 12 flex slots x PPR RB share 0.35 = 4.2
    # -> round(28.2) + 1 = 29
    assert replacement_rank(Pos.RB, settings) == 29


def test_wr_replacement_is_deeper_than_rb_in_ppr(settings):
    # PPR pushes flex toward WR, so WR replacement sits deeper.
    assert replacement_rank(Pos.WR, settings) > replacement_rank(Pos.RB, settings)


def test_standard_scoring_moves_flex_share_to_rb():
    ppr = make_settings(ppr=1.0)
    std = make_settings(ppr=0.0)
    assert flex_share(Pos.RB, std) > flex_share(Pos.RB, ppr)
    assert replacement_rank(Pos.RB, std) > replacement_rank(Pos.RB, ppr)


def test_half_ppr_interpolates_between_the_two():
    half = flex_share(Pos.WR, make_settings(ppr=0.5))
    std = flex_share(Pos.WR, make_settings(ppr=0.0))
    full = flex_share(Pos.WR, make_settings(ppr=1.0))
    assert std < half <= full


def test_superflex_makes_qb_replacement_much_deeper(settings, superflex_settings):
    shallow = replacement_rank(Pos.QB, settings)
    deep = replacement_rank(Pos.QB, superflex_settings)
    # This is the whole reason QBs stop being cheap in superflex.
    assert deep >= shallow + 10


def test_team_count_scales_replacement(settings):
    ten = make_settings(teams=10)
    assert replacement_rank(Pos.RB, ten) < replacement_rank(Pos.RB, settings)


def test_replacement_points_reads_the_ranked_player(settings, pool):
    # QB pool starts at 380 and steps down 6/player; QB13 is index 12.
    pts = replacement_points(Pos.QB, pool, settings)
    assert pts == 380 - 12 * 6


def test_shallow_pool_falls_back_to_worst_available(settings):
    from tests.conftest import linear_pool

    tiny = linear_pool(Pos.TE, 3, 200, 10, 7000)
    # Replacement rank is well past the pool; we should get the worst, not a crash.
    assert replacement_points(Pos.TE, tiny, settings) == 180


def test_empty_pool_returns_zero(settings):
    assert replacement_points(Pos.RB, [], settings) == 0.0
