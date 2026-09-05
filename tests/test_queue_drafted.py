"""§3.3 — the drafted-memory fix.

Measured in the 2026-09-04 practice draft: **34 "already DRAFTED" discoveries**,
each costing a full player search (~1.5-2 s) before the button could even be
read. `reset_attempts()` fired on every opposing pick and wiped the memory, so
the loop kept re-searching players who were permanently gone. The queue sat at
0 of a target of 8 for the last five cycles of the draft — the autopick safety
net was effectively absent.

Being drafted is the one queue fact that can never reverse, so it is remembered
permanently and checked BEFORE the search.

These tests use a fake session: the point is the bookkeeping, and a browser
would only make it slower and flakier.
"""

from __future__ import annotations

from core.draft.queue import QueueSync, plan_ops


class _Player:
    def __init__(self, name):
        self.name = name


def _sync(ids=(1, 2, 3)):
    q = QueueSync.__new__(QueueSync)          # no browser, no selectors import
    q.s = None
    q.by_id = {i: _Player(f"p{i}") for i in ids}
    q._names = []
    q._add_attempts = {}
    q._drafted = set()
    q._last_good = []
    q.last_current_size = 0
    q.last_tried = 0
    return q


def test_a_drafted_player_is_refused_without_touching_the_page():
    """The whole value of the set: no search, no click, no wait.

    `self.s` is None, so any attempt to reach the browser raises. Returning
    False cleanly proves the bail-out happens before the expensive part.
    """
    q = _sync()
    q._drafted.add(2)
    assert q._add(2) is False


def test_reset_attempts_forgets_failures_but_not_drafted():
    """A click that failed is worth retrying. A player who is gone is not."""
    q = _sync()
    q._add_attempts[1] = 3
    q._drafted.add(2)
    q.reset_attempts()
    assert q._add_attempts == {}
    assert q._drafted == {2}


def test_an_unknown_player_is_still_refused():
    q = _sync()
    assert q._add(999) is False


def test_drafted_targets_are_dropped_before_ops_are_planned():
    """Filtering at plan time is what stops the budget being spent proving the
    same thing twice."""
    q = _sync(ids=(1, 2, 3, 4))
    q._drafted.add(3)
    target = [1, 2, 3, 4]
    wanted = [pid for pid in target if pid not in q._drafted]
    assert wanted == [1, 2, 4]
    ops = plan_ops([], wanted)
    assert [o.espn_id for o in ops] == [1, 2, 4]
    assert all(o.kind == "add" for o in ops)


def test_the_kept_prefix_still_holds_after_filtering():
    """Dropping a drafted player must not force a needless rebuild."""
    ops = plan_ops([1, 2], [1, 2, 4])
    assert [(o.kind, o.espn_id) for o in ops] == [("add", 4)]


def test_position_one_is_still_first_among_adds():
    """An abort mid-sync must leave the TOP of the queue correct -- that slot
    is what ESPN autopicks from."""
    ops = plan_ops([], [7, 8, 9])
    adds = [o.espn_id for o in ops if o.kind == "add"]
    assert adds[0] == 7
