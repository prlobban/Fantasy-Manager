"""Roster shape — surplus and shortage by position (doctrine D5).

Three tight ends on a four-man bench is the case this exists for. A one-slot
position cannot start twice, so a second body there is not depth; it is either
trade capital (D5.2) or the first thing to drop. Conversely a position that
feeds two starters plus the flex needs bodies behind it, and a bye or an injury
with no cover costs a starting slot outright.

Pure. Reads the league's slots, never assumes them.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from core.model.schema import InjuryStatus, LeagueSettings, Player, Pos, Valuation

#: Bench bodies a position "should" carry beyond its starters, as cover.
#: RB/WR feed the flex and get hurt; QB/TE/K/DST are streamable (D6).
_COVER: dict[Pos, int] = {Pos.RB: 2, Pos.WR: 1, Pos.QB: 0, Pos.TE: 0, Pos.K: 0, Pos.DST: 0}


@dataclass
class PositionShape:
    pos: Pos
    have: int
    #: Dedicated starting slots + this position's share of flex slots.
    starters: int
    #: Bench bodies the doctrine wants behind those starters.
    cover: int
    #: have - (starters + cover). Positive = surplus, negative = short.
    delta: int
    #: The bodies past what can start or cover, worst ROS first.
    surplus_players: list[Player] = field(default_factory=list)

    @property
    def verdict(self) -> str:
        if self.delta > 0:
            return "surplus"
        if self.delta < 0:
            return "short"
        return "ok"


@dataclass
class RosterShape:
    by_pos: dict[Pos, PositionShape]
    notes: list[str] = field(default_factory=list)

    @property
    def surplus(self) -> dict[Pos, int]:
        return {p: s.delta for p, s in self.by_pos.items() if s.delta > 0}

    @property
    def short(self) -> dict[Pos, int]:
        return {p: -s.delta for p, s in self.by_pos.items() if s.delta < 0}

    def is_surplus(self, pos: Pos) -> bool:
        return self.by_pos.get(pos, None) is not None and self.by_pos[pos].delta > 0

    def summary(self) -> str:
        parts = []
        for pos, s in self.by_pos.items():
            if s.delta:
                parts.append(f"{pos.value} {s.verdict} {abs(s.delta)} "
                             f"(have {s.have}, need {s.starters}+{s.cover})")
        return "; ".join(parts) or "balanced"


def _flex_share(settings: LeagueSettings) -> dict[Pos, float]:
    """A flex slot is split evenly among the positions it accepts."""
    share: dict[Pos, float] = {}
    for s in settings.starting_slots:
        if not s.is_flex:
            continue
        for pos in s.eligible:
            share[pos] = share.get(pos, 0.0) + s.count / len(s.eligible)
    return share


def analyse(
    roster: list[Player],
    valuations: dict[int, Valuation],
    settings: LeagueSettings,
) -> RosterShape:
    """Where the roster is fat and where it is thin."""
    have = Counter(p.pos for p in roster if p.injury_status is not InjuryStatus.IR)
    flex = _flex_share(settings)
    out: dict[Pos, PositionShape] = {}

    for pos in Pos:
        starters = settings.starters_at(pos) + int(round(flex.get(pos, 0.0)))
        if starters == 0 and have.get(pos, 0) == 0:
            continue
        cover = _COVER.get(pos, 0) if starters else 0
        n = have.get(pos, 0)
        delta = n - (starters + cover)

        surplus_players: list[Player] = []
        if delta > 0:
            ranked = sorted(
                (p for p in roster if p.pos is pos and p.espn_id in valuations),
                key=lambda p: valuations[p.espn_id].vor,
            )
            surplus_players = ranked[:delta]

        out[pos] = PositionShape(
            pos=pos, have=n, starters=starters, cover=cover, delta=delta,
            surplus_players=surplus_players,
        )

    shape = RosterShape(by_pos=out)
    for pos, s in out.items():
        if s.delta > 0 and s.starters <= 1:
            names = ", ".join(p.name for p in s.surplus_players)
            shape.notes.append(
                f"D5.2 {pos.value} is a one-slot position holding {s.have}: "
                f"{names} cannot start and should be traded or cut"
            )
        elif s.delta < 0:
            shape.notes.append(
                f"D5.1 {pos.value} short by {-s.delta}: a bye or injury there "
                "costs a starting slot outright"
            )
    return shape


def drop_order(
    roster: list[Player],
    valuations: dict[int, Valuation],
    settings: LeagueSettings,
) -> list[Player]:
    """Bench bodies in the order the doctrine would cut them: surplus at a
    one-slot position first, then surplus anywhere, then the rest by ROS VOR.
    Starters are never in this list — that is the caller's job (§5.4)."""
    shape = analyse(roster, valuations, settings)
    rank: dict[int, tuple[int, int, float]] = {}
    for p in roster:
        if p.espn_id not in valuations:
            continue
        s = shape.by_pos.get(p.pos)
        is_surplus_body = bool(s and p in s.surplus_players)
        one_slot = bool(s and s.starters <= 1)
        rank[p.espn_id] = (
            0 if (is_surplus_body and one_slot) else 1 if is_surplus_body else 2,
            0 if one_slot else 1,
            valuations[p.espn_id].vor,
        )
    return sorted((p for p in roster if p.espn_id in rank), key=lambda p: rank[p.espn_id])
