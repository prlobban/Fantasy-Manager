"""One timestamped snapshot of everything the manager loops need.

Taken fresh at the start of every run and re-taken immediately before any write
(§8.3 — never act on a stale read). Between the morning sweep and an afternoon
claim, a player can be rostered by someone else.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from core.espn import players as players_mod
from core.espn.client import EspnClient, client
from core.espn.settings import SLOT_MAP, LeagueFacts
from core.model.schema import Player, ProGame

log = logging.getLogger(__name__)

BENCH_SLOT = 20
IR_SLOT = 21


@dataclass
class TeamState:
    team_id: int
    name: str
    roster: list[Player] = field(default_factory=list)
    #: espn_id -> current lineup slot name ("QB", "BE", "IR", ...)
    slots: dict[int, str] = field(default_factory=dict)
    wins: int = 0
    losses: int = 0
    points_for: float = 0.0
    waiver_priority: int | None = None

    @property
    def starters(self) -> dict[int, str]:
        return {pid: s for pid, s in self.slots.items() if s not in ("BE", "IR")}

    @property
    def bench_used(self) -> int:
        return sum(1 for s in self.slots.values() if s == "BE")


@dataclass
class LeagueState:
    taken_at: datetime
    facts: LeagueFacts
    week: int
    teams: dict[int, TeamState]
    my_team_id: int
    free_agents: list[Player] = field(default_factory=list)
    #: Player ids inside the 24h waiver window — these cost priority (§5.3.2).
    on_waivers: set[int] = field(default_factory=set)
    opponent_team_id: int | None = None
    #: The week a FORWARD-LOOKING decision should be valued against (§5.9).
    #:
    #: ESPN's `week` stays on the current scoring period until it rolls over on
    #: Tuesday. Between the last Sunday game and that rollover, every game in
    #: `week` is played, so every weekly projection is settled and every waiver
    #: gain computes to exactly 0.0 — the Monday sweep cannot recommend
    #: anything, on the one day Sunday's breakouts are most obvious.
    #: This is `week` while any of its games are still to come, and `week + 1`
    #: once they are all done. The lineup still uses `week`; there is nothing
    #: movable there and pretending otherwise would plan next week's lineup
    #: into this week's locked slots.
    decision_week: int = 0

    def __post_init__(self) -> None:
        if not self.decision_week:
            self.decision_week = self.week

    @property
    def me(self) -> TeamState:
        return self.teams[self.my_team_id]

    @property
    def opponent(self) -> TeamState | None:
        return self.teams.get(self.opponent_team_id) if self.opponent_team_id else None

    @property
    def bench_open(self) -> int:
        return max(0, self.facts.settings.bench_count - self.me.bench_used)

    def all_players(self) -> list[Player]:
        seen: dict[int, Player] = {}
        for t in self.teams.values():
            for p in t.roster:
                seen[p.espn_id] = p
        for p in self.free_agents:
            seen.setdefault(p.espn_id, p)
        return list(seen.values())


def _slot_name(slot_id: int) -> str:
    if slot_id == BENCH_SLOT:
        return "BE"
    if slot_id == IR_SLOT:
        return "IR"
    return SLOT_MAP.get(slot_id, (str(slot_id), ()))[0]


def snapshot(
    c: EspnClient | None = None,
    facts: LeagueFacts | None = None,
    *,
    week: int | None = None,
    free_agent_size: int = 200,
) -> LeagueState:
    """Read the whole league. One call, one timestamp."""
    from core.espn import settings as settings_mod

    c = c or client()
    facts = facts or settings_mod.load(c)
    wk = week or c.current_week

    raw = c.get_view(["mRoster", "mTeam", "mMatchup", "mSettings"])
    byes = players_mod.load_byes(c)
    # Without this every projection reads as live and every player as
    # movable, whatever the clock says — see Player.game_locked.
    pro_games = players_mod.load_pro_games(c)

    teams: dict[int, TeamState] = {}
    for t in raw.get("teams", []):
        tid = int(t["id"])
        ts = TeamState(
            team_id=tid,
            name=t.get("name") or f"team {tid}",
            wins=int((t.get("record", {}).get("overall", {}) or {}).get("wins", 0)),
            losses=int((t.get("record", {}).get("overall", {}) or {}).get("losses", 0)),
            points_for=float((t.get("record", {}).get("overall", {}) or {}).get("pointsFor", 0.0)),
            waiver_priority=t.get("waiverRank"),
        )
        for entry in (t.get("roster", {}) or {}).get("entries", []) or []:
            pl = players_mod._to_player(
                {"player": entry.get("playerPoolEntry", {}).get("player", {}),
                 "onTeamId": tid},
                c.cfg.season,
            )
            if pl is None:
                continue
            pl.bye_week = byes.get(pl.pro_team.upper())
            pl.games = pro_games.get(pl.pro_team.upper(), {})
            ts.roster.append(pl)
            ts.slots[pl.espn_id] = _slot_name(int(entry.get("lineupSlotId", BENCH_SLOT)))
        teams[tid] = ts

    my_id = c.my_team_id
    opponent = _find_opponent(raw, my_id, wk)

    fa, on_waivers = _free_agents(c, free_agent_size, byes, pro_games)

    rostered = [p for t in teams.values() for p in t.roster]
    decision_wk = _decision_week(wk, rostered + fa)
    if decision_wk != wk:
        # ESPN serves these happily; we simply never asked, because no request
        # ever carried a scoringPeriodId. Without it the whole pool values at
        # 0.0 for the week we are actually deciding about.
        _merge_week_projections(c, rostered + fa, decision_wk, free_agent_size)

    log.info(
        "snapshot: week %s (deciding on week %s), %d teams, %d free agents, "
        "%d on waivers",
        wk, decision_wk, len(teams), len(fa), len(on_waivers),
    )
    return LeagueState(
        taken_at=datetime.now(UTC),
        facts=facts,
        week=wk,
        teams=teams,
        my_team_id=my_id,
        free_agents=fa,
        on_waivers=on_waivers,
        opponent_team_id=opponent,
        decision_week=decision_wk,
    )


def _find_opponent(raw: dict, my_id: int, week: int) -> int | None:
    for m in raw.get("schedule", []) or []:
        if int(m.get("matchupPeriodId", -1)) != week:
            continue
        home = (m.get("home") or {}).get("teamId")
        away = (m.get("away") or {}).get("teamId")
        if home == my_id:
            return away
        if away == my_id:
            return home
    return None


def _decision_week(week: int, pool: list[Player]) -> int:
    """§5.9 — the week a waiver or trade decision is actually about.

    `week` while any of its games are still to be played; `week + 1` once the
    slate is done. Decided on the schedule rather than the clock so it does not
    depend on what day the box thinks it is.

    Fails SAFE: with no schedule data at all (`game_locked` fails open, so
    nothing reads as locked) this returns `week` — the old behaviour.
    """
    known = [p for p in pool if p.games.get(week) is not None]
    if not known:
        return week
    return week if any(not p.game_locked(week) for p in known) else week + 1


def _merge_week_projections(c: EspnClient, pool: list[Player], week: int,
                            size: int) -> int:
    """Fill `proj_week[week]` for the pool from ESPN, in place.

    One extra call, carrying the `scoringPeriodId` that the normal fetch omits.
    Non-fatal: a failure leaves the pool valuing at 0.0 for that week, which is
    the behaviour this exists to fix, so it is logged loudly.
    """
    filters = {
        "players": {
            "filterStatus": {"value": ["FREEAGENT", "WAIVERS", "ONTEAM"]},
            "limit": max(size, 300),
            "offset": 0,
            "sortPercOwned": {"sortAsc": False, "sortPriority": 1},
        }
    }
    try:
        data = c.get_view("kona_player_info", filters=filters,
                          params={"scoringPeriodId": week})
    except Exception as e:
        log.warning("could not load week-%s projections: %s", week, e)
        return 0

    proj: dict[int, float] = {}
    for entry in data.get("players") or []:
        p = entry.get("player") or {}
        pid = p.get("id")
        if pid is None:
            continue
        for s in p.get("stats") or []:
            if (int(s.get("statSourceId", -1)) == 1
                    and int(s.get("statSplitTypeId", -1)) == 1
                    and int(s.get("scoringPeriodId") or 0) == week):
                proj[int(pid)] = float(s.get("appliedTotal") or 0.0)
                break

    n = 0
    for pl in pool:
        if (v := proj.get(pl.espn_id)) is not None:
            pl.proj_week[week] = v
            n += 1
    log.info("merged week-%s projections for %d/%d players", week, n, len(pool))
    return n


def _free_agents(
    c: EspnClient, size: int, byes: dict[str, int],
    pro_games: dict[str, dict[int, ProGame]] | None = None,
) -> tuple[list[Player], set[int]]:
    """Unrostered players, and which of them still cost a waiver claim.

    ESPN reports status FREEAGENT vs WAIVERS. §5.3.2 turns on that distinction:
    a player who has cleared waivers costs no priority at all.
    """
    filters = {
        "players": {
            "filterStatus": {"value": ["FREEAGENT", "WAIVERS"]},
            "limit": size,
            "offset": 0,
            "sortPercOwned": {"sortAsc": False, "sortPriority": 1},
        }
    }
    data = c.get_view("kona_player_info", filters=filters)
    out: list[Player] = []
    on_waivers: set[int] = set()
    for entry in data.get("players") or []:
        pl = players_mod._to_player(entry, c.cfg.season)
        if pl is None:
            continue
        pl.bye_week = byes.get(pl.pro_team.upper())
        pl.games = (pro_games or {}).get(pl.pro_team.upper(), {})
        out.append(pl)
        if str(entry.get("status", "")).upper() == "WAIVERS":
            on_waivers.add(pl.espn_id)
    return out, on_waivers
