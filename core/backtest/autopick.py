"""ESPN's own autodraft, reproduced.

The benchmark opponent. Everything here is what ESPN actually does when it
drafts for a team that set nothing up — which is the thing the engine has to
beat to be worth running at all.

**The ranking is ESPN's, not ours.** Every player in a season pull carries
`draftRanksByRankType`, holding an integer `rank` under `STANDARD` and `PPR`.
All 450 players carry both in both seasons, and the STANDARD order reproduces
ESPN's `sortDraftRanks` sort exactly. That is ESPN's published preseason board.

**The algorithm.** ESPN autodrafts from the team's own player queue first, then
takes the best available by that rank, subject to roster legality. A bot has no
queue, so only the second half exists here — which is precisely what "ESPN
picked for me" means for a manager who set nothing up.

There is no VOR, no tier logic, no scarcity, no bye management and no notion of
what other teams need. That absence is the entire point of the comparison: it
is the control, and it must not be quietly improved.
"""

from __future__ import annotations

import logging
from collections import Counter

from core.model.schema import LeagueSettings, Player, Pos

log = logging.getLogger(__name__)

#: ESPN's two published preseason boards. A half-PPR league sits between them,
#: so the benchmark runs both rather than picking the flattering one.
RANKINGS = ("STANDARD", "PPR")

#: A player ESPN has no rank for goes to the very back of the board.
UNRANKED = 10_000


def ranks_from_pool(pool_raw: list[dict], ranking: str = "STANDARD") -> dict[int, int]:
    """espn_id -> ESPN's preseason draft rank, straight off the raw pull."""
    out: dict[int, int] = {}
    for entry in pool_raw:
        p = entry.get("player") or {}
        pid = p.get("id")
        if pid is None:
            continue
        node = (p.get("draftRanksByRankType") or {}).get(ranking) or {}
        rank = node.get("rank")
        if rank is not None:
            out[int(pid)] = int(rank)
    return out


def _unfilled_starting(roster: Counter, settings: LeagueSettings) -> dict[Pos, int]:
    """Dedicated starting slots this roster has not yet filled.

    Flex is excluded on purpose: ESPN's endgame rule is about being able to
    field a legal lineup, and a flex can be filled by a spare it already has.
    """
    out: dict[Pos, int] = {}
    for pos in (Pos.QB, Pos.RB, Pos.WR, Pos.TE, Pos.K, Pos.DST):
        need = settings.starters_at(pos) - roster.get(pos, 0)
        if need > 0:
            out[pos] = need
    return out


def pick(available: list[Player], roster: Counter, *, rounds_left: int,
         settings: LeagueSettings, limits: dict[Pos, int],
         ranks: dict[int, int]) -> Player | None:
    """The next autodraft pick, or None if nothing is legal.

    `rounds_left` counts this pick, so `rounds_left == 1` is the final round.
    """
    legal = [p for p in available
             if roster.get(p.pos, 0) < limits.get(p.pos, 99)]
    if not legal:
        return None

    # Once there are exactly as many picks left as empty starting slots, every
    # remaining pick has to fill one or the team cannot field a lineup. ESPN
    # enforces this; without it a bot ends the draft with no kicker and the
    # comparison stops being against ESPN.
    need = _unfilled_starting(roster, settings)
    if need and rounds_left <= sum(need.values()):
        forced = [p for p in legal if p.pos in need]
        if forced:
            legal = forced

    return min(legal, key=lambda p: ranks.get(p.espn_id, UNRANKED))
