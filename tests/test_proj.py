"""§P — the projection model.

The model did not ship (it loses to ESPN on the arena benchmark, see
docs/projection-model-plan.md §7), but it stays in the tree behind
`model.projection_blend: 0.0` because the measurement is the asset: adding a
season, or a feature, means re-running it rather than rebuilding it.

These tests guard the things that would make a future re-run silently wrong —
leakage, the scoring bridge, the shrinkage identity, and the ladder blend.
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from core.model.schema import Player, Pos
from core.proj import apply as proj_apply
from core.proj import features, model, nflstats

# ── the scoring bridge ───────────────────────────────────────────────────────


def test_bucketed_passing_yards_are_scored():
    """statId 8 is passing yards per 25, not per yard.

    Missing this put QB agreement with ESPN at 68/621. Measured on Kirk Cousins
    wk5 2024: 509 passing yards -> statId 8 = 20.
    """
    line = nflstats._stat_line({"passing_yards": 509, "passing_tds": 4,
                                "passing_interceptions": 1})
    assert line[8] == 20.0            # floor(509 / 25)
    assert line[3] == 509.0
    assert line[4] == 4.0


def test_bucket_floors_rather_than_rounds():
    assert nflstats._stat_line({"passing_yards": 499})[8] == 19.0
    assert nflstats._stat_line({"passing_yards": 500})[8] == 20.0


def test_fumbles_prefer_the_total_column():
    """The per-cause columns miss lines the total catches."""
    assert nflstats._stat_line({"fumbles_lost_total": 1})[72] == 1.0
    assert nflstats._stat_line({"rushing_fumbles_lost": 1})[72] == 1.0


def test_a_stat_the_league_does_not_score_contributes_nothing():
    df = pl.DataFrame({"passing_yards": [1000.0], "receptions": [10.0]})
    out = nflstats.score_weekly(df, {53: 0.5})       # receptions only
    assert out["points"][0] == pytest.approx(5.0)


# ── leakage ──────────────────────────────────────────────────────────────────


def test_a_feature_row_from_the_target_season_is_refused():
    rows = [{"season": 2025, "name": "leak"}]
    with pytest.raises(features.LeakageError):
        features.assert_no_leakage(rows, 2025)


def test_a_feature_row_after_the_target_season_is_refused():
    with pytest.raises(features.LeakageError):
        features.assert_no_leakage([{"season": 2026, "name": "leak"}], 2025)


def test_prior_seasons_are_allowed():
    features.assert_no_leakage([{"season": 2024, "name": "ok"}], 2025)


def test_build_refuses_a_frame_containing_the_target_season():
    df = pl.DataFrame({"season": [2024, 2025], "player_id": ["a", "a"],
                       "games": [17, 17], "points": [100.0, 100.0],
                       "position": ["RB", "RB"], "name": ["x", "x"]})
    with pytest.raises(features.LeakageError):
        features.build(df, 2025, bio={})


# ── birth_date, the bug that made age silently absent ────────────────────────


def test_birth_date_parses_from_a_string():
    """nflverse serves it as a String; an isinstance(date) check dropped all
    24,800 of them and every age came through as None."""
    assert features._date("1998-03-14") == dt.date(1998, 3, 14)
    assert features._date("1998-03-14T00:00:00") == dt.date(1998, 3, 14)
    assert features._date(dt.date(1998, 3, 14)) == dt.date(1998, 3, 14)
    assert features._date("") is None
    assert features._date("not a date") is None
    assert features._date(None) is None


def test_age_is_computed_at_the_start_of_the_season():
    b = features.Bio("g", "RB", dt.date(2000, 9, 1), 2022, 1, 5)
    assert b.age_in(2026) == pytest.approx(26.0, abs=0.05)
    assert b.experience_in(2026) == 4


# ── the shrinkage identity ───────────────────────────────────────────────────


def _row(pos="RB", games=(16.0,), opp=10.0, ppg=12.0, age=None):
    return features.Row(
        gsis_id="g", name="p", position=pos, target_year=2026, age=age,
        experience=3, draft_pick=10, prior_games=list(games),
        prior_rates=[{"carries": opp, "targets": 0.0, "points": ppg}
                     for _ in games],
        prior_shares=[{} for _ in games],
    )


def test_no_history_falls_all_the_way_to_the_positional_mean():
    p = model.PosParams(k_opp=8.0, k_eff=120.0, base_games=15.0,
                        mean_opp_pg=10.0, mean_eff=1.0)
    got = model.project(_row(games=()), p)
    assert got.opp_per_game == pytest.approx(10.0)
    assert got.eff == pytest.approx(1.0)
    assert got.confidence == 0.0


def test_overwhelming_history_keeps_the_players_own_rate():
    """k is in units of the evidence it competes with, so a huge sample wins."""
    p = model.PosParams(k_opp=0.001, k_eff=0.001, base_games=16.0,
                        mean_opp_pg=1.0, mean_eff=0.1)
    got = model.project(_row(games=(17.0, 17.0, 17.0), opp=20.0, ppg=30.0), p)
    assert got.opp_per_game == pytest.approx(20.0, rel=1e-3)
    assert got.eff == pytest.approx(1.5, rel=1e-3)      # 30 points / 20 carries
    assert got.confidence > 0.99


def test_shrinkage_lands_halfway_at_exactly_k_games():
    p = model.PosParams(season_weights=[1.0, 0.0, 0.0], k_opp=16.0, k_eff=1e9,
                        base_games=16.0, mean_opp_pg=0.0, mean_eff=1.0)
    got = model.project(_row(games=(16.0,), opp=10.0), p)
    assert got.opp_per_game == pytest.approx(5.0)       # half own, half mean
    assert got.confidence == pytest.approx(0.5)


def test_efficiency_shrinks_harder_than_opportunity():
    """The asymmetry IS the model — TD rate reverts, target share does not."""
    p = model.PosParams(k_opp=8.0, k_eff=120.0)
    assert p.k_eff > p.k_opp


def test_the_age_penalty_only_bites_past_the_cliff():
    p = model.PosParams(base_games=16.0, age_cliff=27.0, age_slope=1.0,
                        mean_opp_pg=1.0, mean_eff=1.0)
    assert model.project(_row(age=26), p).games == pytest.approx(16.0)
    assert model.project(_row(age=30), p).games == pytest.approx(13.0)


def test_games_are_clamped_to_a_real_season():
    p = model.PosParams(base_games=16.0, age_cliff=20.0, age_slope=10.0,
                        mean_opp_pg=1.0, mean_eff=1.0)
    assert model.project(_row(age=40), p).games >= 1.0


def test_a_projection_is_never_negative():
    p = model.PosParams(base_games=16.0, mean_opp_pg=-5.0, mean_eff=1.0)
    assert model.project(_row(games=()), p).points >= 0.0


# ── the ladder blend ─────────────────────────────────────────────────────────


def _player(pid, proj):
    return Player(espn_id=pid, name=f"p{pid}", pos=Pos.RB, pro_team="KC",
                  eligible_slots=["2"], proj_season=proj)


def _proj(points):
    return model.Projection(points=points, games=16, opp_per_game=1,
                            eff=1, confidence=0.9)


def test_weight_zero_changes_nothing():
    pool = [_player(1, 100.0), _player(2, 50.0)]
    assert proj_apply.blend(pool, {1: _proj(1.0), 2: _proj(9.0)}, 0.0) == 0
    assert [p.proj_season for p in pool] == [100.0, 50.0]


def test_weight_one_reorders_onto_the_covered_ladder():
    pool = [_player(1, 100.0), _player(2, 50.0)]
    proj_apply.blend(pool, {1: _proj(1.0), 2: _proj(9.0)}, 1.0)
    # player 2 is the model's favourite, so he takes the top of the ladder
    assert pool[1].proj_season == pytest.approx(100.0)
    assert pool[0].proj_season == pytest.approx(50.0)


def test_the_ladder_is_built_from_covered_players_only():
    """Reading our rank against the WHOLE pool's ladder would promote every
    covered player over every uncovered one — a rookie would sink on coverage
    alone, not merit."""
    pool = [_player(1, 100.0), _player(2, 60.0), _player(3, 20.0)]
    proj_apply.blend(pool, {2: _proj(5.0), 3: _proj(1.0)}, 1.0)
    assert pool[0].proj_season == pytest.approx(100.0)   # uncovered, untouched
    assert pool[1].proj_season == pytest.approx(60.0)    # best covered
    assert pool[2].proj_season == pytest.approx(20.0)


def test_an_uncovered_player_is_left_alone():
    pool = [_player(1, 100.0), _player(2, 50.0)]
    proj_apply.blend(pool, {1: _proj(1.0)}, 1.0)
    assert pool[1].proj_season == pytest.approx(50.0)


def test_the_blend_is_a_weighted_average():
    pool = [_player(1, 100.0), _player(2, 50.0)]
    proj_apply.blend(pool, {1: _proj(1.0), 2: _proj(9.0)}, 0.25)
    assert pool[1].proj_season == pytest.approx(0.75 * 50 + 0.25 * 100)


def test_a_weight_outside_zero_to_one_is_refused():
    with pytest.raises(ValueError):
        proj_apply.blend([_player(1, 10.0)], {1: _proj(1.0)}, 1.5)


def test_blending_with_no_covered_player_is_a_no_op():
    pool = [_player(1, 100.0)]
    assert proj_apply.blend(pool, {}, 0.5) == 0
    assert pool[0].proj_season == 100.0


# ── the model round-trips ────────────────────────────────────────────────────


def test_model_survives_a_json_round_trip(tmp_path):
    m = model.Model(params={"RB": model.PosParams(k_opp=7.0, k_eff=99.0)},
                    trained_on=[2012, 2013], held_out=[2024, 2025])
    path = tmp_path / "m.json"
    m.to_json(path)
    back = model.Model.from_json(path)
    assert back.params["RB"].k_opp == 7.0
    assert back.trained_on == [2012, 2013]
    assert back.held_out == [2024, 2025]
