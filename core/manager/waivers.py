"""§5 — waivers and free agents, on ROLLING PRIORITY.

This league is `WAIVERS_TRADITIONAL` with `isUsingAcquisitionBudget: False`, so
there is no bidding. The only cost of a claim is dropping to the back of the
queue, which makes priority a one-shot asset that regenerates slowly. The
question is never "is this player good?" but "is he worth being last in line for
the next one?" (§5.3).

Two other things this league forces:
  - Only 4 bench spots, so almost every add needs a drop and the dropped
    player's value is part of the price (§5.4).
  - Free agents who have cleared waivers cost NO priority (§5.3.2). Burning a
    claim on someone who will be free tomorrow is pure waste.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from core.model.priors import priors
from core.model.schema import LeagueSettings, Player, Pos, Valuation

log = logging.getLogger(__name__)


@dataclass
class Candidate:
    player: Player
    valuation: Valuation
    #: Who he would replace in the STARTING lineup, if anyone.
    replaces: Player | None
    #: Weekly points added to the starting lineup. The only number that matters.
    weekly_gain: float
    #: Who we would have to drop to make room.
    drop: Player | None
    drop_cost: float
    archetype: str
    #: True if he is a free agent (no waiver claim needed, no priority spent).
    is_free_agent: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def net_gain(self) -> float:
        return self.weekly_gain - self.drop_cost


@dataclass
class WaiverPlan:
    claims: list[Candidate]
    free_adds: list[Candidate]
    skipped: list[tuple[Candidate, str]]
    priority: int | None
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"waiver priority: {self.priority if self.priority else 'unknown'}"]
        for c in self.free_adds:
            lines.append(f"  FREE ADD  {c.player.name:22} +{c.net_gain:.1f}/wk"
                         f"  drop {c.drop.name if c.drop else '—'}")
        for c in self.claims:
            lines.append(f"  CLAIM     {c.player.name:22} +{c.net_gain:.1f}/wk"
                         f"  drop {c.drop.name if c.drop else '—'}  ({c.archetype})")
        for c, why in self.skipped[:6]:
            lines.append(f"  skip      {c.player.name:22} — {why}")
        for n in self.notes:
            lines.append(f"  note: {n}")
        return "\n".join(lines)


def classify(c_player: Player, val: Valuation, weekly_gain: float,
             our_rb1: Player | None) -> str:
    """§5.3.1 archetypes. Used for the log and for §5.3.3."""
    if our_rb1 is not None and c_player.pos is Pos.RB and c_player.pro_team == our_rb1.pro_team:
        return "own_rb1_handcuff"
    if weekly_gain >= 3.0:
        return "immediate_starter"
    if weekly_gain >= 1.5:
        return "weekly_flex_upgrade"
    if c_player.pos in (Pos.K, Pos.DST) or val.points < 8:
        return "streamer"
    return "upside_stash"


def _min_gain_for_priority(priority: int | None) -> tuple[float, str]:
    """§5.3.1 — the bar scales with how valuable our queue position is."""
    p = priors()
    if priority is None:
        # Unknown priority: assume it is valuable. Being wrong in this direction
        # costs us a marginal add; the other way spends the season's best claim
        # on a streamer.
        return float(p.get("waivers.priority_ladder.top.min_weekly_gain")), "unknown (assumed top)"
    for band in ("top", "middle", "bottom"):
        if priority <= int(p.get(f"waivers.priority_ladder.{band}.max_position")):
            return float(p.get(f"waivers.priority_ladder.{band}.min_weekly_gain")), band
    return float(p.get("waivers.priority_ladder.bottom.min_weekly_gain")), "bottom"


def _best_startable_at(
    roster: list[Player], valuations: dict[int, Valuation], eligible: tuple[Pos, ...]
) -> list[tuple[Player, Valuation]]:
    out = [
        (p, valuations[p.espn_id])
        for p in roster
        if p.espn_id in valuations and p.pos in eligible
    ]
    out.sort(key=lambda pv: -pv[1].points)
    return out


def weekly_gain_for(
    cand: Player,
    cand_val: Valuation,
    roster: list[Player],
    valuations: dict[int, Valuation],
    settings: LeagueSettings,
) -> tuple[float, Player | None]:
    """§5.2 — improvement to the STARTING lineup, not to the roster.

    Compares the candidate against the worst player currently occupying a slot
    he is eligible for. Adding a WR5 who never starts scores zero here, which is
    the correct answer and the whole point of the rule.
    """
    best_gain, replaced = 0.0, None
    for slot in settings.starting_slots:
        if cand.pos not in slot.eligible:
            continue
        occupants = _best_startable_at(roster, valuations, slot.eligible)[: slot.count]
        if len(occupants) < slot.count:
            return cand_val.points, None  # an empty slot: full value
        worst_p, worst_v = occupants[-1]
        gain = cand_val.points - worst_v.points
        if gain > best_gain:
            best_gain, replaced = gain, worst_p
    return best_gain, replaced


def choose_drop(
    roster: list[Player],
    valuations: dict[int, Valuation],
    settings: LeagueSettings,
    *,
    current_week: int = 1,
    bench_open: int = 0,
) -> tuple[Player | None, float, str]:
    """§5.5 — who we can afford to cut, and what it costs.

    Returns (player, cost, reason). Cost is the weekly points they contribute to
    the starting lineup, which for a deep bench body is zero.
    """
    p = priors()
    top_n = int(p.get("waivers.never_drop_top_n"))
    keep_weeks = int(p.get("waivers.keep_injured_return_within_weeks"))

    ranked = sorted(
        (pl for pl in roster if pl.espn_id in valuations),
        key=lambda pl: -valuations[pl.espn_id].vor,
    )
    protected = {pl.espn_id for pl in ranked[:top_n]}

    starters = {
        pl.espn_id
        for slot in settings.starting_slots
        for pl, _ in _best_startable_at(roster, valuations, slot.eligible)[: slot.count]
    }

    droppable: list[tuple[Player, float]] = []
    held: list[str] = []
    for pl in roster:
        v = valuations.get(pl.espn_id)
        if v is None or pl.espn_id in protected:
            continue
        if pl.injury_status.name == "INJURY_RESERVE":
            continue  # IR does not occupy a bench spot

        # §5.5 — hold an injured player who is coming back soon, but ONLY while
        # a bench spot exists. With four bench spots it often will not, and the
        # rule says that is a real decision rather than an automatic hold.
        if pl.injury_status.cannot_start and bench_open > 0:
            weeks_out = _weeks_until_return(pl, current_week)
            if weeks_out is not None and weeks_out <= keep_weeks:
                held.append(f"{pl.name} (back in ~{weeks_out}w)")
                continue

        cost = v.points if pl.espn_id in starters else 0.0
        droppable.append((pl, cost))

    if not droppable:
        reason = "nobody droppable — every bench player is protected (§5.5)"
        if held:
            reason += f"; holding {', '.join(held)}"
        return None, 0.0, reason

    droppable.sort(key=lambda pc: (pc[1], valuations[pc[0].espn_id].vor))
    pl, cost = droppable[0]
    reason = f"lowest-value droppable ({valuations[pl.espn_id].vor:.1f} ROS VOR)"
    if held:
        reason += f"; holding {', '.join(held)} per §5.5"
    return pl, cost, reason


def _weeks_until_return(pl: Player, current_week: int) -> int | None:
    """Best estimate of how long an injured player is out.

    ESPN does not publish a return week, so this reads what it does give us:
    the next week the player carries a projection. No projection anywhere ahead
    means we do not know, and §5.5's protection does not apply to a player whose
    return we cannot see.
    """
    future = [w for w, pts in pl.proj_week.items() if w > current_week and pts > 0]
    return min(future) - current_week if future else None


def build(
    roster: list[Player],
    free_agents: list[Player],
    valuations: dict[int, Valuation],
    settings: LeagueSettings,
    *,
    waiver_priority: int | None = None,
    on_waivers: set[int] | None = None,
    bench_open: int = 0,
    current_week: int = 1,
    max_claims: int = 3,
) -> WaiverPlan:
    """The whole §5 decision.

    `on_waivers` is the set of player ids still inside the 24h waiver window —
    those cost priority. Everyone else is a free agent and costs nothing
    (§5.3.2).
    """
    on_waivers = on_waivers or set()
    min_gain, band = _min_gain_for_priority(waiver_priority)
    streamer_floor = int(priors().get("waivers.streamer_priority_floor"))

    our_rb1 = max(
        (p for p in roster if p.pos is Pos.RB and p.espn_id in valuations),
        key=lambda p: valuations[p.espn_id].vor,
        default=None,
    )

    drop, drop_cost, drop_reason = choose_drop(
        roster, valuations, settings, current_week=current_week, bench_open=bench_open
    )

    cands: list[Candidate] = []
    for fa in free_agents:
        v = valuations.get(fa.espn_id)
        if v is None or v.vetoed:
            continue
        gain, replaces = weekly_gain_for(fa, v, roster, valuations, settings)
        is_fa = fa.espn_id not in on_waivers
        needs_drop = bench_open <= 0
        cands.append(
            Candidate(
                player=fa,
                valuation=v,
                replaces=replaces,
                weekly_gain=gain,
                drop=drop if needs_drop else None,
                drop_cost=drop_cost if needs_drop else 0.0,
                archetype=classify(fa, v, gain, our_rb1),
                is_free_agent=is_fa,
            )
        )

    cands.sort(key=lambda c: -c.net_gain)

    claims: list[Candidate] = []
    free_adds: list[Candidate] = []
    skipped: list[tuple[Candidate, str]] = []

    for c in cands:
        # §5.2 / §5.4 — must improve the starting lineup net of the drop.
        if c.net_gain <= 0:
            skipped.append((c, f"no net starting-lineup gain ({c.net_gain:+.1f}/wk)"))
            continue
        if c.drop is None and bench_open <= 0:
            skipped.append((c, "no bench room and nothing droppable (§5.5)"))
            continue

        if c.is_free_agent:
            # §5.3.2 — free, so the only bar is §5.2.
            c.reasons.append("§5.3.2 free agent — costs no waiver priority")
            free_adds.append(c)
            continue

        # §5.3.3 — never spend a good claim on a one-week streamer.
        if c.archetype == "streamer" and (waiver_priority or 1) <= streamer_floor:
            skipped.append((
                c,
                f"§5.3.3 streamer, and our priority ({waiver_priority}) is too "
                "valuable to spend on one week",
            ))
            continue

        if c.net_gain < min_gain:
            skipped.append((
                c,
                f"§5.3.1 +{c.net_gain:.1f}/wk is under the {min_gain:.1f} bar for "
                f"{band} priority",
            ))
            continue

        c.reasons.append(
            f"§5.3.1 +{c.net_gain:.1f}/wk clears the {min_gain:.1f} bar for {band} priority"
        )
        claims.append(c)

    # §5.3.4 — order by value; only the first success spends our position.
    claims = claims[:max_claims]

    plan = WaiverPlan(
        claims=claims,
        free_adds=free_adds[:max_claims],
        skipped=skipped,
        priority=waiver_priority,
    )
    if drop:
        plan.notes.append(f"drop candidate: {drop.name} — {drop_reason}")
    else:
        plan.notes.append(drop_reason)
    if bench_open > 0:
        plan.notes.append(f"{bench_open} bench spot(s) open — no drop needed")
    return plan
