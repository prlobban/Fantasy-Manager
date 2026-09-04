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
from core.model.schema import Player

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
            ts.roster.append(pl)
            ts.slots[pl.espn_id] = _slot_name(int(entry.get("lineupSlotId", BENCH_SLOT)))
        teams[tid] = ts

    my_id = c.my_team_id
    opponent = _find_opponent(raw, my_id, wk)

    fa, on_waivers = _free_agents(c, free_agent_size, byes)

    log.info(
        "snapshot: week %s, %d teams, %d free agents, %d on waivers",
        wk, len(teams), len(fa), len(on_waivers),
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


def _free_agents(
    c: EspnClient, size: int, byes: dict[str, int]
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
        out.append(pl)
        if str(entry.get("status", "")).upper() == "WAIVERS":
            on_waivers.add(pl.espn_id)
    return out, on_waivers
