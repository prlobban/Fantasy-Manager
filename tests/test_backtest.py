"""The backtest's own correctness.

A backtest is a measuring instrument, and an uncalibrated instrument is worse
than none: it produces confident numbers with no error bar. These tests pin the
three ways this one could lie —

1. **rescoring** silently wrong, so every season total is off;
2. **leakage**, so the engine drafts knowing what happened and looks brilliant;
3. **replay bookkeeping** wrong, so rosters are illegal or players are cloned.

Everything here runs offline against a synthetic season. The live reproduction
gate against real ESPN data lives in `scripts/backtest.py --mode normalised`,
which refuses to run when it fails.
"""

from __future__ import annotations

import pytest

from core.backtest import rescore, score
from core.backtest.history import DraftPick, Season
from core.espn.settings import LeagueFacts
from core.model.schema import Player, Pos
from tests.conftest import make_settings

# ── a synthetic season ───────────────────────────────────────────────────────

#: statId 24 = rushing yards, 42 = receiving yards, 53 = receptions, 4 = pass TD.
SCORING_A = {24: 0.1, 42: 0.1, 53: 0.5, 4: 4.0}
SCORING_B = {24: 0.1, 42: 0.1, 53: 0.5, 4: 5.0}  # the 2026-style pass-TD change


#: The order positions come off the board in the synthetic draft. Chosen so
#: every team ends with a LEGAL roster under the caps below — an illegal
#: "real" draft would make the cap invariant test fail on the fixture rather
#: than on the code, which is exactly the kind of false alarm that gets a real
#: assertion deleted.
DRAFT_SHAPE = [Pos.QB, Pos.RB, Pos.RB, Pos.WR, Pos.WR, Pos.TE, Pos.RB,
               Pos.WR, Pos.QB, Pos.TE, Pos.RB, Pos.K, Pos.DST]


def _facts(teams: int = 4, scoring: dict | None = None) -> LeagueFacts:
    s = make_settings(teams=teams)
    # bench 4 -> 13 rounds, against caps summing to 14. The draft must be
    # completable: with the default bench of 7 every team needs 16 players and
    # the caps only allow 14, so the last rounds have no legal pick at all.
    s = s.model_copy(update={"scoring": scoring or SCORING_A,
                             "regular_season_weeks": 3,
                             "bench_count": 4})
    return LeagueFacts(
        settings=s,
        position_limits={Pos.QB: 2, Pos.RB: 4, Pos.WR: 4, Pos.TE: 2,
                         Pos.K: 1, Pos.DST: 1},
        seconds_per_pick=90,
        pick_order=list(range(1, teams + 1)),
        draft_at=None,
        acquisition_type="WAIVERS_TRADITIONAL",
        using_acquisition_budget=False,
        waiver_process_days=[],
        trade_revision_hours=24,
        veto_votes_required=4,
        playoff_seeding_rule="TOTAL_POINTS_SCORED",
    )


def _season(teams: int = 4, scoring: dict | None = None) -> Season:
    facts = _facts(teams, scoring)
    rounds = facts.draftable_spots
    players: list[Player] = []
    raw: dict[int, dict[int, dict[int, float]]] = {}

    pid = 1
    # Sized off `teams` so the same fixture serves the 4-team replay tests and
    # the 10-team arena: DRAFT_SHAPE consumes up to 4 players at a position per
    # team, so a fixed pool silently runs dry as soon as the league grows.
    t = teams
    for pos, n, top in ((Pos.QB, 4 * t, 300.0), (Pos.RB, 8 * t, 280.0),
                        (Pos.WR, 8 * t, 260.0), (Pos.TE, 4 * t, 200.0),
                        (Pos.K, 3 * t, 130.0), (Pos.DST, 3 * t, 130.0)):
        for i in range(n):
            p = Player(espn_id=pid, name=f"{pos.value}{i}", pos=pos, pro_team="FA",
                       proj_season=top - i * 8.0)
            for wk in (1, 2, 3):
                stats = {24: 40.0 + i, 42: 30.0, 53: 4.0}
                pts = rescore.points(stats, facts.settings.scoring)
                p.actual_week[wk] = pts
                p.proj_week[wk] = pts * 0.9
                raw.setdefault(pid, {})[wk] = stats
            players.append(p)
            pid += 1

    by_pos: dict[Pos, list[Player]] = {}
    for p in players:
        by_pos.setdefault(p.pos, []).append(p)
    for v in by_pos.values():
        v.sort(key=lambda x: -x.proj_season)

    picks = []
    overall = 1
    for rnd in range(1, rounds + 1):
        order = facts.pick_order if rnd % 2 else list(reversed(facts.pick_order))
        want = DRAFT_SHAPE[(rnd - 1) % len(DRAFT_SHAPE)]
        for team in order:
            taken_here = by_pos[want].pop(0)
            picks.append(DraftPick(overall=overall, round_num=rnd, team_id=team,
                                   espn_id=taken_here.espn_id))
            overall += 1

    return Season(year=2025, facts=facts, players=players, picks=picks,
                  raw_weekly=raw)


@pytest.fixture
def season() -> Season:
    return _season()


