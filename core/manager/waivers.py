"""§5 — waivers and free agents, on ROLLING PRIORITY.

This league is `WAIVERS_TRADITIONAL` with `isUsingAcquisitionBudget: False`, so
there is no bidding. The only cost of a claim is dropping to the back of the
queue, which makes priority a one-shot asset that regenerates slowly. The
question is never "is this player good?" but "is he worth being last in line for
the next one?" (§5.3).

Three things this league and Pearce's brief force:
  - Only 4 bench spots, so almost every add needs a drop and the dropped
    player's value is part of the price (§5.4).
  - Free agents who have cleared waivers cost NO priority (§5.3.2) — but every
    add, free or claimed, spends one of the THREE weekly adds (§5.7).
  - A droppable player another team would start is a trade chip, not a cut
    (D4.5). Such an add is held back unless it is urgent.

The drop is chosen from ONE optimal lineup (2026-09-05 fix): the first version
scored each slot independently, so the flex starter never registered as a
starter and was proposed as a free drop on the manager's first live run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from core.manager import roster as roster_mod
from core.model.priors import priors
from core.model.schema import InjuryStatus, LeagueSettings, Player, Pos, Valuation

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
    #: D4.5 — the drop has real ROS value; try a trade before cutting him.
    drop_tradeable: bool
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
    adds_left: int | None = None
    notes: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"waiver priority: {self.priority if self.priority else 'unknown'}"
                 + (f" · adds left this week: {self.adds_left}" if self.adds_left is not None else "")]
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
    # D6.1 — a kicker or defence is a streamer whatever this week's number
    # says. The first live sweep (2026-09-05) scored a D/ST +3.3 as an
    # "immediate starter" and offered priority 2 for it; the agent refused it
    # on doctrine. The code should not have asked.
    if c_player.pos in (Pos.K, Pos.DST):
        return "streamer"
    if weekly_gain >= 3.0:
        return "immediate_starter"
    if weekly_gain >= 1.5:
        return "weekly_flex_upgrade"
    if val.points < 8:
        return "streamer"
    return "upside_stash"


def _min_gain_for_priority(priority: int | None) -> tuple[float, str]:
    """§5.3.1 — the bar scales with how valuable our queue position is."""
    p = priors()
    if priority is None:
        return float(p.get("waivers.priority_ladder.top.min_weekly_gain")), "unknown (assumed top)"
    for band in ("top", "middle", "bottom"):
        if priority <= int(p.get(f"waivers.priority_ladder.{band}.max_position")):
            return float(p.get(f"waivers.priority_ladder.{band}.min_weekly_gain")), band
    return float(p.get("waivers.priority_ladder.bottom.min_weekly_gain")), "bottom"


def _starters(roster: list[Player], valuations: dict[int, Valuation],
              settings: LeagueSettings) -> dict[int, float]:
    """espn_id -> points, for everyone in the ONE optimal lineup."""
    from core.manager.lineup import optimal_lineup

    plan = optimal_lineup(roster, valuations, settings)
    return {a.player.espn_id: a.points for a in plan.assignments if a.player is not None}


def weekly_gain_for(
    cand: Player,
    cand_val: Valuation,
    roster: list[Player],
    valuations: dict[int, Valuation],
    settings: LeagueSettings,
) -> tuple[float, Player | None]:
    """§5.2 — improvement to the STARTING lineup, not to the roster.

    The candidate is added to the roster and the optimal lineup recomputed;
    the gain is the difference. That is exact for the flex, which the old
    per-slot comparison got wrong.
    """
    before = _starters(roster, valuations, settings)
    trial = list(roster) + [cand]
    trial_vals = dict(valuations)
    trial_vals[cand.espn_id] = cand_val
    after = _starters(trial, trial_vals, settings)
    if cand.espn_id not in after:
        return 0.0, None
    gain = sum(after.values()) - sum(before.values())
    displaced = [pid for pid in before if pid not in after]
    by_id = {p.espn_id: p for p in roster}
    replaced = by_id.get(displaced[0]) if displaced else None
    return max(0.0, gain), replaced


def choose_drop(
    roster: list[Player],
    valuations: dict[int, Valuation],
    settings: LeagueSettings,
    *,
    current_week: int = 1,
    bench_open: int = 0,
    ros_valuations: dict[int, Valuation] | None = None,
) -> tuple[Player | None, float, str, bool]:
    """§5.5 / D5.2 / D4.5 — who we can afford to cut, what it costs, and
    whether he is worth trading first.

    Returns (player, cost, reason, tradeable). Cost is the weekly points he
    contributes to the ONE optimal lineup — zero for a bench body. Order of
    preference is the doctrine's: surplus at a one-slot position first, then
    surplus anywhere, then lowest ROS value.
    """
    p = priors()
    top_n = int(p.get("waivers.never_drop_top_n"))
    keep_weeks = int(p.get("waivers.keep_injured_return_within_weeks"))
    trade_min = float(p.get("season.trade_instead_of_drop_min_vor"))
    ros = ros_valuations or valuations

    ranked = sorted(
        (pl for pl in roster if pl.espn_id in ros),
        key=lambda pl: -ros[pl.espn_id].vor,
    )
    protected = {pl.espn_id for pl in ranked[:top_n]}
    starters = _starters(roster, valuations, settings)

    droppable: list[tuple[int, float, Player]] = []
    held: list[str] = []
    order = roster_mod.drop_order(roster, ros, settings)
    for i, pl in enumerate(order):
        v = valuations.get(pl.espn_id)
        if v is None or pl.espn_id in protected:
            continue
        if pl.injury_status is InjuryStatus.IR:
            continue  # IR does not occupy a bench spot

        # §5.5 — hold an injured player who is coming back soon, but ONLY while
        # a bench spot exists.
        if pl.injury_status.cannot_start and bench_open > 0:
            weeks_out = _weeks_until_return(pl, current_week)
            if weeks_out is not None and weeks_out <= keep_weeks:
                held.append(f"{pl.name} (back in ~{weeks_out}w)")
                continue

        cost = starters.get(pl.espn_id, 0.0)
        droppable.append((i, cost, pl))

    if not droppable:
        reason = "nobody droppable — every bench player is protected (§5.5)"
        if held:
            reason += f"; holding {', '.join(held)}"
        return None, 0.0, reason, False

    # Cheapest to the lineup first; among bench bodies, the doctrine's order.
    droppable.sort(key=lambda t: (t[1], t[0]))
    _, cost, pl = droppable[0]
    ros_vor = ros[pl.espn_id].vor if pl.espn_id in ros else 0.0
    tradeable = ros_vor >= trade_min
    shape = roster_mod.analyse(roster, ros, settings)
    why = "surplus at a one-slot position" if (
        shape.by_pos.get(pl.pos) and pl in shape.by_pos[pl.pos].surplus_players
        and shape.by_pos[pl.pos].starters <= 1
    ) else "lowest-value droppable"
    reason = f"{why} ({ros_vor:.1f} ROS VOR)"
    if tradeable:
        reason += f" — D4.5: {ros_vor:.1f} ROS VOR is trade capital; offer him before cutting"
    if held:
        reason += f"; holding {', '.join(held)} per §5.5"
    return pl, cost, reason, tradeable


def _weeks_until_return(pl: Player, current_week: int) -> int | None:
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
    max_claims: int | None = None,
    adds_left: int | None = None,
    ros_valuations: dict[int, Valuation] | None = None,
) -> WaiverPlan:
    """The whole §5 decision.

    `on_waivers` is the set of player ids still inside the 24h waiver window —
    those cost priority. Everyone else is a free agent and costs nothing
    (§5.3.2). `adds_left` is what §5.7 still allows this week.
    """
    p = priors()
    on_waivers = on_waivers or set()
    min_gain, band = _min_gain_for_priority(waiver_priority)
    streamer_floor = int(p.get("waivers.streamer_priority_floor"))
    urgent = float(p.get("season.urgent_add_weekly_gain"))
    cap = int(p.get("season.max_adds_per_week"))
    max_claims = max_claims if max_claims is not None else cap
    if adds_left is not None:
        max_claims = min(max_claims, adds_left)

    our_rb1 = max(
        (x for x in roster if x.pos is Pos.RB and x.espn_id in valuations),
        key=lambda x: valuations[x.espn_id].vor,
        default=None,
    )

    drop, drop_cost, drop_reason, tradeable = choose_drop(
        roster, valuations, settings, current_week=current_week,
        bench_open=bench_open, ros_valuations=ros_valuations,
    )

    cands: list[Candidate] = []
    for fa in free_agents:
        v = valuations.get(fa.espn_id)
        if v is None or v.vetoed:
            continue
        gain, replaces = weekly_gain_for(fa, v, roster, valuations, settings)
        needs_drop = bench_open <= 0
        cands.append(Candidate(
            player=fa, valuation=v, replaces=replaces, weekly_gain=gain,
            drop=drop if needs_drop else None,
            drop_cost=drop_cost if needs_drop else 0.0,
            drop_tradeable=tradeable if needs_drop else False,
            archetype=classify(fa, v, gain, our_rb1),
            is_free_agent=fa.espn_id not in on_waivers,
        ))

    cands.sort(key=lambda c: -c.net_gain)

    claims: list[Candidate] = []
    free_adds: list[Candidate] = []
    skipped: list[tuple[Candidate, str]] = []

    for c in cands:
        if c.net_gain <= 0:
            skipped.append((c, f"no net starting-lineup gain ({c.net_gain:+.1f}/wk)"))
            continue
        if c.drop is None and bench_open <= 0:
            skipped.append((c, "no bench room and nothing droppable (§5.5)"))
            continue
        # D4.5 — the drop is a trade chip. Hold the add unless it is urgent.
        if c.drop_tradeable and c.weekly_gain < urgent:
            skipped.append((
                c,
                f"D4.5 drop {c.drop.name} has trade value — propose a trade first; "
                f"+{c.weekly_gain:.1f}/wk is under the {urgent:.1f} urgent bar",
            ))
            continue

        if c.is_free_agent:
            c.reasons.append("§5.3.2 free agent — costs no waiver priority "
                             "(still one of the week's adds, §5.7)")
            free_adds.append(c)
            continue

        if c.archetype == "streamer" and (waiver_priority or 1) <= streamer_floor:
            skipped.append((
                c,
                f"§5.3.3 streamer, and our priority ({waiver_priority}) is too valuable "
                "to spend on one week",
            ))
            continue
        if c.net_gain < min_gain:
            skipped.append((
                c,
                f"§5.3.1 +{c.net_gain:.1f}/wk is under the {min_gain:.1f} bar for {band} priority",
            ))
            continue

        c.reasons.append(f"§5.3.1 +{c.net_gain:.1f}/wk clears the {min_gain:.1f} bar "
                         f"for {band} priority")
        claims.append(c)

    plan = WaiverPlan(
        claims=claims[:max_claims],
        free_adds=free_adds[:max_claims],
        skipped=skipped,
        priority=waiver_priority,
        adds_left=adds_left,
    )
    if drop:
        plan.notes.append(f"drop candidate: {drop.name} — {drop_reason}")
    else:
        plan.notes.append(drop_reason)
    if bench_open > 0:
        plan.notes.append(f"{bench_open} bench spot(s) open — no drop needed")
    if adds_left is not None:
        plan.notes.append(f"§5.7: {adds_left} of {cap} roster adds left this week")
    return plan
