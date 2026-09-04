"""§2.2b — blend ESPN's consensus draft board into our projection.

**Why this exists.** The autopick benchmark measured our board against ESPN's own
published draft rankings and our board lost. Rank correlation with actual season
points, over the drafted universe:

    our projection   2024 +0.593   2025 +0.484
    ESPN PPR rank    2024 +0.503   2025 +0.382
    our VOR order    2024 +0.264   2025 +0.359

Our projection is the better single signal, but it is not a *better board* — and
the two disagree in ways that turn out to be informative. Blending them improved
every block of the benchmark, taking mean finish from 6.80 of 10 to 3.65 and the
head-to-head from 13/40 to 28/40.

That is not surprising in hindsight. A projection is one model's point estimate.
A consensus ranking is a market: it prices camp reports, role changes, holdouts
and beat-writer noise that no season-total projection carries. Ignoring it was
the engine treating its own model as the only source of information in the world.

**The mechanic.** Rank and points are different units, so the ranking is mapped
onto our own points scale: a player ESPN ranks 12th is worth what the 12th-best
player on OUR board is worth. That makes the blend a weighted average of two
numbers in the same unit rather than a fudge between a rank and a score.

**Which board.** ESPN publishes STANDARD and PPR. This league scores 0.5 a
reception, so PPR is the right one on principle — and it also measured better
(finish 3.65 against 4.22 for STANDARD). The sensitivity is flat between weights
of 0.5 and 0.9, which is what a real effect looks like rather than a spike.
"""

from __future__ import annotations

import logging

from core.model.schema import Player

log = logging.getLogger(__name__)

#: ESPN publishes several; these are the two that rank the whole pool sensibly.
RANK_TYPES = ("PPR", "STANDARD")


def ranks_from_raw(entries: list[dict], rank_type: str = "PPR") -> dict[int, int]:
    """espn_id -> ESPN's published preseason draft rank."""
    out: dict[int, int] = {}
    for entry in entries:
        p = entry.get("player") or entry
        pid = p.get("id")
        if pid is None:
            continue
        node = (p.get("draftRanksByRankType") or {}).get(rank_type) or {}
        rank = node.get("rank")
        if rank is not None:
            out[int(pid)] = int(rank)
    return out


def blend(players: list[Player], ranks: dict[int, int], weight: float) -> int:
    """Rewrite `proj_season` as a weighted mix of ours and the consensus.

    Returns how many players were actually blended. Mutates in place, before
    valuation runs — so VOR, tiers and every downstream consumer see one number
    and there is no second scale anywhere in the system.

    A player ESPN does not rank keeps his own projection untouched, rather than
    being pushed to the back: an unranked player is one ESPN has no opinion
    about, which is not the same as an opinion that he is bad.
    """
    if weight <= 0.0:
        return 0
    if not 0.0 <= weight <= 1.0:
        raise ValueError(f"market blend weight must be in [0, 1], got {weight}")

    ladder = sorted((p.proj_season for p in players), reverse=True)
    if not ladder:
        return 0

    n = 0
    for p in players:
        rank = ranks.get(p.espn_id)
        if not rank:
            continue
        implied = ladder[min(rank - 1, len(ladder) - 1)]
        p.proj_season = (1.0 - weight) * p.proj_season + weight * implied
        n += 1

    log.info("market blend w=%.2f applied to %d/%d players", weight, n, len(players))
    return n
