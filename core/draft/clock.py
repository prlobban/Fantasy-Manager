"""§3.10 — the seam between the draft loop and the judge.

The loop and the judge are separate processes on purpose: §10.2 says nothing on
a clock may block on a model, and the cheapest way to guarantee that is for the
loop to have no way to wait. It writes a small file every cycle; the judge
reads it and decides for itself whether it has room to think.

**Why pace is measured rather than assumed.** The league is 90 seconds a pick,
so seven picks "is" ten and a half minutes. In practice managers pick in twenty
seconds and seven picks is two and a half — under the budget the judge would
otherwise have granted itself. Rehearsal 3 lost four rounds to a background job
that held the clock; a research call is far heavier than that queue sync was.
So the budget is derived from what the room is actually doing, and the judge is
killed outright the moment we are on the clock.
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from core.model.priors import priors

log = logging.getLogger(__name__)

CLOCK_FILE = "clock.json"

#: Before the room has given us anything to measure, assume managers are quick.
#: Erring fast means an early judge run is skipped; erring slow means it is
#: killed mid-flight. Skipping is cheaper.
DEFAULT_PACE_S = 25.0


class Pace:
    """Median seconds per pick over a sliding window of room picks."""

    def __init__(self, window: int | None = None) -> None:
        self.window = window or int(priors().get("judge.pace_window"))
        self._at: deque[float] = deque(maxlen=self.window + 1)

    def saw(self, n_picks: int = 1, *, now: float | None = None) -> None:
        t = now if now is not None else time.monotonic()
        for _ in range(max(1, n_picks)):
            self._at.append(t)

    def observed(self) -> float:
        if len(self._at) < 3:
            return DEFAULT_PACE_S
        gaps = [b - a for a, b in zip(self._at, list(self._at)[1:], strict=False)]
        gaps = [g for g in gaps if g > 0.0]
        if not gaps:
            return DEFAULT_PACE_S
        gaps.sort()
        mid = len(gaps) // 2
        median = gaps[mid] if len(gaps) % 2 else (gaps[mid - 1] + gaps[mid]) / 2
        # A batch of picks read in one poll looks instantaneous; never let that
        # collapse the budget to zero.
        return max(3.0, median)


@dataclass
class Tick:
    next_overall: int
    picks_until_our_turn: int
    our_turn: bool
    pace_s: float
    at: float
    round_num: int = 0
    complete: bool = False

    @property
    def stale_s(self) -> float:
        return max(0.0, time.time() - self.at)


def write(draft_dir: Path, *, room, our_turn: bool, pace: Pace,
          complete: bool = False) -> None:
    """One cycle's worth of state, atomically. Never raises."""
    try:
        payload = {
            "next_overall": room.next_overall,
            "picks_until_our_turn": room.picks_until_my_turn,
            "our_turn": bool(our_turn),
            "pace_s": round(pace.observed(), 1),
            "round_num": room.current_round,
            "complete": bool(complete),
            "at": time.time(),
        }
        p = draft_dir / CLOCK_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(p)
    except Exception as e:                      # a logging seam must not cost a pick
        log.debug("clock write failed: %s", e)


def read(draft_dir: Path) -> Tick | None:
    p = draft_dir / CLOCK_FILE
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return Tick(
            next_overall=int(raw["next_overall"]),
            picks_until_our_turn=int(raw["picks_until_our_turn"]),
            our_turn=bool(raw.get("our_turn")),
            pace_s=float(raw.get("pace_s") or DEFAULT_PACE_S),
            at=float(raw.get("at") or 0.0),
            round_num=int(raw.get("round_num") or 0),
            complete=bool(raw.get("complete")),
        )
    except (KeyError, TypeError, ValueError):
        return None


def budget_for(tick: Tick) -> float:
    """Seconds the judge may spend before our turn. 0.0 means do not start.

    The smaller of Pearce's ceiling and what the room's own pace allows, halved
    for safety. Below the floor there is no point starting: a run that cannot
    finish spends the tokens and produces nothing.
    """
    p = priors()
    ceiling = float(p.get("judge.max_budget_s"))
    floor = float(p.get("judge.min_budget_s"))
    safety = float(p.get("judge.pace_safety"))

    if tick.our_turn or tick.complete or tick.picks_until_our_turn <= 1:
        return 0.0
    room_allows = tick.picks_until_our_turn * tick.pace_s * safety
    budget = min(ceiling, room_allows)
    return budget if budget >= floor else 0.0
