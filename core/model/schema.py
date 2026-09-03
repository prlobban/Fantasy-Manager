"""Types that cross a boundary.

Anything passed between core modules, or handed to the agent, is defined here.
Pydantic so the agent's JSON and core's objects validate against one definition.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

# ─────────────────────────────────────────────────────────────────────────────
# League
# ─────────────────────────────────────────────────────────────────────────────


class Pos(str, Enum):
    QB = "QB"
    RB = "RB"
    WR = "WR"
    TE = "TE"
    K = "K"
    DST = "D/ST"


#: Slots that a FLEX-type roster spot can accept, keyed by the ESPN slot name.
FLEX_ELIGIBLE: dict[str, tuple[Pos, ...]] = {
    "RB/WR/TE": (Pos.RB, Pos.WR, Pos.TE),
    "RB/WR": (Pos.RB, Pos.WR),
    "WR/TE": (Pos.WR, Pos.TE),
    "OP": (Pos.QB, Pos.RB, Pos.WR, Pos.TE),  # superflex
}


class RosterSlot(BaseModel):
    """One starting slot in this league, e.g. 2x RB, 1x RB/WR/TE."""

    name: str
    count: int
    #: Positions eligible for this slot. A single-position slot has one entry.
    eligible: tuple[Pos, ...]

    @property
    def is_flex(self) -> bool:
        return len(self.eligible) > 1


class LeagueSettings(BaseModel):
    """Read from mSettings. Never hardcoded, never assumed (§3.1)."""

    league_id: int
    season: int
    name: str
    team_count: int
    draft_type: str  # asserted == SNAKE in espn/settings.py
    #: Starting slots only — bench and IR are excluded.
    starting_slots: list[RosterSlot]
    bench_count: int
    ir_count: int
    #: statId -> points per unit, straight from ESPN.
    scoring: dict[int, float]
    #: "FAAB" or "ROLLING" or "NONE"
    waiver_type: str
    faab_budget: int | None
    trade_deadline: datetime | None
    playoff_team_count: int
    #: Weeks that are playoff matchups, e.g. [15, 16, 17].
    playoff_weeks: list[int]
    regular_season_weeks: int
    keeper_count: int

    @property
    def is_ppr(self) -> bool:
        # statId 53 = receptions
        return self.scoring.get(53, 0.0) > 0

    @property
    def ppr_value(self) -> float:
        return self.scoring.get(53, 0.0)

    @property
    def is_superflex(self) -> bool:
        return any(Pos.QB in s.eligible and s.is_flex for s in self.starting_slots)

    def starters_at(self, pos: Pos) -> int:
        """Dedicated (non-flex) starting slots for a position."""
        return sum(s.count for s in self.starting_slots if s.eligible == (pos,))

    def flex_slots_accepting(self, pos: Pos) -> int:
        return sum(s.count for s in self.starting_slots if s.is_flex and pos in s.eligible)

    @property
    def roster_size(self) -> int:
        return sum(s.count for s in self.starting_slots) + self.bench_count + self.ir_count


# ─────────────────────────────────────────────────────────────────────────────
# Players
# ─────────────────────────────────────────────────────────────────────────────


class InjuryStatus(str, Enum):
    ACTIVE = "ACTIVE"
    QUESTIONABLE = "QUESTIONABLE"
    DOUBTFUL = "DOUBTFUL"
    OUT = "OUT"
    IR = "INJURY_RESERVE"
    SUSPENSION = "SUSPENSION"
    UNKNOWN = "UNKNOWN"

    @property
    def cannot_start(self) -> bool:
        """§4.3 — never start these."""
        return self in {
            InjuryStatus.OUT,
            InjuryStatus.DOUBTFUL,
            InjuryStatus.IR,
            InjuryStatus.SUSPENSION,
        }


class Player(BaseModel):
    espn_id: int
    name: str
    pos: Pos
    pro_team: str
    #: ESPN lineup slot ids this player is eligible for.
    eligible_slots: list[str] = Field(default_factory=list)
    bye_week: int | None = None
    injury_status: InjuryStatus = InjuryStatus.UNKNOWN

    #: ESPN's own projections, in this league's scoring.
    proj_season: float = 0.0
    proj_week: dict[int, float] = Field(default_factory=dict)
    #: Actual scored points, by scoring period.
    actual_week: dict[int, float] = Field(default_factory=dict)

    percent_owned: float = 0.0
    espn_adp: float | None = None
    adp_stdev: float | None = None

    #: Roster ownership: None = free agent, else the espn team id.
    on_team_id: int | None = None


# ─────────────────────────────────────────────────────────────────────────────
# Valuation — the output of core/model/value.py, §2
# ─────────────────────────────────────────────────────────────────────────────

Window = Literal["ros", "week"]


class Valuation(BaseModel):
    """What a player is worth, and every component that made the number.

    Components are kept so a decision can be explained and so §7 can find which
    term is biased. A Valuation with no components is a bug.
    """

    espn_id: int
    window: Window
    #: The window's projected points, after context and availability.
    points: float
    #: Points above the replacement-level starter at this position (§2.3).
    vor: float
    #: 1-indexed tier within the position (§2.4). 1 is the top tier.
    tier: int
    #: Expected games played / 17, in (0, 1]. §2.5.
    availability: float
    #: Stdev of weekly scores, and share of weeks under 50% of projection. §2.6.
    stdev: float | None = None
    bust_rate: float | None = None
    #: Every multiplier and adjustment applied, named. §8.8.
    components: dict[str, float] = Field(default_factory=dict)
    #: Reasons a hard veto fired (§2.5). Non-empty means undraftable/unstartable.
    vetoes: list[str] = Field(default_factory=list)
    #: Anything the model could not compute. Non-empty forces caution (§8.8).
    missing: list[str] = Field(default_factory=list)

    @property
    def vetoed(self) -> bool:
        return bool(self.vetoes)


# ─────────────────────────────────────────────────────────────────────────────
# Actions + decisions
# ─────────────────────────────────────────────────────────────────────────────


class ActionKind(str, Enum):
    DRAFT_PICK = "draft_pick"
    QUEUE_SYNC = "queue_sync"
    SET_LINEUP = "set_lineup"
    ADD_DROP = "add_drop"
    WAIVER_CLAIM = "waiver_claim"
    PROPOSE_TRADE = "propose_trade"
    ACCEPT_TRADE = "accept_trade"
    REJECT_TRADE = "reject_trade"
    NOTIFY = "notify"


class Action(BaseModel):
    """A requested write. Produced by the agent or by core; always passes
    through gates/write_gate before it reaches the browser."""

    kind: ActionKind
    args: dict = Field(default_factory=dict)
    #: Playbook sections justifying this action. Required — an action with no
    #: citation is rejected at the schema boundary (build-plan §6.3).
    cites: list[str] = Field(min_length=1)
    reason: str
    confidence: float = Field(ge=0.0, le=1.0, default=0.8)


class GateResult(BaseModel):
    allowed: bool
    #: Which gate refused, e.g. "§6.8.2" or "kill_switch".
    refused_by: str | None = None
    reason: str = ""


class DecisionRecord(BaseModel):
    """§7.1 — the action AND the prediction that justified it. Append-only."""

    at: datetime
    kind: ActionKind
    cites: list[str]
    reason: str
    #: The number we acted on, so Tuesday can grade the decision, not the outcome.
    predicted: dict[str, float] = Field(default_factory=dict)
    #: What we passed on, and its number.
    alternative: dict | None = None
    executed: bool = False
    gate: GateResult | None = None
    receipt: str | None = None


# ─────────────────────────────────────────────────────────────────────────────
# The §6.8 gauntlet
# ─────────────────────────────────────────────────────────────────────────────


class GateCheck(BaseModel):
    section: str  # "§6.8.2"
    name: str
    passed: bool
    detail: str


class GauntletResult(BaseModel):
    """§6.8. One fail = reject. No averaging, no overall score (§6.8.0)."""

    offer_id: str
    checks: list[GateCheck]

    @property
    def accepted(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)

    @property
    def failed_on(self) -> list[str]:
        return [f"{c.section} {c.name}" for c in self.checks if not c.passed]
