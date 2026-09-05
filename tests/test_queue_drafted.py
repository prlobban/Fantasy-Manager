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
    q._consecutive_fails = 0
    q._cooldown = 0
    q._last_failure_was_click = False
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


# ── the circuit breaker ──────────────────────────────────────────────────────
#
# Practice run 2, 2026-09-04: a second practice room on the same ESPN account
# displaced ours, the page stopped accepting clicks, and the sync retried three
# players forever at ~7s each — not reading the room for three minutes. The
# queue is a safety net; one that eats the clock is worse than none.


class _Op:
    def __init__(self, espn_id, kind="add"):
        self.kind = kind
        self.espn_id = espn_id


def _failing_sync(n_players=8, succeed=False, click_failure=True):
    """`click_failure` distinguishes a page that refuses clicks from a page
    that is simply telling us a player has no QUEUE button."""
    q = _sync(ids=range(1, n_players + 1))

    def _add(pid):
        q._last_failure_was_click = (not succeed) and click_failure
        return succeed

    q._add = _add
    q._remove = lambda pid: True
    return q


def test_the_breaker_trips_after_consecutive_failures():
    q = _failing_sync()
    ok, tried = q.apply([_Op(i) for i in range(1, 9)])
    assert ok == 0
    # It stops at the threshold rather than grinding through all eight.
    assert tried <= QueueSync.BREAKER_THRESHOLD
    assert q._cooldown == QueueSync.BREAKER_COOLDOWN


def test_a_successful_add_resets_the_failure_run():
    q = _failing_sync(succeed=True)
    q.apply([_Op(i) for i in range(1, 9)])
    assert q._consecutive_fails == 0
    assert q._cooldown == 0


def test_sync_stands_down_while_the_breaker_is_open():
    """It must not even READ the queue — the whole point is spending no time."""
    q = _sync()
    q._cooldown = 2
    q.read_current = lambda: (_ for _ in ()).throw(AssertionError("should not read"))
    ops, ok = q.sync([1, 2, 3])
    assert (ops, ok) == ([], 0)
    assert q._cooldown == 1


def test_the_breaker_reopens_for_a_fresh_probe():
    """A page that recovered deserves a working queue again."""
    q = _sync()
    q._cooldown = 1
    q._consecutive_fails = 9
    q._add_attempts = {1: 4}
    q.read_current = lambda: []
    q.sync([1, 2, 3])
    assert q._cooldown == 0
    assert q._consecutive_fails == 0
    assert q._add_attempts == {}


def test_a_drafted_skip_is_not_evidence_the_page_is_broken():
    """Skipping a drafted player costs nothing and says nothing about health;
    counting it as a failure would trip the breaker on a healthy page."""
    q = _sync(ids=(1, 2, 3))
    q._drafted = {1, 2, 3}
    q._remove = lambda pid: True
    ok, tried = q.apply([_Op(i) for i in (1, 2, 3)])
    assert ok == 0
    assert q._consecutive_fails == 0
    assert q._cooldown == 0


def test_a_missing_queue_button_does_not_trip_the_breaker():
    """Practice run 3 tripped twice on a healthy page: late-draft rows render
    no buttons at all, and the first breaker counted that as the page refusing
    clicks. A page saying "no button here" is a page that is working."""
    q = _failing_sync(click_failure=False)
    ok, tried = q.apply([_Op(i) for i in range(1, 9)])
    assert ok == 0
    assert tried == 8                      # it worked through the whole list
    assert q._consecutive_fails == 0
    assert q._cooldown == 0
