"""§7 — the Tuesday loop. How this system gets less wrong.

The central discipline is §7.3: distinguish a bad DECISION from a bad OUTCOME.
Starting the 14-point projection over the 9-point one was correct even when it
scored 3. In a game this variant, grading outcomes teaches the wrong lesson, so
the number that actually measures the agent is manager efficiency — what we
scored against the best we could have scored with the roster we had.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field

from core.model.schema import LeagueSettings, Player, Pos
from core.state import decisions

log = logging.getLogger(__name__)


@dataclass
class WeekResult:
    week: int
    our_points: float
    their_points: float
    won: bool
    margin: float


@dataclass
class Efficiency:
    actual: float
    best_possible: float
    #: actual / best_possible. The only number that measures the AGENT.
    pct: float
    points_left_on_bench: float
    worst_call: str | None = None


@dataclass
class Calibration:
    """Projected vs actual, looking for DIRECTION not magnitude (§7.3)."""

    by_position: dict[str, float] = field(default_factory=dict)
    overall_bias: float = 0.0
    sample: int = 0
    notes: list[str] = field(default_factory=list)


@dataclass
class Review:
    result: WeekResult | None
    efficiency: Efficiency | None
    calibration: Calibration
    league: list[str] = field(default_factory=list)
    decisions_made: int = 0


def efficiency(
    started: list[tuple[Player, float]],
    bench: list[tuple[Player, float]],
    settings: LeagueSettings,
) -> Efficiency:
    """Actual starting points vs the best lineup available in hindsight."""
    actual = sum(pts for _, pts in started)
    everyone = started + bench

    by_pos: dict[Pos, list[tuple[Player, float]]] = {}
    for pl, pts in everyone:
        by_pos.setdefault(pl.pos, []).append((pl, pts))
    for v in by_pos.values():
        v.sort(key=lambda pp: -pp[1])

    used: set[int] = set()
    best = 0.0
    for slot in sorted(settings.starting_slots, key=lambda s: len(s.eligible)):
        for _ in range(slot.count):
            pick, pick_pts = None, float("-inf")
            for pos in slot.eligible:
                for pl, pts in by_pos.get(pos, []):
                    if pl.espn_id in used:
                        continue
                    if pts > pick_pts:
                        pick, pick_pts = pl, pts
                    break
            if pick is not None:
                used.add(pick.espn_id)
                best += pick_pts

    left = max(0.0, best - actual)
    worst = None
    if left > 0 and bench:
        top_bench = max(bench, key=lambda pp: pp[1])
        weakest = min(started, key=lambda pp: pp[1]) if started else None
        if weakest and top_bench[1] > weakest[1]:
            worst = (
                f"benched {top_bench[0].name} ({top_bench[1]:.1f}) while starting "
                f"{weakest[0].name} ({weakest[1]:.1f})"
            )

    return Efficiency(
        actual=round(actual, 1),
        best_possible=round(best, 1),
        pct=round(actual / best, 3) if best > 0 else 0.0,
        points_left_on_bench=round(left, 1),
        worst_call=worst,
    )


def calibration(
    observations: list[tuple[Player, float, float]],
    *,
    min_sample: int = 5,
) -> Calibration:
    """(player, projected, actual) -> where the model is biased.

    Reports a direction per position only when there is enough sample. One loud
    week is noise; §7.3 says only a repeated directional miss moves a prior.
    """
    cal = Calibration(sample=len(observations))
    if not observations:
        cal.notes.append("no observations")
        return cal

    errs = [actual - proj for _, proj, actual in observations if proj > 0]
    if errs:
        cal.overall_bias = round(statistics.mean(errs), 2)

    by_pos: dict[str, list[float]] = {}
    for pl, proj, actual in observations:
        if proj > 0:
            by_pos.setdefault(pl.pos.value, []).append(actual - proj)

    for pos, e in by_pos.items():
        if len(e) < min_sample:
            cal.notes.append(f"{pos}: only {len(e)} observations — not enough to read")
            continue
        cal.by_position[pos] = round(statistics.mean(e), 2)

    if cal.overall_bias > 2:
        cal.notes.append(
            f"projections ran {cal.overall_bias:+.1f} LOW on average this week — "
            "one week is noise, look for this repeating (§7.3)"
        )
    elif cal.overall_bias < -2:
        cal.notes.append(
            f"projections ran {cal.overall_bias:+.1f} HIGH on average this week — "
            "one week is noise, look for this repeating (§7.3)"
        )
    return cal


def build(
    week: int,
    started: list[tuple[Player, float]],
    bench: list[tuple[Player, float]],
    settings: LeagueSettings,
    *,
    opponent_points: float | None = None,
    projections: dict[int, float] | None = None,
    league_notes: list[str] | None = None,
) -> Review:
    eff = efficiency(started, bench, settings)

    result = None
    if opponent_points is not None:
        result = WeekResult(
            week=week,
            our_points=eff.actual,
            their_points=round(opponent_points, 1),
            won=eff.actual > opponent_points,
            margin=round(eff.actual - opponent_points, 1),
        )

    obs: list[tuple[Player, float, float]] = []
    if projections:
        for pl, pts in started + bench:
            if (proj := projections.get(pl.espn_id)):
                obs.append((pl, proj, pts))

    return Review(
        result=result,
        efficiency=eff,
        calibration=calibration(obs),
        league=league_notes or [],
        decisions_made=len(decisions.read_all(limit=200)),
    )
