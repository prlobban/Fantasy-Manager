"""A 10-team snake draft where each seat is either the engine or an ESPN bot.

The replay in `replay.py` grades the engine against nine humans whose picks are
fixed, and needs a rule for what a human does when the engine steals his target
— a rule the result turns out to be sensitive to. This module has no such rule.
The opponents are an algorithm, they react to any board the same way, and the
control is that same algorithm sitting in our seat.

One league shape for both seasons (2026's, his actual one), so the two years are
comparable and the answer is about the league he is in rather than the ones the
data came from.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field

from core.backtest import autopick
from core.draft import picker
from core.draft.board import Board
from core.draft.room import Pick, RoomModel
from core.espn.settings import LeagueFacts
from core.model.schema import Player, Pos

log = logging.getLogger(__name__)

ENGINE = "engine"
AUTOPICK = "autopick"


@dataclass
class SeatPick:
    overall: int
    round_num: int
    seat: int
    espn_id: int
    name: str
    pos: Pos
    #: VOR only where the engine made the pick; a bot has no such notion.
    vor: float | None = None


@dataclass
class ArenaResult:
    rosters: dict[int, list[Player]] = field(default_factory=dict)
    picks: list[SeatPick] = field(default_factory=list)
    engine_seat: int | None = None

    def roster_of(self, seat: int) -> list[Player]:
        return self.rosters.get(seat, [])

    def our_picks(self) -> list[SeatPick]:
        return [p for p in self.picks if p.seat == self.engine_seat]


def snake_order(teams: int, rounds: int) -> list[int]:
    """Seat on the clock for each overall pick, 1-indexed seats."""
    out: list[int] = []
    for r in range(1, rounds + 1):
        seats = range(1, teams + 1) if r % 2 else range(teams, 0, -1)
        out.extend(seats)
    return out


def run(board: Board, facts: LeagueFacts, ranks: dict[int, int], *,
        engine_seat: int | None, teams: int = 10,
        seat_ranks: dict[int, int] | None = None) -> ArenaResult:
    """Draft the whole league. `engine_seat=None` is the all-autopick control.

    `seat_ranks` puts a THIRD kind of drafter in `engine_seat`: the autopick
    algorithm running off a different ranking (in practice our own projection
    order). That is the diagnostic that separates the two ways this engine can
    lose — if a seat drafting on our raw projections beats the full picker, the
    picker's logic is subtracting value from its own inputs; if it loses to
    ESPN too, the projections are the weaker board.
    """
    rounds = facts.draftable_spots
    settings = facts.settings
    limits = facts.position_limits
    order = snake_order(teams, rounds)
    rosters: dict[int, Counter] = {s: Counter() for s in range(1, teams + 1)}
    picked: dict[int, list[Player]] = {s: [] for s in range(1, teams + 1)}
    taken: set[int] = set()

    # The engine reads the room through the same RoomModel the live loop uses,
    # so it sees runs, demand and its own roster exactly as it will on Saturday.
    room = RoomModel(facts=_facts_for_seat(facts, order, teams),
                     my_team_id=engine_seat or 0)

    out = ArenaResult(engine_seat=engine_seat)

    for i, seat in enumerate(order):
        overall = i + 1
        round_num = i // teams + 1
        rounds_left = rounds - round_num + 1
        roster = rosters[seat]

        chosen: Player | None
        vor: float | None = None
        if seat == engine_seat and seat_ranks is not None:
            avail = [p for p in board.players if p.espn_id not in taken]
            chosen = autopick.pick(avail, roster, rounds_left=rounds_left,
                                   settings=settings, limits=limits, ranks=seat_ranks)
        elif seat == engine_seat:
            plan = picker.rank(board.rows, room)
            chosen = plan.best.player if plan.best else None
            if chosen is not None:
                vor = plan.best.valuation.vor
            else:
                # Same guard as the replay: never silently drop a pick, or the
                # engine finishes a body short and every rival looks better.
                chosen = _best_legal(board, taken, roster, limits, ranks)
                log.warning("engine had no legal candidate at #%d — forced %s",
                            overall, chosen.name if chosen else "nothing")
        else:
            avail = [p for p in board.players if p.espn_id not in taken]
            chosen = autopick.pick(avail, roster, rounds_left=rounds_left,
                                   settings=settings, limits=limits, ranks=ranks)

        if chosen is None:
            log.warning("no legal pick at #%d for seat %d", overall, seat)
            continue

        taken.add(chosen.espn_id)
        roster[chosen.pos] += 1
        picked[seat].append(chosen)
        out.picks.append(SeatPick(overall=overall, round_num=round_num, seat=seat,
                                  espn_id=chosen.espn_id, name=chosen.name,
                                  pos=chosen.pos, vor=vor))
        room.apply([Pick(overall=overall, team_id=seat, espn_id=chosen.espn_id,
                         pos=chosen.pos, name=chosen.name)])

    out.rosters = {s: list(v) for s, v in picked.items()}
    _assert_legal(out, facts, teams)
    return out


def _facts_for_seat(facts: LeagueFacts, order: list[int], teams: int) -> LeagueFacts:
    """LeagueFacts whose `pick_order` is the arena's seat order.

    RoomModel derives whose turn it is from `pick_order`, so the engine must see
    the arena's seats (1..10) rather than the historical league's team ids, or
    it computes the gap to its own next pick from the wrong snake.
    """
    import dataclasses

    return dataclasses.replace(facts, pick_order=list(range(1, teams + 1)))


def _best_legal(board: Board, taken: set[int], roster: Counter,
                limits: dict[Pos, int], ranks: dict[int, int]) -> Player | None:
    legal = [p for p in board.players
             if p.espn_id not in taken and roster.get(p.pos, 0) < limits.get(p.pos, 99)]
    if not legal:
        return None
    return min(legal, key=lambda p: ranks.get(p.espn_id, autopick.UNRANKED))


def _assert_legal(res: ArenaResult, facts: LeagueFacts, teams: int) -> None:
    """Every roster full, legal, and nobody drafted twice.

    Checked on every single draft rather than in a test, because a benchmark
    that quietly produces a short roster reports a number that means nothing,
    and the failure is invisible in the average.
    """
    seen: set[int] = set()
    for seat in range(1, teams + 1):
        roster = res.rosters.get(seat, [])
        if len(roster) != facts.draftable_spots:
            raise AssertionError(
                f"seat {seat} has {len(roster)} players, expected "
                f"{facts.draftable_spots}")
        counts = Counter(p.pos for p in roster)
        for pos, n in counts.items():
            cap = facts.position_limits.get(pos)
            if cap is not None and n > cap:
                raise AssertionError(f"seat {seat} over cap at {pos.value}: {n} > {cap}")
        for p in roster:
            if p.espn_id in seen:
                raise AssertionError(f"{p.name} drafted twice")
            seen.add(p.espn_id)
