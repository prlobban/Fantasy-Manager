"""§3.4–§3.7 — the pick decision.

Pure and fast. This is the code that runs on the clock (§3.2, §8.7), so it takes
a prebuilt board and a room model and returns a ranking. No I/O, no model call,
no network. Target: well under 100 ms for a 450-player board.

The decision, in order:
  §3.7  drop anything illegal (position cap, vetoed, already taken)
  §3.5  compute Cost(pos) = best now - E[best at our next pick]
  §3.6  adjust for what the room ahead of us needs, and for runs
  §3.4  take the best player in the tier that is about to break
  §3.7  apply the soft roster-construction rules as score adjustments
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.draft.room import RoomModel
from core.draft.survival import PositionOutlook, position_outlook
from core.model.priors import priors
from core.model.schema import Player, Pos, Valuation


@dataclass
class Candidate:
    player: Player
    valuation: Valuation
    score: float
    #: Every adjustment applied, named — this is what §3.8 logs and §7 grades.
    reasons: dict[str, float] = field(default_factory=dict)
    note: str = ""


@dataclass
class PickPlan:
    candidates: list[Candidate]
    outlooks: dict[Pos, PositionOutlook]
    run_on: Pos | None
    picks_until_next: int
    round_num: int

    @property
    def best(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None

    def top(self, n: int) -> list[Candidate]:
        return self.candidates[:n]

    def explain(self, n: int = 5) -> str:
        lines = [
            f"round {self.round_num} · {self.picks_until_next} picks until our next turn"
            + (f" · RUN ON at {self.run_on.value}" if self.run_on else "")
        ]
        for pos, o in sorted(self.outlooks.items(), key=lambda kv: -kv[1].cost):
            lines.append(
                f"  {pos.value:5} cost={o.cost:6.1f}  now={o.best_now:6.1f} "
                f"next~={o.expected_next:6.1f}  tier{o.top_tier} has {o.top_tier_remaining} left"
            )
        for i, c in enumerate(self.top(n), 1):
            bits = " ".join(f"{k}{v:+.1f}" for k, v in c.reasons.items() if k != "base")
            lines.append(
                f"  {i}. {c.player.name:22} {c.player.pos.value:4} "
                f"vor={c.valuation.vor:6.1f} score={c.score:6.1f}  {bits}"
            )
        return "\n".join(lines)


def _eligible(
    board: list[tuple[Player, Valuation]],
    room: RoomModel,
) -> list[tuple[Player, Valuation]]:
    """§3.7 — hard legality. Anything dropped here can never enter the queue."""
    taken = room.taken_ids()
    out = []
    for player, val in board:
        if player.espn_id in taken:
            continue
        if val.vetoed:
            continue
        if room.position_cap_reached(player.pos):
            continue
        out.append((player, val))
    return out


def rank(
    board: list[tuple[Player, Valuation]],
    room: RoomModel,
    *,
    top_n: int = 60,
) -> PickPlan:
    """Rank the available players for OUR next pick."""
    p = priors()
    avail = _eligible(board, room)
    round_num = room.current_round
    rounds_left = room.rounds_left
    my_pos = room.my_positions
    by_id = {pl.espn_id: pl for pl, _ in board}
    my_byes = room.my_bye_weeks(by_id)

    # Gap that matters is from OUR pick to our NEXT pick (§3.5).
    picks_until = room.picks_until_my_turn_after_that
    if picks_until >= 999:
        picks_until = 0  # last pick of the draft: nothing to wait for

    # ── §3.5 per-position outlook ────────────────────────────────────────────
    by_pos: dict[Pos, list[tuple[Player, Valuation]]] = {}
    for pl, v in avail:
        by_pos.setdefault(pl.pos, []).append((pl, v))

    outlooks = {
        pos: position_outlook(pos, cands, picks_until, current_pick=room.next_overall)
        for pos, cands in by_pos.items()
    }

    # ── §3.6 room adjustments ────────────────────────────────────────────────
    demand = room.demand_before_my_turn()
    run = room.run_on()

    # Normalise Cost into a bounded bonus so it steers without swamping VOR.
    max_cost = max((o.cost for o in outlooks.values()), default=0.0) or 1.0

    no_k_dst_until = int(p.get("draft.no_kicker_dst_until_last_n_rounds"))
    bench_spots = room.facts.settings.bench_count

    out: list[Candidate] = []
    for player, val in avail:
        o = outlooks[player.pos]
        reasons: dict[str, float] = {"base": val.vor}
        score = val.vor
        notes: list[str] = []

        # §3.5 — waiting at this position is expensive.
        scarcity = 0.35 * val.vor * (o.cost / max_cost) if max_cost > 0 else 0.0
        if scarcity:
            score += scarcity
            reasons["scarcity"] = scarcity

        # §3.4 — the tier is about to break. Only the top remaining tier counts;
        # a thin tier five deep in the position is not urgent.
        if val.tier == o.top_tier and o.top_tier_remaining <= 2:
            bump = 0.12 * val.vor * (3 - o.top_tier_remaining)
            score += bump
            reasons["tier_break"] = bump
            notes.append(f"last {o.top_tier_remaining} in tier {val.tier}")

        # §3.6 — teams ahead of us need this position.
        if d := demand.get(player.pos, 0):
            bump = min(0.10, 0.02 * d) * val.vor
            score += bump
            reasons["room_demand"] = bump

        # §3.6 — a run is on. Get in front of it, or take what's being skipped.
        if run is not None:
            if player.pos is run:
                bump = 0.06 * val.vor
                score += bump
                reasons["run_join"] = bump
            elif o.cost < 0.25 * max_cost:
                # Everyone's ignoring this position; the value is still here later.
                bump = -0.04 * val.vor
                score += bump
                reasons["run_wait"] = bump

        # §3.7 — never take K or D/ST before the final rounds.
        if player.pos in (Pos.K, Pos.DST) and rounds_left > no_k_dst_until:
            score -= 1e6
            reasons["too_early_k_dst"] = -1e6
            notes.append("K/DST too early")

        # §3.7 — a second QB in a 1QB league is close to worthless.
        if player.pos is Pos.QB and my_pos.get(Pos.QB, 0) >= 1:
            if not room.facts.settings.is_superflex:
                score -= 0.55 * max(val.vor, 1.0) + 15.0
                reasons["backup_qb"] = -(0.55 * max(val.vor, 1.0) + 15.0)

        # §3.7 — starting-lineup holes come before depth. A team with no TE in
        # round 10 has a real problem; a fourth WR does not fix it.
        need = _starting_hole(room, player.pos, my_pos)
        if need > 0 and rounds_left <= _slots_left_to_fill(room, my_pos) + 2:
            bump = 0.18 * val.vor * need
            score += bump
            reasons["fills_hole"] = bump

        # §3.7 — bye collision among starters.
        if player.bye_week and player.bye_week in my_byes.get(player.pos, set()):
            starters = room.facts.settings.starters_at(player.pos)
            if my_pos.get(player.pos, 0) < starters:
                score -= 0.08 * val.vor
                reasons["bye_collision"] = -0.08 * val.vor
                notes.append(f"bye {player.bye_week} collision")

        # §3.7 — late rounds are for upside, but bench depth is a hard budget.
        # With a shallow bench a pure lottery ticket competes with bye cover.
        if rounds_left <= 3 and bench_spots <= 4:
            if val.availability < 0.8:
                score -= 0.05 * val.vor
                reasons["shallow_bench_risk"] = -0.05 * val.vor

        out.append(
            Candidate(
                player=player,
                valuation=val,
                score=round(score, 3),
                reasons={k: round(v, 2) for k, v in reasons.items()},
                note="; ".join(notes),
            )
        )

    out.sort(key=lambda c: c.score, reverse=True)
    return PickPlan(
        candidates=out[:top_n],
        outlooks=outlooks,
        run_on=run,
        picks_until_next=picks_until,
        round_num=round_num,
    )


def _starting_hole(room: RoomModel, pos: Pos, have) -> int:
    """How many dedicated starting slots at this position are still empty."""
    return max(0, room.facts.settings.starters_at(pos) - have.get(pos, 0))


def _slots_left_to_fill(room: RoomModel, have) -> int:
    """Total unfilled starting slots across the roster."""
    total = sum(s.count for s in room.facts.settings.starting_slots)
    filled = sum(have.values())
    return max(0, total - filled)
