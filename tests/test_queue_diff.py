"""§3.3 — queue diffing.

Pure logic, no browser. This is the mechanism the whole draft leans on, so the
properties worth pinning are: the result is always exactly the target, the op
count stays small, and position 1 is never left wrong.
"""

from __future__ import annotations

import random

from core.draft.queue import QueueOp, plan_ops


def apply_ops(current: list[int], ops: list[QueueOp]) -> list[int]:
    """Reference implementation of what the browser ACTUALLY does.

    An add APPENDS — ESPN's add-to-queue control has no target index. The
    earlier reference inserted at op.index, which matched a wrong model in
    plan_ops and let a wrong-top-of-queue bug through the suite.
    """
    q = list(current)
    for op in ops:
        if op.kind == "remove":
            if op.espn_id in q:
                q.remove(op.espn_id)
        elif op.kind == "add":
            q.append(op.espn_id)
        else:
            raise AssertionError(f"unknown op {op.kind}: the room supports remove and add only")
    return q


def test_new_top_pick_is_moved_above_existing_entries():
    """The case that slipped through: our new #1 was not in the queue yet.
    The add appends, so a move MUST follow or position 1 is wrong."""
    ops = plan_ops([1], [2, 1])
    assert [(o.kind, o.espn_id) for o in ops] == [("remove", 1), ("add", 2), ("add", 1)]
    assert apply_ops([1], ops) == [2, 1]


def test_empty_to_full_adds_everything():
    ops = plan_ops([], [1, 2, 3])
    assert all(o.kind == "add" for o in ops)
    assert apply_ops([], ops) == [1, 2, 3]


def test_identical_queues_need_no_ops():
    assert plan_ops([1, 2, 3], [1, 2, 3]) == []


def test_drafted_player_is_removed():
    ops = plan_ops([1, 2, 3], [2, 3])
    assert [o.kind for o in ops] == ["remove"]
    assert ops[0].espn_id == 1
    assert apply_ops([1, 2, 3], ops) == [2, 3]


def test_the_common_case_is_cheap():
    """After an opposing pick: one player gone, one new name at the bottom.
    That must not cost a full rebuild."""
    current = list(range(1, 13))
    target = [p for p in current if p != 5] + [99]
    ops = plan_ops(current, target)
    assert len(ops) <= 3, f"expected a cheap diff, got {len(ops)} ops"
    assert apply_ops(current, ops) == target


def test_reorder_keeps_the_matching_prefix_and_rebuilds_the_rest():
    current = [1, 2, 3, 4, 5]
    target = [1, 2, 5, 3, 4]
    ops = plan_ops(current, target)
    # 1,2,5 are already in order -> keep; remove 3,4; append 3,4.
    assert [(o.kind, o.espn_id) for o in ops] == [
        ("remove", 3), ("remove", 4), ("add", 3), ("add", 4)]
    assert apply_ops(current, ops) == target


def test_opponent_takes_our_number_one_costs_one_add():
    """ESPN removes a drafted player from the queue itself; we only add."""
    current = [2, 3, 4, 5]          # 1 was just drafted and vanished
    target = [2, 3, 4, 5, 6]
    ops = plan_ops(current, target)
    assert [(o.kind, o.espn_id) for o in ops] == [("add", 6)]


def test_first_add_puts_the_top_pick_first_when_rebuilding():
    ops = plan_ops([1, 2, 3], [9, 8, 7])
    adds = [o for o in ops if o.kind == "add"]
    assert adds[0].espn_id == 9


def test_complete_reversal_still_produces_the_target():
    current = [1, 2, 3, 4, 5]
    target = [5, 4, 3, 2, 1]
    assert apply_ops(current, plan_ops(current, target)) == target


def test_top_of_queue_is_always_correct():
    """§3.3 — position 1 is the only entry ESPN's autopick reads first. If the
    diff ever leaves the wrong player there, a blown pick follows."""
    rng = random.Random(7)
    for _ in range(300):
        current = rng.sample(range(1, 30), rng.randint(0, 12))
        target = rng.sample(range(1, 30), rng.randint(1, 12))
        result = apply_ops(current, plan_ops(current, target))
        assert result[0] == target[0]


def test_property_result_always_equals_target():
    rng = random.Random(11)
    for _ in range(500):
        current = rng.sample(range(1, 40), rng.randint(0, 15))
        target = rng.sample(range(1, 40), rng.randint(0, 15))
        assert apply_ops(current, plan_ops(current, target)) == target


def test_rebuild_never_exceeds_one_remove_and_one_add_per_player():
    current = [1, 2, 3, 4]
    target = [4, 1, 2, 3]
    ops = plan_ops(current, target)
    assert apply_ops(current, ops) == target
    from collections import Counter
    per = Counter((o.kind, o.espn_id) for o in ops)
    assert max(per.values()) == 1
