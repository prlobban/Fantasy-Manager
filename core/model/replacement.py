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


def streaming_bonus(pos: Pos, settings: LeagueSettings, *, weeks: float) -> float:
    """§2.3 / D6 — what streaming adds to replacement level at a one-starter
    position, in points over `weeks`.

    The rank-N+1 baseline is one player's average. Nobody starts that player
    every week: at QB, TE, K and D/ST a manager starts the best MATCHUP off
    the wire each week, and the max over a handful of free agents beats any
    one of their averages. That gap is why the market drafts QBs and TEs
    rounds later than season-total VOR says to — and why this engine took a
    quarterback 12th overall and a rookie tight end 29th on 2026-09-05.
    Priors per position, swept on the autopick benchmark.
    """
    if settings.starters_at(pos) != 1:
        return 0.0
    from core.model.priors import priors
    try:
        per_week = float(priors().get(f"model.streaming_bonus_per_week.{pos.name}"))
    except KeyError:
        return 0.0
    return per_week * weeks


def replacement_points(
    pos: Pos,
    pool: list[Player],
    settings: LeagueSettings,
    *,
    projection: str = "proj_season",
    week: int | None = None,
    points_of: dict[int, float] | None = None,
    weeks: float | None = None,
) -> float:
    """Projected points of the replacement-level player at `pos`.

    🔴 `points_of` — the ALREADY-ADJUSTED points every caller in the live path
    passes — is the scale VOR is measured on, so it is the scale the baseline
    must be measured on too. Both the ranking and the returned value use it.

    Without it this read raw `proj_season` while `compute_vor` subtracted the
    baseline from availability-adjusted points: two different scales, and the
    gap between them differed by position (6 pts at D/ST, 36 at QB), so it
    silently re-ranked players ACROSS positions — the one thing VOR exists to
    get right. The tell was that the replacement player himself did not score
    zero: Mahomes, the QB at replacement rank, came out at -63.5 VOR.
    Caught 2026-09-04.

    Falls back to the worst available player at the position if the pool is
    shallower than the replacement rank, which happens for K and D/ST in small
    pools. Returns 0.0 for an empty pool — the caller sees vor == points, which
    is correct: with nobody to replace him, every point is surplus.
    """
    at_pos = [p for p in pool if p.pos is pos]
    if not at_pos:
        return 0.0

    def proj(p: Player) -> float:
        if points_of is not None:
            return points_of.get(p.espn_id, 0.0)
        if week is not None:
            return p.proj_week.get(week, 0.0)
        return getattr(p, projection, 0.0)

    ranked = sorted(at_pos, key=proj, reverse=True)
    idx = min(replacement_rank(pos, settings), len(ranked)) - 1
    base = proj(ranked[idx])

    # In-season, at a one-starter position, replacement level is not "the
    # 11th-best season total" — it is the best player sitting on the wire
    # THIS week, because that is who you would actually start instead
    # (D6, streaming). A pool that is still all unrostered (a draft board)
    # has no wire, so the rank baseline stands.
    if settings.starters_at(pos) == 1 and _in_season(at_pos, settings, pos):
        free = [p for p in at_pos if p.on_team_id is None]
        if free:
            base = max(base, max(proj(p) for p in free))
    if weeks is None:
        weeks = 1.0 if week is not None else float(settings.regular_season_weeks)
    return base + streaming_bonus(pos, settings, weeks=weeks)


def _in_season(at_pos: list[Player], settings: LeagueSettings, pos: Pos) -> bool:
    """True once the league has actually rostered its starters at `pos`."""
    rostered = sum(1 for p in at_pos if p.on_team_id is not None)
    return rostered >= settings.team_count * settings.starters_at(pos)


def replacement_baseline(
    pool: list[Player],
    settings: LeagueSettings,
    *,
    week: int | None = None,
    points_of: dict[int, float] | None = None,
    weeks: float | None = None,
) -> dict[Pos, float]:
    """Replacement points for every position present in the pool.

    Pass `points_of` whenever you have adjusted points — see the note in
    replacement_points about why the two must share a scale. `weeks` is the
    window the points cover (1 for a week, the weeks remaining for ROS); it
    scales the streaming bonus.
    """
    present = Counter(p.pos for p in pool)
    return {
        pos: replacement_points(pos, pool, settings, week=week, points_of=points_of,
                                weeks=weeks)
        for pos in present
    }


def flex_eligible_positions(slot_name: str) -> tuple[Pos, ...]:
    """Positions a named flex slot accepts. Unknown slots accept nothing, which
    fails loudly rather than silently making a slot universal."""
    return FLEX_ELIGIBLE.get(slot_name, ())
