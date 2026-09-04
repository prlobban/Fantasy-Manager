"""Score a replayed roster against what actually happened.

Three policies, and the gap between them is the point:

- **hindsight** — the best legal lineup each week, knowing the results. The
  ceiling the roster contained, so it grades the DRAFT alone and nothing else.
- **engine** — `core.manager.lineup.optimal_lineup` choosing on that week's
  projection. Grades draft plus start/sit, which is the number a real season
  would produce.
- **naive** — set once off preseason season projections and never touched. The
  baseline a human beats by accident, and the honest floor to measure against.

All three run through the SAME slot-filling code the live manager uses, fed
different points. Reimplementing "best lineup" here would mean the backtest
grades a reimplementation instead of the engine.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field

from core.manager.lineup import optimal_lineup
from core.model.schema import LeagueSettings, Player, Valuation

log = logging.getLogger(__name__)

Policy = str  # "hindsight" | "engine" | "naive"
POLICIES: tuple[Policy, ...] = ("hindsight", "engine", "naive")


def _val(espn_id: int, pts: float) -> Valuation:
    """A Valuation carrying only what the lineup optimiser reads."""
    return Valuation(espn_id=espn_id, window="week", points=pts, vor=0.0,
                     tier=1, availability=1.0)


@dataclass
class TeamSeason:
    team_id: int
    policy: Policy
    weekly: dict[int, float] = field(default_factory=dict)

    @property
    def total(self) -> float:
        return sum(self.weekly.values())

    @property
    def mean(self) -> float:
        return statistics.fmean(self.weekly.values()) if self.weekly else 0.0

    @property
    def stdev(self) -> float:
        v = list(self.weekly.values())
        return statistics.pstdev(v) if len(v) > 1 else 0.0


@dataclass
class LeagueResult:
    """Every replayed team scored the same way, so ours can be ranked."""

    season: int
    policy: Policy
    weeks: list[int]
    teams: dict[int, TeamSeason] = field(default_factory=dict)

    def rank_of(self, team_id: int) -> int:
        order = sorted(self.teams.values(), key=lambda t: -t.total)
        return next(i + 1 for i, t in enumerate(order) if t.team_id == team_id)

    def all_play(self, team_id: int) -> tuple[int, int]:
        """(wins, losses) against every other team every week.

        Preferred over the real schedule on purpose: a 13-week schedule against
        9 opponents is 13 coin flips of opponent luck, while all-play is the
        same rosters with the luck removed — and the engine is being graded on
        the roster it built, not on who it happened to draw.
        """
        me = self.teams[team_id]
        wins = losses = 0
        for wk in self.weeks:
            mine = me.weekly.get(wk, 0.0)
            for tid, other in self.teams.items():
                if tid == team_id:
                    continue
                theirs = other.weekly.get(wk, 0.0)
                if mine > theirs:
                    wins += 1
                elif mine < theirs:
                    losses += 1
        return wins, losses


def _points_for(policy: Policy, p: Player, wk: int,
                actuals: dict[int, dict[int, float]]) -> float:
    if policy == "hindsight":
        return actuals.get(p.espn_id, {}).get(wk, 0.0)
    if policy == "engine":
        return p.proj_week.get(wk, 0.0)
    return p.proj_season  # naive: one fixed ordering all season


def score_roster(roster: list[Player], settings: LeagueSettings, *,
                 policy: Policy, weeks: list[int],
                 actuals: dict[int, dict[int, float]]) -> dict[int, float]:
    """Points scored per week by one roster under one lineup policy.

    The lineup is CHOSEN on the policy's basis but always SCORED on actuals —
    that separation is the entire measurement. A lineup picked on projections
    that then scores what it really scored is what a manager actually lives.
    """
    out: dict[int, float] = {}
    for wk in weeks:
        vals = {p.espn_id: _val(p.espn_id, _points_for(policy, p, wk, actuals))
                for p in roster}
        plan = optimal_lineup(roster, vals, settings, week=wk)
        out[wk] = sum(
            actuals.get(a.player.espn_id, {}).get(wk, 0.0)
            for a in plan.assignments if a.player is not None
        )
    return out


def score_league(rosters: dict[int, list[Player]], settings: LeagueSettings, *,
                 season: int, policy: Policy, weeks: list[int],
                 actuals: dict[int, dict[int, float]]) -> LeagueResult:
    res = LeagueResult(season=season, policy=policy, weeks=weeks)
    for team_id, roster in rosters.items():
        res.teams[team_id] = TeamSeason(
            team_id=team_id, policy=policy,
            weekly=score_roster(roster, settings, policy=policy,
                                weeks=weeks, actuals=actuals),
        )
    return res


def actuals_from_season(season, rescored: dict[int, dict[int, float]] | None = None
                        ) -> dict[int, dict[int, float]]:
    """espn_id -> week -> points actually scored.

    `rescored` (from `rescore.rescored_weeks`) substitutes a different scoring
    map; without it, ESPN's own totals for that season are used.
    """
    if rescored is not None:
        return rescored
    return {p.espn_id: dict(p.actual_week) for p in season.players}
