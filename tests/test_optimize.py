"""The vor_weight interpolation, and the guard that stops it drifting.

`vor_weight` moves the whole draft board between two orderings, so a mistake in
it silently changes every pick the engine will ever make. The identity at 1.0 is
the important one: it is what makes the parameter safe to ship at its default.
"""

from __future__ import annotations

import pytest

from core.draft import picker
from core.draft.room import RoomModel
from core.model.priors import overridden
from tests.test_backtest import _season


@pytest.fixture
def board_and_room():
    from core.backtest import replay

    s = _season(teams=10)
    board = replay.build_board(s, injury_history=False)
    room = RoomModel(facts=s.facts, my_team_id=1)
    return board, room


def test_vor_weight_1_is_exactly_the_vor_board(board_and_room):
    """The default must be a no-op, or shipping it is a change nobody reviewed."""
    board, room = board_and_room
    with overridden(draft__vor_weight=1.0):
        plan = picker.rank(board.rows, room)
    for c in plan.candidates:
        assert c.reasons["base"] == pytest.approx(c.valuation.vor)


def test_vor_weight_0_is_the_raw_projection_board(board_and_room):
    """points = vor + replacement, so removing all of the baseline is points."""
    board, room = board_and_room
    with overridden(draft__vor_weight=0.0):
        plan = picker.rank(board.rows, room)
    for c in plan.candidates:
        assert c.reasons["base"] == pytest.approx(c.valuation.points)


def test_intermediate_weights_sit_between(board_and_room):
    board, room = board_and_room
    got = {}
    for w in (0.0, 0.5, 1.0):
        with overridden(draft__vor_weight=w):
            plan = picker.rank(board.rows, room)
        got[w] = {c.player.espn_id: c.reasons["base"] for c in plan.candidates}
    pid = next(iter(got[1.0]))
    lo, mid, hi = got[1.0][pid], got[0.5][pid], got[0.0][pid]
    assert min(lo, hi) <= mid <= max(lo, hi)
    assert mid == pytest.approx((lo + hi) / 2)


def test_a_below_replacement_player_is_never_rewarded_for_risk(board_and_room):
    """Both bugs this file guards were the same shape: `score -= w * vor` on a
    NEGATIVE vor adds to the score, turning a penalty into a bonus for exactly
    the players it was meant to demote."""
    board, room = board_and_room
    with overridden(draft__vor_weight=1.0):
        plan = picker.rank(board.rows, room)
    for c in plan.candidates:
        for name, v in c.reasons.items():
            if name in ("bye_collision", "shallow_bench_risk", "stacking",
                        "depth_while_short"):
                assert v <= 0.0, f"{name} was a bonus ({v:+.2f}) for {c.player.name}"
