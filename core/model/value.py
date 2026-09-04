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


def _clamp_override(raw: float, cap: float) -> float:
    return max(1.0 - cap, min(1.0 + cap, raw))


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
        base = player.proj_week.get(week)
        if base is None:
            base = 0.0
            missing.append(f"no ESPN projection for week {week}")
    else:
        base = player.proj_season
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
                       availability_of=avail)
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
            stdev=var.stdev,
            bust_rate=var.bust_rate,
            components=comps,
            vetoes=dur.vetoes,
            missing=missing,
        )
    return out
