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
    hole_weight = float(p.get("draft.hole_weight"))
    stack_penalty = float(p.get("draft.stack_penalty"))
    bench_cost = float(p.get("draft.bench_opportunity_cost"))
    bench_spots = room.facts.settings.bench_count

    # §3.7 — the endgame constraint. Once the rounds remaining equal the number
    # of mandatory starting slots still empty, EVERY remaining pick must fill
    # one. Without this the picker happily takes a 4th RB in round 12 on raw
    # VOR and finishes the draft unable to field a D/ST — caught by the
    # simulator, which is exactly what it is for. A soft bonus cannot fix this:
    # a spare RB's VOR dwarfs any starting kicker's, so it has to be a hard
    # legality rule, not a preference.
    mandatory = _unfilled_mandatory(room, my_pos)
    must_fill_now = rounds_left <= sum(mandatory.values())

    # Flex capacity still open, for the surplus discount below.
    flex_open = _flex_open(room, my_pos)

    # How much of our remaining draft capital the outstanding starting holes
    # represent. 0 = plenty of room, 1 = every remaining pick is spoken for.
    # K and D/ST are excluded from both sides: they are deliberately deferred
    # to the last rounds (§3.7), so counting them would make every middle
    # round look like an emergency.
    skill_holes = sum(
        n for pos, n in mandatory.items() if pos not in (Pos.K, Pos.DST)
    ) + flex_open
    usable_picks = max(1, rounds_left - len(
        [pos for pos in (Pos.K, Pos.DST) if pos in mandatory]
    ))
    hole_pressure = min(1.0, skill_holes / usable_picks)

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

        # §3.7 — the endgame constraint, applied before anything else can
        # outweigh it. Anyone who does not fill a mandatory hole is illegal.
        if must_fill_now and player.pos not in mandatory:
            score -= 1e6
            reasons["endgame_must_fill"] = -1e6
            notes.append(f"must fill {'/'.join(x.value for x in mandatory)}")

        # §3.7 — never take K or D/ST before the final rounds, UNLESS the
        # endgame rule above says this is the moment.
        elif player.pos in (Pos.K, Pos.DST) and rounds_left > no_k_dst_until:
            score -= 1e6
            reasons["too_early_k_dst"] = -1e6
            notes.append("K/DST too early")

        # §3.7 — SURPLUS DISCOUNT. VOR measures a player against the starter
        # you'd otherwise field at his position. Once that slot is already
        # filled, the next man at the same position is not worth his VOR — he
        # is worth bye cover and injury insurance, which is far less.
        #
        # Without this the engine stacks a position it cannot start. Caught by
        # the simulator taking TE1, TE2 and TE3 in rounds 2-4 of a one-TE
        # league: all three graded highly on VOR because TE replacement level
        # is low, and two of them could never leave the bench.
        surplus_mult = _surplus_multiplier(room, player.pos, my_pos, flex_open)
        if surplus_mult < 1.0:
            # A bench body's value is a FRACTION OF HIS UPSIDE, never a negative
            # number. An earlier version wrote `-(1 - mult) * max(vor, 0)`,
            # which meant a saturated-position player with negative VOR took no
            # penalty at all and passed straight through at face value. That is
            # how the engine ended up preferring a third-string tight end to a
            # wide receiver in a lineup that had no wide receivers.
            # ...and a NEGATIVE VOR is never lifted toward zero: a backup at a
            # covered position is worth his (negative) VOR at best. The first
            # rehearsal (2026-09-04) took a second QB at -9 VOR in round 7 with
            # a starting WR slot still empty, because the floor at zero had
            # turned -9 into 0.
            effective = surplus_mult * val.vor if val.vor > 0 else val.vor
            delta = effective - val.vor
            score += delta
            reasons["surplus"] = delta
            if surplus_mult <= 0.25:
                notes.append(f"{player.pos.value} already covered")

            # ...but zero is still too generous while a starting slot is empty.
            # In the dead rounds every remaining player grades below replacement,
            # so a bench body floored at 0 outranks a genuine starter sitting at
            # -30 — which is how a backup QB beat a receiver on a roster with
            # one receiver. Spending a pick on depth while a slot is open has a
            # real cost, and this is it.
            if hole_pressure > 0:
                oppo = -bench_cost * hole_pressure
                score += oppo
                reasons["depth_while_short"] = oppo

        # §3.7 — bench spots are a hard budget (4 here). Every body beyond a
        # position's starters (+ the flex, for RB/WR only) costs a fixed
        # amount, because in the dead rounds every VOR is negative and a
        # multiplier has nothing to bite on. The first rehearsal drafted TE2,
        # TE3 and QB2 that way while starting two receivers. Applied outside
        # the surplus block on purpose: an open flex does not excuse a TE2.
        stack = _stack_depth(room, player.pos, my_pos)
        if stack > 0:
            pen = -stack_penalty * stack
            score += pen
            reasons["stacking"] = pen
            notes.append(f"{player.pos.value}{my_pos.get(player.pos, 0) + 1} is depth")

        # §3.7 — starting-lineup holes come before depth.
        #
        # Scaled by VOR, and that scaling is the whole point. An earlier
        # version scaled by RAW PROJECTED POINTS, on the reasoning that an
        # empty slot scores zero rather than replacement level. That reasoning
        # is wrong mid-draft: the endgame rule below guarantees every mandatory
        # slot gets filled before the draft ends, so the real counterfactual is
        # never zero — it is "the player I take at this position later", which
        # is exactly what VOR measures.
        #
        # Scaling by raw points quietly reintroduced the cross-position bias
        # VOR exists to remove. Quarterbacks accumulate the most raw points and
        # are worth the least positionally, so they collected the largest bonus
        # on the board: caught 2026-09-04 with Drake Maye (29 VOR) ranked above
        # Kenneth Walker (45 VOR) at pick 17, purely on this term.
        #
        # The gate before that (`rounds_left <= slots_left + 2`) never fired in
        # the middle rounds, which left the engine indifferent between a wide
        # receiver and a third tight end while it had no wide receivers at all.
        if _starting_hole(room, player.pos, my_pos) > 0 and hole_pressure > 0:
            bump = hole_weight * max(val.vor, 0.0) * hole_pressure
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


