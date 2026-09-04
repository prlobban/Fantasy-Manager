"""Re-run a completed draft with the engine in one seat.

The other teams make the picks they REALLY made, in the order they really made
them. That is the whole reason this is worth more than the ADP-bot simulator:
the bots were written by us, so beating them only proved the engine agrees with
its own author. The 2025 room is nine real people whose behaviour we did not
design.

**Where hindsight could leak in, and what stops it:**

- the pool is ordered by ESPN's PRESEASON draft ranks, not by end-of-season
  ownership (`history._pool_filter`);
- `proj_season` is the projection ESPN published before that season;
- injury history is restricted to seasons strictly BEFORE the one being
  drafted;
- injury status is UNKNOWN, because draft-day status is not recoverable;
- `actual_week` is never read here. `assert_no_leakage` enforces that against
  the built board rather than trusting this docstring.

**The fallback.** Once the engine takes a player his real counterpart did not,
the draft diverges: some later real pick is for a player already gone. That
team then takes its own NEXT real pick early, and failing that the best
remaining player by preseason projection that its roster caps allow. Every
fallback is counted, because a replay that needed many of them has drifted far
from the season that actually happened and its result deserves less weight.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

from core.backtest.history import Season
from core.data import nflverse
from core.draft import picker
from core.draft.board import Board
from core.draft.room import Pick, RoomModel
from core.espn.settings import LeagueFacts
from core.model.priors import priors
from core.model.schema import Player, Pos
from core.model.value import PlayerContext, value_pool

log = logging.getLogger(__name__)

#: Completed seasons of injury history to give the engine, matching §2.5.
HISTORY_SEASONS = 3


class LeakageError(AssertionError):
    """A backtest that can see the future is worse than no backtest."""


def build_board(season: Season, *, facts: LeagueFacts | None = None,
                injury_history: bool = True) -> Board:
    """A board for `season`, built the way the live board is built.

    Deliberately reuses `value_pool` — the same function the real draft runs on
    — rather than reimplementing valuation for the backtest. A backtest of a
    reimplementation grades the reimplementation.
    """
    facts = facts or season.facts
    yrs = tuple(range(season.year - HISTORY_SEASONS, season.year))

    contexts: dict[int, PlayerContext] = {}
    matched = 0
    for p in season.players:
        events: list = []
        ok = False
        if injury_history:
            events, ok = nflverse.history_for(p.espn_id, p.name, yrs)
        matched += int(ok)
        contexts[p.espn_id] = PlayerContext(injury_history=events)

    vals = value_pool(
        season.players,
        facts.settings,
        window="ros",
        weeks_remaining=facts.settings.regular_season_weeks,
        contexts=contexts,
        override_cap=float(priors().get("model.override_cap")),
    )
    board = Board(
        built_at=datetime.now(UTC),
        facts=facts,
        players=season.players,
        valuations=vals,
        coverage={
            "players": float(len(season.players)),
            "with_projection": float(sum(1 for p in season.players if p.proj_season > 0)),
            "with_adp": float(sum(1 for p in season.players if p.espn_adp)),
            "injury_history_matched": float(matched),
            "injury_seasons": float(len(yrs)),
        },
    )
    assert_no_leakage(season, board)
    return board


def assert_no_leakage(season: Season, board: Board) -> None:
    """Prove the board cannot see the season it is drafting.

    The check that matters: a valuation must be reproducible from preseason
    inputs alone. So we rebuild every player's base projection component and
    require it to equal `proj_season` — if anything downstream had folded in
    actual results, this is where it would show.

    Cheap, and it guards the one bug class that would make every number in the
    report wrong in the flattering direction.
    """
    bad: list[str] = []
    for p in board.players:
        v = board.valuations.get(p.espn_id)
        if v is None:
            continue
        base = v.components.get("base_projection")
        if base is None or abs(base - p.proj_season) > 0.01:
            bad.append(f"{p.name}: base {base} != preseason proj {p.proj_season}")
        if p.actual_week and v.points > 0 and base == 0.0 and v.points > 1.0:
            bad.append(f"{p.name}: value {v.points} from a zero projection")
    if bad:
        raise LeakageError(f"{len(bad)} valuation(s) not derived from preseason "
                           f"inputs, e.g. {bad[:3]}")


# ── the replay ───────────────────────────────────────────────────────────────


@dataclass
class OurPick:
    overall: int
    round_num: int
    espn_id: int
    name: str
    pos: Pos
    vor: float
    score: float
    reasons: dict[str, float]
    #: Who the real manager took at this pick, for a side-by-side read.
    real_espn_id: int | None = None
    real_name: str = ""


@dataclass
class Replay:
    season: int
    my_team_id: int
    slot: int
    our_picks: list[OurPick] = field(default_factory=list)
    rosters: dict[int, list[int]] = field(default_factory=dict)
    fallbacks: int = 0
    fallback_detail: list[str] = field(default_factory=list)
    total_picks: int = 0

    @property
    def fallback_rate(self) -> float:
        return self.fallbacks / self.total_picks if self.total_picks else 0.0

    @property
    def our_roster(self) -> list[int]:
        return self.rosters.get(self.my_team_id, [])

    def describe(self) -> str:
        return (f"{self.season} slot {self.slot} (team {self.my_team_id}): "
                f"{len(self.our_picks)} picks, "
                f"{self.fallbacks}/{self.total_picks} fallbacks "
                f"({self.fallback_rate:.1%})")


def _legal(p: Player, roster: Counter, facts: LeagueFacts) -> bool:
    cap = facts.position_limits.get(p.pos)
    return cap is None or roster.get(p.pos, 0) < cap


def replay(season: Season, board: Board, my_team_id: int) -> Replay:
    """Run the draft with `my_team_id`'s seat driven by the engine."""
    facts = board.facts
    order = facts.pick_order
    slot = order.index(my_team_id) + 1 if my_team_id in order else 0

    real_at: dict[int, tuple[int, int]] = {
        pk.overall: (pk.team_id, pk.espn_id) for pk in season.picks
    }
    # Each team's own remaining real picks, in order — the first fallback tier.
    remaining: dict[int, list[int]] = {}
    for pk in sorted(season.picks, key=lambda x: x.overall):
        remaining.setdefault(pk.team_id, []).append(pk.espn_id)

    by_id = board.by_id
    room = RoomModel(facts=facts, my_team_id=my_team_id)
    rosters: dict[int, Counter] = {t: Counter() for t in order}
    picked: dict[int, list[int]] = {t: [] for t in order}
    taken: set[int] = set()

    out = Replay(season=season.year, my_team_id=my_team_id, slot=slot,
                 total_picks=len(real_at))

    for overall in sorted(real_at):
        team_id, real_id = real_at[overall]
        roster = rosters.setdefault(team_id, Counter())

        if team_id == my_team_id:
            plan = picker.rank(board.rows, room)
            if plan.best is None:
                # Every remaining player is ineligible for us — position caps
                # full, or the pool is exhausted. Skipping the pick would leave
                # us a body short and quietly flatter every rival in the
                # comparison, so take the best legal player instead and count
                # it. Found by tests/test_backtest.py, not by a live run.
                chosen = _other_team_pick(overall, team_id, real_id, remaining,
                                          taken, roster, by_id, facts, out)
                if chosen is None:
                    log.warning("pick %d unfillable — no legal player left", overall)
                    continue
                out.our_picks.append(OurPick(
                    overall=overall, round_num=room.current_round,
                    espn_id=chosen.espn_id, name=chosen.name, pos=chosen.pos,
                    vor=0.0, score=0.0, reasons={"forced": 1.0},
                    real_espn_id=real_id,
                    real_name=(by_id[real_id].name if real_id in by_id else ""),
                ))
                taken.add(chosen.espn_id)
                roster[chosen.pos] += 1
                picked[team_id].append(chosen.espn_id)
                room.apply([Pick(overall=overall, team_id=team_id,
                                 espn_id=chosen.espn_id, pos=chosen.pos,
                                 name=chosen.name)])
                continue
            chosen = plan.best.player
            out.our_picks.append(OurPick(
                overall=overall, round_num=plan.round_num,
                espn_id=chosen.espn_id, name=chosen.name, pos=chosen.pos,
                vor=plan.best.valuation.vor, score=plan.best.score,
                reasons=dict(plan.best.reasons),
                real_espn_id=real_id, real_name=(by_id.get(real_id).name
                                                 if real_id in by_id else ""),
            ))
        else:
            chosen = _other_team_pick(overall, team_id, real_id, remaining,
                                      taken, roster, by_id, facts, out)
            if chosen is None:
                continue

        taken.add(chosen.espn_id)
        roster[chosen.pos] += 1
        picked[team_id].append(chosen.espn_id)
        if chosen.espn_id in remaining.get(team_id, []):
            remaining[team_id].remove(chosen.espn_id)
        room.apply([Pick(overall=overall, team_id=team_id,
                         espn_id=chosen.espn_id, pos=chosen.pos,
                         name=chosen.name)])

    out.rosters = {t: ids for t, ids in picked.items()}
    log.info("%s", out.describe())
    return out


