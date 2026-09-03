"""§2 — the whole valuation engine, end to end.

These are the tests that catch a fork in the model: one function, four consumers
(§10.4). If a consumer ever needs its own valuation path, one of these breaks.
"""

from __future__ import annotations

from core.model.durability import InjuryEvent
from core.model.schema import InjuryStatus, Pos
from core.model.value import PlayerContext, value_pool
from tests.conftest import make_player


def test_every_player_gets_a_valuation(settings, pool):
    vals = value_pool(pool, settings, window="ros")
    assert len(vals) == len(pool)
    assert all(v.components for v in vals.values()), "a Valuation with no components is a bug"


def test_ros_window_scales_by_availability(settings):
    healthy = make_player(1, Pos.RB, 300.0)
    fragile = make_player(2, Pos.RB, 300.0)
    ctx = {
        2: PlayerContext(
            injury_history=[
                InjuryEvent(2025, 4, "hamstring"),
                InjuryEvent(2024, 3, "calf strain"),
            ]
        )
    }
    vals = value_pool([healthy, fragile], settings, window="ros", contexts=ctx)
    assert vals[2].points < vals[1].points
    assert vals[2].availability < vals[1].availability


def test_vetoed_player_scores_zero_and_says_why(settings):
    out = make_player(1, Pos.RB, 300.0, injury_status=InjuryStatus.OUT)
    fine = make_player(2, Pos.RB, 100.0)
    vals = value_pool([out, fine], settings, window="ros")
    assert vals[1].vetoed
    assert vals[1].points == 0.0
    assert "§2.5" in vals[1].vetoes[0]


def test_vetoed_players_do_not_drag_the_replacement_baseline(settings):
    """A shelf of injured players must not make replacement level look worse
    than it is — otherwise everyone's VOR inflates."""
    from tests.conftest import linear_pool

    live = linear_pool(Pos.RB, 40, 300, 5, 2000)
    baseline_vals = value_pool(live, settings, window="ros")

    dead = [
        make_player(9000 + i, Pos.RB, 250.0, injury_status=InjuryStatus.OUT)
        for i in range(10)
    ]
    with_dead = value_pool(live + dead, settings, window="ros")

    assert with_dead[2000].vor == baseline_vals[2000].vor


def test_weekly_window_applies_context_multipliers(settings):
    p = make_player(1, Pos.WR, 200.0, proj_week={5: 14.0})
    plain = value_pool([p], settings, window="week", week=5)
    boosted = value_pool(
        [p],
        settings,
        window="week",
        week=5,
        contexts={1: PlayerContext(multipliers={"opp_def": 1.20})},
    )
    assert boosted[1].points > plain[1].points
    assert boosted[1].components["ctx.opp_def"] == 1.20


def test_missing_weekly_projection_is_flagged_not_guessed(settings):
    p = make_player(1, Pos.WR, 200.0, proj_week={})
    vals = value_pool([p], settings, window="week", week=5)
    assert vals[1].points == 0.0
    assert any("no ESPN projection" in m for m in vals[1].missing)


def test_missing_weekly_context_is_flagged(settings):
    p = make_player(1, Pos.WR, 200.0, proj_week={5: 14.0})
    vals = value_pool([p], settings, window="week", week=5)
    assert any("no weekly context" in m for m in vals[1].missing)


def test_news_override_is_capped(settings):
    """§3.2 — the agent's pre-draft pass may nudge the board, not rewrite it."""
    p = make_player(1, Pos.RB, 200.0)
    runaway = value_pool(
        [p],
        settings,
        window="ros",
        contexts={1: PlayerContext(news_override=3.0)},  # tries to triple him
        override_cap=0.15,
    )
    assert runaway[1].components["news_override"] == 1.15


def test_news_override_floor_is_also_capped(settings):
    p = make_player(1, Pos.RB, 200.0)
    vals = value_pool(
        [p],
        settings,
        window="ros",
        contexts={1: PlayerContext(news_override=0.0)},
        override_cap=0.15,
    )
    assert vals[1].components["news_override"] == 0.85


def test_questionable_costs_a_week_but_not_the_season(settings):
    q = make_player(1, Pos.RB, 200.0, proj_week={3: 15.0},
                    injury_status=InjuryStatus.QUESTIONABLE)
    healthy = make_player(2, Pos.RB, 200.0, proj_week={3: 15.0})
    weekly = value_pool([q, healthy], settings, window="week", week=3)
    assert weekly[1].points < weekly[2].points
    assert not weekly[1].vetoed


def test_variance_is_measured_when_there_is_enough_history(settings):
    steady = make_player(
        1, Pos.WR, 200.0,
        actual_week={w: 12.0 for w in range(1, 9)},
        proj_week={w: 12.0 for w in range(1, 9)},
    )
    swingy = make_player(
        2, Pos.WR, 200.0,
        actual_week={1: 2, 2: 30, 3: 1, 4: 28, 5: 3, 6: 26, 7: 4, 8: 25},
        proj_week={w: 12.0 for w in range(1, 9)},
    )
    vals = value_pool([steady, swingy], settings, window="ros")
    assert vals[2].stdev > vals[1].stdev
    assert vals[2].bust_rate > vals[1].bust_rate


def test_variance_does_not_change_the_base_value(settings):
    """§2.6 — variance is stored, never baked in. Risk preference is §4.2's job."""
    steady = make_player(1, Pos.WR, 200.0,
                         actual_week={w: 12.0 for w in range(1, 9)},
                         proj_week={w: 12.0 for w in range(1, 9)})
    swingy = make_player(2, Pos.WR, 200.0,
                         actual_week={1: 2, 2: 30, 3: 1, 4: 28, 5: 3, 6: 26, 7: 4, 8: 25},
                         proj_week={w: 12.0 for w in range(1, 9)})
    vals = value_pool([steady, swingy], settings, window="ros")
    assert vals[1].points == vals[2].points


def test_the_headline_result_qbs_are_overrated(settings, pool):
    """The single most important behaviour in the model: the highest-projected
    player in football should not be the most valuable pick in a 1QB league."""
    vals = value_pool(pool, settings, window="ros")
    best = max(vals.values(), key=lambda v: v.vor)
    qb1 = vals[1000]
    assert best.espn_id != qb1.espn_id
    assert best.vor > qb1.vor
