"""§2.3–2.4 — value over replacement, and tiers.

Tiers matter more than ranks. The difference between the 14th and 16th ranked
player is noise; the difference between the last man in a tier and the first man
out of it is the whole game. §3.4 picks on tiers, not on the ordinal list.
"""

from __future__ import annotations

import statistics
from collections import defaultdict

from core.model.priors import priors
from core.model.replacement import replacement_baseline
from core.model.schema import LeagueSettings, Player, Pos


def vor_for(points: float, pos: Pos, baseline: dict[Pos, float]) -> float:
    """Points above the replacement-level starter at this position."""
    return points - baseline.get(pos, 0.0)


def compute_vor(
    pool: list[Player],
    settings: LeagueSettings,
    *,
    points_of: dict[int, float],
    week: int | None = None,
) -> dict[int, float]:
    """VOR for every player in the pool, keyed by espn_id.

    `points_of` is the already-adjusted projection per player (context and
    availability applied) so this function stays purely about the baseline.
    """
    baseline = replacement_baseline(pool, settings, week=week)
    return {
        p.espn_id: vor_for(points_of.get(p.espn_id, 0.0), p.pos, baseline)
        for p in pool
    }


def tiers_for_position(
    values: list[tuple[int, float]],
    *,
    gap_multiple: float | None = None,
) -> dict[int, int]:
    """Assign 1-indexed tiers to (espn_id, vor) pairs at ONE position.

    A tier ends where the drop to the next player exceeds `gap_multiple` times
    the median gap seen so far inside the current tier (§2.4). Using the median
    of the current tier rather than of the whole position stops one enormous
    gap at the top from swallowing every later break.
    """
    if gap_multiple is None:
        gap_multiple = float(priors().get("model.tier_break_gap_multiple"))

    ranked = sorted(values, key=lambda kv: kv[1], reverse=True)
    if not ranked:
        return {}

    out: dict[int, int] = {}
    tier = 1
    current_gaps: list[float] = []
    out[ranked[0][0]] = tier

    for (_prev_id, prev_v), (pid, v) in zip(ranked, ranked[1:], strict=False):
        gap = prev_v - v
        # Need at least two gaps inside a tier before a median means anything;
        # until then, keep accumulating rather than breaking on the first step.
        if len(current_gaps) >= 2:
            median = statistics.median(current_gaps)
            # A zero median (identical projections) can't scale — fall back to
            # an absolute break so ties don't create one tier per player.
            threshold = median * gap_multiple if median > 0 else float("inf")
            if gap > threshold:
                tier += 1
                current_gaps = []
                out[pid] = tier
                continue
        current_gaps.append(gap)
        out[pid] = tier

    return out


def compute_tiers(
    pool: list[Player],
    vors: dict[int, float],
    *,
    gap_multiple: float | None = None,
) -> dict[int, int]:
    """Tiers across the whole pool, computed independently per position."""
    by_pos: dict[Pos, list[tuple[int, float]]] = defaultdict(list)
    for p in pool:
        by_pos[p.pos].append((p.espn_id, vors.get(p.espn_id, 0.0)))

    out: dict[int, int] = {}
    for _pos, values in by_pos.items():
        out.update(tiers_for_position(values, gap_multiple=gap_multiple))
    return out


def tier_members(
    pool: list[Player],
    tiers: dict[int, int],
    pos: Pos,
    tier: int,
) -> list[Player]:
    """Everyone left in a given position's tier. §3.4 asks how many remain."""
    return [p for p in pool if p.pos is pos and tiers.get(p.espn_id) == tier]
