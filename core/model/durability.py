"""§2.5 — durability as a DISCOUNT, not a ban.

The spec said "we do not want an injury prone player." Read literally that vetoes
several of the ten best players in football, so it is implemented as a multiplier
on expected games played, with a short hard-veto list for players who genuinely
cannot help us.

The signal that actually repeats is soft tissue. A hamstring pull predicts another
hamstring pull; a broken collarbone predicts almost nothing. Weighting those the
same is the most common mistake in amateur injury analysis, and it is why this
module separates them explicitly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from core.model.priors import priors
from core.model.schema import InjuryStatus, Pos

GAMES_PER_SEASON = 17

#: Injuries that recur. A prior event raises the hazard of another.
_SOFT_TISSUE = re.compile(
    r"hamstring|groin|calf|quad|hip flexor|adductor|achilles|soft tissue",
    re.I,
)
#: Structural injuries with a real recurrence risk of their own.
_CHRONIC = re.compile(r"\bacl\b|\bmcl\b|\bpcl\b|micro ?fracture|lisfranc|back|neck|concussion", re.I)
#: One-off breaks. Heal clean, predict little.
_ACUTE_CLEAN = re.compile(r"fracture|broken|collarbone|clavicle|finger|thumb|rib|laceration", re.I)


@dataclass(frozen=True)
class InjuryEvent:
    season: int
    games_missed: int
    description: str = ""

    @property
    def is_soft_tissue(self) -> bool:
        return bool(_SOFT_TISSUE.search(self.description))

    @property
    def is_chronic(self) -> bool:
        return bool(_CHRONIC.search(self.description))

    @property
    def is_clean_acute(self) -> bool:
        return bool(_ACUTE_CLEAN.search(self.description)) and not self.is_soft_tissue


@dataclass
class DurabilityResult:
    availability: float
    expected_games: float
    vetoes: list[str] = field(default_factory=list)
    components: dict[str, float] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)

    @property
    def vetoed(self) -> bool:
        return bool(self.vetoes)


#: Recency weights for the last three seasons, newest first.
_SEASON_WEIGHTS = (0.5, 0.3, 0.2)

#: Age past which workload starts to bite, by position. RB is the sharp one.
_AGE_CLIFF: dict[Pos, int] = {Pos.RB: 27, Pos.WR: 30, Pos.TE: 31, Pos.QB: 36}


def _age_penalty(pos: Pos, age: int | None) -> float:
    """Multiplier for age-related breakdown risk. 1.0 = no penalty."""
    if age is None:
        return 1.0
    cliff = _AGE_CLIFF.get(pos)
    if cliff is None or age <= cliff:
        return 1.0
    years_past = age - cliff
    # 3% per year past the cliff, floored so an old player is never written off
    # on age alone.
    return max(0.85, 1.0 - 0.03 * years_past)


def _recurrence_penalty(history: list[InjuryEvent]) -> tuple[float, dict[str, float]]:
    """Soft-tissue events repeat; clean breaks do not. §2.5."""
    recent = sorted(history, key=lambda e: e.season, reverse=True)[:3]
    soft = sum(1 for e in recent if e.is_soft_tissue)
    chronic = sum(1 for e in recent if e.is_chronic)

    penalty = 1.0
    parts: dict[str, float] = {}

    # "Two soft-tissue events in two years is a pattern; one broken collarbone
    # is not." One event is noise; the second is where the discount bites.
    if soft >= 2:
        penalty *= 0.88
        parts["soft_tissue_pattern"] = 0.88
    elif soft == 1:
        penalty *= 0.97
        parts["soft_tissue_single"] = 0.97

    if chronic >= 1:
        p = 0.93**chronic
        penalty *= p
        parts["chronic"] = p

    return penalty, parts


def availability(
    *,
    pos: Pos,
    status: InjuryStatus,
    history: list[InjuryEvent],
    age: int | None = None,
    weeks_remaining: int = GAMES_PER_SEASON,
    current_week: int = 1,
    suspension_through_week: int | None = None,
    ir_return_week: int | None = None,
) -> DurabilityResult:
    """Expected games played / 17, plus any hard vetoes.

    `history` should hold the last three seasons of injury events. An empty
    history is recorded in `missing` rather than treated as perfect health —
    rookies have no history and are not therefore durable.
    """
    p = priors()
    vetoes: list[str] = []
    components: dict[str, float] = {}
    missing: list[str] = []

    # ── Hard vetoes (§2.5). These are not discounts. ──────────────────────────
    if status is InjuryStatus.IR and ir_return_week is None:
        vetoes.append("§2.5 on IR with no designated return")
    if status is InjuryStatus.SUSPENSION:
        veto_after = p.get("model.suspension_veto_after_week")
        if suspension_through_week is None or suspension_through_week > veto_after:
            vetoes.append(f"§2.5 suspended past week {veto_after}")
    if status is InjuryStatus.OUT:
        vetoes.append("§2.5 ruled OUT")

    if vetoes:
        return DurabilityResult(0.0, 0.0, vetoes=vetoes, components=components)

    # ── Base rate: games actually played, recency-weighted ────────────────────
    if not history:
        base = 0.94  # league-average-ish; flagged so callers know it's assumed
        missing.append("no injury history — using league base rate")
        components["base_rate_assumed"] = base
    else:
        by_season = sorted({e.season for e in history}, reverse=True)[:3]
        weighted, wsum = 0.0, 0.0
        for w, season in zip(_SEASON_WEIGHTS, by_season, strict=False):
            missed = sum(e.games_missed for e in history if e.season == season)
            played = max(0, GAMES_PER_SEASON - missed)
            weighted += w * (played / GAMES_PER_SEASON)
            wsum += w
        base = weighted / wsum if wsum else 0.94
        components["base_rate"] = round(base, 4)
        if len(by_season) < 3:
            missing.append(f"only {len(by_season)} season(s) of injury history")

    # ── Adjustments ───────────────────────────────────────────────────────────
    rec_mult, rec_parts = _recurrence_penalty(history)
    components.update(rec_parts)

    age_mult = _age_penalty(pos, age)
    if age_mult < 1.0:
        components["age"] = round(age_mult, 4)
    if age is None:
        missing.append("age unknown")

    # A current QUESTIONABLE tag costs part of the coming week, not the season.
    status_mult = 1.0
    if status is InjuryStatus.QUESTIONABLE:
        status_mult = 0.85
        components["questionable"] = status_mult
    elif status is InjuryStatus.DOUBTFUL:
        # Not a season veto, but he is not playing this week.
        status_mult = 0.4
        components["doubtful"] = status_mult

    avail = max(0.0, min(1.0, base * rec_mult * age_mult * status_mult))

    # An IR player with a known return misses those weeks outright.
    if status is InjuryStatus.IR and ir_return_week is not None:
        out_weeks = max(0, ir_return_week - current_week)
        if weeks_remaining > 0:
            avail *= max(0.0, (weeks_remaining - out_weeks) / weeks_remaining)
            components["ir_weeks_missed"] = float(out_weeks)

    return DurabilityResult(
        availability=round(avail, 4),
        expected_games=round(avail * weeks_remaining, 2),
        vetoes=vetoes,
        components=components,
        missing=missing,
    )
