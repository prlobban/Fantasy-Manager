"""§6.1 and §6.8.10 — the counters that make trade autonomy defensible.

Every limit here is read from priors.yaml, and every check returns a reason
string naming the section it enforces, so a refusal is self-explaining in the
log and in Slack.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

from core.model.priors import priors
from core.state import store


def offer_hash(give: list[int], get: list[int], to_team: int) -> str:
    """Stable id for a trade shape, so §6.1's no-repropose rule can recognise
    the same offer coming back around with the players in a different order."""
    key = f"{to_team}|{sorted(give)}|{sorted(get)}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _recent(entries: list[dict], days: float) -> list[dict]:
    cutoff = datetime.now(UTC) - timedelta(days=days)
    out = []
    for e in entries:
        try:
            at = datetime.fromisoformat(e["at"])
            if not at.tzinfo:
                at = at.replace(tzinfo=UTC)
            if at >= cutoff:
                out.append(e)
        except Exception:
            continue
    return out


# ── outgoing proposals (§6.1) ────────────────────────────────────────────────


def can_propose(to_team: int, give: list[int], get: list[int]) -> tuple[bool, str]:
    p = priors()
    s = store.load()
    proposals = s.get("trade_proposals") or []

    per_day = int(p.get("trades.max_proposals_per_day"))
    if len(_recent(proposals, 1)) >= per_day:
        return False, f"§6.1 already made {per_day} proposal(s) today"

    per_week = int(p.get("trades.max_proposals_per_week"))
    if len(_recent(proposals, 7)) >= per_week:
        return False, f"§6.1 already made {per_week} proposals this week"

    open_per_mgr = int(p.get("trades.max_open_offers_per_manager"))
    open_to_them = [e for e in _recent(proposals, 14) if e.get("to_team") == to_team]
    if len(open_to_them) >= open_per_mgr:
        return False, f"§6.1 an offer to team {to_team} is already outstanding"

    h = offer_hash(give, get, to_team)
    no_repropose = float(p.get("trades.no_repropose_days"))
    for e in _recent(s.get("trade_rejections") or [], no_repropose):
        if e.get("offer_hash") == h:
            return False, f"§6.1 this exact offer was rejected within {no_repropose:.0f} days"

    return True, "ok"


def record_proposal(to_team: int, give: list[int], get: list[int]) -> None:
    store.append(
        "trade_proposals",
        {"at": store.now_iso(), "to_team": to_team,
         "offer_hash": offer_hash(give, get, to_team)},
    )


def record_rejection(by_team: int, give: list[int], get: list[int]) -> None:
    store.append(
        "trade_rejections",
        {"at": store.now_iso(), "by_team": by_team,
         "offer_hash": offer_hash(give, get, by_team)},
    )


# ── roster adds (§5.7 — three a week, Pearce 2026-09-05) ─────────────────────


def adds_this_week() -> int:
    return len(_recent(store.load().get("roster_adds") or [], 7))


def adds_left() -> int:
    cap = int(priors().get("season.max_adds_per_week"))
    return max(0, cap - adds_this_week())


def can_add() -> tuple[bool, str]:
    """A waiver claim or a free-agent add both spend one of the week's three.
    The cap exists so the sweep has to choose, not so it can churn."""
    cap = int(priors().get("season.max_adds_per_week"))
    used = adds_this_week()
    if used >= cap:
        return False, f"§5.7 already made {used} of {cap} roster adds this week"
    return True, f"§5.7 add {used + 1} of {cap} this week"


def record_add(add_id: int, drop_id: int | None) -> None:
    store.append("roster_adds",
                 {"at": store.now_iso(), "add": add_id, "drop": drop_id})


def proposals_left() -> tuple[int, int]:
    """(left today, left this week) under §6.1."""
    p = priors()
    props = store.load().get("trade_proposals") or []
    day = int(p.get("trades.max_proposals_per_day")) - len(_recent(props, 1))
    week = int(p.get("trades.max_proposals_per_week")) - len(_recent(props, 7))
    return max(0, day), max(0, week)


# ── incoming acceptances (§6.8.9, §6.8.10) ───────────────────────────────────


def note_offer_seen(offer_id: str) -> str:
    """First-seen timestamp, for the cool-down. Idempotent."""
    s = store.load()
    seen = dict(s.get("offers_first_seen") or {})
    if offer_id not in seen:
        seen[offer_id] = store.now_iso()
        store.update(offers_first_seen=seen)
    return seen[offer_id]


def cooldown_elapsed(offer_id: str) -> tuple[bool, str]:
    """§6.8.9 — never accept inside the cool-down. Defeats pressure plays."""
    p = priors()
    mins = float(p.get("trades.gauntlet.cooldown_minutes"))
    first = note_offer_seen(offer_id)
    try:
        at = datetime.fromisoformat(first)
        if not at.tzinfo:
            at = at.replace(tzinfo=UTC)
    except Exception:
        return False, "§6.8.9 could not read first-seen time"
    waited = (datetime.now(UTC) - at).total_seconds() / 60.0
    if waited < mins:
        return False, f"§6.8.9 cool-down: seen {waited:.0f}m ago, need {mins:.0f}m"
    return True, f"§6.8.9 cool-down satisfied ({waited:.0f}m)"


def can_accept(offer_id: str, from_team: int) -> tuple[bool, str]:
    p = priors()
    s = store.load()
    accepts = s.get("trade_accepts") or []

    per_week = int(p.get("trades.gauntlet.max_accepts_per_week"))
    if len(_recent(accepts, 7)) >= per_week:
        return False, f"§6.8.10 already accepted {per_week} trade(s) this week"

    same_mgr_days = float(p.get("trades.gauntlet.same_manager_cooldown_days"))
    for e in _recent(accepts, same_mgr_days):
        if e.get("from_team") == from_team:
            return False, (
                f"§6.8.10 already accepted a trade from team {from_team} "
                f"within {same_mgr_days:.0f} days"
            )

    return cooldown_elapsed(offer_id)


def record_accept(offer_id: str, from_team: int) -> None:
    store.append(
        "trade_accepts",
        {"at": store.now_iso(), "offer_id": offer_id, "from_team": from_team},
    )
