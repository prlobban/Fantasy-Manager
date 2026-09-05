"""§2.2b — blend ESPN's consensus draft board into our projection, and §6.3 —
what the market thinks a player is worth in a trade.

**Why the blend exists.** The autopick benchmark measured our board against
ESPN's own published draft rankings and our board lost. Rank correlation with
actual season points, over the drafted universe:

    our projection   2024 +0.593   2025 +0.484
    ESPN PPR rank    2024 +0.503   2025 +0.382
    our VOR order    2024 +0.264   2025 +0.359

Our projection is the better single signal, but it is not a *better board* — and
the two disagree in ways that turn out to be informative. Blending them improved
every block of the benchmark, taking mean finish from 6.80 of 10 to 3.65 and the
head-to-head from 13/40 to 28/40.

A projection is one model's point estimate. A consensus ranking is a market: it
prices camp reports, role changes, holdouts and beat-writer noise that no
season-total projection carries.

**The mechanic.** Rank and points are different units, so the ranking is mapped
onto our own points scale. Two ladders are available:

- `by_position=False` (the 09-05 draft): a player ESPN ranks 12th overall is
  worth what the 12th-best player on OUR board is worth, *whatever his
  position*. That ladder is dominated by QBs and RBs at the top, so a tight end
  ranked 45th inherits a running back's 45th-place points and then has a
  tight end's replacement level subtracted from it. The 2026 draft post-mortem
  (docs/draft-post-mortem-2026.md) found this is how Colston Loveland, ADP 42,
  became the 20th-most valuable player on the board and a third-round pick.
- `by_position=True`: the ladder is built per position, from ESPN's rank
  ORDER within the position — the 3rd-ranked TE is worth what our 3rd-best TE
  projects. Same units on both sides of the average; no cross-position
  contamination.

Both are kept because the benchmark decides, not the argument.

**Which board.** ESPN publishes STANDARD and PPR. This league scores 0.5 a
reception, so PPR is the right one on principle — and it also measured better
(finish 3.65 against 4.22 for STANDARD).
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict

from core.model.schema import Player, Pos

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


def blend(players: list[Player], ranks: dict[int, int], weight: float,
          *, by_position: bool = False) -> int:
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
    if not players:
        return 0

    if by_position:
        # Per-position ladders, and the consensus order WITHIN the position.
        ladders: dict[Pos, list[float]] = defaultdict(list)
        for p in players:
            ladders[p.pos].append(p.proj_season)
        for lad in ladders.values():
            lad.sort(reverse=True)
        pos_rank: dict[int, int] = {}
        by_pos: dict[Pos, list[Player]] = defaultdict(list)
        for p in players:
            if ranks.get(p.espn_id):
                by_pos[p.pos].append(p)
        for _pos, ps in by_pos.items():
            for i, p in enumerate(sorted(ps, key=lambda x: ranks[x.espn_id]), 1):
                pos_rank[p.espn_id] = i
        n = 0
        for p in players:
            r = pos_rank.get(p.espn_id)
            if not r:
                continue
            lad = ladders[p.pos]
            implied = lad[min(r - 1, len(lad) - 1)]
            p.proj_season = (1.0 - weight) * p.proj_season + weight * implied
            n += 1
        log.info("market blend w=%.2f (by position) applied to %d/%d players",
                 weight, n, len(players))
        return n

    ladder = sorted((p.proj_season for p in players), reverse=True)
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


# ── §6.3 / D9 — what the market thinks a player is worth ────────────────────

def trade_value(market_rank: float | None, *, decay: float = 0.02) -> float:
    """A redraft trade-value curve, 0–100, from a market rank.

    Shape: the 1st player is 100, the 12th ~80, the 24th ~63, the 48th ~39,
    the 96th ~15, the 150th ~5. That is the shape of every published trade
    value chart: the top is steep, the middle is flat, and past round eight
    almost everything is worth the same small number. A player with no market
    rank at all (nobody drafted him, nobody owns him) is worth 1.

    This is deliberately NOT our valuation. Our valuation says what he is
    worth *to our lineup*; this says what another human believes he is worth,
    which is what decides whether an offer gets accepted (D9).
    """
    if market_rank is None or market_rank <= 0:
        return 1.0
    return round(max(1.0, 100.0 * math.exp(-decay * (market_rank - 1.0))), 1)


def market_rank(player: Player, ros_rank: int | None, *, week: int,
                adp_decay_weeks: float = 8.0) -> float | None:
    """The rank the market currently uses for a player.

    In September the market IS the draft: what the room paid for him is what
    the room still thinks he is worth. As the season goes on ADP stops
    describing anyone and the rest-of-season projection rank takes over. The
    two are averaged with a weight that runs from all-ADP in week 1 to all-ROS
    by `adp_decay_weeks` weeks in.
    """
    adp = player.espn_adp
    if adp is None and ros_rank is None:
        return None
    if adp is None:
        return float(ros_rank)
    if ros_rank is None:
        return float(adp)
    t = min(1.0, max(0.0, (week - 1) / max(1.0, adp_decay_weeks)))
    return (1.0 - t) * float(adp) + t * float(ros_rank)
