"""§2.6 — variance as a stored attribute, never a baked-in preference.

Weekly variance matters, but which DIRECTION it matters in depends on the
matchup, and that is decided at §4.2 by lineup.py. This module measures; it does
not judge. Baking a risk preference into the base number would silently apply a
draft-time opinion to a week-14 start/sit call.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass


@dataclass(frozen=True)
class VarianceProfile:
    stdev: float | None
    bust_rate: float | None
    boom_rate: float | None
    games: int

    @property
    def known(self) -> bool:
        return self.games >= 4


def profile(
    weekly_actual: dict[int, float],
    weekly_projected: dict[int, float] | None = None,
    *,
    min_games: int = 4,
) -> VarianceProfile:
    """Weekly scoring volatility.

    bust_rate: share of played weeks under 50% of that week's projection (§2.6).
    boom_rate: share over 150%. Both need a projection to mean anything; without
    one they come back None rather than being faked off the mean.

    Weeks with zero points are excluded: a bye or an inactive is not a bust, and
    counting them turns "was injured" into "is volatile."
    """
    played = {w: pts for w, pts in weekly_actual.items() if pts > 0}
    n = len(played)
    if n < min_games:
        return VarianceProfile(None, None, None, n)

    scores = list(played.values())
    stdev = statistics.pstdev(scores) if n > 1 else 0.0

    bust = boom = None
    if weekly_projected:
        pairs = [
            (pts, weekly_projected[w])
            for w, pts in played.items()
            if weekly_projected.get(w, 0.0) > 0
        ]
        if len(pairs) >= min_games:
            bust = sum(1 for a, p in pairs if a < 0.5 * p) / len(pairs)
            boom = sum(1 for a, p in pairs if a > 1.5 * p) / len(pairs)

    return VarianceProfile(
        stdev=round(stdev, 3),
        bust_rate=round(bust, 3) if bust is not None else None,
        boom_rate=round(boom, 3) if boom is not None else None,
        games=n,
    )


def consistency_score(p: VarianceProfile, mean_points: float) -> float | None:
    """Coefficient of variation, inverted: 1.0 is metronomic, 0.0 is a coin flip.

    Only meaningful relative to other players at the same position — a TE and a
    QB do not share a scale.
    """
    if not p.known or p.stdev is None or mean_points <= 0:
        return None
    cv = p.stdev / mean_points
    return round(max(0.0, 1.0 - cv), 3)
