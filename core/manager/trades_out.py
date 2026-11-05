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
from datetime import UTC, datetime, timedelta

from core.gates import rate_limits
from core.manager import roster as roster_mod
from core.manager.gauntlet import _starting_points
from core.model import market
from core.model.priors import priors
from core.model.schema import LeagueSettings, Player, Pos, Valuation
from core.state import store

log = logging.getLogger(__name__)

#: §6.1's open-offer window. `rate_limits.can_propose` reads `trade_proposals`
#: 14 days back, so a promise expires exactly when the block it created does.
_OPEN_OFFER_DAYS = 14.0


def promised_ids() -> dict[int, int]:
    """§6.1a — our players already promised away in an OPEN outgoing offer,
    mapped to the team we offered them to.

    An offer closes on one signal: every player we asked to GET arriving on
    our roster (§6.1a). A player we offered to GIVE who leaves our roster
    first takes that possibility with him — ESPN voids a proposal whose
    proposer no longer owns a piece of it, so the GET side can never land and
    §6.1 keeps that manager blocked for the rest of the fortnight for nothing.

    2026-10-09: Jordan Addison was half of the 10-05 Kelce+Addison offer to
    team 6 and the cheapest body on our bench, so core handed him over as the
    drop behind an add. §6.1 then refused every proposal to team 6 until
    10-19, including a +103.7 ROS deal for Kenneth Walker III that that
    manager had asked for in chat. Read by `waivers.choose_drop` (which stops
    offering him) and by `add_drop` (which refuses the write).

    2026-10-12: the same hole in the TRADE path. A body leaves our roster just
    as finally when a second manager accepts an offer for him, so `build`
    keeps him off every other manager's board and `value_check` refuses the
    proposal.

    Offers recorded before 2026-09-16 carry no player ids and promise nobody;
    `rate_limits.unverifiable_proposals` surfaces those instead.
    """
    cutoff = datetime.now(UTC) - timedelta(days=_OPEN_OFFER_DAYS)
    out: dict[int, int] = {}
    for e in store.load().get("trade_proposals") or []:
        team = e.get("to_team")
        if team is None:
            continue
        try:
            at = datetime.fromisoformat(e["at"])
            if not at.tzinfo:
                at = at.replace(tzinfo=UTC)
        except Exception:
            continue
        if at < cutoff:
            continue
        for pid in e.get("give") or []:
            out[int(pid)] = int(team)
    return out


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
    #: ...and as a number: + fixes shape, − breaks it. A shortage counts
    #: double a surplus. Worth `trades.shape_point_value` ROS points each.
    shape_score: int = 0
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
    """Words and a score for what the deal does to our roster shape.

    A SHORTAGE and a SURPLUS are not the same size of problem, and the first
    version scored them identically (±1 each) — which is how a deal that left
    RB short kept outranking one that fixed it (Pearce, 2026-09-07). A
    shortage costs a starting slot outright the first time someone is hurt or
    on bye; a surplus costs a bench spot. So fixing or creating a shortage
    moves the score twice as far as fixing or creating a surplus.
    """
    words, score = [], 0
    for pos in Pos:
        b = before.by_pos.get(pos)
        a = after.by_pos.get(pos)
        bd = b.delta if b else 0
        ad = a.delta if a else 0
        if bd == ad:
            continue
        # A shortage on either side of the deal is the expensive kind.
        weight = 2 if (bd < 0 or ad < 0) else 1
        if abs(ad) < abs(bd):
            score += weight
            words.append(f"{pos.value} {'surplus' if bd > 0 else 'shortage'} {abs(bd)}→{abs(ad)}")
        else:
            score -= weight
            words.append(f"{pos.value} becomes {'surplus' if ad > 0 else 'short'} {abs(ad)}")
    return "; ".join(words) or "shape unchanged", score


