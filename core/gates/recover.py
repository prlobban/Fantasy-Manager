"""Heal a stale selector mid-write, then retry — once, and only when it is safe.

§10.6 says core fails closed, and full self-healing does not repeal that. It
changes what "closed" means: the system stops asking a human to go find the new
class name, and starts finding it itself. It does NOT start guessing about
whether a write already landed.

## The one thing that can go badly wrong

`add_drop` spends one of three weekly adds (§5.7). A blind retry after a failure
can spend a second one on the same player, and on 2026-09-12 this system already
learned the hard way that ESPN commits the add leg the instant it sees an open
bench spot — the drop leg can fail with the add already done. That is why the
old code refused to retry at all:

    "I did not retry, because a blind retry on add_drop could double-spend one
     of the three weekly adds."  — Polaris, #fantasy, 2026-09-14

Refusing to retry is safe and useless. Retrying blindly is useful and unsafe.
The way out is that the selector group NAMES THE STEP the action died on, so we
know what had already happened:

* **Pre-commit groups** — the search box, the row, the Add button itself. The
  failure happened before any click that changes the league. Nothing landed.
  Retry is free.
* **Post-commit groups** — the drop button, the Continue button. These are only
  reached AFTER Add was clicked, so the add may already be on the roster.
  Never retried on faith: the roster is re-read from ESPN's API, and the retry
  only proceeds if the league genuinely did not change.

The read API is the right authority for that check because it is a different
code path from the browser that just failed. Asking the broken thing whether it
broke is not a check.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from core.model.schema import ActionKind

log = logging.getLogger(__name__)

#: Selector groups reached BEFORE any league-changing click. A failure here
#: means nothing committed, so a retry cannot duplicate anything.
PRE_COMMIT: frozenset[str] = frozenset({
    "PLAYER_SEARCH", "PLAYER_TABLE_ROW", "ADD_PLAYER_BUTTON",
    "LINEUP_SLOT_ROW", "LINEUP_MOVE_BUTTON", "LINEUP_HERE_BUTTON",
    "TEAM_DROP_TOOLBAR",
    "TRADE_PROPOSE_BUTTON", "TRADE_PLAYER_CHECKBOX", "TRADE_REVIEW_BUTTON",
    "DRAFT_SEARCH", "DRAFT_PLAYER_ROW",
    "QUEUE_CONTAINER", "QUEUE_ROW", "QUEUE_ADD_BUTTON", "QUEUE_REMOVE_BUTTON",
    # Draft-room READ groups. These parse the board, the pick train and the
    # clock; none of them actuates anything, so there is nothing a retry could
    # duplicate. Classified explicitly rather than left out, because `assess`
    # refuses to guess and an unclassified group can never be retried at all.
    "DRAFT_BOARD_CELL_ANY", "DRAFT_PICK_ROW", "DRAFT_PICK_TRAIN",
    "DRAFT_ON_CLOCK", "DRAFT_TIMER",
})

#: Groups only reachable after a click that may already have changed the
#: league. Retry requires positive proof from the read API that it did not.
POST_COMMIT: frozenset[str] = frozenset({
    "DROP_PLAYER_BUTTON", "CONFIRM_BUTTON",
})

#: One heal-and-retry per action, per sweep. A selector that breaks twice in one
#: action is not a stale class name — it is something this loop does not
#: understand, and the right response to that is a human, not a third attempt.
MAX_ATTEMPTS = 1


@dataclass
class Recovery:
    attempted: bool
    healed: bool = False
    retried: bool = False
    group: str | None = None
    detail: str = ""

    def __str__(self) -> str:
        if not self.attempted:
            return f"no recovery attempted: {self.detail}"
        if self.retried:
            return f"healed {self.group} and retried: {self.detail}"
        if self.healed:
            return f"healed {self.group} but did not retry: {self.detail}"
        return f"could not heal {self.group}: {self.detail}"


def _league_unchanged(action) -> tuple[bool, str]:
    """Did the failed write leave the league alone?

    Re-reads the roster from ESPN's API — deliberately NOT from the browser
    session that just failed — and compares it against what the action intended.
    Returns (unchanged, explanation). Fails closed: any doubt reads as "changed",
    which blocks the retry.
    """
    args = action.args or {}
    try:
        from core.espn import league_state as ls

        s = ls.snapshot()
        mine = {p.espn_id for p in s.me.roster}
    except Exception as e:
        return False, f"could not re-read the roster to check ({e}) — refusing to retry"

    if action.kind in (ActionKind.ADD_DROP, ActionKind.WAIVER_CLAIM):
        add_id, drop_id = args.get("add_id"), args.get("drop_id")
        if add_id is not None and add_id in mine:
            return False, (f"the add already landed — player {add_id} is on the roster. "
                           "Retrying would spend a second weekly add (§5.7)")
        if drop_id is not None and drop_id not in mine:
            return False, (f"player {drop_id} is already off the roster — the drop leg "
                           "committed, so this is a partial write, not a clean failure")
        return True, "roster is unchanged: the add did not land"

    if action.kind == ActionKind.PROPOSE_TRADE:
        # A trade that got as far as a post-commit selector may have been sent.
        # There is no cheap authoritative read for "did my offer go out", so
        # this never retries — §6.1 allows one open offer per manager and a
        # duplicate would burn the week's proposal cap.
        return False, "cannot prove the offer was not sent — trades are never blind-retried"

    return True, "no league-changing step had been reached"


def assess(action, exc, *, attempts: int = 0) -> tuple[bool, str]:
    """Should this failure be healed and retried? (decision, why)"""
    group = getattr(exc, "group", None)
    if not group:
        return False, "not a selector failure — nothing to heal"
    if attempts >= MAX_ATTEMPTS:
        return False, f"already attempted {attempts}x; a second failure is not a stale selector"
    if getattr(exc, "receipt", None) is not None:
        return False, "partial write: something already committed, retry would duplicate it"

    from core.browser import groups as G

    if group not in G.HEALABLE:
        return False, f"{group} is not healable — this write has no reverse (§10.6)"
    if group in PRE_COMMIT:
        return True, f"{group} fails before any league-changing click"
    if group in POST_COMMIT:
        ok, why = _league_unchanged(action)
        return ok, why
    return False, f"{group} is not classified pre- or post-commit — refusing to guess"


def recover(action, exc, performer, *, attempts: int = 0):
    """Heal the stale selector and re-run `performer`. Raises on failure.

    Returns the receipt from the successful retry. The caller keeps its own
    exception handling — this either produces a receipt or raises, exactly like
    the original performer did.
    """
    from core.browser import groups as G
    from core.browser import selfheal
    from core.browser.session import EspnSession

    group = getattr(exc, "group", None)
    ok, why = assess(action, exc, attempts=attempts)
    if not ok:
        raise RecoveryRefused(why)

    g = G.get(group)
    target = g.target if g else G.TEAM

    # A fresh session: the one that failed may be on a half-open modal, and
    # discovery has to see the page in its normal state to score it correctly.
    s = EspnSession(headless=True)
    s.start()
    try:
        selfheal.goto_target(s, target)
        s.page.wait_for_timeout(6000)
        s.dismiss_overlays()
        h = selfheal.heal_group(s.page, group)
    finally:
        s.close()

    if not h.healed:
        raise RecoveryRefused(f"heal failed: {h.why}")

    log.warning("recovered %s: %s — retrying %s", group, h.candidate, action.kind.value)
    return h, performer()


class RecoveryRefused(RuntimeError):
    """The failure was not one that may be healed-and-retried."""
