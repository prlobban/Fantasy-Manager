"""§6.1–§6.7 — outgoing trade proposals.

The outgoing case is the opposite problem to §6.8. We choose the terms, so the
risk is not being fleeced — it is reputational. In a ten-team league of friends
with money on it, a manager who thinks you are hunting them stops trading with
you for the season, and that costs more than any single deal wins.

So the bar is not "can we win this trade" but "does this plausibly help both
sides, and would it survive being screenshotted in the group chat" (§6.3).
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field

from core.gates import rate_limits
from core.manager.gauntlet import _starting_points
from core.model.priors import priors
from core.model.schema import LeagueSettings, Player, Pos, Valuation

log = logging.getLogger(__name__)


@dataclass
class Proposal:
    to_team: int
    to_team_name: str
    give: list[Player]
    get: list[Player]
    our_gain: float
    their_gain: float
    rationale: str
    fairness: str
    warnings: list[str] = field(default_factory=list)

    @property
    def mutual(self) -> bool:
        return self.our_gain > 0 and self.their_gain > 0

    def describe(self) -> str:
        g = ", ".join(p.name for p in self.give)
        r = ", ".join(p.name for p in self.get)
        return (
            f"to {self.to_team_name}: give {g} / get {r} "
            f"(us {self.our_gain:+.1f}, them {self.their_gain:+.1f} ROS starting pts)"
        )


def _surplus_and_holes(
    roster: list[Player], settings: LeagueSettings
) -> tuple[dict[Pos, int], dict[Pos, int]]:
    from collections import Counter

    have = Counter(p.pos for p in roster)
    surplus: dict[Pos, int] = {}
    holes: dict[Pos, int] = {}
    for pos in (Pos.QB, Pos.RB, Pos.WR, Pos.TE):
        starters = settings.starters_at(pos)
        n = have.get(pos, 0)
        if n > starters + 1:
            surplus[pos] = n - starters - 1
        elif n < starters:
            holes[pos] = starters - n
    return surplus, holes


def _delta(roster, give, get, vals, settings) -> float:
    give_ids = {p.espn_id for p in give}
    after = [p for p in roster if p.espn_id not in give_ids] + list(get)
    return _starting_points(after, vals, settings) - _starting_points(roster, vals, settings)


def build(
    our_roster: list[Player],
    their_rosters: dict[int, tuple[str, list[Player]]],
    valuations: dict[int, Valuation],
    settings: LeagueSettings,
    *,
    max_proposals: int = 3,
    max_per_side: int = 2,
) -> list[Proposal]:
    """Generate candidate proposals, best first.

    Only 1-for-1 and 2-for-1 shapes: bigger packages are harder to evaluate,
    harder for the other manager to accept, and §6.5 says consolidation beats
    dilution anyway.
    """
    p = priors()
    protected_n = int(p.get("trades.protected_top_n"))

    our_ranked = sorted(
        (pl for pl in our_roster if pl.espn_id in valuations),
        key=lambda pl: -valuations[pl.espn_id].vor,
    )
    protected = {pl.espn_id for pl in our_ranked[:protected_n]}

    our_surplus, our_holes = _surplus_and_holes(our_roster, settings)
    proposals: list[Proposal] = []

    for tid, (name, their_roster) in their_rosters.items():
        their_surplus, their_holes = _surplus_and_holes(their_roster, settings)

        # §6.3 / §6.4 — only look where the needs are complementary. An offer
        # that ignores what they actually need is noise and burns a slot.
        give_positions = [pos for pos in our_surplus if pos in their_holes]
        get_positions = [pos for pos in their_surplus if pos in our_holes]
        if not give_positions or not get_positions:
            continue

        givables = [
            pl for pl in our_roster
            if pl.pos in give_positions and pl.espn_id not in protected
            and pl.espn_id in valuations
        ]
        gettables = [
            pl for pl in their_roster
            if pl.pos in get_positions and pl.espn_id in valuations
        ]
        if not givables or not gettables:
            continue

        combos = []
        for g in givables[:4]:
            for r in gettables[:4]:
                combos.append(([g], [r]))
        for pair in itertools.combinations(givables[:4], 2):
            for r in gettables[:3]:
                combos.append((list(pair), [r]))

        for give, get in combos[: max_per_side * 8]:
            ours = _delta(our_roster, give, get, valuations, settings)
            theirs = _delta(their_roster, get, give, valuations, settings)

            if ours <= 0:
                continue
            # §6.3 — must plausibly help both sides.
            if theirs <= 0:
                continue

            allowed, why = rate_limits.can_propose(
                tid, [x.espn_id for x in give], [x.espn_id for x in get]
            )
            warnings = [] if allowed else [f"rate limit: {why}"]

            # §6.5 — never dilute a premium asset.
            if len(get) > len(give):
                best_out = max(valuations[x.espn_id].vor for x in give)
                best_in = max(valuations[x.espn_id].vor for x in get)
                if best_in < best_out * 0.9:
                    continue

            fairness = (
                "balanced" if abs(ours - theirs) < 5
                else ("favours us" if ours > theirs else "favours them")
            )
            proposals.append(Proposal(
                to_team=tid, to_team_name=name, give=give, get=get,
                our_gain=round(ours, 1), their_gain=round(theirs, 1),
                rationale=(
                    f"we are deep at {give[0].pos.value} and short at "
                    f"{get[0].pos.value}; they are the mirror image"
                ),
                fairness=fairness,
                warnings=warnings,
            ))

    # Prefer trades that help us, but among those prefer the ones that also
    # clearly help them — those are the ones that actually get accepted.
    proposals.sort(key=lambda pr: (-pr.our_gain, -pr.their_gain))
    return proposals[:max_proposals]
