"""§3.10 — the budget, and the reason it is measured rather than assumed.

The league gives 90 seconds a pick, so seven picks "is" ten and a half minutes.
Managers do not use their clock. These tests pin the case that actually bites:
a fast room where the nominal gap is fine and the real one is not.

Rehearsal 3 lost four rounds to a background job that held the clock. A judge
run is far heavier than that queue sync was, which is why there is both a floor
(do not start what you cannot finish) and a kill (stop the moment it stops
mattering).
"""

from __future__ import annotations

import json

import pytest

from core.draft import clock as C


def tick(**over) -> C.Tick:
    base = {"next_overall": 17, "picks_until_our_turn": 7, "our_turn": False,
            "pace_s": 90.0, "at": 0.0}
    base.update(over)
    return C.Tick(**base)


# ── the budget ───────────────────────────────────────────────────────────────

def test_a_slow_room_is_capped_by_the_ceiling():
    """7 picks x 90s x 0.5 = 315s, over Pearce's 300s gate."""
    assert C.budget_for(tick(picks_until_our_turn=7, pace_s=90.0)) == 300.0


def test_a_fast_room_is_capped_by_the_room():
    """The case the nominal clock hides: 7 picks x 20s x 0.5 = 70s, not 315."""
    assert C.budget_for(tick(picks_until_our_turn=7, pace_s=20.0)) == 70.0


def test_too_little_time_means_do_not_start():
    """3 picks x 20s x 0.5 = 30s, under the 60s floor. A run that cannot
    finish spends the tokens and produces nothing."""
    assert C.budget_for(tick(picks_until_our_turn=3, pace_s=20.0)) == 0.0


def test_on_the_clock_means_no_budget():
    assert C.budget_for(tick(our_turn=True)) == 0.0


def test_one_pick_away_means_no_budget():
    """Starting here guarantees being killed mid-run."""
    assert C.budget_for(tick(picks_until_our_turn=1, pace_s=90.0)) == 0.0


def test_a_complete_draft_means_no_budget():
    assert C.budget_for(tick(complete=True)) == 0.0


# ── the pace measurement ─────────────────────────────────────────────────────

def test_pace_is_the_default_until_there_is_something_to_measure():
    p = C.Pace()
    p.saw(now=100.0)
    assert p.observed() == C.DEFAULT_PACE_S


def test_pace_tracks_a_fast_room():
    p = C.Pace()
    for i in range(6):
        p.saw(now=100.0 + i * 15.0)
    assert p.observed() == pytest.approx(15.0)


def test_pace_is_a_median_so_one_slow_manager_does_not_skew_it():
    p = C.Pace()
    times = [0.0, 10.0, 20.0, 200.0, 210.0, 220.0]   # one 180s think
    for t in times:
        p.saw(now=t)
    assert p.observed() == pytest.approx(10.0)


def test_a_batch_of_picks_read_in_one_poll_never_collapses_the_budget():
    """The loop can see 4 picks in a single read; they share a timestamp and
    would otherwise look like a 0-second pace."""
    p = C.Pace()
    p.saw(4, now=100.0)
    p.saw(now=130.0)
    assert p.observed() >= 3.0
    assert C.budget_for(tick(pace_s=p.observed())) >= 0.0


def test_pace_window_is_bounded():
    p = C.Pace(window=4)
    for i in range(50):
        p.saw(now=float(i))
    assert len(p._at) <= 5


# ── the clock file ───────────────────────────────────────────────────────────

class _Room:
    next_overall = 17
    picks_until_my_turn = 7
    current_round = 2


def test_clock_round_trips(tmp_path):
    p = C.Pace()
    for i in range(6):
        p.saw(now=float(i * 20))
    C.write(tmp_path, room=_Room(), our_turn=False, pace=p)
    got = C.read(tmp_path)
    assert got is not None
    assert got.next_overall == 17 and got.picks_until_our_turn == 7
    assert got.our_turn is False


def test_reading_a_missing_clock_is_none(tmp_path):
    assert C.read(tmp_path) is None


def test_a_corrupt_clock_is_none_not_a_crash(tmp_path):
    (tmp_path / C.CLOCK_FILE).write_text("{not json", encoding="utf-8")
    assert C.read(tmp_path) is None


def test_a_truncated_clock_is_none_not_a_crash(tmp_path):
    (tmp_path / C.CLOCK_FILE).write_text(json.dumps({"our_turn": True}),
                                         encoding="utf-8")
    assert C.read(tmp_path) is None


def test_write_leaves_no_temp_file(tmp_path):
    """The judge polls this file constantly; it must never catch a partial."""
    C.write(tmp_path, room=_Room(), our_turn=False, pace=C.Pace())
    assert not list(tmp_path.glob("*.tmp"))


def test_a_write_failure_never_raises(tmp_path):
    """§10.6 — a logging seam must not be able to cost a pick."""
    C.write(tmp_path / "does" / "not" / "exist" / "x", room=_Room(),
            our_turn=False, pace=C.Pace())


def test_stale_seconds_is_reported(tmp_path):
    C.write(tmp_path, room=_Room(), our_turn=False, pace=C.Pace())
    got = C.read(tmp_path)
    assert got.stale_s < 5.0


# ── the kill ─────────────────────────────────────────────────────────────────

def test_a_long_agent_run_is_killed_when_our_turn_arrives(tmp_path, monkeypatch):
    """The property the whole two-process design rests on.

    The loop never waits for the judge, but a judge still running while we are
    on the clock is burning tokens on an answer that is already worthless — and
    on a shared machine, competing for it. So the watcher kills it.
    """
    import subprocess
    import sys
    import threading
    import time

    # A stand-in for `claude -p`: sleeps far longer than the budget.
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    proc_box = {"proc": proc}

    C.write(tmp_path, room=_Room(), our_turn=False, pace=C.Pace())

    killed = threading.Event()

    def watcher():
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            time.sleep(0.05)
            t = C.read(tmp_path)
            if t and (t.our_turn or t.picks_until_our_turn <= 1):
                killed.set()
                proc_box["proc"].kill()
                return

    th = threading.Thread(target=watcher, daemon=True)
    th.start()

    time.sleep(0.2)

    # The room reaches our pick.
    class _OnClock:
        next_overall = 17
        picks_until_my_turn = 0
        current_round = 2

    C.write(tmp_path, room=_OnClock(), our_turn=True, pace=C.Pace())

    proc.wait(timeout=10)
    th.join(timeout=5)

    assert killed.is_set(), "the watcher never saw our turn"
    assert proc.returncode != 0, "the agent process survived our turn"
