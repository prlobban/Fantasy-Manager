"""§4 — start / sit.

Two parts. First the optimal lineup: an assignment problem over legal slots,
maximising projected points. Then §4.2, the rule that answers "consistency or
risk": which direction variance helps depends entirely on the matchup, so the
preference is chosen per week, never set globally.

Nuance this league forces: playoff seeding is TOTAL_POINTS_SCORED. §4.2's floor
branch therefore means "prefer the steadier player between two near-equals", NOT
"leave points on the bench" — points-for is a tiebreaker we are always competing
for.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from core.model.priors import priors
from core.model.schema import LeagueSettings, Player, Pos, Valuation

log = logging.getLogger(__name__)


@dataclass
class SlotAssignment:
    slot: str
    player: Player | None
    valuation: Valuation | None

    @property
    def points(self) -> float:
        return self.valuation.points if self.valuation else 0.0


@dataclass
class LineupPlan:
    assignments: list[SlotAssignment]
    bench: list[Player]
    projected_points: float
    #: (player, from_slot, to_slot, why)
    changes: list[tuple[Player, str, str, str]] = field(default_factory=list)
    variance_mode: str = "neutral"
    margin: float | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.changes)

    def summary(self) -> str:
        lines = [
            f"projected {self.projected_points:.1f}"
            + (f" vs opponent {self.projected_points - self.margin:.1f}"
               if self.margin is not None else "")
            + f" · playing for {self.variance_mode}"
        ]
        for a in self.assignments:
            who = a.player.name if a.player else "— EMPTY —"
            lines.append(f"  {a.slot:9} {who:24} {a.points:6.1f}")
        for pl, frm, to, why in self.changes:
            lines.append(f"  CHANGE {pl.name}: {frm} -> {to} ({why})")
        for n in self.notes:
            lines.append(f"  note: {n}")
        return "\n".join(lines)


def _startable(p: Player, v: Valuation) -> bool:
    """§4.3 — never start OUT or DOUBTFUL, and never a vetoed player."""
    return not v.vetoed and not p.injury_status.cannot_start


def optimal_lineup(
    roster: list[Player],
    valuations: dict[int, Valuation],
    settings: LeagueSettings,
    *,
    week: int | None = None,
) -> LineupPlan:
    """Maximise projected points across legal slots.

    Greedy by scarcity: fill the most constrained slots (single-position) before
    flex. With this many slots and players an exact assignment would also be
    cheap, but scarcity-first is easier to explain in a log and gives the same
    answer whenever the eligibility graph is this shallow.
    """
    by_id = {p.espn_id: p for p in roster}
    available = {
        p.espn_id
        for p in roster
        if p.espn_id in valuations and _startable(p, valuations[p.espn_id])
    }

    slots: list[tuple[str, tuple[Pos, ...]]] = []
    for s in settings.starting_slots:
        slots.extend([(s.name, s.eligible)] * s.count)
    slots.sort(key=lambda s: len(s[1]))  # most constrained first

    assignments: list[SlotAssignment] = []
    for name, eligible in slots:
        best_id, best_pts = None, float("-inf")
        for pid in available:
            p = by_id[pid]
            if p.pos not in eligible:
                continue
            pts = valuations[pid].points
            if pts > best_pts:
                best_id, best_pts = pid, pts
        if best_id is None:
            assignments.append(SlotAssignment(name, None, None))
        else:
            available.discard(best_id)
            assignments.append(
                SlotAssignment(name, by_id[best_id], valuations[best_id])
            )

    bench = [by_id[pid] for pid in available]
    total = sum(a.points for a in assignments)
    plan = LineupPlan(assignments=assignments, bench=bench, projected_points=total)

    for a in assignments:
        if a.player is None:
            plan.notes.append(f"{a.slot} could not be filled — no eligible healthy player")
    return plan


def apply_variance_preference(
    plan: LineupPlan,
    roster: list[Player],
    valuations: dict[int, Valuation],
    settings: LeagueSettings,
    opponent_projected: float | None,
) -> LineupPlan:
    """§4.2 — floor when heavily favoured, ceiling when heavily behind.

    | margin        | play for | why                                          |
    |---------------|----------|----------------------------------------------|
    | favoured >12  | floor    | we win unless something breaks               |
    | inside ±12    | points   | nothing to game                              |
    | behind >12    | ceiling  | losing by 5 and by 30 pay the same           |
    """
    p = priors()
    if opponent_projected is None:
        plan.notes.append("no opponent projection — playing straight expected points")
        return plan

    margin = plan.projected_points - opponent_projected
    plan.margin = margin
    floor_at = float(p.get("lineup.floor_margin_pts"))
    ceil_at = float(p.get("lineup.ceiling_margin_pts"))

    if margin > floor_at:
        mode, budget = "floor", float(p.get("lineup.floor_max_points_sacrificed"))
    elif margin < -ceil_at:
        mode, budget = "ceiling", float(p.get("lineup.ceiling_max_points_sacrificed"))
    else:
        plan.variance_mode = "expected points"
        return plan

    plan.variance_mode = mode
    by_id = {pl.espn_id: pl for pl in roster}
    bench_ids = {pl.espn_id for pl in plan.bench}

    for a in plan.assignments:
        if a.player is None or a.valuation is None:
            continue
        slot_eligible = next(
            (s.eligible for s in settings.starting_slots if s.name == a.slot), ()
        )
        starter_v = a.valuation
        if starter_v.stdev is None:
            continue

        for pid in list(bench_ids):
            cand = by_id[pid]
            cv = valuations.get(pid)
            if cv is None or cv.stdev is None or cand.pos not in slot_eligible:
                continue
            if not _startable(cand, cv):
                continue
            give_up = starter_v.points - cv.points
            if give_up < 0 or give_up > budget:
                continue
            better = (
                cv.stdev < starter_v.stdev if mode == "floor" else cv.stdev > starter_v.stdev
            )
            if not better:
                continue

            plan.changes.append((
                cand, "BE", a.slot,
                f"§4.2 {mode}: stdev {cv.stdev:.1f} vs {starter_v.stdev:.1f}, "
                f"costs {give_up:.1f} proj pts",
            ))
            bench_ids.discard(pid)
            bench_ids.add(a.player.espn_id)
            a.player, a.valuation = cand, cv
            break

    plan.bench = [by_id[i] for i in bench_ids]
    plan.projected_points = sum(a.points for a in plan.assignments)
    return plan


def diff_against_current(
    plan: LineupPlan,
    current_starters: dict[int, str],
) -> list[tuple[Player, str, str, str]]:
    """What actually has to change on ESPN. `current_starters` maps espn_id ->
    slot name for whoever is currently in a starting slot."""
    moves: list[tuple[Player, str, str, str]] = []
    planned = {
        a.player.espn_id: a.slot for a in plan.assignments if a.player is not None
    }

    for pid, slot in planned.items():
        cur = current_starters.get(pid)
        if cur != slot:
            player = next(a.player for a in plan.assignments if a.player.espn_id == pid)
            moves.append((player, cur or "BE", slot, "optimal lineup"))
    return moves


def build(
    roster: list[Player],
    valuations: dict[int, Valuation],
    settings: LeagueSettings,
    *,
    opponent_projected: float | None = None,
    current_starters: dict[int, str] | None = None,
    week: int | None = None,
) -> LineupPlan:
    """The whole §4 decision, start to finish."""
    plan = optimal_lineup(roster, valuations, settings, week=week)
    plan = apply_variance_preference(
        plan, roster, valuations, settings, opponent_projected
    )
    if current_starters is not None:
        plan.changes = diff_against_current(plan, current_starters) + plan.changes
    return plan
