"""§6.1–§6.7 — outgoing trade proposals, shape-driven (D4, D5).

The outgoing case is the opposite problem to §6.8. We choose the terms, so the
risk is not being fleeced — it is reputational. In a ten-team league of friends
with money on it, a manager who thinks you are hunting them stops trading with
you for the season, and that costs more than any single deal wins.

So the bar is not "can we win this trade" but "does this plausibly help both
sides, and would it survive being screenshotted in the group chat" (§6.3).

2026-09-05: rewritten around ROSTER SHAPE. The first version only looked where
the other team had a positional HOLE, which meant three tight ends on a bench
never generated a single idea — nobody in a 10-team league has zero TEs. A
surplus body at a one-slot position is trade capital whenever the other
manager would START him (D5.2, D4.1), hole or not.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field

from core.gates import rate_limits
from core.manager import roster as roster_mod
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
    #: What the deal does to our roster shape, in words.
    shape_effect: str = ""
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


def _delta(roster, give, get, vals, settings) -> float:
    give_ids = {p.espn_id for p in give}
    after = [p for p in roster if p.espn_id not in give_ids] + list(get)
    return _starting_points(after, vals, settings) - _starting_points(roster, vals, settings)


def _shape_effect(before: roster_mod.RosterShape, after: roster_mod.RosterShape) -> tuple[str, int]:
    """Words and a score: +1 for every surplus/shortage the deal removes,
    −1 for every one it creates."""
    words, score = [], 0
    for pos in Pos:
        b = before.by_pos.get(pos)
        a = after.by_pos.get(pos)
        bd = b.delta if b else 0
        ad = a.delta if a else 0
        if bd == ad:
            continue
        if abs(ad) < abs(bd):
            score += 1
            words.append(f"{pos.value} {'surplus' if bd > 0 else 'shortage'} {abs(bd)}→{abs(ad)}")
        else:
            score -= 1
            words.append(f"{pos.value} becomes {'surplus' if ad > 0 else 'short'} {abs(ad)}")
    return "; ".join(words) or "shape unchanged", score


def build(
    our_roster: list[Player],
    their_rosters: dict[int, tuple[str, list[Player]]],
    valuations: dict[int, Valuation],
    settings: LeagueSettings,
    *,
    max_proposals: int = 5,
) -> list[Proposal]:
    """Generate candidate proposals, best first. 1-for-1 and 2-for-1 only:
    bigger packages are harder to evaluate, harder to accept, and §6.5 says
    consolidation beats dilution anyway."""
    p = priors()
    protected_n = int(p.get("trades.protected_top_n"))

    our_ranked = sorted(
        (pl for pl in our_roster if pl.espn_id in valuations),
        key=lambda pl: -valuations[pl.espn_id].vor,
    )
    protected = {pl.espn_id for pl in our_ranked[:protected_n]}
    our_shape = roster_mod.analyse(our_roster, valuations, settings)
    our_starters = {a for a in _starter_ids(our_roster, valuations, settings)}

    # What we would give: surplus bodies first (D5.2), then any non-protected
    # non-starter. Starters are givable only inside a 2-for-1 upgrade.
    surplus = [pl for s in our_shape.by_pos.values() for pl in s.surplus_players]
    bench = [pl for pl in our_roster
             if pl.espn_id in valuations and pl.espn_id not in protected
             and pl.espn_id not in our_starters and pl not in surplus]
    givables = surplus + bench
    if not givables:
        return []

    proposals: list[Proposal] = []
    for tid, (name, their_roster) in their_rosters.items():
        their_vals_ok = [pl for pl in their_roster if pl.espn_id in valuations]
        if not their_vals_ok:
            continue
        # What we would take: their players at positions where we are short or
        # where a starter upgrade exists — ranked by ROS VOR, top handful.
        want_pos = set(our_shape.short) or {Pos.RB, Pos.WR}
        gettables = sorted(
            (pl for pl in their_vals_ok if pl.pos in want_pos or pl.pos in (Pos.RB, Pos.WR)),
            key=lambda pl: -valuations[pl.espn_id].vor,
        )[:6]
        if not gettables:
            continue

        combos: list[tuple[list[Player], list[Player]]] = []
        for g in givables[:5]:
            for r in gettables:
                combos.append(([g], [r]))
        for pair in itertools.combinations(givables[:5], 2):
            for r in gettables[:4]:
                combos.append((list(pair), [r]))

        for give, get in combos:
            ours = _delta(our_roster, give, get, valuations, settings)
            theirs = _delta(their_roster, get, give, valuations, settings)
            if ours <= 0 or theirs <= 0:
                continue  # §6.2 / §6.3 — both sides, or nothing

            # §6.5 — never dilute a premium asset.
            if len(get) > len(give):
                best_out = max(valuations[x.espn_id].vor for x in give)
                best_in = max(valuations[x.espn_id].vor for x in get)
                if best_in < best_out * 0.9:
                    continue

            give_ids = {x.espn_id for x in give}
            after_roster = [x for x in our_roster if x.espn_id not in give_ids] + list(get)
            effect, score = _shape_effect(
                our_shape, roster_mod.analyse(after_roster, valuations, settings))
            if score < 0:
                continue  # a deal that creates a new hole is not a fix

            allowed, why = rate_limits.can_propose(
                tid, [x.espn_id for x in give], [x.espn_id for x in get])
            warnings = [] if allowed else [f"rate limit: {why}"]
            fairness = ("balanced" if abs(ours - theirs) < 5
                        else "favours us" if ours > theirs else "favours them")
            proposals.append(Proposal(
                to_team=tid, to_team_name=name, give=give, get=get,
                our_gain=round(ours, 1), their_gain=round(theirs, 1),
                rationale=(
                    f"{give[0].name} would start for them (+{theirs:.1f} ROS starting pts); "
                    f"{get[0].name} fills our {get[0].pos.value} (+{ours:.1f})"
                ),
                fairness=fairness, shape_effect=effect, warnings=warnings,
            ))

    # Best for us among deals that also clearly help them and fix our shape.
    proposals.sort(key=lambda pr: (-(pr.our_gain + 3.0 * pr.shape_effect.count("→")),
                                   -pr.their_gain))
    # One idea per counterparty at the top, so three slots are not spent on
    # one manager (§6.1 max_open_offers_per_manager).
    seen: set[int] = set()
    out: list[Proposal] = []
    for pr in proposals:
        if pr.to_team in seen:
            continue
        seen.add(pr.to_team)
        out.append(pr)
        if len(out) >= max_proposals:
            break
    return out


def _starter_ids(roster, valuations, settings) -> set[int]:
    from core.manager.lineup import optimal_lineup

    plan = optimal_lineup(roster, valuations, settings)
    return {a.player.espn_id for a in plan.assignments if a.player is not None}


def value_check(our_roster, their_roster, give, get, valuations,
                settings) -> tuple[bool, str, float, float]:
    """§6.2 + §6.3 in code, for the propose_trade write gate: both sides
    must gain in ROS starting points, or the proposal is refused."""
    ours = _delta(our_roster, give, get, valuations, settings)
    theirs = _delta(their_roster, get, give, valuations, settings)
    if ours <= 0:
        return False, f"§6.2 our starting lineup does not improve ({ours:+.1f})", ours, theirs
    if theirs <= 0:
        return (False, f"§6.3 does not help the other side ({theirs:+.1f}) — fails the "
                "group-chat test", ours, theirs)
    return True, f"§6.2/§6.3 us {ours:+.1f}, them {theirs:+.1f}", ours, theirs
