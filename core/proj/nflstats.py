"""nflverse weekly stats, scored under an arbitrary league's rules.

This is the bridge that makes a 13-season training sample possible. ESPN only
serves preseason projections for 2024 and 2025 — 2023's rows are present but
zeroed, and the league does not exist before that (docs/projection-model-plan.md
§0). nflverse carries opportunity and production back past 2010, so the model is
fitted there and only *tested* against ESPN.

**The bridge is verified, not asserted.** `verify()` scores nflverse lines under
a season's own ESPN scoring map and compares them to ESPN's own `appliedTotal`
for the same player-week. A statId mapped to the wrong column would otherwise
produce a plausible number that is quietly wrong for thirteen seasons, and every
downstream result would inherit it.

Only the four positions where opportunity modelling means anything are handled:
QB, RB, WR, TE. Kickers and defences stay on ESPN's projection — their week to
week outcome is close to noise and no opportunity model helps.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import polars as pl

from core.data.nflverse import _cached
from core.model.schema import Pos

log = logging.getLogger(__name__)

#: nflverse weekly column -> ESPN statId. Every id here is confirmed by
#: `verify()` against ESPN's own arithmetic before the model consumes it.
STAT_IDS: dict[str, int] = {
    "passing_yards": 3,
    "passing_tds": 4,
    "passing_2pt_conversions": 19,
    "passing_interceptions": 20,
    "rushing_yards": 24,
    "rushing_tds": 25,
    "rushing_2pt_conversions": 26,
    "receiving_yards": 42,
    "receiving_tds": 43,
    "receiving_2pt_conversions": 44,
    "receptions": 53,
    "fumbles_lost": 72,
}

#: Fumbles lost. nflverse splits them by how the fumble happened AND carries a
#: total; the per-cause columns miss 13 lines a season that the total catches
#: (e.g. Deebo Samuel wk13 2024), so the total wins where it is present.
_FUMBLE_TOTAL = "fumbles_lost_total"
_FUMBLE_COLS = ("sack_fumbles_lost", "rushing_fumbles_lost", "receiving_fumbles_lost")

#: **Bucketed stats.** ESPN scores some yardage in whole units of N yards rather
#: than per yard, as a separate statId carrying the bucket COUNT. Measured on
#: Kirk Cousins wk5 2024: 509 passing yards, and statId 8 = 20.0 = floor(509/25).
#: The 2026 league scores statId 8 at 1.0 — so it pays 1 point per 25 passing
#: yards, i.e. 0.04/yd. Missing this put QB agreement at 68/621; with it, QBs
#: reproduce like every other position.
#: statId -> (source column, yards per bucket)
BUCKETS: dict[int, tuple[str, int]] = {8: ("passing_yards", 25)}

#: The positions this model covers.
MODELLED: tuple[Pos, ...] = (Pos.QB, Pos.RB, Pos.WR, Pos.TE)

_POS_MAP = {"QB": Pos.QB, "RB": Pos.RB, "WR": Pos.WR, "TE": Pos.TE}

#: Opportunity columns — the persistent half of the model (§2 of the plan).
OPPORTUNITY = ("carries", "targets", "receptions", "attempts", "completions")

#: Everything the feature builder reads off a season row.
_SUM_COLS = (
    *OPPORTUNITY,
    "passing_yards", "passing_tds", "passing_interceptions",
    "rushing_yards", "rushing_tds",
    "receiving_yards", "receiving_tds", "receiving_air_yards",
    "receiving_first_downs", "rushing_first_downs",
)
_MEAN_COLS = ("target_share", "air_yards_share", "wopr")


def weekly(seasons: list[int]) -> pl.DataFrame:
    """Regular-season weekly lines for the modelled positions."""
    import nflreadpy as nfl

    key = f"pstats-{min(seasons)}-{max(seasons)}"
    df = _cached(key, lambda: nfl.load_player_stats(seasons=seasons))
    return df.filter(
        pl.col("season_type") == "REG",
        pl.col("position").is_in(list(_POS_MAP)),
    )


def _stat_line(row: dict) -> dict[int, float]:
    """One nflverse weekly row as an ESPN statId -> value line."""
    out: dict[int, float] = {}
    for col, sid in STAT_IDS.items():
        if col == "fumbles_lost":
            continue
        v = row.get(col)
        if v:
            out[sid] = float(v)
    fum = float(row.get(_FUMBLE_TOTAL) or 0.0) or sum(
        float(row.get(c) or 0.0) for c in _FUMBLE_COLS)
    if fum:
        out[STAT_IDS["fumbles_lost"]] = fum
    for sid, (col, per) in BUCKETS.items():
        v = float(row.get(col) or 0.0)
        if v:
            out[sid] = float(int(v // per))
    return out


def score_weekly(df: pl.DataFrame, scoring: dict[int, float]) -> pl.DataFrame:
    """Add a `points` column: this league's scoring applied to each line.

    Vectorised rather than row-wise — thirteen seasons is ~250k rows, and the
    per-row dict build in `_stat_line` is only used by `verify`, where clarity
    matters more than speed.
    """
    terms = []
    for col, sid in STAT_IDS.items():
        rate = scoring.get(sid, 0.0)
        if not rate:
            continue
        if col == "fumbles_lost":
            per_cause = sum((pl.col(c).fill_null(0.0) for c in _FUMBLE_COLS),
                            start=pl.lit(0.0))
            expr = (pl.col(_FUMBLE_TOTAL).fill_null(0.0)
                    if _FUMBLE_TOTAL in df.columns else per_cause)
        elif col in df.columns:
            expr = pl.col(col).fill_null(0.0)
        else:
            continue
        terms.append(expr * rate)
    for sid, (col, per) in BUCKETS.items():
        rate = scoring.get(sid, 0.0)
        if rate and col in df.columns:
            terms.append((pl.col(col).fill_null(0.0) // per) * rate)
    total = sum(terms, start=pl.lit(0.0)) if terms else pl.lit(0.0)
    return df.with_columns(total.alias("points"))


# ── verification ─────────────────────────────────────────────────────────────


@dataclass
class Agreement:
    """How well the nflverse bridge reproduces ESPN's own arithmetic."""

    lines: int = 0
    agreed: int = 0
    by_pos: dict[str, tuple[int, int]] = field(default_factory=dict)
    worst_gap: float = 0.0
    worst_example: str = ""

    @property
    def rate(self) -> float:
        return self.agreed / self.lines if self.lines else 0.0

    def report(self) -> str:
        parts = " · ".join(f"{p} {a}/{t}" for p, (a, t) in sorted(self.by_pos.items()))
        return (f"{self.agreed}/{self.lines} lines agree ({self.rate:.1%})   {parts}\n"
                f"worst gap {self.worst_gap:.2f} — {self.worst_example}")


