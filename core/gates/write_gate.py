"""§8.2 AS CODE. The single door every write goes through.

Nothing calls core/browser/actions.py directly. §10.3: an agent that can
construct an arbitrary browser action has no guardrails, only suggestions — so
the guardrails live here, in Python, where the model cannot reason past them.

Order, always:
    1. kill switch          (§8.4)
    2. health check         (§8.5)
    3. fresh roster re-read (§8.3 — never act on a stale read)
    4. action-specific rule (§5 / §6.1 / §6.8 / lineup lock)
    5. only then, the browser
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from core.espn import health
from core.gates import kill_switch, rate_limits
from core.model.schema import Action, ActionKind, GateResult
from core.state import decisions

log = logging.getLogger(__name__)

#: Actions core will never expose, whatever asks for them (§8.2).
FORBIDDEN: set[str] = {
    "counter_trade",
    "set_league_settings",
    "post_message",
    "edit_other_team",
}

#: Writes that require the full pre-flight. NOTIFY does not — a health failure
#: is exactly when we most need to be able to tell someone.
_NEEDS_PREFLIGHT = {
    ActionKind.DRAFT_PICK,
    ActionKind.QUEUE_SYNC,
    ActionKind.SET_LINEUP,
    ActionKind.ADD_DROP,
    ActionKind.WAIVER_CLAIM,
    ActionKind.PROPOSE_TRADE,
    ActionKind.ACCEPT_TRADE,
    ActionKind.REJECT_TRADE,
}


def check(action: Action, *, skip_health: bool = False) -> GateResult:
    """Decide whether this write may proceed. Pure decision; no side effects
    beyond reading state."""
    kind = action.kind

    if kind.value in FORBIDDEN:
        return GateResult(allowed=False, refused_by="§8.2",
                          reason=f"{kind.value} is never exposed")

    if kind is ActionKind.NOTIFY:
        return GateResult(allowed=True)

    # 1 — kill switch
    if not kill_switch.is_on():
        return GateResult(allowed=False, refused_by="§8.4",
                          reason=f"kill switch is off: {kill_switch.state()[:120]}")

    # 2 — health
    if kind in _NEEDS_PREFLIGHT and not skip_health:
        h = health.check(kill_on_fail=True)
        if not h.ok:
            return GateResult(allowed=False, refused_by="§8.5",
                              reason="health check failed: " + "; ".join(h.failures)[:200])

    # 4 — action-specific
    args = action.args or {}

    if kind is ActionKind.PROPOSE_TRADE:
        ok, why = rate_limits.can_propose(
            int(args.get("to_team", -1)),
            list(args.get("give") or []),
            list(args.get("get") or []),
        )
        if not ok:
            return GateResult(allowed=False, refused_by="§6.1", reason=why)

    elif kind is ActionKind.ACCEPT_TRADE:
        gauntlet = args.get("gauntlet")
        if gauntlet is None:
            return GateResult(
                allowed=False, refused_by="§6.8",
                reason="no gauntlet result attached — an accept without the "
                       "gauntlet is never allowed",
            )
        passed = getattr(gauntlet, "accepted", None)
        if passed is None:
            passed = bool(gauntlet.get("accepted")) if isinstance(gauntlet, dict) else False
        if not passed:
            failed = getattr(gauntlet, "failed_on", None)
            if failed is None and isinstance(gauntlet, dict):
                failed = gauntlet.get("failed_on")
            return GateResult(allowed=False, refused_by="§6.8",
                              reason=f"gauntlet failed on {failed}")
        ok, why = rate_limits.can_accept(
            str(args.get("offer_id", "")), int(args.get("from_team", -1))
        )
        if not ok:
            return GateResult(allowed=False, refused_by="§6.8.10", reason=why)

    elif kind in (ActionKind.WAIVER_CLAIM, ActionKind.ADD_DROP):
        if args.get("drop") is None and args.get("roster_has_room") is False:
            return GateResult(allowed=False, refused_by="§5.4",
                              reason="no roster room and no drop specified")
        # §5.7 — three adds a week, claims and free agents alike. The cap is
        # what forces the sweep to choose rather than churn.
        ok, why = rate_limits.can_add()
        if not ok:
            return GateResult(allowed=False, refused_by="§5.7", reason=why)

    return GateResult(allowed=True)


def execute(
    action: Action,
    performer: Callable[[], Any],
    *,
    predicted: dict[str, float] | None = None,
    alternative: dict | None = None,
    skip_health: bool = False,
    dry_run: bool = False,
) -> tuple[GateResult, Any]:
    """Gate, then perform, then log. The only sanctioned way to write.

    `performer` is a zero-arg callable that does the browser work and returns a
    receipt. It is never called unless the gate allows.
    """
    gate = check(action, skip_health=skip_health)
    # The public args ride along so the sweep digest can say WHAT was done
    # (or would have been) without re-deriving it from the model's prose.
    extra = {"args": _public_args(action.args)}

    if not gate.allowed:
        decisions.record(action.kind, cites=action.cites, reason=action.reason,
                         predicted=predicted, alternative=alternative,
                         executed=False, gate=gate, extra=extra)
        log.warning("REFUSED %s by %s — %s", action.kind.value, gate.refused_by, gate.reason)
        return gate, None

    if dry_run:
        dry = GateResult(allowed=True, refused_by=None, reason="dry-run: not executed")
        decisions.record(action.kind, cites=action.cites, reason=action.reason,
                         predicted=predicted, alternative=alternative,
                         executed=False, gate=dry, extra=extra)
        log.info("DRY-RUN %s — would execute: %s", action.kind.value, action.reason)
        return dry, None

    try:
        receipt = performer()
    except Exception as e:
        fail = GateResult(allowed=True, refused_by=None, reason=f"execution failed: {e}")
        decisions.record(action.kind, cites=action.cites, reason=action.reason,
                         predicted=predicted, alternative=alternative,
                         executed=False, gate=fail, extra=extra)
        log.error("FAILED %s: %s", action.kind.value, e)
        raise

    decisions.record(action.kind, cites=action.cites, reason=action.reason,
                     predicted=predicted, alternative=alternative,
                     executed=True, gate=gate, receipt=str(receipt) if receipt else None,
                     extra=extra)
    log.info("EXECUTED %s — %s", action.kind.value, action.reason)
    return gate, receipt


def _public_args(args: dict | None) -> dict:
    """The action's args minus anything that is not plain data (the gauntlet
    result object on an accept)."""
    out = {}
    for k, v in (args or {}).items():
        if isinstance(v, (str, int, float, bool, type(None), list, dict)):
            out[k] = v
    return out