def market_values(pool: list[Player], valuations: dict[int, Valuation], *,
                  week: int) -> dict[int, float]:
    """espn_id -> market trade value (D9), for everyone in the pool.

    The ROS rank used for the decay is the VOR rank over `valuations` — every
    player core valued this run — which is the same cross-position shape ADP
    has. `pool` says who to price, not what to price him against.

    2026-10-17: it used to rank within `pool`, so the same player was worth
    two different numbers depending on who asked. `build` prices against ten
    rosters and `value_check` priced against two, which put Swift + Hubbard
    for James Cook III on the board at a market ratio of 1.03 and through the
    gate at 1.24 — core disagreeing with itself about the one number §6.3 is
    enforced on and the agent quotes to another manager. Every caller passes
    the same league-wide valuations (`value_pool` over `all_players()`), so
    ranking over those makes the price the same wherever it is computed.

    2026-11-05: a vetoed player ranks LAST, whatever his vor says. `value_pool`
    keeps him out of the pool VOR is computed on, so his `vor` falls back to
    the 0.0 default — exactly replacement level — and by week 9 only ~70 of
    ~320 valued players are above replacement. That seated every unplayable
    body in the top quarter of this rank: an IR'd back with no designated
    return priced at 22.3 of market value while the healthy below-replacement
    receiver we would actually package priced at 2.6, behind all 31 of them.
    No human pays for a player who cannot take the field, and vor.py:70-79
    already refuses this on a draft board — but that guard is in `compute_vor`,
    which never sees a vetoed player.
    """
    p = priors()
    decay_weeks = float(p.get("trades.market_adp_decay_weeks"))
    ranked = sorted(valuations.items(),
                    key=lambda kv: (bool(kv[1].vetoed), -kv[1].vor, kv[0]))
    ros_rank = {pid: i for i, (pid, _) in enumerate(ranked, 1)}
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
    promised: dict[int, int] | None = None,
) -> list[Proposal]:
    """Generate candidate proposals, best first. 1-for-1 and 2-for-1 only:
    bigger packages are harder to evaluate, harder to accept, and §6.5 says
    consolidation beats dilution anyway.

    Hard filters, all irreversible-loss rules: our lineup must improve
    (§6.2), the market ratio must clear the floor (§6.3 as a number), a
    protected asset is never diluted (§6.5), and a body already promised to
    another manager is not offered again (§6.1a). Everything else — their
    model gain, the shape effect — is a flag the agent reads.

    `promised` is `promised_ids()` — espn_id -> the team we have already
    offered him to in an offer that is still open.
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
    promised = promised or {}

    proposals: list[Proposal] = []
    for tid, (name, their_roster) in their_rosters.items():
        their_vals_ok = [pl for pl in their_roster if pl.espn_id in valuations]
        if not their_vals_ok:
            continue

        # §6.1a — a body we have already promised to a DIFFERENT manager is
        # not ours to put on this manager's board. If this offer is accepted
        # he leaves our roster, ESPN voids the offer whose proposer no longer
        # owns a piece of it, §6.1a can then never see its GET side arrive,
        # and §6.1 blocks the first manager for the rest of the fortnight for
        # nothing. He stays available to the manager he is promised to —
        # that second offer is §6.1's one-open-offer rule's business, and it
        # already reports it as a rate-limit warning below.
        #
        # 2026-10-12: Garrett Wilson was half of the open 10-08 Andrews +
        # Garrett Wilson offer to team 9 and still appeared on the give side
        # of the ideas to three other managers; the sweep spent its one
        # escalation asking Pearce for a read on one of them.
        spoken_for = {pid for pid, owed in promised.items() if owed != tid}
        team_givables = [pl for pl in givables if pl.espn_id not in spoken_for]
        team_starters = [pl for pl in starters if pl.espn_id not in spoken_for]
        if not team_givables:
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
        for g in team_givables[:6]:
            for r in gettables:
                combos.append(([g], [r]))
        for pair in itertools.combinations((team_givables + team_starters)[:7], 2):
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
            if ours < float(p.get("trades.marginal_gain")):
                flags.append(f"D4.7 marginal: +{ours:.1f} ROS starting pts is not worth a "
                             "slot or a manager's goodwill — wait for a better board")
            if theirs <= 0:
                flags.append(f"§6.3 our model says their lineup does not improve "
                             f"({theirs:+.1f}) — the market ratio {ratio:.2f} is what "
                             "carries this one; say why they accept")
            if score < 0:
                flags.append(f"D5 creates a shape problem: {effect}")
            short_after = roster_mod.analyse(after_roster, valuations, settings).short
            if short_after:
                flags.append(
                    "D2.4 need over name: this deal leaves "
                    + ", ".join(f"{k.value} short {v}" for k, v in short_after.items())
                    + " — a shortage costs a starting slot the first time "
                      "someone is hurt or on bye")
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
                fairness=fairness, shape_effect=effect, shape_score=score,
                market_out=m_out, market_in=m_in,
                warnings=warnings, flags=flags,
            ))

    # Offers our own model says help them first (advisory, but a better bet),
    # then by ROS starting points PLUS what the deal does to our shape, priced
    # in the same unit. The old key counted "→" characters in a sentence,
    # which weighted a fixed shortage the same as a converted surplus.
    shape_pts = float(p.get("trades.shape_point_value"))
    proposals.sort(key=lambda pr: (
        pr.their_gain <= 0,
        -(pr.our_gain + shape_pts * pr.shape_score),
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
                settings, *, week: int = 1, to_team: int | None = None,
                promised: dict[int, int] | None = None,
                ) -> tuple[bool, str, float, float]:
    """The propose_trade gate, in code.

    Hard: our starting lineup must improve (§6.2); the market ratio must
    clear `trades.min_market_ratio` (§6.3, the group-chat test as a number);
    a protected top-N asset is never sent out for lesser parts (§6.5); a body
    already promised to another manager is not promised twice (§6.1a).
    Advisory: their model gain, reported in the message either way.

    Every refusal reason starts with the section it enforces, which is what
    `propose_trade` reports as `refused_by`.
    """
    p = priors()
    min_ratio = float(p.get("trades.min_market_ratio"))
    protected_n = int(p.get("trades.protected_top_n"))

    ours = _delta(our_roster, give, get, valuations, settings)
    theirs = _delta(their_roster, get, give, valuations, settings)

    # §6.1a, 2026-10-12 — checked before the value rules, because a positive
    # §6.2 gain is exactly what makes this one tempting. An accepted second
    # offer takes him off our roster, ESPN voids the first offer, §6.1a can
    # never settle it, and §6.1 blocks that manager for the fortnight.
    for x in give:
        owed = (promised or {}).get(x.espn_id)
        if owed is not None and owed != to_team:
            return (False, f"§6.1a {x.name} is already part of an offer still open "
                    f"with team {owed}, so he cannot be promised twice: if this one "
                    "is accepted ESPN voids that offer and §6.1 blocks that manager "
                    "for the rest of the fortnight. Offer someone else, or wait the "
                    "offer out (§8.2 gives core no withdraw — Pearce pulls it in the "
                    "app if it has to go).", ours, theirs)

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
