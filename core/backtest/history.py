"""Pull a completed season out of ESPN and freeze it on disk.

Everything here is measured, not assumed — see docs/backtest-plan.md §0 for the
probe results. Three facts drive the design:

**Preseason projections survive.** The `statSourceId=1, statSplitTypeId=0` row
for a past season is the projection ESPN published BEFORE that season, not a
refit. Joe Mixon 2025 carries proj 163.5 against an actual of 0.0, and Jayden
Daniels 444 against 138. Nothing fitted after the fact is that wrong. This is
what makes an honest backtest possible at all.

**ADP is dead.** `ownership.averageDraftPosition` is a LIVE field — for 2025 it
returns 170.0 for every player, Gibbs included. It cannot be used. Instead we
read `mDraftDetail`, the league's real draft, and use a player's actual pick
number as his ADP. That is better than a proxy: it is what the room really did.

**Weekly actuals need one call per scoring period.** The default player view
returns season totals only; passing `scoringPeriodId` adds that week's actual
(src 0, split 1) and that week's projection (src 1, split 1).

A completed season does not change, so the cache under
`data/backtest/<year>/` never expires.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field, replace
from pathlib import Path

from core.config import Settings, settings
from core.espn.client import EspnClient
from core.espn.players import _POS_BY_ID, PRO_TEAMS
from core.espn.settings import LeagueFacts
from core.espn.settings import load as load_settings
from core.model.schema import InjuryStatus, Player, Pos

log = logging.getLogger(__name__)

#: Scoring periods to pull. 18 covers the modern regular season; a league whose
#: regular season is shorter simply has empty tail weeks.
MAX_WEEK = 18

#: Players per season pull, matching the live board's default so the backtest
#: and the real draft see a pool of the same depth.
POOL_SIZE = 450
PAGE = 150


@dataclass(frozen=True)
class DraftPick:
    overall: int
    round_num: int
    team_id: int
    espn_id: int
    keeper: bool = False


@dataclass
class Season:
    """One completed season, everything the replay and the scorer need."""

    year: int
    facts: LeagueFacts
    #: Preseason state only: proj_season and proj_week are projections, and
    #: injury_status is UNKNOWN because draft-day status is not recoverable.
    players: list[Player]
    picks: list[DraftPick]
    #: espn_id -> week -> raw stat line (statId -> value). For rescoring.
    raw_weekly: dict[int, dict[int, dict[int, float]]] = field(default_factory=dict)
    #: espn_id -> the raw stat line behind the PRESEASON season projection.
    #: Lets normalised mode rescore what the engine drafts on, not just what it
    #: is graded on — drafting on 2025 scoring and grading on 2026 scoring
    #: measures the engine against an objective it was never given.
    raw_projection: dict[int, dict[int, float]] = field(default_factory=dict)

    @property
    def by_id(self) -> dict[int, Player]:
        return {p.espn_id: p for p in self.players}

    @property
    def team_ids(self) -> list[int]:
        """Teams in draft order of first pick — index 0 drafted first."""
        seen: list[int] = []
        for p in sorted(self.picks, key=lambda x: x.overall):
            if p.team_id not in seen:
                seen.append(p.team_id)
        return seen

    def pseudo_adp(self) -> dict[int, float]:
        """espn_id -> the overall pick he really went at.

        The replacement for ESPN's dead historical ADP. Players who went
        undrafted are absent, which `survival.py` already reads as "no ADP" and
        handles rather than inventing a number for.
        """
        out: dict[int, float] = {}
        for p in self.picks:
            out.setdefault(p.espn_id, float(p.overall))
        return out


# ── the pull ─────────────────────────────────────────────────────────────────


def cache_dir(year: int, cfg: Settings | None = None) -> Path:
    d = (cfg or settings()).data_dir / "backtest" / str(year)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _client_for(year: int, cfg: Settings | None = None) -> EspnClient:
    """A client pinned to a past season.

    `Settings` is frozen, so this is a copy with the season swapped rather than
    a mutation of process-wide state — two seasons can be open at once without
    one silently reconfiguring the other.
    """
    base = cfg or settings()
    return EspnClient(replace(base, season=year))


def _cached_json(path: Path, fetch) -> dict | list:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            log.warning("backtest cache %s unreadable (%s) — refetching", path.name, e)
    data = fetch()
    path.write_text(json.dumps(data), encoding="utf-8")
    return data


def _pool_filter(offset: int, limit: int) -> dict:
    """The player-universe filter for a PAST season.

    Two departures from the live pool loader, both measured:

    - **No `filterStatus`.** A past season is served from ESPN's
      `leagueHistory` endpoint, where FREEAGENT/WAIVERS/ONTEAM matches nothing:
      the 2024 pull returned 0 players with it and 450 without.
    - **Sorted by draft rank, not ownership.** Ownership is an END-of-season
      figure, so a player who broke out ranks above one who busted, and pool
      membership itself would carry hindsight. Draft rank is ESPN's PRESEASON
      order. It also covers the room better: sorted by draft rank, all 90 of
      2024's and all 130 of 2025's real picks are inside the top 450, and every
      one of the 450 carries a projection.
    """
    return {"players": {
        "limit": limit,
        "offset": offset,
        "sortDraftRanks": {"sortPriority": 1, "sortAsc": True, "value": "STANDARD"},
    }}


def _pull_pool(c: EspnClient, d: Path, *, size: int = POOL_SIZE) -> list[dict]:
    def fetch() -> list[dict]:
        out: list[dict] = []
        for offset in range(0, size, PAGE):
            filters = _pool_filter(offset, min(PAGE, size - offset))
            batch = (c.get_view("kona_player_info", filters=filters) or {}).get("players") or []
            if not batch:
                break
            out.extend(batch)
        return out

    return _cached_json(d / "pool.json", fetch)  # type: ignore[return-value]


def _pull_week(c: EspnClient, d: Path, week: int, *, size: int = POOL_SIZE) -> list[dict]:
    wd = d / "weeks"
    wd.mkdir(exist_ok=True)

    def fetch() -> list[dict]:
        out: list[dict] = []
        for offset in range(0, size, PAGE):
            filters = _pool_filter(offset, min(PAGE, size - offset))
            batch = (c.get_view("kona_player_info", filters=filters,
                                params={"scoringPeriodId": week}) or {}).get("players") or []
            if not batch:
                break
            out.extend(batch)
        return out

    return _cached_json(wd / f"{week}.json", fetch)  # type: ignore[return-value]


def _pull_draft(c: EspnClient, d: Path) -> list[dict]:
    def fetch() -> list[dict]:
        data = c.get_view("mDraftDetail") or {}
        return ((data.get("draftDetail") or {}).get("picks")) or []

    return _cached_json(d / "draft.json", fetch)  # type: ignore[return-value]


# ── parsing ──────────────────────────────────────────────────────────────────


def _season_projection(rows: list[dict], year: int) -> tuple[float, dict[int, float]]:
    """(preseason season projection, the raw stat line behind it)."""
    for s in rows:
        if (int(s.get("seasonId", 0)) == year
                and int(s.get("statSourceId", -1)) == 1
                and int(s.get("statSplitTypeId", -1)) == 0):
            raw = {int(k): float(v) for k, v in (s.get("stats") or {}).items()}
            return float(s.get("appliedTotal") or 0.0), raw
    return 0.0, {}


def _week_rows(rows: list[dict], year: int, week: int) -> tuple[float | None, float | None, dict]:
    """(actual, projection, raw stat line) for one scoring period.

    `None` for actual means the player has no row at all that week — a bye, an
    inactive, or simply not in the league yet. That is deliberately distinct
    from 0.0, which means he played and scored nothing.
    """
    actual = proj = None
    raw: dict = {}
    for s in rows:
        if int(s.get("seasonId", 0)) != year or int(s.get("scoringPeriodId") or 0) != week:
            continue
        if int(s.get("statSplitTypeId", -1)) != 1:
            continue
        src = int(s.get("statSourceId", -1))
        if src == 0:
            actual = float(s.get("appliedTotal") or 0.0)
            raw = {int(k): float(v) for k, v in (s.get("stats") or {}).items()}
        elif src == 1:
            proj = float(s.get("appliedTotal") or 0.0)
    return actual, proj, raw


def _to_player(entry: dict, year: int) -> tuple[Player, dict[int, float]] | None:
    p = entry.get("player") or {}
    pid = p.get("id")
    pos = _POS_BY_ID.get(int(p.get("defaultPositionId", -1)))
    if pid is None or pos is None:
        return None
    proj, proj_raw = _season_projection(p.get("stats") or [], year)
    return Player(
        espn_id=int(pid),
        name=p.get("fullName") or f"player {pid}",
        pos=pos,
        pro_team=PRO_TEAMS.get(int(p.get("proTeamId", 0)), "FA"),
        eligible_slots=[str(s) for s in (p.get("eligibleSlots") or [])],
        # Draft-day injury status is not recoverable from a past season. UNKNOWN
        # is the honest value: durability then runs on injury HISTORY only,
        # which is stated as a limitation rather than papered over.
        injury_status=InjuryStatus.UNKNOWN,
        proj_season=proj,
        percent_owned=float((p.get("ownership") or {}).get("percentOwned") or 0.0),
        # espn_adp is filled from the real draft, not from ESPN's dead field.
        espn_adp=None,
    ), proj_raw


def load(year: int, *, cfg: Settings | None = None, weeks: int = MAX_WEEK,
         size: int = POOL_SIZE) -> Season:
    """Load one completed season, pulling from ESPN on a cache miss."""
    d = cache_dir(year, cfg)
    c = _client_for(year, cfg)

    facts = _load_facts(year, d, cfg)

    pool_raw = _pull_pool(c, d, size=size)
    players: list[Player] = []
    raw_projection: dict[int, dict[int, float]] = {}
    seen: set[int] = set()
    for entry in pool_raw:
        got = _to_player(entry, year)
        if got is None or got[0].espn_id in seen:
            continue
        pl, proj_raw = got
        seen.add(pl.espn_id)
        players.append(pl)
        if proj_raw:
            raw_projection[pl.espn_id] = proj_raw

    by_id = {p.espn_id: p for p in players}
    raw_weekly: dict[int, dict[int, dict[int, float]]] = {}
    for wk in range(1, weeks + 1):
        for entry in _pull_week(c, d, wk, size=size):
            raw_id = (entry.get("player") or {}).get("id")
            if raw_id is None or int(raw_id) not in by_id:
                continue
            pid = int(raw_id)
            actual, proj, raw = _week_rows(entry["player"].get("stats") or [], year, wk)
            if actual is not None:
                by_id[pid].actual_week[wk] = actual
                raw_weekly.setdefault(pid, {})[wk] = raw
            if proj is not None:
                by_id[pid].proj_week[wk] = proj

    picks = [
        DraftPick(
            overall=int(x["overallPickNumber"]),
            round_num=int(x.get("roundId") or 0),
            team_id=int(x["teamId"]),
            espn_id=int(x["playerId"]),
            keeper=bool(x.get("keeper")),
        )
        for x in _pull_draft(c, d)
        if x.get("playerId") and x.get("teamId") and x.get("overallPickNumber")
    ]
    picks.sort(key=lambda x: x.overall)

    adp = {}
    for pk in picks:
        adp.setdefault(pk.espn_id, float(pk.overall))
    for pl in players:
        pl.espn_adp = adp.get(pl.espn_id)

    log.info("season %d: %d players, %d picks, %d with a real ADP",
             year, len(players), len(picks), sum(1 for p in players if p.espn_adp))
    season = Season(year=year, facts=facts, players=players, picks=picks,
                    raw_weekly=raw_weekly, raw_projection=raw_projection)
    attach_byes(season)
    return season


def _load_facts(year: int, d: Path, cfg: Settings | None) -> LeagueFacts:
    """League settings for that season, parsed fresh each run.

    Deliberately NOT cached to disk as an object: a change to the settings
    parser should be picked up on the next run rather than frozen into a cache
    written by an older version of it. The call is one request.
    """
    return load_settings(_client_for(year, cfg))


#: A week whose row count is at or below this share of the team's median week is
#: that team's bye. Byes are unmistakable in the data — the real minimum sits far
#: below a third of median — so the threshold is a sanity bound, not a tuning knob.
_BYE_SHARE = 0.34


def attach_byes(season: Season) -> int:
    """Derive each player's bye week from the weekly pulls. Returns players set.

    ESPN does not hand back a past season's bye schedule, so it is recovered
    from the data: in its bye week a team's players almost all have no stat row
    at all. Taking the week with the fewest rows (rather than demanding zero)
    handles the seven 2025 teams that still emit a row for a D/ST or kicker.
    Detects all 32 teams in both 2024 and 2025.

    **This matters more than it looks.** Without byes the engine's bye-stacking
    logic is inert while the scorer still gives a player on bye zero points —
    the harness quietly handicaps the engine against a bot that has no bye logic
    to disable. Found by the autopick benchmark, 2026-09-04.
    """
    import statistics
    from collections import defaultdict

    rows: dict[str, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for p in season.players:
        if p.pro_team in ("FA", ""):
            continue
        for wk in p.actual_week:
            rows[p.pro_team][wk] += 1

    window = range(4, 15)          # byes never fall outside this in the modern NFL
    byes: dict[str, int] = {}
    for team, weeks in rows.items():
        counts = {w: weeks.get(w, 0) for w in window}
        median = statistics.median(counts.values())
        low = min(counts, key=lambda w: counts[w])
        if median > 0 and counts[low] <= _BYE_SHARE * median:
            byes[team] = low

    n = 0
    for p in season.players:
        bye = byes.get(p.pro_team)
        if bye:
            p.bye_week = bye
            n += 1
    log.info("season %d: byes for %d/%d teams, %d players",
             season.year, len(byes), len(rows), n)
    return n


def positions_drafted(season: Season) -> dict[Pos, int]:
    """Diagnostic: what the room actually took, by position."""
    by_id = season.by_id
    out: dict[Pos, int] = {}
    for pk in season.picks:
        p = by_id.get(pk.espn_id)
        if p:
            out[p.pos] = out.get(p.pos, 0) + 1
    return out
