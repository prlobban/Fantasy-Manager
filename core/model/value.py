"""§2 — THE valuation function. One model, four consumers.

The draft, waivers, trades and start/sit are the same question over different
windows: is player A worth more to this roster than player B? §10.4 says there is
one implementation. Every consumer calls `value_pool` and nothing else; if the
agent is computing value in prose, the model has forked.

Pure: no I/O, no network, no clock. Everything it needs arrives as arguments,
which is what makes the whole engine testable without cookies.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.model.durability import DurabilityResult, InjuryEvent, availability
from core.model.schema import LeagueSettings, Player, Valuation, Window
from core.model.variance import VarianceProfile, profile
from core.model.vor import compute_tiers, compute_vor


@dataclass
class PlayerContext:
    """Everything outside ESPN's projection that bears on a player's value.

    Assembled by the caller from core/data. Absent fields degrade the valuation
    honestly (they land in `missing`) rather than being invented.
    """

    injury_history: list[InjuryEvent] = field(default_factory=list)
    age: int | None = None
    #: §2.7 weekly multipliers, each named so §7 can find which one is biased.
    #: e.g. {"opp_def": 1.08, "usage_trend": 1.05, "game_script": 0.95}
    multipliers: dict[str, float] = field(default_factory=dict)
    #: Agent's bounded pre-draft news override (§3.2). Applied last, capped.
    news_override: float | None = None
    news_reason: str | None = None
    #: Research veto (§3.2) — season-ending injury, long suspension, retired,
    #: unsigned. Joins the §2.5 veto list rather than being a second mechanism,
    #: so there is one answer to "is this player draftable at all" and one
    #: place that answer is enforced.
    news_veto: str | None = None


def _clamp_override(raw: float, cap: float) -> float:
    return max(1.0 - cap, min(1.0 + cap, raw))


def role_rate(player: Player, current_week: int) -> float | None:
    """His in-season role, as a weekly rate: the LOWER of ESPN's two most
    recent weekly projections at or before `current_week`, or None when there
    are not two to read.

    Two, because one week is a spot start, not a role: ESPN prices a backup
    quarterback's one-game start into his season row and nothing more (his
    season projection equals that week's, exactly). Two consecutive weekly
    projections at a starter's level are ESPN itself saying the job is his.
    The lower of them, because a role has to be proven at its weakest recent
    week before it reprices the rest of his season. Bye weeks are not
    evidence and are skipped; a 0.0 week (OUT, no role) is evidence, and it
    returns 0.0 — this can only ever raise a valuation, never cut one.
    """
    weeks = [w for w in sorted(player.proj_week, reverse=True)
             if w <= current_week and w != player.bye_week
             and (not player.games or w in player.games)]
    if len(weeks) < 2:
        return None
    return min(player.proj_week[w] for w in weeks[:2])


def ros_base(
    player: Player, *, current_week: int, weeks_remaining: int,
) -> tuple[float, dict[str, float]]:
    """The rest-of-season base projection, and the components that made it.

    ESPN's season row (`proj_season`) is refreshed in-season, and it is on a
    rest-of-season scale — every remaining game, this week included: across
    the live pool in week 5 of 2026 it came to 1.01x (weekly projection x
    games left on his schedule), median of 111 rostered players, 0.86-1.10
    p10-p90. But it moves slowly, and it lags a role change by weeks. Found
    2026-10-08: Emanuel Wilson, a third-stringer promoted to lead back, carried
    a 24.9 season row against weekly projections of 11.1 and 12.7, so his ROS
    VOR read -109.2 and §4.7 benched him for a QUESTIONABLE D'Andre Swift.

    🔴 The trigger has no tunable. A season row that cannot cover even the
    fantasy weeks left at his lowest recent weekly projection
    (`role_rate x weeks_remaining > proj_season`) is not a forecast of a role
    that pays less — it is a forecast of a different role. In week 5 that is
    a season row under 0.77x the scale above, below the pool's p10, so an
    established player's ordinary matchup noise never trips it, and it gets
    stricter as the season runs down. When it trips, ROS is rebuilt from the
    weekly rate over the games left on his schedule — the scale every other
    player's season row is already on — so he is compared like for like. With
    no schedule loaded, `weeks_remaining` stands in: fewer games, a lower
    number, never a higher one.

    Only ever raises. Missing, OUT and bye weeks fall through to ESPN's row.
    """
    base = player.proj_season
    comps: dict[str, float] = {}
    rate = role_rate(player, current_week)
    if rate is None or rate * weeks_remaining <= base:
        return base, comps
    games = (sum(1 for w in player.games if w >= current_week)
             if player.games else weeks_remaining)
    rebuilt = max(base, rate * games)
    comps["espn_season_projection"] = round(base, 3)
    comps["role.weekly_rate"] = round(rate, 3)
    comps["role.games_left"] = float(games)
    return rebuilt, comps


def value_one(
    player: Player,
    settings: LeagueSettings,
    *,
    window: Window,
    week: int | None = None,
    weeks_remaining: int = 17,
    current_week: int = 1,
    ctx: PlayerContext | None = None,
    override_cap: float = 0.15,
) -> tuple[float, DurabilityResult, VarianceProfile, dict[str, float], list[str]]:
    """Adjusted projected points for one player, plus the parts that made it.

    Returns (points, durability, variance, components, missing). VOR and tier are
    pool-level and get attached by `value_pool`.
    """
    ctx = ctx or PlayerContext()
    components: dict[str, float] = {}
    missing: list[str] = []

    # ── Base projection, in this league's scoring (§2.2) ──────────────────────
    if window == "week":
        if week is None:
            raise ValueError("window='week' requires a week number")
        if player.game_locked(week):
            # His game has kicked off, so the projection forecasts an event
            # that already happened. What this slot produces is settled and no
            # move can change it (§2.1) — use the real number.
            base = player.actual_week.get(week, 0.0)
            components["locked_actual"] = round(base, 3)
        else:
            base = player.proj_week.get(week)
            if base is None:
                base = 0.0
                missing.append(f"no ESPN projection for week {week}")
    else:
        # In-season, a season row that lags a role change is rebuilt from
        # the weekly projections (see ros_base). Preseason there are not two
        # weeks to read, so this is ESPN's row unchanged.
        base, role = ros_base(player, current_week=current_week,
                              weeks_remaining=weeks_remaining)
        components.update(role)
        if not base:
            missing.append("no ESPN season projection")
    components["base_projection"] = round(base, 3)

    # ── Durability (§2.5) ─────────────────────────────────────────────────────
    dur = availability(
        pos=player.pos,
        status=player.injury_status,
        history=ctx.injury_history,
        age=ctx.age,
        weeks_remaining=weeks_remaining,
        current_week=current_week,
    )
    if ctx.news_veto:
        dur.vetoes.append(f"research: {ctx.news_veto}")
    missing.extend(dur.missing)
    components.update({f"dur.{k}": v for k, v in dur.components.items()})

    points = base
    if window == "ros":
        # Over a season, availability scales the whole projection: a player who
        # plays 80% of games banks 80% of the points.
        points *= dur.availability
        components["availability"] = dur.availability
    else:
        # For one week, availability is not a scalar — either he plays or he
        # doesn't. QUESTIONABLE already discounts inside durability; a healthy
        # player's season-long durability is irrelevant to this Sunday.
        if player.injury_status.name in {"QUESTIONABLE", "DOUBTFUL"}:
            wk_mult = dur.components.get("questionable", dur.components.get("doubtful", 1.0))
            points *= wk_mult
            components["status_week"] = wk_mult

    # ── Context multipliers, weekly window only (§2.7) ────────────────────────
    if window == "week":
        for name, mult in ctx.multipliers.items():
            points *= mult
            components[f"ctx.{name}"] = round(mult, 4)
        if not ctx.multipliers:
            missing.append("no weekly context (opponent, usage, game script)")

    # ── News override, bounded (§2.8, §3.2) ───────────────────────────────────
    if ctx.news_override is not None:
        mult = _clamp_override(ctx.news_override, override_cap)
        points *= mult
        components["news_override"] = round(mult, 4)

    # ── Variance, measured not judged (§2.6) ──────────────────────────────────
    var = profile(player.actual_week, player.proj_week)

    return max(0.0, points), dur, var, components, missing


def value_pool(
    pool: list[Player],
    settings: LeagueSettings,
    *,
    window: Window,
    week: int | None = None,
    weeks_remaining: int = 17,
    current_week: int = 1,
    contexts: dict[int, PlayerContext] | None = None,
    override_cap: float = 0.15,
) -> dict[int, Valuation]:
    """Value every player in the pool. THE entry point (§10.4).

    VOR and tiers are pool-relative, so they can only be computed once the whole
    pool has been valued — which is why this, not value_one, is the public API.
    """
    contexts = contexts or {}

    points: dict[int, float] = {}
    parts: dict[int, tuple] = {}

    for p in pool:
        pts, dur, var, comps, missing = value_one(
            p,
            settings,
            window=window,
            week=week,
            weeks_remaining=weeks_remaining,
            current_week=current_week,
            ctx=contexts.get(p.espn_id),
            override_cap=override_cap,
        )
        points[p.espn_id] = pts
        parts[p.espn_id] = (dur, var, comps, missing)

    # A vetoed player is worth nothing to us, but must not drag the replacement
    # baseline down — he is excluded from the pool the baseline is computed on.
    live = [p for p in pool if not parts[p.espn_id][0].vetoed]
    # Season-long: availability discounts the surplus, not the total (see
    # compute_vor). Weekly: it is not a scalar, so it is not passed.
    avail = ({p.espn_id: parts[p.espn_id][0].availability for p in live}
             if window == "ros" else None)
    vors = compute_vor(live, settings, points_of=points, week=week,
                       availability_of=avail,
                       weeks=1.0 if window == "week" else float(weeks_remaining))
    tiers = compute_tiers(live, vors)

    out: dict[int, Valuation] = {}
    for p in pool:
        dur, var, comps, missing = parts[p.espn_id]
        out[p.espn_id] = Valuation(
            espn_id=p.espn_id,
            window=window,
            points=round(points[p.espn_id], 3),
            vor=round(vors.get(p.espn_id, 0.0), 3),
            tier=tiers.get(p.espn_id, 99),
            availability=dur.availability,
            locked=bool(window == "week" and week is not None
                        and p.game_locked(week)),
            stdev=var.stdev,
            bust_rate=var.bust_rate,
            components=comps,
            vetoes=dur.vetoes,
            missing=missing,
        )
    return out
