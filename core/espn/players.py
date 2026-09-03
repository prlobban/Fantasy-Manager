"""§2.2 — the player pool, with ESPN's own projections.

ESPN's projection is the right base because it is computed against the exact
scoring settings in play. A generic PPR ranking off a website is not (§2.2), and
in this league it would be doubly wrong: half-PPR with 5-point passing TDs.

Stat rows are identified by (statSourceId, statSplitTypeId, seasonId):
    src 1, split 0  -> projected season total
    src 1, split 1  -> projected, one scoring period
    src 0, split 0  -> actual season total
    src 0, split 1  -> actual, one scoring period
Verified against the live API 2026-09-03. Passing a
`filterStatsForTopScoringPeriodIds` filter SUPPRESSES the projected season row,
which is why this module deliberately sends no stat filter.
"""

from __future__ import annotations

import logging

from core.espn.client import EspnClient, client
from core.model.schema import InjuryStatus, Player, Pos

log = logging.getLogger(__name__)

#: ESPN defaultPositionId -> Pos. Protocol, not policy.
_POS_BY_ID: dict[int, Pos] = {1: Pos.QB, 2: Pos.RB, 3: Pos.WR, 4: Pos.TE, 5: Pos.K, 16: Pos.DST}

#: ESPN proTeamId -> abbreviation. 0 is free agent / no team.
PRO_TEAMS: dict[int, str] = {
    0: "FA", 1: "ATL", 2: "BUF", 3: "CHI", 4: "CIN", 5: "CLE", 6: "DAL", 7: "DEN",
    8: "DET", 9: "GB", 10: "TEN", 11: "IND", 12: "KC", 13: "LV", 14: "LAR", 15: "MIA",
    16: "MIN", 17: "NE", 18: "NO", 19: "NYG", 20: "NYJ", 21: "PHI", 22: "ARI",
    23: "PIT", 24: "LAC", 25: "SF", 26: "SEA", 27: "TB", 28: "WSH", 29: "CAR",
    30: "JAX", 33: "BAL", 34: "HOU",
}

_INJURY = {
    "ACTIVE": InjuryStatus.ACTIVE,
    "NORMAL": InjuryStatus.ACTIVE,
    "QUESTIONABLE": InjuryStatus.QUESTIONABLE,
    "DOUBTFUL": InjuryStatus.DOUBTFUL,
    "OUT": InjuryStatus.OUT,
    "INJURY_RESERVE": InjuryStatus.IR,
    "SUSPENSION": InjuryStatus.SUSPENSION,
}


def _parse_stats(rows: list[dict], season: int) -> tuple[float, dict[int, float], dict[int, float]]:
    """-> (projected season total, projected by week, actual by week)."""
    proj_season = 0.0
    proj_week: dict[int, float] = {}
    actual_week: dict[int, float] = {}

    for s in rows:
        if int(s.get("seasonId", 0)) != season:
            continue
        src = int(s.get("statSourceId", -1))
        split = int(s.get("statSplitTypeId", -1))
        applied = float(s.get("appliedTotal") or 0.0)
        spid = int(s.get("scoringPeriodId") or 0)

        if src == 1 and split == 0:
            proj_season = applied
        elif src == 1 and split == 1 and spid:
            proj_week[spid] = applied
        elif src == 0 and split == 1 and spid:
            actual_week[spid] = applied

    return proj_season, proj_week, actual_week


def _to_player(entry: dict, season: int) -> Player | None:
    p = entry.get("player") or {}
    pid = p.get("id")
    pos = _POS_BY_ID.get(int(p.get("defaultPositionId", -1)))
    if pid is None or pos is None:
        return None  # IDP, coaches, punters — not modelled

    proj_season, proj_week, actual_week = _parse_stats(p.get("stats") or [], season)
    own = p.get("ownership") or {}

    adp = own.get("averageDraftPosition")
    # ESPN reports 0 for players with no draft data; that is "undrafted", not
    # "first overall". Treated as unknown so survival maths doesn't invert.
    if not adp or adp <= 0:
        adp = None

    raw_status = str(p.get("injuryStatus") or "").upper()
    status = _INJURY.get(raw_status, InjuryStatus.UNKNOWN)
    if status is InjuryStatus.UNKNOWN and not p.get("injured", False):
        status = InjuryStatus.ACTIVE

    return Player(
        espn_id=int(pid),
        name=p.get("fullName") or f"player {pid}",
        pos=pos,
        pro_team=PRO_TEAMS.get(int(p.get("proTeamId", 0)), "FA"),
        eligible_slots=[str(s) for s in (p.get("eligibleSlots") or [])],
        injury_status=status,
        proj_season=proj_season,
        proj_week=proj_week,
        actual_week=actual_week,
        percent_owned=float(own.get("percentOwned") or 0.0),
        espn_adp=adp,
        on_team_id=entry.get("onTeamId") or None,
    )


def load_pool(
    c: EspnClient | None = None,
    *,
    size: int = 450,
    page: int = 150,
) -> list[Player]:
    """The top `size` players by ownership, with projections.

    Ownership order is the right sort: it is ESPN's own view of who matters, and
    it keeps every rostered player and every plausible waiver target in the pool
    without pulling 3,000 practice-squad names.
    """
    c = c or client()
    out: list[Player] = []
    seen: set[int] = set()

    for offset in range(0, size, page):
        filters = {
            "players": {
                "filterStatus": {"value": ["FREEAGENT", "WAIVERS", "ONTEAM"]},
                "limit": min(page, size - offset),
                "offset": offset,
                "sortPercOwned": {"sortAsc": False, "sortPriority": 1},
            }
        }
        # No stat filter, deliberately — see the module docstring.
        data = c.get_view("kona_player_info", filters=filters)
        batch = data.get("players") or []
        if not batch:
            break
        for entry in batch:
            pl = _to_player(entry, c.cfg.season)
            if pl and pl.espn_id not in seen:
                seen.add(pl.espn_id)
                out.append(pl)
        if len(batch) < min(page, size - offset):
            break

    log.info("loaded %d players", len(out))
    return out


def load_byes(c: EspnClient | None = None) -> dict[str, int]:
    """Bye week per pro-team abbreviation.

    This lives on the SEASONS endpoint, not the league endpoint, so it cannot go
    through EspnClient.get_view (which prepends the league path). Verified
    2026-09-03: 32 teams returned.
    """
    import httpx

    c = c or client()
    url = f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl/seasons/{c.cfg.season}"
    try:
        r = httpx.get(
            url,
            params={"view": "proTeamSchedules_wl"},
            cookies={"SWID": c.cfg.swid, "espn_s2": c.cfg.espn_s2},
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=20,
        )
        r.raise_for_status()
        teams = r.json().get("settings", {}).get("proTeams", [])
    except Exception as e:  # non-fatal: byes refine decisions, they don't gate them
        log.warning("could not load bye weeks: %s", e)
        return {}

    return {
        t["abbrev"].upper(): int(t["byeWeek"])
        for t in teams
        if t.get("abbrev") and t.get("byeWeek")
    }


def attach_byes(players: list[Player], c: EspnClient | None = None) -> int:
    """Fill bye_week in place. Returns how many were resolved.

    Bye weeks matter twice: §3.7 forbids stacking starters on one bye, and a bye
    is not an injury — §2.6 must not read a zero-point bye week as a bust.
    """
    byes = load_byes(c)
    if not byes:
        log.warning("no bye weeks resolved — §3.7 bye collision checks will be skipped")
        return 0
    n = 0
    for p in players:
        if (bye := byes.get(p.pro_team.upper())) is not None:
            p.bye_week = bye
            n += 1
    return n
