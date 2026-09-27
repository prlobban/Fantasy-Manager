"""The whole lineup a set of moves is meant to produce — and proof it did.

2026-09-27 07:32: three moves (Warren flex->RB, McConkey BE->WR, Wilson
WR->flex), each landing exactly where it was aimed, and the receipt said
verified. But "McConkey to WR" swapped with the FIRST occupied WR row, which
was Jefferson, and the flex move then evicted a back who cannot play WR. We
fielded eight starters with our best player on the bench.
"""

from __future__ import annotations

from core.mcp_server import lineup_shortfall, resolve_lineup_target
from core.model.schema import Player, Pos, RosterSlot

SLOTS = [
    RosterSlot(name="RB", count=2, eligible=(Pos.RB,)),
    RosterSlot(name="WR", count=2, eligible=(Pos.WR,)),
    RosterSlot(name="RB/WR/TE", count=1, eligible=(Pos.RB, Pos.WR, Pos.TE)),
]
WEEK = 3


def pl(pid, pos, name, proj) -> Player:
    p = Player(espn_id=pid, name=name, pos=pos, pro_team="XX")
    p.proj_week = {WEEK: proj}
    return p


HUBBARD = pl(1, Pos.RB, "Chuba Hubbard", 14.0)
SWIFT = pl(2, Pos.RB, "D'Andre Swift", 11.1)
WARREN = pl(3, Pos.RB, "Jaylen Warren", 12.0)
JEFFERSON = pl(4, Pos.WR, "Justin Jefferson", 15.2)
WILSON = pl(5, Pos.WR, "Garrett Wilson", 13.0)
MCCONKEY = pl(6, Pos.WR, "Ladd McConkey", 12.7)
BY_ID = {p.espn_id: p for p in (HUBBARD, SWIFT, WARREN, JEFFERSON, WILSON, MCCONKEY)}

BEFORE = {1: "RB", 2: "RB", 4: "WR", 5: "WR", 3: "RB/WR/TE", 6: "BE"}
MOVES = [
    {"espn_id": 3, "slot": "RB"},
    {"espn_id": 6, "slot": "WR"},
    {"espn_id": 5, "slot": "RB/WR/TE"},
]


def test_the_displaced_player_is_the_one_the_plan_benches():
    target = resolve_lineup_target(MOVES, BEFORE, SLOTS, BY_ID, WEEK)
    assert target == {1: "RB", 3: "RB", 4: "WR", 6: "WR", 5: "RB/WR/TE", 2: "BE"}


def test_jefferson_is_a_keeper_at_wr():
    target = resolve_lineup_target(MOVES, BEFORE, SLOTS, BY_ID, WEEK)
    keepers_wr = {pid for pid, sl in target.items() if sl == "WR"}
    assert JEFFERSON.espn_id in keepers_wr
    assert WILSON.espn_id not in keepers_wr  # his row is the one to swap into


def test_the_0927_result_is_not_verified():
    """What ESPN actually showed at 11:00: WR one short, Jefferson benched."""
    target = resolve_lineup_target(MOVES, BEFORE, SLOTS, BY_ID, WEEK)
    after = {1: "RB", 3: "RB", 6: "WR", 5: "RB/WR/TE", 4: "BE", 2: "BE"}
    wrong, short = lineup_shortfall(target, after, SLOTS)
    assert (4, "WR", "BE") in wrong
    assert short == ["WR 1/2"]


def test_a_correct_lineup_is_clean():
    target = resolve_lineup_target(MOVES, BEFORE, SLOTS, BY_ID, WEEK)
    wrong, short = lineup_shortfall(target, dict(target), SLOTS)
    assert wrong == [] and short == []


def test_an_empty_slot_the_target_never_filled_is_not_a_shortfall():
    """A roster that cannot fill a slot is not a write failure."""
    before = {1: "RB", 4: "WR", 5: "WR", 6: "BE"}
    target = resolve_lineup_target([], before, SLOTS, BY_ID, WEEK)
    wrong, short = lineup_shortfall(target, dict(before), SLOTS)
    assert wrong == [] and short == []


def test_moving_a_starter_to_the_bench_frees_the_slot():
    target = resolve_lineup_target([{"espn_id": 2, "slot": "BE"}], BEFORE, SLOTS, BY_ID, WEEK)
    assert target[2] == "BE"
    assert target[1] == "RB"