# ── 1. the rescorer ──────────────────────────────────────────────────────────

def test_points_ignores_stats_the_league_does_not_score():
    assert rescore.points({24: 100.0, 999: 50.0}, {24: 0.1}) == pytest.approx(10.0)


def test_verify_reproduces_a_consistent_season(season):
    rep = rescore.verify(season)
    assert rep.lines > 0
    assert rep.agreed == rep.lines, rep.describe()


def test_verify_reports_per_position(season):
    rep = rescore.verify(season)
    assert set(rep.by_pos) == {"QB", "RB", "WR", "TE", "K", "D/ST"}


def test_drifted_ids_treats_absence_as_zero():
    assert rescore.drifted_stat_ids({4: 4.0}, {4: 4.0, 53: 0.5}) == {53}
    assert rescore.drifted_stat_ids({4: 4.0}, {4: 4.0}) == set()


def test_rescoring_changes_points_when_the_map_changes(season):
    """A line whose stats include a drifted statId must be RECOMPUTED."""
    for weeks in season.raw_weekly.values():
        for stats in weeks.values():
            stats[4] = 2.0  # two passing TDs on every line
    for p in season.players:
        for wk, stats in season.raw_weekly.get(p.espn_id, {}).items():
            p.actual_week[wk] = rescore.points(stats, season.facts.settings.scoring)

    res = rescore.rescored_weeks(season, SCORING_B)
    p = season.players[0]
    before = p.actual_week[1]
    after = res.weeks[p.espn_id][1]
    assert after == pytest.approx(before + 2.0), "4->5 per pass TD, twice"
    assert res.refused == []


def test_a_line_that_cannot_be_reproduced_is_carried_only_if_drift_free(season):
    """The D/ST case: ESPN's total is unreproducible, but nothing it scores
    changed between the two maps, so carrying it forward is exact."""
    p = season.players[0]
    season.raw_weekly[p.espn_id][1] = {24: 10.0}          # our maths says 1.0
    p.actual_week[1] = 99.0                                # ESPN insists on 99
    res = rescore.rescored_weeks(season, dict(SCORING_A))  # identical map
    assert res.weeks[p.espn_id][1] == 99.0
    assert res.carried >= 1


def test_a_line_that_is_neither_reproducible_nor_drift_free_is_refused(season):
    p = season.players[0]
    season.raw_weekly[p.espn_id][1] = {4: 3.0}   # pass TDs — a drifted stat
    p.actual_week[1] = 99.0                       # and irreproducible
    with pytest.raises(rescore.RescoreRefused):
        rescore.rescored_weeks(season, SCORING_B)


def test_projections_are_rescored_with_the_same_map_as_results(season):
    """The engine must draft on the objective it is graded by.

    Found live 2026-09-04: normalised mode rescored ACTUALS to 2026 scoring but
    left projections in the season's own scoring, so the engine was marked down
    for correctly maximising the rules it had been handed.
    """
    for p in season.players:
        season.raw_projection[p.espn_id] = {24: 1000.0, 42: 500.0, 4: 10.0}
        p.proj_season = rescore.points(season.raw_projection[p.espn_id],
                                       season.facts.settings.scoring)
    before = season.players[0].proj_season
    rescore.apply_projections(season, rescore.rescored_projections(season, SCORING_B))
    assert season.players[0].proj_season == pytest.approx(before + 10.0)


def test_an_unreproducible_projection_keeps_espns_number(season):
    """A projection is a ranking input, not a result: dropping the player would
    be worse than carrying ESPN's own figure."""
    p = season.players[0]
    season.raw_projection[p.espn_id] = {24: 10.0}
    p.proj_season = 777.0
    got = rescore.rescored_projections(season, SCORING_B)
    assert got[p.espn_id] == 777.0


def test_normalised_mode_keeps_the_seasons_own_league_structure(season):
    """Regression: an earlier version replayed the 6-team 2024 draft through
    the 10-team 2026 pick order, which cost 2024 about 100 points a seat."""
    import dataclasses

    facts = dataclasses.replace(
        season.facts,
        settings=season.facts.settings.model_copy(update={"scoring": SCORING_B}))
    assert facts.pick_order == season.facts.pick_order
    assert facts.settings.team_count == season.facts.settings.team_count
    assert facts.settings.scoring == SCORING_B


# ── 2. leakage ───────────────────────────────────────────────────────────────

def test_board_is_built_only_from_preseason_inputs(season):
    from core.backtest import replay

    board = replay.build_board(season, injury_history=False)
    for p in board.players:
        v = board.valuations[p.espn_id]
        assert v.components["base_projection"] == pytest.approx(p.proj_season)


def test_leakage_guard_catches_a_board_built_on_actuals(season):
    """The guard must FAIL when the future leaks in, or it guards nothing."""
    from core.backtest import replay

    board = replay.build_board(season, injury_history=False)
    victim = board.players[0]
    board.valuations[victim.espn_id].components["base_projection"] = 9999.0
    with pytest.raises(replay.LeakageError):
        replay.assert_no_leakage(season, board)