def _unfilled_mandatory(room: RoomModel, have) -> dict[Pos, int]:
    """Dedicated starting slots still empty, by position.

    Flex is excluded on purpose: any spare RB/WR/TE fills it, so it is never a
    position-specific obligation. These are the slots that will leave the team
    unable to field a legal lineup if the draft ends without them.
    """
    out: dict[Pos, int] = {}
    for slot in room.facts.settings.starting_slots:
        if slot.is_flex:
            continue
        pos = slot.eligible[0]
        need = max(0, slot.count - have.get(pos, 0))
        if need:
            out[pos] = need
    return out


#: What a bench body is worth, as a fraction of his VOR — roughly the share of
#: weeks he can actually reach our starting lineup.
#:
#: Split by how many starting slots his position has, because that drives how
#: often a backup plays. A bench RB in a 2-RB-plus-flex league covers byes and
#: the near-certainty of an RB injury: call it 4 weeks of 14. A bench TE in a
#: one-TE league plays on the bye and almost never again.
#:
#: [v1 priors] — §7 moves these.
_SURPLUS_LADDER_DEEP = (0.30, 0.12, 0.05)    # position has 2+ starting slots
_SURPLUS_LADDER_SHALLOW = (0.15, 0.06, 0.02)  # position has 1 starting slot

#: Positions a flex slot can absorb.
_FLEX_POSITIONS = (Pos.RB, Pos.WR, Pos.TE)


def _flex_open(room: RoomModel, have) -> int:
    """Flex slots not yet covered by a spare RB/WR/TE already on the roster."""
    settings = room.facts.settings
    flex_slots = sum(s.count for s in settings.starting_slots if s.is_flex)
    spare = sum(
        max(0, have.get(pos, 0) - settings.starters_at(pos)) for pos in _FLEX_POSITIONS
    )
    return max(0, flex_slots - spare)


def _stack_depth(room: RoomModel, pos: Pos, have) -> int:
    """How many bodies too many `pos` would have if we took this player (>= 0)."""
    settings = room.facts.settings
    starters = settings.starters_at(pos)
    allowance = starters
    # The flex is one shared slot. Only the two-starter positions (RB, WR)
    # get to count it as depth; a second TE in a one-TE league is a stack,
    # not a flex plan (§3.7).
    if pos in _FLEX_POSITIONS and starters >= 2:
        allowance += sum(s.count for s in settings.starting_slots if s.is_flex)
    # `have` is the roster BEFORE this pick; the candidate is the +1.
    return max(0, have.get(pos, 0) + 1 - allowance)


def _surplus_multiplier(room: RoomModel, pos: Pos, have, flex_open: int) -> float:
    """How much of this player's VOR actually reaches our starting lineup.

    1.0 while he fills a dedicated starting slot, still 1.0 if he slots into an
    open flex, then falls away down the ladder for pure bench depth.
    """
    settings = room.facts.settings
    count = have.get(pos, 0)

    if count < settings.starters_at(pos):
        return 1.0
    # An open flex is a real starting slot for a spare RB/WR. A second TE in
    # a one-TE league is not a flex plan (§3.7), so TE gets no exemption.
    if pos in _FLEX_POSITIONS and flex_open > 0 and settings.starters_at(pos) >= 2:
        return 1.0

    depth = count - settings.starters_at(pos)
    if pos in _FLEX_POSITIONS:
        depth -= sum(s.count for s in settings.starting_slots if s.is_flex)
    depth = max(0, depth)

    ladder = (
        _SURPLUS_LADDER_DEEP
        if settings.starters_at(pos) >= 2
        else _SURPLUS_LADDER_SHALLOW
    )
    return ladder[min(depth, len(ladder) - 1)]


def _slots_left_to_fill(room: RoomModel, have) -> int:
    """Total unfilled starting slots, counted per position rather than by
    headcount — a roster of six WRs has not filled the TE slot."""
    settings = room.facts.settings
    dedicated_gap = sum(_unfilled_mandatory(room, have).values())
    flex_slots = sum(s.count for s in settings.starting_slots if s.is_flex)
    flex_eligible_spare = sum(
        max(0, have.get(pos, 0) - settings.starters_at(pos))
        for pos in (Pos.RB, Pos.WR, Pos.TE)
    )
    return dedicated_gap + max(0, flex_slots - flex_eligible_spare)
