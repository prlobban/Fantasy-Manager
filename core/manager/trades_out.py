"""§6.1–§6.7 — outgoing trade proposals, shape-driven and market-aware (D4, D5, D9).

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

2026-09-05, later: **two numbers per side, and only one of them is ours.**
`their_gain` — what our model says the deal does to THEIR lineup — is
mechanically fair and market-blind: it graded Kyle Pitts for Bucky Irving as
+3.5 for the other manager, and no human alive accepts that. Whether an offer
gets accepted is a question about a person, and the best proxy for a person's
valuation is the market: what the room paid for each player (ADP), decaying
into the rest-of-season rank as the season goes on (`core.model.market`). So
every idea now carries `market_ratio` — what they receive over what they give,
in market value — and the only hard reputational rule is a floor on that
ratio (`trades.min_market_ratio`). `their_gain` is advisory. The agent writes
`why_they_accept`; Tuesday grades it against what actually happened.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field

from core.gates import rate_limits
from core.manager import roster as roster_mod
from core.manager.gauntlet import _starting_points
from core.model import market
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
    #: ADVISORY — our model's read of their lineup delta. See module docstring.
    their_gain: float
    rationale: str
    fairness: str
    #: What the deal does to our roster shape, in words.
    shape_effect: str = ""
    #: Market value we send / market value we receive (D9).
    market_out: float = 0.0
    market_in: float = 0.0
    warnings: list[str] = field(default_factory=list)
    #: core's objections, each naming a section. Not refusals.
    flags: list[str] = field(default_factory=list)

    @property
    def market_ratio(self) -> float:
        """What they receive over what they give, in market value. 1.0 is a
        market-even swap; below `trades.min_market_ratio` it reads as a
        lowball and is refused at the gate."""
        return round(self.market_out / self.market_in, 2) if self.market_in > 0 else 0.0

    @property
    def mutual(self) -> bool:
        return self.our_gain > 0 and self.their_gain > 0

    def describe(self) -> str:
        g = ", ".join(p.name for p in self.give)
        r = ", ".join(p.name for p in self.get)
        return (
            f"to {self.to_team_name}: give {g} / get {r} "
            f"(us {self.our_gain:+.1f}, them {self.their_gain:+.1f} ROS starting pts, "
            f"market ratio {self.market_ratio})"
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


def market_values(pool: list[Player], valuations: dict[int, Valuation], *,
                  week: int) -> dict[int, float]:
    """espn_id -> market trade value (D9), for everyone in the pool.

    The ROS rank used for the decay is the pool-wide VOR rank, which is the
    same cross-position shape ADP has.
    """
    p = priors()
    decay_weeks = float(p.get("trades.market_adp_decay_weeks"))
    ranked = sorted((pl for pl in pool if pl.espn_id in valuations),
                    key=lambda pl: -valuations[pl.espn_id].vor)
    ros_rank = {pl.espn_id: i for i, pl in enumerate(ranked, 1)}
    out: dict[int, float] = {}
    for pl in pool:
        mr = market.market_rank(pl, ros_rank.get(pl.espn_id), week=week,
                               adp_decay_weeks=decay_weeks)
        out[pl.espn_id] = market.trade_value(mr)
    return out


def build(
    our_roster: list[Player],
    their_rosters: dict[int, tuple[str, list[Player]]],
    valuations: dict[int, Valuation],
    settings: LeagueSettings,
    *,
    max_proposals: int = 5,
    week: int = 1,
) -> list[Proposal]:
    """Generate candidate proposals, best first. 1-for-1 and 2-for-1 only:
    bigger packages are harder to evaluate, harder to accept, and §6.5 says
    consolidation beats dilution anyway.

    Hard filters, all irreversible-loss rules: our lineup must improve
    (§6.2), the market ratio must clear the floor (§6.3 as a number), and a
    protected asset is never diluted (§6.5). Everything else — their model
    gain, the shape effect — is a flag the agent reads.
    """
    p = priors()
    protected_n = int(p.get("trades.protected_top_n"))
    min_ratio = float(p.get("trades.min_market_ratio"))
    n_get = int(p.get("trades.gettables_per_team"))

    pool = list(our_roster) + [pl for _, r in their_rosters.values() for pl in r]
    mv = market_values(pool, valuations, week=week)

    our_ranked = sorted(
        (pl for pl in our_roster if pl.espn_id in valuations),
        key=lambda pl: -valuations[pl.espn_id].vor,
    )
    protected = {pl.espn_id for pl in our_ranked[:protected_n]}
    our_shape = roster_mod.analyse(our_roster, valuations, settings)
    our_starters = {a for a in _starter_ids(our_roster, valuations, settings)}

    # What we would give: surplus bodies first (D5.2), then any non-protected
    # non-starter, then non-protected starters inside a 2-for-1 upgrade.
    surplus = [pl for s in our_shape.by_pos.values() for pl in s.surplus_players]
    bench = [pl for pl in our_roster
             if pl.espn_id in valuations and pl.espn_id not in protected
             and pl.espn_id not in our_starters and pl not in surplus]
    starters = [pl for pl in our_roster
                if pl.espn_id in valuations and pl.espn_id not in protected
                and pl.espn_id in our_starters and pl not in surplus]
    givables = surplus + bench
    if not givables:
        return []

    proposals: list[Proposal] = []
    for tid, (name, their_roster) in their_rosters.items():
        their_vals_ok = [pl for pl in their_roster if pl.espn_id in valuations]
        if not their_vals_ok:
            continue
        # What we would take: their best by ROS VOR at any lineup position.
        # Whether he actually helps us is the delta below, not a position rule.
        gettables = sorted(
            (pl for pl in their_vals_ok if pl.pos not in (Pos.K, Pos.DST)),
            key=lambda pl: -valuations[pl.espn_id].vor,
        )[:n_get]
        if not gettables:
            continue

        combos: list[tuple[list[Player], list[Player]]] = []
        for g in givables[:6]:
            for r in gettables:
                combos.append(([g], [r]))
        for pair in itertools.combinations((givables + starters)[:7], 2):
            for r in gettables[:5]:
                combos.append((list(pair), [r]))

        for give, get in combos:
            ours = _delta(our_roster, give, get, valuations, settings)
            if ours <= 0:
                continue  # §6.2 — our lineup must improve, or there is no trade
            theirs = _delta(their_roster, get, give, valuations, settings)

            # §6.5 — never dilute a premium asset.
            if len(get) > len(give):
                best_out = max(valuations[x.espn_id].vor for x in give)
                best_in = max(valuations[x.espn_id].vor for x in get)
                if best_in < best_out * 0.9:
                    continue

            m_out = round(sum(mv.get(x.espn_id, 1.0) for x in give), 1)
            m_in = round(sum(mv.get(x.espn_id, 1.0) for x in get), 1)
            ratio = m_out / m_in if m_in > 0 else 0.0
            if ratio < min_ratio:
                continue  # §6.3 — reads as a lowball; refused at the gate anyway

            give_ids = {x.espn_id for x in give}
            after_roster = [x for x in our_roster if x.espn_id not in give_ids] + list(get)
            effect, score = _shape_effect(
                our_shape, roster_mod.analyse(after_roster, valuations, settings))

            flags: list[str] = []
            if theirs <= 0:
                flags.append(f"§6.3 our model says their lineup does not improve "
                             f"({theirs:+.1f}) — the market ratio {ratio:.2f} is what "
                             "carries this one; say why they accept")
            if score < 0:
                flags.append(f"D5 creates a shape problem: {effect}")
            if ratio > 1.3:
                flags.append(f"D9 we overpay by market ({ratio:.2f}) — fine if the "
                             "lineup gain is real, but do not add a sweetener")

            allowed, why = rate_limits.can_propose(
                tid, [x.espn_id for x in give], [x.espn_id for x in get])
            warnings = [] if allowed else [f"rate limit: {why}"]
            fairness = ("balanced" if 0.9 <= ratio <= 1.1
                        else "favours them by market" if ratio > 1.1
                        else "favours us by market")
            proposals.append(Proposal(
                to_team=tid, to_team_name=name, give=give, get=get,
                our_gain=round(ours, 1), their_gain=round(theirs, 1),
                rationale=(
                    f"{get[0].name} adds +{ours:.1f} ROS starting pts for us; "
                    f"they receive {m_out:.0f} of market value for {m_in:.0f} "
                    f"(ratio {ratio:.2f})"
                    + (f"; {give[0].name} would start for them (+{theirs:.1f})"
                       if theirs > 0 else "")
                ),
                fairness=fairness, shape_effect=effect,
                market_out=m_out, market_in=m_in,
                warnings=warnings, flags=flags,
            ))

    # Offers our own model says help them first (advisory, but a better bet),
    # then best for us with a bonus for fixing our shape, then the market read.
    proposals.sort(key=lambda pr: (
        pr.their_gain <= 0,
        -(pr.our_gain + 3.0 * pr.shape_effect.count("→")),
        -pr.market_ratio))
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
                settings, *, week: int = 1) -> tuple[bool, str, float, float]:
    """The propose_trade gate, in code.

    Hard: our starting lineup must improve (§6.2); the market ratio must
    clear `trades.min_market_ratio` (§6.3, the group-chat test as a number);
    a protected top-N asset is never sent out for lesser parts (§6.5).
    Advisory: their model gain, reported in the message either way.
    """
    p = priors()
    min_ratio = float(p.get("trades.min_market_ratio"))
    protected_n = int(p.get("trades.protected_top_n"))

    ours = _delta(our_roster, give, get, valuations, settings)
    theirs = _delta(their_roster, get, give, valuations, settings)
    if ours <= 0:
        return False, f"§6.2 our starting lineup does not improve ({ours:+.1f})", ours, theirs

    ranked = sorted((pl for pl in our_roster if pl.espn_id in valuations),
                    key=lambda pl: -valuations[pl.espn_id].vor)
    protected = {pl.espn_id for pl in ranked[:protected_n]}
    if any(x.espn_id in protected for x in give) and len(get) > len(give):
        return False, "§6.5 a protected top asset is not traded for a package", ours, theirs

    mv = market_values(list(our_roster) + list(their_roster), valuations, week=week)
    m_out = sum(mv.get(x.espn_id, 1.0) for x in give)
    m_in = sum(mv.get(x.espn_id, 1.0) for x in get)
    ratio = m_out / m_in if m_in > 0 else 0.0
    if ratio < min_ratio:
        return (False, f"§6.3 they receive {m_out:.0f} of market value for {m_in:.0f} "
                f"(ratio {ratio:.2f}, floor {min_ratio}) — reads as a lowball", ours, theirs)
    note = (f"§6.2 us {ours:+.1f} · market ratio {ratio:.2f} · "
            f"their model gain {theirs:+.1f}" + (" (advisory: negative)" if theirs <= 0 else ""))
    return True, note, ours, theirs
