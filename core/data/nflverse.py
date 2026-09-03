"""nflverse loaders — the history ESPN doesn't expose.

ESPN gives projections and current status. It does not give injury history, snap
share, or target share, which are what §2.5 (durability) and §2.7 (usage trend)
actually need.

Uses `nflreadpy` (polars). `nfl_data_py` was archived in September 2025 and must
not be reintroduced.

Everything is cached to parquet under data/cache/ with a day-granular stamp, so a
draft-day rebuild costs one download, not one per player.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections import defaultdict
from functools import lru_cache

import polars as pl

from core.config import settings
from core.model.durability import InjuryEvent

log = logging.getLogger(__name__)

#: How many prior seasons of history to pull. §2.5 weighs the last three.
HISTORY_SEASONS = 3


def _cache_path(name: str) -> "object":
    stamp = dt.date.today().isoformat()
    return settings().cache_dir / f"{name}-{stamp}.parquet"


def _cached(name: str, loader) -> pl.DataFrame:
    """Load from today's parquet cache, else fetch and write it."""
    path = _cache_path(name)
    if path.exists():
        try:
            return pl.read_parquet(path)
        except Exception as e:
            log.warning("cache %s unreadable (%s) — refetching", path.name, e)
    df = loader()
    if isinstance(df, pl.LazyFrame):
        df = df.collect()
    try:
        df.write_parquet(path)
    except Exception as e:
        log.warning("could not cache %s: %s", name, e)
    return df


def seasons_back(n: int = HISTORY_SEASONS, *, current: int | None = None) -> list[int]:
    """The last n COMPLETED seasons. The current one is excluded — it has no
    injury history yet in September and including it skews every rate."""
    cur = current or settings().season
    return list(range(cur - n, cur))


# ── raw tables ───────────────────────────────────────────────────────────────


def injuries(seasons: list[int] | None = None) -> pl.DataFrame:
    import nflreadpy as nfl

    yrs = seasons or seasons_back()
    return _cached(f"injuries-{yrs[0]}-{yrs[-1]}", lambda: nfl.load_injuries(yrs))


def weekly_stats(seasons: list[int] | None = None) -> pl.DataFrame:
    import nflreadpy as nfl

    yrs = seasons or seasons_back()
    return _cached(
        f"weekly-{yrs[0]}-{yrs[-1]}",
        lambda: nfl.load_player_stats(yrs, summary_level="week"),
    )


def snap_counts(seasons: list[int] | None = None) -> pl.DataFrame:
    import nflreadpy as nfl

    yrs = seasons or seasons_back()
    return _cached(f"snaps-{yrs[0]}-{yrs[-1]}", lambda: nfl.load_snap_counts(yrs))


def players_table() -> pl.DataFrame:
    """Cross-platform player IDs, including ESPN's — the join key."""
    import nflreadpy as nfl

    return _cached("players", nfl.load_players)


# ── the ESPN id bridge ───────────────────────────────────────────────────────


@lru_cache(maxsize=1)
def espn_to_gsis() -> dict[int, str]:
    """Map ESPN player id -> nflverse gsis_id.

    Without this the two data sources can't be joined at all, so a failure here
    is loud: durability silently falling back to the league base rate for every
    player would look like a working system producing worse decisions.
    """
    df = players_table()
    cols = set(df.columns)

    espn_col = next((c for c in ("espn_id", "espn_player_id") if c in cols), None)
    gsis_col = next((c for c in ("gsis_id", "gsis_it_id", "player_id") if c in cols), None)
    if not espn_col or not gsis_col:
        log.error(
            "players table has no ESPN<->gsis mapping (columns: %s). "
            "Durability will use base rates for everyone (§8.8).",
            sorted(cols)[:25],
        )
        return {}

    out: dict[int, str] = {}
    for row in df.select([espn_col, gsis_col]).iter_rows():
        espn, gsis = row
        if espn is None or gsis is None:
            continue
        try:
            out[int(float(espn))] = str(gsis)
        except (TypeError, ValueError):
            continue
    log.info("mapped %d ESPN ids to gsis ids", len(out))
    return out


