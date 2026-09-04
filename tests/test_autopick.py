"""The ESPN autopick bot and the arena it drafts in.

The bot is the benchmark's control, so a bug in it does not make the engine
look slightly wrong — it makes the entire comparison meaningless in a direction
nobody can see from the summary number. These tests pin its behaviour and the
arena's bookkeeping.
"""

from __future__ import annotations

from collections import Counter

import pytest

from core.backtest import arena, autopick
from core.model.schema import Player, Pos
from tests.test_backtest import _season


@pytest.fixture
def season():
    return _season(teams=10)


def _ranks(players: list[Player]) -> dict[int, int]:
    """Rank by projection, which for the fixture is a clean known order."""
    return {p.espn_id: i for i, p in
            enumerate(sorted(players, key=lambda x: -x.proj_season), 1)}


# ── the bot ──────────────────────────────────────────────────────────────────

def test_takes_the_best_ranked_player_available(season):
    settings = season.facts.settings
    ranks = _ranks(season.players)
    best = min(season.players, key=lambda p: ranks[p.espn_id])
    got = autopick.pick(season.players, Counter(), rounds_left=13,
                        settings=settings, limits=season.facts.position_limits,
                        ranks=ranks)
    assert got is best


def test_never_exceeds_a_position_limit(season):
    settings = season.facts.settings
    ranks = _ranks(season.players)
    limits = {**season.facts.position_limits, Pos.QB: 1}
    roster = Counter({Pos.QB: 1})
    got = autopick.pick([p for p in season.players if p.pos is Pos.QB] +
                        [p for p in season.players if p.pos is Pos.RB],
                        roster, rounds_left=13, settings=settings,
                        limits=limits, ranks=ranks)
    assert got.pos is Pos.RB


def test_endgame_forces_unfilled_starting_slots(season):
    """With one pick left and no kicker, ESPN takes a kicker — even though
    every kicker is ranked below every remaining running back."""
    settings = season.facts.settings
    ranks = _ranks(season.players)
    roster = Counter({Pos.QB: 1, Pos.RB: 2, Pos.WR: 2, Pos.TE: 1, Pos.DST: 1})
    got = autopick.pick(season.players, roster, rounds_left=1, settings=settings,
                        limits=season.facts.position_limits, ranks=ranks)
    assert got.pos is Pos.K


def test_an_unranked_player_sorts_last(season):
    settings = season.facts.settings
    two = sorted(season.players, key=lambda p: -p.proj_season)[:2]
    ranks = {two[1].espn_id: 5}          # the WORSE player is the only ranked one
    got = autopick.pick(two, Counter(), rounds_left=13, settings=settings,
                        limits=season.facts.position_limits, ranks=ranks)
    assert got is two[1]


def test_ranks_are_read_off_the_raw_pull():
    raw = [{"player": {"id": 7, "draftRanksByRankType": {
                "STANDARD": {"rank": 3}, "PPR": {"rank": 9}}}},
           {"player": {"id": 8, "draftRanksByRankType": {}}}]
    assert autopick.ranks_from_pool(raw, "STANDARD") == {7: 3}
    assert autopick.ranks_from_pool(raw, "PPR") == {7: 9}


# ── the arena ────────────────────────────────────────────────────────────────

def test_snake_order_turns_around_each_round():
    got = arena.snake_order(3, 2)
    assert got == [1, 2, 3, 3, 2, 1]


def test_control_draft_is_deterministic(season):
    """The whole design rests on this: the all-autopick draft is the baseline
    for every seat at once, which is only valid if it never varies."""
    from core.backtest import replay

    board = replay.build_board(season, injury_history=False)
    ranks = _ranks(season.players)
    a = arena.run(board, season.facts, ranks, engine_seat=None, teams=10)
    b = arena.run(board, season.facts, ranks, engine_seat=None, teams=10)
    assert [p.espn_id for p in a.picks] == [p.espn_id for p in b.picks]


def test_every_seat_ends_full_and_legal(season):
    from core.backtest import replay

    board = replay.build_board(season, injury_history=False)
    res = arena.run(board, season.facts, _ranks(season.players),
                    engine_seat=4, teams=10)
    # arena._assert_legal runs inside run(); this pins the contract explicitly.
    for seat in range(1, 11):
        assert len(res.rosters[seat]) == season.facts.draftable_spots
    assert len(res.our_picks()) == season.facts.draftable_spots


def test_engine_seat_actually_differs_from_the_control(season):
    """If the engine drafted the same roster as the bot, the benchmark would
    report a delta of zero forever and look perfectly healthy."""
    from core.backtest import replay

    board = replay.build_board(season, injury_history=False)
    ranks = _ranks(season.players)
    ctrl = arena.run(board, season.facts, ranks, engine_seat=None, teams=10)
    eng = arena.run(board, season.facts, ranks, engine_seat=3, teams=10)
    mine_ctrl = {p.espn_id for p in ctrl.rosters[3]}
    mine_eng = {p.espn_id for p in eng.rosters[3]}
    assert mine_ctrl != mine_eng


def test_seat_ranks_puts_a_third_drafter_in_the_seat(season):
    """The diagnostic path: our board order, drafted by the bot's algorithm."""
    from core.backtest import replay

    board = replay.build_board(season, injury_history=False)
    ranks = _ranks(season.players)
    vor = {pl.espn_id: i for i, (pl, _) in enumerate(board.rows, 1)}
    res = arena.run(board, season.facts, ranks, engine_seat=2, teams=10,
                    seat_ranks=vor)
    assert len(res.rosters[2]) == season.facts.draftable_spots
    # No VOR is recorded, because the picker never ran for that seat.
    assert all(p.vor is None for p in res.our_picks())


def test_a_short_roster_raises_rather_than_scoring_silently(season):
    from core.backtest import replay

    board = replay.build_board(season, injury_history=False)
    res = arena.run(board, season.facts, _ranks(season.players),
                    engine_seat=1, teams=10)
    res.rosters[5] = res.rosters[5][:-1]
    with pytest.raises(AssertionError):
        arena._assert_legal(res, season.facts, 10)


# ── the bye derivation, which changed the benchmark's answer ─────────────────

def test_byes_are_derived_from_the_weeks_a_team_did_not_play():
    """Found by the benchmark: without byes the engine's bye logic is inert
    while the scorer still zeroes a player on bye."""
    from core.backtest.history import attach_byes

    s = _season(teams=10)
    for p in s.players:
        p.pro_team = "DET"
        p.actual_week = {w: 10.0 for w in range(1, 15) if w != 8}
    n = attach_byes(s)
    assert n == len(s.players)
    assert all(p.bye_week == 8 for p in s.players)
