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
    availability_of: dict[int, float] | None = None,
    weeks: float | None = None,
) -> dict[int, float]:
    """VOR for every player in the pool, keyed by espn_id.

    `points_of` is the already-adjusted projection per player.

    🔴 `availability_of` changes the formula, and the reason matters (§2.3,
    §2.5). Over a season, availability must scale the SURPLUS, not the total:

        VOR = availability x (points - replacement points)

    Multiplying the total instead — which is what an availability-scaled
    `points_of` does on its own — assumes the slot scores ZERO in the weeks a
    player misses. It does not: you start a waiver-wire replacement, so the
    floor is replacement level, not nothing. That mistake charged injury-prone
    players their whole projection for missed weeks rather than the surplus,
    and it produced values that were obviously wrong on their face — Jayden
    Daniels at -103 VOR, Lamar Jackson at -37, both elite quarterbacks.

    The test that catches it: the player AT the replacement rank must come out
    at exactly 0.0. Under the old formula Mahomes, the replacement quarterback,
    scored -63.5. Caught 2026-09-04.

    Pass availability for a season-long (`ros`) window. For a single week
    leave it out: availability is not a scalar there — either he plays or he
    does not, and durability.py has already applied the status discount.
    """
    if availability_of:
        # Recover the pre-availability projection, baseline on that scale,
        # then discount the surplus.
        raw_of = {
            p.espn_id: (points_of.get(p.espn_id, 0.0) / a
                        if (a := availability_of.get(p.espn_id, 1.0)) > 0 else 0.0)
            for p in pool
        }
        baseline = replacement_baseline(pool, settings, week=week, points_of=raw_of,
                                        weeks=weeks)
        out: dict[int, float] = {}
        for p in pool:
            a = availability_of.get(p.espn_id, 1.0)
            if a <= 0:
                # Cannot play at all. Multiplying the surplus by zero would
                # score him 0.0 — i.e. exactly replacement level — which in the
                # dead rounds, where every real player is negative, would float
                # him to the TOP of the board. He is worth less than nothing:
                # he cannot fill the slot he occupies. (Today every such player
                # is also vetoed and the picker drops him; this is here so that
                # stops being load-bearing.)
                out[p.espn_id] = -baseline.get(p.pos, 0.0)
                continue
            out[p.espn_id] = a * vor_for(raw_of.get(p.espn_id, 0.0), p.pos, baseline)
        return out

    baseline = replacement_baseline(pool, settings, week=week, points_of=points_of,
                                    weeks=weeks)
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
