"""§2.3 — replacement level, computed from THIS league.

Value is not points. Value is points above the worst player you would otherwise
have to start. The top QBs project huge and are worth little, because QB12 also
projects huge — that difference only shows up once replacement level is computed
against the league's actual starting requirements.
"""

from __future__ import annotations

from collections import Counter

from core.model.schema import FLEX_ELIGIBLE, LeagueSettings, Player, Pos

#: Share of flex slots that end up filled by each position, league-wide.
#: A flex is nominally RB/WR/TE but is not filled evenly — in PPR it skews WR,
#: in standard it skews RB. These are starting weights; §7 moves them if the
#: season says otherwise.
_FLEX_SHARE_PPR: dict[Pos, float] = {Pos.RB: 0.35, Pos.WR: 0.55, Pos.TE: 0.10}
_FLEX_SHARE_STD: dict[Pos, float] = {Pos.RB: 0.60, Pos.WR: 0.35, Pos.TE: 0.05}


def flex_share(pos: Pos, settings: LeagueSettings) -> float:
    """Expected share of flex slots this position fills, given scoring."""
    if pos not in (Pos.RB, Pos.WR, Pos.TE):
        return 0.0
    ppr = settings.ppr_value
    if ppr >= 0.5:
        base = _FLEX_SHARE_PPR
    elif ppr > 0:
        # Half-PPR: interpolate between the two.
        w = ppr / 0.5
        return _FLEX_SHARE_STD[pos] * (1 - w) + _FLEX_SHARE_PPR[pos] * w
    else:
        base = _FLEX_SHARE_STD
    return base[pos]


def replacement_rank(pos: Pos, settings: LeagueSettings) -> int:
    """The positional rank of the replacement-level starter.

        rank = (teams x dedicated starters) + (teams x flex slots x flex share) + 1

    Superflex is handled naturally: QB gains flex share because the OP slot lists
    QB as eligible, which is exactly why QBs stop being cheap in those leagues.
    """
    dedicated = settings.team_count * settings.starters_at(pos)

    flex_count = settings.flex_slots_accepting(pos)
    if flex_count and pos == Pos.QB:
        # Superflex: nearly every team starts a second QB if one is available.
        share = 0.9
    else:
        share = flex_share(pos, settings)
    flexed = settings.team_count * flex_count * share

    return int(round(dedicated + flexed)) + 1


def replacement_points(
    pos: Pos,
    pool: list[Player],
    settings: LeagueSettings,
    *,
    projection: str = "proj_season",
    week: int | None = None,
) -> float:
    """Projected points of the replacement-level player at `pos`.

    Falls back to the worst available player at the position if the pool is
    shallower than the replacement rank, which happens for K and D/ST in small
    pools. Returns 0.0 for an empty pool — the caller sees vor == points, which
    is correct: with nobody to replace him, every point is surplus.
    """
    at_pos = [p for p in pool if p.pos is pos]
    if not at_pos:
        return 0.0

    def proj(p: Player) -> float:
        if week is not None:
            return p.proj_week.get(week, 0.0)
        return getattr(p, projection, 0.0)

    ranked = sorted(at_pos, key=proj, reverse=True)
    idx = min(replacement_rank(pos, settings), len(ranked)) - 1
    return proj(ranked[idx])


def replacement_baseline(
    pool: list[Player],
    settings: LeagueSettings,
    *,
    week: int | None = None,
) -> dict[Pos, float]:
    """Replacement points for every position present in the pool."""
    present = Counter(p.pos for p in pool)
    return {
        pos: replacement_points(pos, pool, settings, week=week) for pos in present
    }


def flex_eligible_positions(slot_name: str) -> tuple[Pos, ...]:
    """Positions a named flex slot accepts. Unknown slots accept nothing, which
    fails loudly rather than silently making a slot universal."""
    return FLEX_ELIGIBLE.get(slot_name, ())
