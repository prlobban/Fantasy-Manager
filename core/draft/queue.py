"""§3.3 — the draft queue. The load-bearing mechanism of the whole draft.

ESPN autopicks from the top of the live Player Queue before falling back to its
own list. So if the queue is always correct, a failure of the click leg degrades
to "ESPN drafted our own #1" instead of "ESPN drafted its default." There is no
state of the world in which we get ESPN's list.

Two halves, deliberately separated:

  plan_ops()  — pure. Given the current queue and the target, produce the
                minimal ordered list of add/remove/move operations. Fully
                testable with no browser.
  QueueSync   — executes those ops against the page.

Minimal diffing matters on the clock: rebuilding a 12-deep queue from scratch
after every pick in the room would be ~24 UI operations per pick. A diff is
usually 1–3.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

log = logging.getLogger(__name__)

OpKind = Literal["remove", "add", "move"]


@dataclass(frozen=True)
class QueueOp:
    kind: OpKind
    espn_id: int
    #: Target index for add/move. Ignored for remove.
    index: int = 0
    reason: str = ""


def plan_ops(current: list[int], target: list[int]) -> list[QueueOp]:
    """Minimal ops to turn `current` into `target`.

    Strategy:
      1. Remove anything not in the target (drafted players, demoted names).
      2. Add anything missing. **An add lands at the END of the queue** — that
         is what ESPN's "add to queue" control does, and the model here has to
         match the UI or the moves computed from it are wrong. The first
         version modelled adds as insert-at-index; for current=[A],
         target=[B, A] it emitted one add and zero moves, and the live queue
         ended up [A, B] with the wrong player on top. Caught 2026-09-04.
      3. Walk the target left to right; anything already in the right place
         costs nothing, anything else is moved into position.

    Step 3 is a single forward pass rather than anything cleverer, and that is
    deliberate. An earlier version anchored on the longest common subsequence to
    shave a move or two; the anchors went stale as each move shifted the list,
    and a property test caught it producing the wrong final order. On the clock,
    a provably correct queue beats a marginally shorter op list.

    Order is preserved exactly, because position 1 is the only entry ESPN's
    autopick actually reads first.
    """
    ops: list[QueueOp] = []
    target_set = set(target)

    # 1 — removals
    working = []
    for pid in current:
        if pid in target_set:
            working.append(pid)
        else:
            ops.append(QueueOp("remove", pid, reason="not in target"))

    # 2 — additions. Each one appends; step 3 moves it into place.
    working_set = set(working)
    for idx, pid in enumerate(target):
        if pid not in working_set:
            ops.append(QueueOp("add", pid, index=idx, reason="new to queue"))
            working.append(pid)
            working_set.add(pid)

    # 3 — reorder, forward pass. Entries already correct are skipped for free.
    for idx, pid in enumerate(target):
        cur_idx = working.index(pid)
        if cur_idx != idx:
            ops.append(QueueOp("move", pid, index=idx, reason=f"{cur_idx}->{idx}"))
            working.pop(cur_idx)
            working.insert(idx, pid)

    return ops


class QueueSync:
    """Applies queue ops to the live draft room.

    Every method returns whether it believes it succeeded; the caller re-reads
    and re-plans rather than trusting a single op. On the clock, a wrong queue
    is worse than a slow one.
    """

    #: Give up re-adding a player after this many attempts that never showed
    #: up in a re-read. A name the queue renders differently from the board
    #: would otherwise be re-added every cycle, and on ESPN the add control is
    #: a toggle — a second click can REMOVE him.
    MAX_ADD_ATTEMPTS = 2

    def __init__(self, session, by_id: dict[int, object] | None = None):
        from core.browser import selectors as S

        self.s = session
        self.by_id = by_id or {}
        # Longest name first, so "Mike Evans Jr." cannot be claimed by "Mike Evans".
        self._names: list[tuple[str, int]] = sorted(
            ((S.norm(getattr(pl, "name", "") or ""), pid) for pid, pl in self.by_id.items()),
            key=lambda np: -len(np[0]),
        )
        self._add_attempts: dict[int, int] = {}
        self._last_good: list[int] = []

    # ── read ─────────────────────────────────────────────────────────────────

    def read_current(self) -> list[int] | None:
        """Player ids currently in the queue, in order.

        None means "could not find the queue container" — distinct from an
        empty queue. sync() skips a None read rather than re-adding everything,
        because a queue that flickered out of the DOM for one render is not a
        queue that emptied.
        """
        from core.browser import selectors as S

        rows = S.first_present(self.s.page, S.QUEUE_ROW)
        if rows is None:
            log.warning("queue container not found — selectors may be stale")
            return None
        out: list[int] = []
        for i in range(rows.count()):
            try:
                text = rows.nth(i).inner_text() or ""
            except Exception:
                continue
            pid = self._id_from_text(text)
            if pid is not None and pid not in out:
                out.append(pid)
        return out

    def _id_from_text(self, text: str) -> int | None:
        from core.browser import selectors as S

        norm = S.norm(text)
        if not norm:
            return None
        for name, pid in self._names:
            if name and name in norm:
                return pid
        return None

    # ── write ────────────────────────────────────────────────────────────────

    def apply(self, ops: list[QueueOp]) -> tuple[int, int]:
        """Run the ops. Returns (succeeded, attempted)."""
        ok = 0
        for op in ops:
            try:
                if op.kind == "remove":
                    done = self._remove(op.espn_id)
                elif op.kind == "add":
                    done = self._add(op.espn_id)
                else:
                    done = self._move(op.espn_id, op.index)
                ok += int(done)
            except Exception as e:
                log.warning("queue op %s %s failed: %s", op.kind, op.espn_id, e)
        return ok, len(ops)

    def _add(self, espn_id: int) -> bool:
        from core.browser import selectors as S

        pl = self.by_id.get(espn_id)
        if pl is None:
            return False
        attempts = self._add_attempts.get(espn_id, 0)
        if attempts >= self.MAX_ADD_ATTEMPTS:
            log.warning("not re-adding %s to the queue: %d attempts never showed up "
                        "in a re-read (name mismatch?)", getattr(pl, "name", espn_id), attempts)
            return False
        self._add_attempts[espn_id] = attempts + 1

        page = self.s.page
        name = getattr(pl, "name", "")
        box = S.first_present(page, S.DRAFT_SEARCH)
        if box is None:
            return False
        box.first.fill(name)
        page.wait_for_timeout(400)
        # The button must be in the row that carries THIS player's name. The
        # first add-button on the page after a search is not necessarily his.
        btn = S.in_row_with(page, name, S.QUEUE_ADD_BUTTON)
        if btn is None:
            return False
        btn.first.click()
        return True

    def _remove(self, espn_id: int) -> bool:
        from core.browser import selectors as S

        row = self._queue_row_for(espn_id)
        if row is None:
            return False
        btn = row.locator(S.QUEUE_REMOVE_BUTTON)
        if btn.count() == 0:
            return False
        btn.first.click()
        return True

    def _move(self, espn_id: int, index: int) -> bool:
        """Reorder is drag-and-drop in ESPN's queue, which is the least reliable
        interaction available. Preferred fallback: remove and re-add, which puts
        the player back at a known position."""
        from core.browser import selectors as S

        row = self._queue_row_for(espn_id)
        if row is None:
            return False
        try:
            rows = S.first_present(self.s.page, S.QUEUE_ROW)
            if rows is None or index >= rows.count():
                return False
            row.first.drag_to(rows.nth(index))
            return True
        except Exception:
            # Remove + re-add puts him at the END, not at `index`. It is only
            # correct when the target is the last slot; otherwise say so.
            if self._remove(espn_id) and self._add(espn_id):
                if index < len(self._last_good) - 1:
                    log.warning("move fallback for %s landed at the end, wanted %d — "
                                "the next sync will retry", espn_id, index)
                return True
            return False

    def _queue_row_for(self, espn_id: int):
        from core.browser import selectors as S

        pl = self.by_id.get(espn_id)
        if pl is None:
            return None
        rows = S.first_present(self.s.page, S.QUEUE_ROW)
        if rows is None:
            return None
        want = S.norm(getattr(pl, "name", ""))
        for i in range(rows.count()):
            try:
                if want and want in S.norm(rows.nth(i).inner_text() or ""):
                    return rows.nth(i)
            except Exception:
                continue
        return None

    # ── the thing the draft loop calls ───────────────────────────────────────

    def sync(self, target: list[int], *, dry_run: bool = False) -> tuple[list[QueueOp], int]:
        """Bring the live queue in line with `target`. Returns (ops, succeeded).

        Cheap when nothing changed (one DOM read, zero ops), so the loop calls
        it every cycle rather than only after a new pick — that is what retries
        an op that failed to land, and what fixes a move fallback that parked
        someone at the end.
        """
        current = self.read_current()
        if current is None:
            log.warning("queue unreadable this cycle — leaving it as it stands")
            return [], 0
        # Anyone the queue now shows has proven his name matches; reset his
        # add budget so a later legitimate re-add is not refused.
        for pid in current:
            self._add_attempts.pop(pid, None)
        self._last_good = current

        ops = plan_ops(current, target)
        if not ops:
            return [], 0
        log.info("queue sync: %d ops (current=%d, target=%d)", len(ops), len(current), len(target))
        if dry_run:
            return ops, 0
        ok, _ = self.apply(ops)
        return ops, ok
