"""§3.5 — will he still be there at our next pick?

The formal version of "future sight." For each position:

    Cost(pos) = BestAvailableNow(pos) - E[BestAvailable(pos) at our next pick]

Draft the position with the largest Cost — the one where waiting hurts most.
A player 60% likely to survive is not a player you must take now.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from core.model.schema import Player, Pos, Valuation

#: When ESPN gives no ADP spread, assume this much noise (in picks) around ADP.
#: Real drafts are messier than ADP early and tighter late, so the spread scales
#: with ADP itself rather than being flat.
def default_adp_stdev(adp: float) -> float:
    return max(4.0, 0.22 * adp)


def p_survives(adp: float | None, adp_stdev: float | None, picks_until: int, *,
               taken_offset: int = 0) -> float:
    """Probability a player is still on the board `picks_until` picks from now.

    Modelled as P(his draft slot > current_pick + picks_until) with the slot
    normally distributed around ADP. Undrafted players (no ADP) are treated as
    near-certain to survive, which is correct: nobody is taking a player the
    room has never heard of.
    """
    if picks_until <= 0:
        return 1.0
    if adp is None:
        return 0.97

    sd = adp_stdev or default_adp_stdev(adp)
    if sd <= 0:
        return 0.0 if adp <= taken_offset + picks_until else 1.0

    # z of the threshold pick under the player's ADP distribution.
    z = (taken_offset + picks_until - adp) / sd
    # P(slot > threshold) = 1 - Phi(z)
    return max(0.0, min(1.0, 0.5 * math.erfc(z / math.sqrt(2.0))))


@dataclass
class PositionOutlook:
    pos: Pos
    best_now: float
    expected_next: float
    cost: float
    #: The player we'd take now at this position.
    best_now_id: int | None
    #: How many are left in the top remaining tier — the §3.4 "about to break" signal.
    top_tier_remaining: int
    top_tier: int | None


def expected_best_available(
    candidates: list[tuple[Player, Valuation]],
    picks_until: int,
    *,
    current_pick: int = 0,
) -> float:
    """E[VOR of the best player left at this position] after `picks_until` picks.

    Walks the position's board in value order. The expected best is the first
    survivor, so the probability that player k is the best remaining is
    P(k survives) x P(nobody better survives).
    """
    if not candidates:
        return 0.0

    ranked = sorted(candidates, key=lambda cv: cv[1].vor, reverse=True)
    expected = 0.0
    none_better_survived = 1.0

    for player, val in ranked:
        # picks_until is relative to now; ADP is absolute, so offset by where we are.
        ps = p_survives(player.espn_adp, player.adp_stdev, picks_until,
                        taken_offset=current_pick)
        expected += none_better_survived * ps * val.vor
        none_better_survived *= 1.0 - ps
        if none_better_survived < 1e-4:
            break

    # Whatever probability mass is left means every listed player is gone; the
    # replacement-level fallback is 0 VOR by definition.
    return expected


def position_outlook(
    pos: Pos,
    candidates: list[tuple[Player, Valuation]],
    picks_until: int,
    *,
    current_pick: int = 0,
) -> PositionOutlook:
    if not candidates:
        return PositionOutlook(pos, 0.0, 0.0, 0.0, None, 0, None)

    ranked = sorted(candidates, key=lambda cv: cv[1].vor, reverse=True)
    best_player, best_val = ranked[0]
    exp_next = expected_best_available(candidates, picks_until, current_pick=current_pick)

    top_tier = best_val.tier
    remaining = sum(1 for _, v in ranked if v.tier == top_tier)

    return PositionOutlook(
        pos=pos,
        best_now=best_val.vor,
        expected_next=exp_next,
        cost=best_val.vor - exp_next,
        best_now_id=best_player.espn_id,
        top_tier_remaining=remaining,
        top_tier=top_tier,
    )