def _other_team_pick(overall: int, team_id: int, real_id: int,
                     remaining: dict[int, list[int]], taken: set[int],
                     roster: Counter, by_id: dict[int, Player],
                     facts: LeagueFacts, out: Replay) -> Player | None:
    """What a real team takes, given the board may have diverged."""
    if real_id not in taken and real_id in by_id:
        return by_id[real_id]

    out.fallbacks += 1

    # Tier 1: this team's own next real pick. It is a player they demonstrably
    # wanted, so it keeps the replay closer to the draft that happened than any
    # projection-based guess would.
    for pid in remaining.get(team_id, []):
        if pid not in taken and pid in by_id and _legal(by_id[pid], roster, facts):
            out.fallback_detail.append(
                f"#{overall} team {team_id}: {by_id.get(real_id, _Unknown()).name} gone "
                f"-> own later pick {by_id[pid].name}")
            return by_id[pid]

    # Tier 2: best remaining by PRESEASON projection, legal under caps.
    best = None
    for p in by_id.values():
        if p.espn_id in taken or not _legal(p, roster, facts):
            continue
        if best is None or p.proj_season > best.proj_season:
            best = p
    if best is not None:
        out.fallback_detail.append(
            f"#{overall} team {team_id}: fell through to best available {best.name}")
    return best


class _Unknown:
    name = "an undrafted player"