@lru_cache(maxsize=1)
def name_to_gsis() -> dict[str, str]:
    """Fallback join on normalised display name.

    ESPN ids are missing for a meaningful slice of nflverse rows, and a rookie's
    id often lands late. Name matching is imperfect, so it is only ever a
    fallback and every use is counted.
    """
    df = players_table()
    cols = set(df.columns)
    name_col = next(
        (c for c in ("display_name", "full_name", "player_name", "football_name") if c in cols),
        None,
    )
    gsis_col = next((c for c in ("gsis_id", "player_id") if c in cols), None)
    if not name_col or not gsis_col:
        return {}
    out: dict[str, str] = {}
    for name, gsis in df.select([name_col, gsis_col]).iter_rows():
        if name and gsis:
            out.setdefault(normalise_name(str(name)), str(gsis))
    return out


def normalise_name(n: str) -> str:
    return (
        n.lower()
        .replace(".", "")
        .replace("'", "")
        .replace("-", " ")
        .replace(" jr", "")
        .replace(" sr", "")
        .replace(" ii", "")
        .replace(" iii", "")
        .replace(" iv", "")
        .strip()
    )


# ── the thing §2.5 actually consumes ─────────────────────────────────────────

#: nflverse report_status values that mean "did not play".
_MISSED = {"out", "doubtful", "injured reserve", "ir", "pup", "nfi"}


@lru_cache(maxsize=1)
def injury_history_by_gsis() -> dict[str, list[InjuryEvent]]:
    """Per-player injury events over the last three completed seasons.

    One event per (season, primary injury), with games missed counted from
    weekly report rows where the player was ruled out. That is an approximation
    — the injury report is a weekly snapshot, not a ledger — and it is the best
    signal available without a paid feed.
    """
    try:
        df = injuries()
    except Exception as e:
        log.error("could not load nflverse injuries: %s — durability falls back to base rates", e)
        return {}

    cols = set(df.columns)
    gsis_col = next((c for c in ("gsis_id", "player_id") if c in cols), None)
    status_col = next((c for c in ("report_status", "game_status") if c in cols), None)
    desc_col = next(
        (c for c in ("report_primary_injury", "primary_injury", "practice_primary_injury")
         if c in cols),
        None,
    )
    if not gsis_col or not status_col:
        log.error("injury table missing expected columns; have %s", sorted(cols)[:25])
        return {}

    keep = [c for c in (gsis_col, "season", status_col, desc_col) if c]
    agg: dict[tuple[str, int], dict] = defaultdict(lambda: {"missed": 0, "descs": []})

    for row in df.select(keep).iter_rows(named=True):
        gsis = row.get(gsis_col)
        season = row.get("season")
        if not gsis or season is None:
            continue
        status = str(row.get(status_col) or "").strip().lower()
        desc = str(row.get(desc_col) or "") if desc_col else ""
        k = (str(gsis), int(season))
        if status in _MISSED:
            agg[k]["missed"] += 1
        if desc:
            agg[k]["descs"].append(desc)

    out: dict[str, list[InjuryEvent]] = defaultdict(list)
    for (gsis, season), v in agg.items():
        if not v["missed"] and not v["descs"]:
            continue
        # Most frequently reported injury that season is the primary one.
        desc = ""
        if v["descs"]:
            desc = max(set(v["descs"]), key=v["descs"].count)
        out[gsis].append(
            InjuryEvent(season=season, games_missed=int(v["missed"]), description=desc)
        )

    log.info("built injury history for %d players", len(out))
    return dict(out)


def history_for(espn_id: int, name: str) -> tuple[list[InjuryEvent], bool]:
    """(events, matched) for one ESPN player. `matched` False means we found no
    nflverse record at all — the caller should treat durability as unknown
    rather than as clean."""
    hist = injury_history_by_gsis()
    if not hist:
        return [], False

    gsis = espn_to_gsis().get(espn_id)
    if gsis is None:
        gsis = name_to_gsis().get(normalise_name(name))
    if gsis is None:
        return [], False

    return hist.get(gsis, []), True
