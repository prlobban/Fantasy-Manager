"""§3.6 — model the room, not just the board.

Two adjustments come out of this:
  1. A player at a position the teams picking before us still need is LESS
     likely to survive than raw ADP says.
  2. A run is on when N of the last M picks share a position. Runs are
     contagious — either get in front of one, or deliberately let it pass and
     take the position everyone just skipped.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from core.espn.settings import LeagueFacts
from core.model.priors import priors
from core.model.schema import Player, Pos


@dataclass
class Pick:
    overall: int
    team_id: int
    espn_id: int
    pos: Pos | None = None
    name: str = ""


@dataclass
class RoomModel:
    """Live state of the draft room."""

    facts: LeagueFacts
    my_team_id: int
    picks: list[Pick] = field(default_factory=list)
    _rosters: dict[int, list[Pos]] = field(default_factory=lambda: defaultdict(list))

    # ── ingest ───────────────────────────────────────────────────────────────

    def apply(self, picks: list[Pick]) -> list[Pick]:
        """Record picks we haven't seen. Returns the newly applied ones.

        A pick with `team_id == 0` came from the DOM reader, which cannot see
        the team; the snake order determines it, so it is inferred here. That
        inference is what makes the practice draft (DOM-only) produce a usable
        room model — without it every DOM pick landed on a phantom team 0 and
        our own roster read as empty.
        """
        known = {p.overall for p in self.picks}
        new = [p for p in picks if p.overall > 0 and p.overall not in known]
        for p in sorted(new, key=lambda x: x.overall):
            if not p.team_id:
                p.team_id = self.team_on_clock(p.overall)
            self.picks.append(p)
            if p.pos:
                self._rosters[p.team_id].append(p.pos)
        self.picks.sort(key=lambda x: x.overall)
        return new

    # ── where we are ─────────────────────────────────────────────────────────

    @property
    def picks_made(self) -> int:
        return len(self.picks)

    @property
    def next_overall(self) -> int:
        """The pick number about to happen (1-indexed).

        Highest seen + 1, not count + 1: picks are strictly sequential, so if
        the DOM reader misses one row the count would put us a pick behind for
        the rest of the draft, while the max stays right.
        """
        return (self.picks[-1].overall + 1) if self.picks else 1

    @property
    def n_teams(self) -> int:
        return len(self.facts.pick_order)

    def team_on_clock(self, overall: int | None = None) -> int:
        o = overall or self.next_overall
        rnd = (o - 1) // self.n_teams + 1
        idx = (o - 1) % self.n_teams
        order = self.facts.pick_order
        return order[idx] if rnd % 2 == 1 else order[self.n_teams - 1 - idx]

    @property
    def on_the_clock_is_us(self) -> bool:
        return self.team_on_clock() == self.my_team_id

    @property
    def my_picks(self) -> list[int]:
        return self.facts.my_picks(self.my_team_id)

    @property
    def my_next_pick(self) -> int | None:
        return next((p for p in self.my_picks if p >= self.next_overall), None)

    @property
    def my_pick_after_next(self) -> int | None:
        upcoming = [p for p in self.my_picks if p >= self.next_overall]
        return upcoming[1] if len(upcoming) > 1 else None

    @property
    def picks_until_my_turn(self) -> int:
        nxt = self.my_next_pick
        return max(0, nxt - self.next_overall) if nxt else 999

    @property
    def picks_until_my_turn_after_that(self) -> int:
        """The gap that §3.5 actually cares about: from OUR pick to our next one."""
        a, b = self.my_next_pick, self.my_pick_after_next
        return (b - a) if (a and b) else 999

    @property
    def current_round(self) -> int:
        return (self.next_overall - 1) // self.n_teams + 1

    @property
    def rounds_left(self) -> int:
        return max(0, self.facts.draftable_spots - self.current_round + 1)

    @property
    def is_complete(self) -> bool:
        return self.next_overall > self.facts.draftable_spots * self.n_teams

    # ── rosters ──────────────────────────────────────────────────────────────

    def roster_positions(self, team_id: int) -> Counter:
        return Counter(self._rosters.get(team_id, []))

    @property
    def my_positions(self) -> Counter:
        return self.roster_positions(self.my_team_id)

    def taken_ids(self) -> set[int]:
        return {p.espn_id for p in self.picks}

    # ── §3.6 needs ───────────────────────────────────────────────────────────

    def _unfilled_starting_need(self, team_id: int) -> Counter:
        """Starting slots this team has not yet filled, by position.

        Flex slots count toward every position they accept — a team one RB short
        of a full lineup is genuinely in the market for an RB even if the gap is
        nominally a flex.
        """
        have = self.roster_positions(team_id)
        need: Counter = Counter()
        for slot in self.facts.settings.starting_slots:
            for pos in slot.eligible:
                dedicated = self.facts.settings.starters_at(pos)
                shortfall = max(0, dedicated - have.get(pos, 0))
                if slot.eligible == (pos,):
                    need[pos] = max(need[pos], shortfall)
                else:
                    # A flex need is real but weaker than a dedicated hole.
                    need[pos] = max(need[pos], min(1, shortfall))
        return need

    def demand_before_my_turn(self) -> Counter:
        """How many teams picking between now and our turn still need each position.

        This is the §3.6 adjustment: three teams ahead of us needing an RB makes
        the RB board thinner than ADP alone predicts.
        """
        start = self.next_overall
        end = self.my_next_pick or start
        demand: Counter = Counter()
        for o in range(start, end):
            tid = self.team_on_clock(o)
            if tid == self.my_team_id:
                continue
            for pos, n in self._unfilled_starting_need(tid).items():
                if n > 0:
                    demand[pos] += 1
        return demand

    def run_on(self) -> Pos | None:
        """§3.6 — is a positional run happening right now?"""
        p = priors()
        count = int(p.get("draft.run_detect_count"))
        window = int(p.get("draft.run_detect_window"))
        recent = [pk.pos for pk in self.picks[-window:] if pk.pos]
        if len(recent) < window:
            return None
        pos, n = Counter(recent).most_common(1)[0]
        return pos if n >= count else None

    # ── §3.7 legality ────────────────────────────────────────────────────────

    def position_cap_reached(self, pos: Pos, team_id: int | None = None) -> bool:
        cap = self.facts.position_limits.get(pos)
        if cap is None:
            return False
        return self.roster_positions(team_id or self.my_team_id).get(pos, 0) >= cap

    def my_bye_weeks(self, by_id: dict[int, Player]) -> dict[Pos, set[int]]:
        """Bye weeks already committed at each position, for §3.7's collision rule."""
        out: dict[Pos, set[int]] = defaultdict(set)
        for pk in self.picks:
            if pk.team_id != self.my_team_id:
                continue
            pl = by_id.get(pk.espn_id)
            if pl and pl.bye_week:
                out[pl.pos].add(pl.bye_week)
        return out
