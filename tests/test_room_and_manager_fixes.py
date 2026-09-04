"""Regression tests for the 2026-09-04 self-review.

Each test pins a bug that the existing suite let through: a DOM-only draft
room misattributing every pick, the room count drifting after a missed row,
a lineup plan listing one player twice, and an IR player being offered as a
drop candidate.
"""

from __future__ import annotations

from core.draft.room import Pick, RoomModel
from core.manager import lineup, waivers
from core.model.schema import InjuryStatus, Player, Pos, Valuation
from tests.test_draft_sim import make_facts


def _val(pid: int, pts: float, vor: float = 0.0, stdev: float | None = None) -> Valuation:
    return Valuation(espn_id=pid, window="week", points=pts, vor=vor, tier=1,
                     availability=1.0, stdev=stdev)


def test_dom_picks_with_no_team_are_attributed_by_the_snake_order():
    facts = make_facts()
    me = facts.pick_order[0]
    room = RoomModel(facts=facts, my_team_id=me)
    # The DOM reader cannot see teams; it hands over team_id=0.
    room.apply([Pick(overall=1, team_id=0, espn_id=11, pos=Pos.RB, name="a")])
    assert room.picks[0].team_id == me
    assert room.my_positions[Pos.RB] == 1
    # Pick 2 belongs to slot 2.
    room.apply([Pick(overall=2, team_id=0, espn_id=12, pos=Pos.WR, name="b")])
    assert room.picks[1].team_id == facts.pick_order[1]


def test_next_overall_follows_the_highest_pick_seen_not_the_count():
    facts = make_facts()
    room = RoomModel(facts=facts, my_team_id=facts.pick_order[0])
    # Row 2 missed by the DOM parser: the room must still know pick 4 is next.
    room.apply([
        Pick(overall=1, team_id=0, espn_id=1, pos=Pos.RB),
        Pick(overall=3, team_id=0, espn_id=3, pos=Pos.RB),
    ])
    assert room.next_overall == 4
    assert room.current_round == 1


def test_negative_or_zero_overall_is_never_applied():
    facts = make_facts()
    room = RoomModel(facts=facts, my_team_id=facts.pick_order[0])
    new = room.apply([Pick(overall=-1004, team_id=0, espn_id=9, pos=Pos.RB),
                      Pick(overall=0, team_id=0, espn_id=8, pos=Pos.RB)])
    assert new == []
    assert room.next_overall == 1


def test_lineup_build_never_lists_a_player_twice():
    settings = make_facts().settings
    roster = [
        Player(espn_id=1, name="QB1", pos=Pos.QB, pro_team="A"),
        Player(espn_id=2, name="RB1", pos=Pos.RB, pro_team="A"),
        Player(espn_id=3, name="RB2", pos=Pos.RB, pro_team="A"),
        Player(espn_id=4, name="RBbench", pos=Pos.RB, pro_team="A"),
        Player(espn_id=5, name="WR1", pos=Pos.WR, pro_team="A"),
        Player(espn_id=6, name="WR2", pos=Pos.WR, pro_team="A"),
        Player(espn_id=7, name="TE1", pos=Pos.TE, pro_team="A"),
        Player(espn_id=8, name="K1", pos=Pos.K, pro_team="A"),
        Player(espn_id=9, name="D1", pos=Pos.DST, pro_team="A"),
        Player(espn_id=10, name="FLEX", pos=Pos.WR, pro_team="A"),
    ]
    vals = {
        1: _val(1, 20), 2: _val(2, 15, stdev=8), 3: _val(3, 12, stdev=6),
        4: _val(4, 11.5, stdev=2),  # a floor play inside the 1.5-pt budget
        5: _val(5, 14), 6: _val(6, 13), 7: _val(7, 9), 8: _val(8, 8), 9: _val(9, 7),
        10: _val(10, 10),
    }
    # Heavily favoured -> floor mode -> RBbench swaps in for RB2.
    current = {1: "QB", 2: "RB", 3: "RB", 5: "WR", 6: "WR", 7: "TE", 8: "K", 9: "D/ST",
               10: "RB/WR/TE"}
    plan = lineup.build(roster, vals, settings, opponent_projected=50.0,
                        current_starters=current)
    ids = [c[0].espn_id for c in plan.changes]
    assert len(ids) == len(set(ids)), plan.changes
    assert 4 in ids


def test_ir_player_is_not_a_drop_candidate():
    settings = make_facts().settings
    roster = [
        Player(espn_id=1, name="starter", pos=Pos.RB, pro_team="A"),
        Player(espn_id=2, name="on_ir", pos=Pos.RB, pro_team="A",
               injury_status=InjuryStatus.IR),
        Player(espn_id=3, name="scrub", pos=Pos.RB, pro_team="A"),
    ]
    vals = {1: _val(1, 15, vor=30), 2: _val(2, 0, vor=-40), 3: _val(3, 2, vor=-5)}
    # never_drop_top_n is 5 in priors, so shrink the protected set by ranking:
    # only three players, all "protected" by top-N — so use a bigger roster.
    roster += [Player(espn_id=i, name=f"p{i}", pos=Pos.WR, pro_team="A") for i in range(4, 10)]
    vals.update({i: _val(i, 5 + i, vor=i) for i in range(4, 10)})
    drop, _cost, _why = waivers.choose_drop(roster, vals, settings)
    assert drop is not None
    assert drop.espn_id != 2, "an IR player does not occupy a bench spot and is never the drop"
