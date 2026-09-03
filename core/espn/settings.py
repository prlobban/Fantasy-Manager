"""§3.1 — read the league before reasoning about it.

Scoring, roster slots, team count, waiver type, position caps and playoff shape
all come from mSettings at run time. Nothing here is hardcoded; the only literals
are ESPN's own id→name mappings, which are protocol, not policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from core.espn.client import EspnClient, EspnReadError, client
from core.model.schema import LeagueSettings, Pos, RosterSlot

#: ESPN lineup slot id -> (display name, eligible positions).
#: Protocol-level: these are ESPN's constants, not our judgment.
SLOT_MAP: dict[int, tuple[str, tuple[Pos, ...]]] = {
    0: ("QB", (Pos.QB,)),
    2: ("RB", (Pos.RB,)),
    3: ("RB/WR", (Pos.RB, Pos.WR)),
    4: ("WR", (Pos.WR,)),
    5: ("WR/TE", (Pos.WR, Pos.TE)),
    6: ("TE", (Pos.TE,)),
    7: ("OP", (Pos.QB, Pos.RB, Pos.WR, Pos.TE)),  # superflex
    16: ("D/ST", (Pos.DST,)),
    17: ("K", (Pos.K,)),
    23: ("RB/WR/TE", (Pos.RB, Pos.WR, Pos.TE)),  # standard flex
}
BENCH_SLOT = 20
IR_SLOT = 21

#: ESPN defensive-player slot ids we do not model (IDP leagues).
_IDP_SLOTS = {8, 9, 10, 11, 12, 13, 14, 15, 18, 19, 22, 24}

#: ESPN default position id -> Pos, for positionLimits.
POSITION_ID_MAP: dict[int, Pos] = {
    1: Pos.QB,
    2: Pos.RB,
    3: Pos.WR,
    4: Pos.TE,
    5: Pos.K,
    16: Pos.DST,
}


@dataclass(frozen=True)
class LeagueFacts:
    """LeagueSettings plus the things only the raw payload carries."""

    settings: LeagueSettings
    #: Hard roster caps per position. -1 in ESPN means "no limit"; absent here.
    position_limits: dict[Pos, int]
    #: Seconds on the clock per pick.
    seconds_per_pick: int
    #: Draft order by team id, index 0 == pick 1.
    pick_order: list[int]
    draft_at: datetime | None
    #: "WAIVERS_TRADITIONAL" (rolling priority) or a budget system.
    acquisition_type: str
    using_acquisition_budget: bool
    waiver_process_days: list[str]
    trade_revision_hours: int
    veto_votes_required: int
    playoff_seeding_rule: str

    @property
    def is_faab(self) -> bool:
        return self.using_acquisition_budget

    @property
    def draftable_spots(self) -> int:
        """Rounds in the draft: starters + bench, IR excluded."""
        s = self.settings
        return sum(x.count for x in s.starting_slots) + s.bench_count

    def my_picks(self, team_id: int) -> list[int]:
        """Overall pick numbers for a team across the whole snake draft."""
        if team_id not in self.pick_order:
            raise EspnReadError(f"team {team_id} not in pick order {self.pick_order}")
        slot = self.pick_order.index(team_id) + 1
        n = len(self.pick_order)
        return [
            (r - 1) * n + (slot if r % 2 == 1 else n - slot + 1)
            for r in range(1, self.draftable_spots + 1)
        ]


def _ms_to_dt(ms: int | None) -> datetime | None:
    return datetime.fromtimestamp(ms / 1000) if ms else None


def load(c: EspnClient | None = None) -> LeagueFacts:
    """Read mSettings and build the typed view of this league."""
    c = c or client()
    raw = c.get_view("mSettings")["settings"]

    roster = raw["rosterSettings"]
    sched = raw["scheduleSettings"]
    draft = raw["draftSettings"]
    acq = raw["acquisitionSettings"]
    trade = raw.get("tradeSettings", {})
    scoring_items = raw["scoringSettings"]["scoringItems"]

    # ── starting slots ───────────────────────────────────────────────────────
    counts = {int(k): int(v) for k, v in roster["lineupSlotCounts"].items() if int(v) > 0}
    unknown = set(counts) - set(SLOT_MAP) - {BENCH_SLOT, IR_SLOT} - _IDP_SLOTS
    if unknown:
        raise EspnReadError(
            f"unmapped ESPN lineup slots {sorted(unknown)} — this league uses a format "
            "core does not model. Refusing to guess (§8.8)."
        )
    if idp := (set(counts) & _IDP_SLOTS):
        raise EspnReadError(
            f"league has IDP slots {sorted(idp)}; core models offence + D/ST only."
        )

    starting = [
        RosterSlot(name=SLOT_MAP[sid][0], count=n, eligible=SLOT_MAP[sid][1])
        for sid, n in sorted(counts.items())
        if sid in SLOT_MAP
    ]

    # ── draft type ───────────────────────────────────────────────────────────
    dtype = str(draft.get("type", "")).upper()
    if dtype != "SNAKE":
        raise EspnReadError(
            f"draft type is {dtype!r}, not SNAKE. §3 is written for a snake and does "
            "not transfer — escalate rather than proceeding (§3.1)."
        )

    settings_obj = LeagueSettings(
        league_id=c.cfg.league_id,
        season=c.cfg.season,
        name=raw.get("name", ""),
        team_count=int(raw.get("size") or sched.get("matchupPeriodCount", 0) or len(c.league.teams)),
        draft_type=dtype,
        starting_slots=starting,
        bench_count=counts.get(BENCH_SLOT, 0),
        ir_count=counts.get(IR_SLOT, 0),
        scoring={int(i["statId"]): float(i["points"]) for i in scoring_items},
        waiver_type=str(acq.get("acquisitionType", "")),
        faab_budget=int(acq["acquisitionBudget"])
        if acq.get("isUsingAcquisitionBudget")
        else None,
        trade_deadline=_ms_to_dt(trade.get("deadlineDate")),
        playoff_team_count=int(sched.get("playoffTeamCount", 0)),
        playoff_weeks=_playoff_weeks(sched),
        regular_season_weeks=int(sched.get("matchupPeriodCount", 0)),
        keeper_count=int(draft.get("keeperCount", 0) or 0),
    )

    limits = {
        POSITION_ID_MAP[int(pid)]: int(cap)
        for pid, cap in (roster.get("positionLimits") or {}).items()
        if int(pid) in POSITION_ID_MAP and int(cap) > 0
    }

    return LeagueFacts(
        settings=settings_obj,
        position_limits=limits,
        seconds_per_pick=int(draft.get("timePerSelection") or 0),
        pick_order=[int(t) for t in (draft.get("pickOrder") or [])],
        draft_at=_ms_to_dt(draft.get("date")),
        acquisition_type=str(acq.get("acquisitionType", "")),
        using_acquisition_budget=bool(acq.get("isUsingAcquisitionBudget")),
        waiver_process_days=list(acq.get("waiverProcessDays") or []),
        trade_revision_hours=int(trade.get("revisionHours") or 0),
        veto_votes_required=int(trade.get("vetoVotesRequired") or 0),
        playoff_seeding_rule=str(sched.get("playoffSeedingRule", "")),
    )


def _playoff_weeks(sched: dict) -> list[int]:
    """Playoff weeks = the matchup periods after the regular season."""
    reg = int(sched.get("matchupPeriodCount", 0))
    teams = int(sched.get("playoffTeamCount", 0))
    length = int(sched.get("playoffMatchupPeriodLength", 1) or 1)
    if not reg or not teams:
        return []
    # A 6-team bracket is 3 rounds (bye, semi, final); 4-team is 2.
    rounds = 3 if teams > 4 else 2
    return [reg + 1 + i for i in range(rounds * length)]