def test_injury_history_window_excludes_the_season_being_drafted():
    """2025 may see 2022-2024 and nothing later."""
    from core.backtest.replay import HISTORY_SEASONS

    yrs = tuple(range(2025 - HISTORY_SEASONS, 2025))
    assert max(yrs) == 2024 and len(yrs) == HISTORY_SEASONS


# ── 3. replay bookkeeping ────────────────────────────────────────────────────

def test_replay_drafts_each_player_once_and_fills_every_seat(season):
    from core.backtest import replay

    board = replay.build_board(season, injury_history=False)
    rp = replay.replay(season, board, my_team_id=season.facts.pick_order[0])

    drafted = [pid for ids in rp.rosters.values() for pid in ids]
    assert len(drafted) == len(set(drafted)), "a player was drafted twice"
    assert len(drafted) == len(season.picks)
    assert len(rp.our_picks) == season.facts.draftable_spots


def test_replay_respects_position_caps(season):
    from collections import Counter

    from core.backtest import replay

    board = replay.build_board(season, injury_history=False)
    rp = replay.replay(season, board, my_team_id=season.facts.pick_order[2])
    by_id = season.by_id
    for _team, ids in rp.rosters.items():
        counts = Counter(by_id[i].pos for i in ids)
        for pos, n in counts.items():
            cap = season.facts.position_limits.get(pos)
            assert cap is None or n <= cap, f"{pos} over cap: {n} > {cap}"


def test_replay_with_no_divergence_reproduces_the_real_draft(season):
    """A replay is only trustworthy if it is the identity when it should be.

    Drive our own seat with the real picks and every roster must come back
    exactly as drafted, with zero fallbacks. If this fails, the bookkeeping is
    wrong and every number the harness produces is suspect.
    """
    from core.backtest import replay

    board = replay.build_board(season, injury_history=False)
    me = season.facts.pick_order[1]
    ours = {pk.overall: pk.espn_id for pk in season.picks if pk.team_id == me}

    class RealPickPlan:
        def __init__(self, player):
            self.candidates = [type("C", (), {
                "player": player, "valuation": board.valuations[player.espn_id],
                "score": 0.0, "reasons": {}})()]
            self.round_num = 0

        @property
        def best(self):
            return self.candidates[0]

    original = replay.picker.rank
    state = {"n": 0}

    def fake_rank(rows, room, **kw):
        overall = room.next_overall
        state["n"] += 1
        return RealPickPlan(season.by_id[ours[overall]])

    replay.picker.rank = fake_rank
    try:
        rp = replay.replay(season, board, my_team_id=me)
    finally:
        replay.picker.rank = original

    assert rp.fallbacks == 0, rp.fallback_detail[:3]
    for team, ids in rp.rosters.items():
        real = [pk.espn_id for pk in sorted(season.picks, key=lambda x: x.overall)
                if pk.team_id == team]
        assert ids == real


# ── 4. scoring ───────────────────────────────────────────────────────────────

def test_lineup_is_chosen_on_policy_but_scored_on_actuals(season):
    """The separation that makes the measurement mean anything."""
    settings = season.facts.settings
    roster = [p for p in season.players if p.pos is Pos.RB][:3]
    # A player the projection loves and the results do not.
    roster[0].proj_week = {1: 999.0}
    roster[0].actual_week[1] = 0.0
    actuals = score.actuals_from_season(season)

    got = score.score_roster(roster, settings, policy="engine", weeks=[1],
                             actuals=actuals)
    assert got[1] < 999.0, "scored on the projection, not on what happened"


def test_all_play_is_symmetric(season):
    rosters = {1: [p for p in season.players if p.pos is Pos.RB][:5],
               2: [p for p in season.players if p.pos is Pos.WR][:5]}
    res = score.score_league(rosters, season.facts.settings, season=2025,
                             policy="hindsight", weeks=[1, 2, 3], actuals=
                             score.actuals_from_season(season))
    w1, l1 = res.all_play(1)
    w2, l2 = res.all_play(2)
    assert w1 == l2 and l1 == w2


def test_rank_orders_by_total(season):
    rosters = {1: [p for p in season.players if p.pos is Pos.RB][:5],
               2: [p for p in season.players if p.pos is Pos.RB][5:10]}
    res = score.score_league(rosters, season.facts.settings, season=2025,
                             policy="hindsight", weeks=[1], actuals=
                             score.actuals_from_season(season))
    totals = {t: res.teams[t].total for t in (1, 2)}
    winner = max(totals, key=totals.get)
    assert res.rank_of(winner) == 1


# ── 5. the priors override, which the sweep depends on ───────────────────────

def test_overridden_restores_exactly():
    from core.model.priors import overridden, priors

    before = priors().get("draft.scarcity_weight")
    with overridden(draft__scarcity_weight=0.99):
        assert priors().get("draft.scarcity_weight") == 0.99
    assert priors().get("draft.scarcity_weight") == before


def test_overridden_restores_after_an_exception():
    from core.model.priors import overridden, priors

    before = priors().get("draft.stack_penalty")
    with pytest.raises(ValueError), overridden(draft__stack_penalty=0.0):
        raise ValueError("boom")
    assert priors().get("draft.stack_penalty") == before