def verify(season: int, scoring: dict[int, float], *, tolerance: float = 0.02) -> Agreement:
    """Score nflverse lines and ESPN's own lines under the SAME map, and compare.

    **Both sides must be scored under the target (2026) map, not the season's
    own.** ESPN's statId convention changed: 2024 and 2025 score yardage through
    per-10-yard buckets (statId 28 rushing, 48 receiving), which nflverse has no
    column for, while 2026 scores raw yards (24, 42), which it does. Scoring
    nflverse under a 2024 map therefore drops all yardage and reports ~17%
    agreement — a property of that map, not of the mapping under test.

    Since the pipeline rescores every season into 2026 rules anyway, the map
    under test here is the map actually used downstream.
    """
    from core.backtest import history
    from core.backtest.rescore import points
    from core.data.nflverse import espn_to_gsis

    s = history.load(season)
    bridge = espn_to_gsis()
    gsis_of = {p.espn_id: bridge.get(p.espn_id) for p in s.players}
    pos_of = {p.espn_id: p.pos for p in s.players}

    idx = {(row["player_id"], int(row["week"])): row
           for row in weekly([season]).iter_rows(named=True)}

    ag = Agreement()
    for espn_id, weeks in s.raw_weekly.items():
        gsis = gsis_of.get(espn_id)
        pos = pos_of.get(espn_id)
        if not gsis or pos not in MODELLED:
            continue
        for wk, raw in weeks.items():
            row = idx.get((gsis, int(wk)))
            if row is None:
                continue
            espn_pts = points(raw, scoring)
            ours = points(_stat_line(row), scoring)
            ag.lines += 1
            a, t = ag.by_pos.get(pos.value, (0, 0))
            gap = abs(ours - espn_pts)
            if gap <= tolerance:
                ag.agreed += 1
                a += 1
            elif gap > ag.worst_gap:
                ag.worst_gap = gap
                ag.worst_example = (f"{row.get('player_display_name')} wk{wk}: "
                                    f"nflverse {ours:.2f} vs ESPN {espn_pts:.2f}")
            ag.by_pos[pos.value] = (a, t + 1)
    return ag


# ── season aggregation ───────────────────────────────────────────────────────


def seasons(years: list[int], scoring: dict[int, float]) -> pl.DataFrame:
    """One row per player-season: totals, opportunity, games and points.

    `games` counts weeks the player actually appeared, which is what the
    availability model is calibrated against — not roster weeks, not 17.
    """
    df = score_weekly(weekly(years), scoring)
    aggs = [pl.len().alias("games"), pl.col("points").sum().alias("points")]
    for c in _SUM_COLS:
        if c in df.columns:
            aggs.append(pl.col(c).fill_null(0.0).sum().alias(c))
    for c in _MEAN_COLS:
        if c in df.columns:
            aggs.append(pl.col(c).fill_null(0.0).mean().alias(c))
    aggs.append(pl.col("position").first().alias("position"))
    aggs.append(pl.col("player_display_name").first().alias("name"))
    aggs.append(pl.col("team").last().alias("team"))
    return df.group_by(["player_id", "season"]).agg(aggs).sort(["season", "points"],
                                                              descending=[False, True])
