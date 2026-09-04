"""§6.8 — the anti-fleece gauntlet on INCOMING trade offers.

The incoming case is structurally adversarial in a way the outgoing case is not:
the other manager chose the terms AND the timing, and every offer that lands is
by construction one that someone else thinks is good for them.

So the default answer is NO (§6.8.0). Thirteen gates; one failure rejects. No
averaging, no overall score, no "close enough". Rejecting a good trade costs a
little value; accepting a bad one costs the season. The decision rule is
asymmetric because the payoff is.

The MODEL narrates this. The CODE decides it — write_gate re-runs the result
before any accept, so the agent cannot talk its way past a failed gate (§10.3).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from core.model.priors import priors
from core.model.schema import GateCheck, GauntletResult, LeagueSettings, Player, Valuation

log = logging.getLogger(__name__)


@dataclass
class Offer:
    """An incoming trade proposal."""

    offer_id: str
    from_team: int
    #: Players we would RECEIVE.
    incoming: list[Player]
    #: Players we would GIVE UP.
    outgoing: list[Player]
    proposed_at: datetime
    #: Their roster, for the §6.8.2 both-sides calculation.
    their_roster: list[Player] = field(default_factory=list)


@dataclass
class NewsItem:
    espn_id: int
    published_at: datetime
    headline: str
    source: str


def _starting_points(
    roster: list[Player],
    valuations: dict[int, Valuation],
    settings: LeagueSettings,
) -> float:
    """Best legal starting lineup's ROS points. The only number §6.8.1 cares
    about — depth that never starts is worth close to zero."""
    by_pos: dict = {}
    for p in roster:
        if p.espn_id in valuations:
            by_pos.setdefault(p.pos, []).append(p)
    for v in by_pos.values():
        v.sort(key=lambda p: -valuations[p.espn_id].points)

    used: set[int] = set()
    total = 0.0
    for slot in sorted(settings.starting_slots, key=lambda s: len(s.eligible)):
        for _ in range(slot.count):
            best, best_pts = None, float("-inf")
            for pos in slot.eligible:
                for p in by_pos.get(pos, []):
                    if p.espn_id in used:
                        continue
                    pts = valuations[p.espn_id].points
                    if pts > best_pts:
                        best, best_pts = p, pts
                    break
            if best is not None:
                used.add(best.espn_id)
                total += best_pts
    return total


def _delta(
    roster: list[Player],
    give: list[Player],
    get: list[Player],
    valuations: dict[int, Valuation],
    settings: LeagueSettings,
) -> float:
    give_ids = {p.espn_id for p in give}
    after = [p for p in roster if p.espn_id not in give_ids] + list(get)
    return (
        _starting_points(after, valuations, settings)
        - _starting_points(roster, valuations, settings)
    )


def run(
    offer: Offer,
    our_roster: list[Player],
    valuations: dict[int, Valuation],
    settings: LeagueSettings,
    *,
    news: list[NewsItem] | None = None,
    first_seen: datetime | None = None,
    accepts_this_week: int = 0,
    last_accept_from_them: datetime | None = None,
    now: datetime | None = None,
    bench_open: int = 0,
    playoff_weeks: tuple[int, ...] = (15, 16, 17),
) -> GauntletResult:
    """Run all thirteen gates. Every one must pass."""
    p = priors()
    g = lambda k: p.get(f"trades.gauntlet.{k}")  # noqa: E731
    now = now or datetime.now(UTC)
    news = news or []
    checks: list[GateCheck] = []

    def add(section: str, name: str, passed: bool, detail: str) -> None:
        checks.append(GateCheck(section=section, name=name, passed=passed, detail=detail))

    involved = {pl.espn_id for pl in offer.incoming + offer.outgoing}
    missing_vals = [pl.name for pl in offer.incoming + offer.outgoing
                    if pl.espn_id not in valuations]

    # ── §6.8.11 — missing data means reject. Checked first: everything below
    #    would otherwise be computed on invented numbers.
    add(
        "§6.8.11", "complete data", not missing_vals,
        "all players valued" if not missing_vals
        else f"no valuation for {', '.join(missing_vals)} — never estimate to clear a gate",
    )
    if missing_vals:
        return GauntletResult(offer_id=offer.offer_id, checks=checks)

    our_gain = _delta(our_roster, offer.outgoing, offer.incoming, valuations, settings)

    # ── §6.8.1 — margin gate. A positive number is not enough.
    min_gain = float(g("min_starting_points_gain"))
    add(
        "§6.8.1", "margin", our_gain >= min_gain,
        f"starting-lineup ROS {our_gain:+.1f} vs required +{min_gain:.1f}"
        + ("" if our_gain >= min_gain else
           " — inside the model's own error bars, where their read beats ours"),
    )

    # ── §6.8.2 — both-sides gate. Reject if THEY gain more than we do.
    if offer.their_roster:
        their_gain = _delta(
            offer.their_roster, offer.incoming, offer.outgoing, valuations, settings
        )
        add(
            "§6.8.2", "both sides", our_gain >= their_gain,
            f"we gain {our_gain:+.1f}, they gain {their_gain:+.1f}"
            + ("" if our_gain >= their_gain else
               " — a redraft trade is near zero-sum; the side that gains more wins it"),
        )
    else:
        add("§6.8.2", "both sides", False,
            "their roster unavailable — cannot compute their side, so cannot rule out a fleece")

    # ── §6.8.3 — "why would they send this?"
    reason = _infer_their_reason(offer, valuations, settings)
    add(
        "§6.8.3", "why would they send this", reason is not None,
        reason or "no coherent reason found — the missing logic is never in our favour",
    )

    # ── §6.8.4 — information gate.
    window = float(g("news_window_hours"))
    recent = [
        n for n in news
        if n.espn_id in involved and (now - n.published_at) <= timedelta(hours=window)
    ]
    add(
        "§6.8.4", "information window", not recent,
        "no fresh news on any player involved" if not recent
        else f"news within {window:.0f}h: "
             + "; ".join(f"{n.headline[:60]} ({n.source})" for n in recent[:3]),
    )

    # ── §6.8.5 — health gate. The most common fleece there is.
    unhealthy = [
        pl.name for pl in offer.incoming
        if pl.injury_status.cannot_start or valuations[pl.espn_id].vetoed
    ]
    add(
        "§6.8.5", "incoming health", not unhealthy,
        "all incoming players available" if not unhealthy
        else f"{', '.join(unhealthy)} cannot start — name-brand-but-hurt is the "
             "classic fleece",
    )

    # ── §6.8.6 — consolidation gate.
    if len(offer.incoming) > len(offer.outgoing) and offer.outgoing:
        tol = float(g("consolidation_tolerance"))
        best_in = max(valuations[pl.espn_id].vor for pl in offer.incoming)
        best_out = max(valuations[pl.espn_id].vor for pl in offer.outgoing)
        ok = best_out <= 0 or best_in >= best_out * (1 - tol)
        add(
            "§6.8.6", "consolidation", ok,
            f"best in {best_in:.1f} VOR vs best out {best_out:.1f} "
            f"(tolerance {tol:.0%})"
            + ("" if ok else " — you can only start so many; dilution loses"),
        )
    else:
        add("§6.8.6", "consolidation", True, "not a diluting trade")

    # ── §6.8.7 — drop-cost gate.
    net_roster_change = len(offer.incoming) - len(offer.outgoing)
    forced_drops = max(0, net_roster_change - bench_open)
    if forced_drops:
        keep = {pl.espn_id for pl in offer.outgoing}
        spare = sorted(
            (valuations[pl.espn_id].points for pl in our_roster
             if pl.espn_id in valuations and pl.espn_id not in keep),
        )[:forced_drops]
        drop_cost = sum(spare)
        ok = (our_gain - drop_cost) >= min_gain
        add(
            "§6.8.7", "drop cost", ok,
            f"{forced_drops} forced drop(s) costing {drop_cost:.1f}; "
            f"net {our_gain - drop_cost:+.1f}",
        )
    else:
        add("§6.8.7", "drop cost", True, "no forced drop")

    # ── §6.8.8 — playoff schedule gate.
    bye_clash = [
        pl.name for pl in offer.incoming
        if pl.bye_week and pl.bye_week in playoff_weeks
    ]
    add(
        "§6.8.8", "playoff schedule", not bye_clash,
        "no incoming player is on bye during weeks 15-17" if not bye_clash
        else f"{', '.join(bye_clash)} on bye in the playoffs",
    )

    # ── §6.8.9 — cool-down.
    seen = first_seen or offer.proposed_at
    waited_min = (now - seen).total_seconds() / 60.0
    need_min = float(g("cooldown_minutes"))
    add(
        "§6.8.9", "cool-down", waited_min >= need_min,
        f"seen {waited_min:.0f} min ago, need {need_min:.0f} — "
        "an expiring offer is a red flag, not urgency",
    )

    # ── §6.8.10 — rate limits.
    max_wk = int(g("max_accepts_per_week"))
    same_days = float(g("same_manager_cooldown_days"))
    rate_ok = accepts_this_week < max_wk
    detail = f"{accepts_this_week} accept(s) this week, cap {max_wk}"
    if rate_ok and last_accept_from_them:
        since = (now - last_accept_from_them).days
        if since < same_days:
            rate_ok = False
            detail = (f"accepted from team {offer.from_team} {since}d ago, "
                      f"cooldown {same_days:.0f}d")
    add("§6.8.10", "rate limits", rate_ok, detail)

    # ── §6.8.12 — notification is a record, not a gate; always satisfiable.
    add("§6.8.12", "notify on fire", True, "acceptance posts the full gauntlet to #fantasy")

    # ── §6.8.13 — counters are not authorised. Nothing to check; recorded so
    #    the printed result shows all thirteen.
    add("§6.8.13", "no counter-offer", True, "accept or reject only")

    result = GauntletResult(offer_id=offer.offer_id, checks=checks)
    log.info(
        "gauntlet %s: %s%s",
        offer.offer_id,
        "PASS" if result.accepted else "REJECT",
        "" if result.accepted else f" on {result.failed_on}",
    )
    return result


def _infer_their_reason(
    offer: Offer, valuations: dict[int, Valuation], settings: LeagueSettings
) -> str | None:
    """§6.8.3 — write down, in one sentence, why they sent this.

    Acceptable reasons are structural: a positional surplus, a hole, a punt. If
    none fits, we have not found the logic yet, and the logic we cannot see is
    never in our favour.
    """
    if not offer.their_roster:
        return None

    from collections import Counter

    theirs = Counter(p.pos for p in offer.their_roster)
    getting = Counter(p.pos for p in offer.incoming)   # what WE get = what they send
    giving = Counter(p.pos for p in offer.outgoing)    # what THEY get

    # They are trading FROM a surplus.
    for pos, n in getting.items():
        starters = settings.starters_at(pos)
        if theirs.get(pos, 0) - n >= starters + 1:
            return (
                f"they have {theirs[pos]} {pos.value}s and start {starters}, so they "
                f"are dealing from a surplus"
            )

    # They are trading INTO a hole.
    for pos in giving:
        starters = settings.starters_at(pos)
        if theirs.get(pos, 0) < starters:
            return (
                f"they have {theirs.get(pos, 0)} {pos.value}s and must start "
                f"{starters}, so they are filling a hole"
            )

    # They are consolidating: fewer, better players.
    if len(offer.outgoing) > len(offer.incoming):
        return "they are consolidating depth into a better starter"

    return None
