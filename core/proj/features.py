"""Player-season feature rows, and the (year N -> year N+1) training pairs.

**Leakage is the failure mode this module exists to prevent.** A model that sees
any part of season Y while projecting season Y looks superb and is worthless. The
rule is enforced here in code, not left to discipline: `features_for` takes a
target year and refuses to read a season row at or after it (`assert_no_leakage`),
and there is a test that feeds it a leaked row and expects the raise.

Everything a feature reads is knowable in August of the target year: prior-season
production, prior-season opportunity, age, and draft capital. Nothing else.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

import polars as pl

from core.proj import nflstats

log = logging.getLogger(__name__)

#: How many prior seasons a feature row looks back over.
LOOKBACK = 3

#: Roughly when a season starts, for computing age at the start of it.
_SEASON_START = (9, 1)


class LeakageError(AssertionError):
    """Raised when a feature would be built from the season it projects."""


@dataclass(frozen=True)
class Bio:
    gsis_id: str
    position: str
    birth_date: dt.date | None
    rookie_season: int | None
    draft_round: int | None
    draft_pick: int | None

    def age_in(self, year: int) -> float | None:
        if not self.birth_date:
            return None
        start = dt.date(year, *_SEASON_START)
        return round((start - self.birth_date).days / 365.25, 2)

    def experience_in(self, year: int) -> int | None:
        if not self.rookie_season:
            return None
        return year - int(self.rookie_season)


def bios() -> dict[str, Bio]:
    """gsis_id -> biographical facts. Draft capital included: for a rookie with
    no prior season it is the only real signal available, and it is the best
    public predictor of rookie production."""
    from core.data.nflverse import players_table

    df = players_table()
    cols = set(df.columns)
    want = ["gsis_id", "position", "birth_date", "rookie_season",
            "draft_round", "draft_pick"]
    have = [c for c in want if c in cols]
    out: dict[str, Bio] = {}
    for row in df.select(have).iter_rows(named=True):
        gid = row.get("gsis_id")
        if not gid:
            continue
        bd = _date(row.get("birth_date"))
        out[str(gid)] = Bio(
            gsis_id=str(gid),
            position=str(row.get("position") or ""),
            birth_date=bd,
            rookie_season=_int(row.get("rookie_season")),
            draft_round=_int(row.get("draft_round")),
            draft_pick=_int(row.get("draft_pick")),
        )
    return out


def _date(v) -> dt.date | None:
    """nflverse serves `birth_date` as a STRING, not a date.

    An `isinstance(v, date)` check therefore silently discarded all 24,800 of
    them and every player's age came through as None — which made the fitted
    age cliff meaningless (it "chose" no age effect because there was no age)
    and left the durability calibration untestable. Parse, don't assume.
    """
    if isinstance(v, dt.datetime):
        return v.date()
    if isinstance(v, dt.date):
        return v
    if isinstance(v, str) and v.strip():
        try:
            return dt.date.fromisoformat(v.strip()[:10])
        except ValueError:
            return None
    return None


def _int(v) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def assert_no_leakage(rows: list[dict], target_year: int) -> None:
    """Every source row must predate the season being projected."""
    bad = [r for r in rows if int(r["season"]) >= target_year]
    if bad:
        raise LeakageError(
            f"{len(bad)} feature rows are from season {target_year} or later "
            f"while projecting {target_year} (e.g. {bad[0].get('name')} "
            f"{bad[0]['season']}). Features may only read strictly prior seasons."
        )


# ── the feature row ──────────────────────────────────────────────────────────

#: Per-game rates the model works in. Season totals are confounded by games
#: played, which is modelled separately — mixing them makes an injured starter
#: look like a part-timer.
_RATE_COLS = ("carries", "targets", "receptions", "attempts",
              "receiving_air_yards", "points")


@dataclass
class Row:
    """One player, one target season, everything knowable beforehand."""

    gsis_id: str
    name: str
    position: str
    target_year: int
    age: float | None
    experience: int | None
    draft_pick: int | None
    #: index 0 = most recent prior season, 1 = the one before, ...
    prior_games: list[float]
    prior_rates: list[dict[str, float]]
    prior_shares: list[dict[str, float]]
    #: What actually happened. None when building a live (unplayed) projection.
    actual_points: float | None = None
    actual_games: float | None = None

    @property
    def has_history(self) -> bool:
        return bool(self.prior_games) and self.prior_games[0] > 0


def build(seasons_df: pl.DataFrame, target_year: int, *,
          bio: dict[str, Bio] | None = None,
          actuals: pl.DataFrame | None = None) -> list[Row]:
    """Feature rows for every player with at least one prior season.

    `seasons_df` must contain ONLY seasons before `target_year` — checked, not
    trusted. `actuals` is the target season's rows, supplied separately so that
    the only path by which target-season data enters is the label.
    """
    src = seasons_df.to_dicts()
    assert_no_leakage(src, target_year)

    bio = bio if bio is not None else bios()

    by_player: dict[str, list[dict]] = {}
    for r in src:
        if target_year - int(r["season"]) <= LOOKBACK:
            by_player.setdefault(str(r["player_id"]), []).append(r)

    label_pts: dict[str, float] = {}
    label_gms: dict[str, float] = {}
    if actuals is not None:
        for r in actuals.to_dicts():
            label_pts[str(r["player_id"])] = float(r["points"])
            label_gms[str(r["player_id"])] = float(r["games"])

    out: list[Row] = []
    for pid, rows in by_player.items():
        rows.sort(key=lambda r: -int(r["season"]))
        b = bio.get(pid)
        games = [float(r["games"] or 0) for r in rows]
        rates, shares = [], []
        for r in rows:
            g = max(float(r["games"] or 0), 1.0)
            rates.append({c: float(r.get(c) or 0.0) / g for c in _RATE_COLS})
            shares.append({c: float(r.get(c) or 0.0)
                           for c in ("target_share", "air_yards_share", "wopr")})
        out.append(Row(
            gsis_id=pid,
            name=str(rows[0].get("name") or pid),
            position=str(rows[0].get("position") or (b.position if b else "")),
            target_year=target_year,
            age=b.age_in(target_year) if b else None,
            experience=b.experience_in(target_year) if b else None,
            draft_pick=b.draft_pick if b else None,
            prior_games=games,
            prior_rates=rates,
            prior_shares=shares,
            actual_points=label_pts.get(pid),
            actual_games=label_gms.get(pid),
        ))
    return out


def training_set(years: list[int], scoring: dict[int, float]) -> list[Row]:
    """Labelled rows for every target year in `years`.

    Pulls one wide frame and slices it per target year, so the expensive
    nflverse load happens once rather than once per season.
    """
    lo = min(years) - LOOKBACK
    wide = nflstats.seasons(list(range(lo, max(years) + 1)), scoring)
    bio = bios()
    rows: list[Row] = []
    for y in years:
        prior = wide.filter(pl.col("season") < y)
        actual = wide.filter(pl.col("season") == y)
        rows.extend(build(prior, y, bio=bio, actuals=actual))
    log.info("training set: %d rows across %d target years", len(rows), len(years))
    return rows
