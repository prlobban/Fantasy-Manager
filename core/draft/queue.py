"""§3.3 — the draft queue. The load-bearing mechanism of the whole draft.

ESPN autopicks from the top of the live Player Queue before falling back to its
own list. So if the queue is always correct, a failure of the click leg degrades
to "ESPN drafted our own #1" instead of "ESPN drafted its default." There is no
state of the world in which we get ESPN's list.

Two halves, deliberately separated:

  plan_ops()  — pure. Given the current queue and the target, produce the
                ordered list of remove/add operations. Fully testable with no
                browser.
  QueueSync   — executes those ops against the page.

Diffing matters on the clock: a full rebuild of an 8-deep queue is ~16 UI
operations at ~2 s each. The common case after an opposing pick is 0–1.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

log = logging.getLogger(__name__)

OpKind = Literal["remove", "add"]


@dataclass(frozen=True)
class QueueOp:
    kind: OpKind
    espn_id: int
    #: Position the add is expected to land at. Ignored for remove.
    index: int = 0
    reason: str = ""


def _kept_prefix_len(current: list[int], target: list[int]) -> int:
    """Largest k such that target[:k] appears in `current` in that order."""
    best = 0
    for k in range(1, len(target) + 1):
        it = iter(current)
        if all(any(c == t for c in it) for t in target[:k]):
            best = k
        else:
            break
    return best


def plan_ops(current: list[int], target: list[int]) -> list[QueueOp]:
    """Ops to turn `current` into `target`, using REMOVE and ADD only.

    Verified 2026-09-04 in live practice rooms: "add to queue" APPENDS, "Remove"
    works, and drag-and-drop reorder lands about one time in seven. So the
    plan never relies on a move. The only orderings reachable with remove +
    append are: (some subsequence of current, in its current order) followed
    by (new appends). Hence:

      1. Keep the longest prefix of `target` that already sits in `current`
         in the right relative order.
      2. Remove everything else that is in `current`.
      3. Append the rest of `target`, in target order — so position 1 is
         correct after the FIRST add even if the rest never lands.

    Common cases: an opponent takes someone deep in our queue -> one remove.
    Our #1 is drafted and the rest shifts up -> one add (ESPN drops drafted
    players from the queue itself). A re-rank that changes the top -> a
    rebuild, which is the price of a queue that is provably right.
    """
    k = _kept_prefix_len(current, target)
    keep = set(target[:k])
    ops: list[QueueOp] = [
        QueueOp("remove", pid, reason="not kept") for pid in current if pid not in keep
    ]
    ops += [
        QueueOp("add", pid, index=k + i, reason="append in target order")
        for i, pid in enumerate(target[k:])
    ]
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
    MAX_ADD_ATTEMPTS = 4

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
        #: Players the DOM has told us are DRAFTED. Permanent, and deliberately
        #: NOT cleared by reset_attempts().
        #:
        #: Being drafted is not a transient failure — it is the one queue fact
        #: that can never reverse. Before this set existed, `reset_attempts()`
        #: fired on every opposing pick and wiped the memory, so the loop
        #: re-searched the same dead players every cycle. Measured in the
        #: 2026-09-04 practice draft: **34 "already DRAFTED" discoveries**, each
        #: costing a full search (~1.5-2 s) before the button could even be
        #: read. That was the whole reason the queue sat at 0.
        self._drafted: set[int] = set()
        self._last_good: list[int] = []
        self.last_current_size = 0
        #: Ops actually attempted by the last apply(). An op the budget or an
        #: abort never reached is not a failure, and reporting it as one made
        #: every practice log read like the queue was broken.
        self.last_tried = 0

    def ensure_autopick_off(self) -> bool:
        """Switch ESPN's Autopick toggle OFF if it is on. Returns True if it
        had to.

        Verified 2026-09-04 (rehearsal #4): after ONE missed pick ESPN flips
        the team to Autopick, and every later turn is filled instantly from
        the top of the queue — before any click can run. That is the safety
        net in its purest form, but it takes the decision away from the loop
        (no re-rank after the pick before ours). So the loop turns it back
        off. If the toggle cannot be found, nothing happens.
        """
        from core.browser import selectors as S

        try:
            box = self.s.page.locator(S.QUEUE_AUTOPICK_TOGGLE)
            if box.count() == 0 or not box.first.is_checked():
                return False
            # The input is visually hidden; its label is the click target.
            label = box.first.locator("xpath=ancestor::label[1]")
            (label if label.count() else box).first.click(force=True, timeout=2_000)
            self.s.page.wait_for_timeout(400)
            still = box.first.is_checked()
            log.warning("ESPN had Autopick ON for us — switched it %s",
                        "off" if not still else "OFF FAILED (still on)")
            return not still
        except Exception as e:
            log.info("autopick check failed: %s", str(e)[:80])
            return False

    def reset_attempts(self) -> None:
        """The room moved on (a new pick landed): earlier failures are stale.

        `_drafted` is NOT cleared — a click that failed is worth retrying, a
        player who is gone is not.
        """
        self._add_attempts.clear()

    # ── read ─────────────────────────────────────────────────────────────────

    def read_current(self) -> list[int] | None:
        """Player ids currently in the queue, in order.

        None means "could not find the queue container" — distinct from an
        empty queue. sync() skips a None read rather than re-adding everything,
        because a queue that flickered out of the DOM for one render is not a
        queue that emptied.

        Verified 2026-09-04: every queue row carries the ESPN player id in
        `data-drag-id`. That is the read. Names are only a fallback, and they
        are abbreviated in the queue ("J. Taylor"), so a name match is a last
        resort, not the mechanism.
        """
        from core.browser import selectors as S

        page = self.s.page
        if S.first_present(page, S.QUEUE_CONTAINER) is None:
            log.warning("queue container not found — selectors may be stale")
            return None
        out: list[int] = []
        rows = page.locator(S.QUEUE_ROW)
        for i in range(rows.count()):
            row = rows.nth(i)
            pid: int | None = None
            try:
                raw = row.get_attribute(S.QUEUE_ROW_ID_ATTR)
                if raw and raw.lstrip("-").isdigit():
                    pid = int(raw)
            except Exception:
                pass
            if pid is None:
                try:
                    pid = self._id_from_text(row.inner_text() or "")
                except Exception:
                    pid = None
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

    def apply(self, ops: list[QueueOp], *, budget_s: float | None = None,
              abort=None) -> tuple[int, int]:
        """Run the ops. Returns (succeeded, attempted).

        Time-boxed, and abortable. Rehearsal #3 (2026-09-04) lost a pick to a
        sync that started two seconds before our turn and ran through the
        whole clock; the click leg never got a look in and ESPN autopicked
        from the queue. So a sync stops issuing ops when `budget_s` runs out
        or when `abort()` says we are on the clock — the rest waits for the
        next cycle. The queue being a little behind is fine; the click being
        blocked is not.
        """
        import time

        ok = 0
        tried = 0
        start = time.monotonic()
        for i, op in enumerate(ops):
            if budget_s is not None and time.monotonic() - start > budget_s:
                log.info("queue sync: budget of %.0fs spent after %d/%d ops — rest next cycle",
                         budget_s, i, len(ops))
                break
            if abort is not None and abort():
                log.info("queue sync: aborted after %d/%d ops — we are on the clock", i, len(ops))
                break
            tried += 1
            try:
                if op.kind == "remove":
                    done = self._remove(op.espn_id)
                else:
                    done = self._add(op.espn_id)
                ok += int(done)
            except Exception as e:
                log.warning("queue op %s %s failed: %s", op.kind, op.espn_id, e)
        self.last_tried = tried
        return ok, tried

    def _add(self, espn_id: int) -> bool:
        from core.browser import selectors as S

        pl = self.by_id.get(espn_id)
        if pl is None:
            return False
        if espn_id in self._drafted:
            # Free. The whole point of the set: no search, no click, no wait.
            return False
        attempts = self._add_attempts.get(espn_id, 0)
        if attempts >= self.MAX_ADD_ATTEMPTS:
            log.warning("not re-adding %s to the queue: %d clicks never showed up "
                        "in a re-read", getattr(pl, "name", espn_id), attempts)
            return False

        page = self.s.page
        name = getattr(pl, "name", "")
        # The table is virtualised: the player must be searched into view.
        # Search applies on ENTER (verified), and it must be cleared after,
        # or the table stays filtered to one row for the rest of the draft.
        if not S.search_player(page, name, settle_ms=700):
            return False
        try:
            row = S.player_row(page, name)
            if row is None:
                log.warning("queue add: no row for %r after search", name)
                return False
            btn = row.locator(S.QUEUE_ADD_BUTTON)
            if btn.count() == 0:
                # Already queued, just drafted (the DOM knows before our
                # reader does), or we are on the clock and the button reads
                # DRAFT. Nothing to add here; say which.
                labels = [t.strip() for t in row.locator("button").all_inner_texts()]
                log.info("queue add: %r has no QUEUE button (%s)", name, labels or "none")
                if any("DRAFTED" in lb.upper() for lb in labels):
                    self._drafted.add(espn_id)
                    self._add_attempts[espn_id] = self.MAX_ADD_ATTEMPTS
                return False
            # Verified 2026-09-04: a click that "succeeds" does not always add.
            # Prove it by re-reading the queue; one retry with a forced click.
            # Only a click that actually happened counts against the budget;
            # a row that was not there is a transient, not evidence.
            self._add_attempts[espn_id] = attempts + 1
            # The table re-renders on every pick in the room, and a click on a
            # button that is being re-mounted is lost. Three escalating tries:
            # a normal click, a forced one, then a DOM-dispatched click that
            # needs no hit-testing at all.
            for attempt in range(3):
                try:
                    if attempt == 0:
                        btn.first.click(timeout=2_500)
                    elif attempt == 1:
                        btn.first.click(force=True, timeout=2_500)
                    else:
                        btn.first.dispatch_event("click", timeout=2_000)
                except Exception as e:
                    log.info("queue add: click %d on %r failed: %s", attempt + 1, name,
                             str(e).splitlines()[0][:90])
                page.wait_for_timeout(600)
                if espn_id in (self.read_current() or []):
                    return True
                # He may have been drafted in the meantime: then stop trying.
                labels = [t.strip().upper() for t in row.locator("button").all_inner_texts()]
                if any("DRAFTED" in lb for lb in labels):
                    self._drafted.add(espn_id)
                    self._add_attempts[espn_id] = self.MAX_ADD_ATTEMPTS
                    return False
            log.warning("queue add: %r clicked three ways, never appeared in the queue", name)
            return False
        finally:
            S.clear_search(page)

    def _remove(self, espn_id: int) -> bool:
        from core.browser import selectors as S

        row = self._queue_row_for(espn_id)
        if row is None:
            return False
        btn = row.locator(S.QUEUE_REMOVE_BUTTON)
        if btn.count() == 0:
            return False
        btn.first.click()
        self.s.page.wait_for_timeout(400)
        return True

    def _queue_row_for(self, espn_id: int):
        from core.browser import selectors as S

        page = self.s.page
        by_attr = page.locator(f"{S.QUEUE_CONTAINER.split(',')[0].strip()} "
                               f"tr[{S.QUEUE_ROW_ID_ATTR}='{espn_id}']")
        try:
            if by_attr.count():
                return by_attr.first
        except Exception:
            pass
        pl = self.by_id.get(espn_id)
        if pl is None:
            return None
        rows = page.locator(S.QUEUE_ROW)
        want = S.norm(getattr(pl, "name", ""))
        for i in range(rows.count()):
            try:
                if want and want in S.norm(rows.nth(i).inner_text() or ""):
                    return rows.nth(i)
            except Exception:
                continue
        return None

    # ── the thing the draft loop calls ───────────────────────────────────────

    def sync(self, target: list[int], *, dry_run: bool = False,
             budget_s: float | None = 25.0, abort=None) -> tuple[list[QueueOp], int]:
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
        #: Size of the queue as this sync found it. Read by the draft log so
        #: it does not have to hit the DOM again while the clock is running.
        self.last_current_size = len(current)

        # A player the DOM has already shown as DRAFTED can never be queued.
        # Filtering here means no op is even planned for him, which is what
        # stops the sync spending its budget proving the same thing twice.
        wanted = [pid for pid in target if pid not in self._drafted]
        dropped = len(target) - len(wanted)
        if dropped:
            log.info("queue sync: skipped %d already-drafted target(s)", dropped)

        ops = plan_ops(current, wanted)
        self.last_tried = 0
        if not ops:
            return [], 0
        log.info("queue sync: %d ops (current=%d, target=%d)", len(ops), len(current), len(wanted))
        if dry_run:
            return ops, 0
        ok, tried = self.apply(ops, budget_s=budget_s, abort=abort)
        return ops, ok
