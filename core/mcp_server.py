"""core, exposed to the agent over MCP.

🔴 THE TOOL LIST IS THE WRITE TABLE (§8.2). If an action is not defined here,
the agent cannot perform it — not because it is told not to, but because no such
capability exists in its context. Combined with `--strict-mcp-config` and
`--allowedTools "mcp__fantasy__*"`, the model has no Bash, no web access and no
filesystem. That is §10.3: guardrails in code, not in a prompt.

Every write tool routes through core.gates.write_gate. None of them touch
core.browser.actions directly.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from mcp.server.mcpserver import MCPServer

from core.config import settings
from core.espn import league_state as ls_mod
from core.espn.client import client
from core.gates import kill_switch, write_gate
from core.model.schema import Action, ActionKind
from core.model.value import value_pool
from core.notify import notify as _notify

log = logging.getLogger(__name__)
mcp = MCPServer("fantasy")

_state: ls_mod.LeagueState | None = None


def _snap(refresh: bool = False) -> ls_mod.LeagueState:
    """Cached snapshot within one agent run; refreshed before every write."""
    global _state
    if _state is None or refresh:
        _state = ls_mod.snapshot()
    return _state


def _vals(state: ls_mod.LeagueState, window: str = "week"):
    """Valuations with this morning's research folded in (D1.4), same as the
    packet — the tools and the packet must never disagree."""
    from core.manager import research as R
    from core.model.priors import priors

    dossiers = R.load_all(week=state.week)
    return value_pool(
        state.all_players(), state.facts.settings,
        window=window, week=state.week if window == "week" else None,
        weeks_remaining=max(1, state.facts.settings.regular_season_weeks - state.week + 1),
        current_week=state.week,
        contexts=R.contexts(dossiers, window=window),
        override_cap=float(priors().get("model.override_cap")),
    )


def _ok(**kw: Any) -> str:
    return json.dumps(kw, default=str, indent=1)


# ═════════════════════════════════════ READS ═════════════════════════════════


@mcp.tool()
def get_settings() -> str:
    """League rules: scoring, roster slots, waiver type, caps, playoff shape.

    Read this before reasoning about anything else (§3.1). Never assume scoring
    or roster shape — this league is half-PPR with rolling-priority waivers, and
    both differ from the common defaults.
    """
    s = _snap()
    f = s.facts
    return _ok(
        name=f.settings.name, teams=f.settings.team_count, week=s.week,
        scoring_ppr=f.settings.ppr_value,
        starting_slots={x.name: x.count for x in f.settings.starting_slots},
        bench=f.settings.bench_count, ir=f.settings.ir_count,
        position_caps={p.value: n for p, n in f.position_limits.items()},
        waiver_type=f.acquisition_type, is_faab=f.is_faab,
        trade_deadline=f.settings.trade_deadline,
        playoff_weeks=f.settings.playoff_weeks,
        playoff_seeding=f.playoff_seeding_rule,
    )


@mcp.tool()
def get_roster() -> str:
    """Our roster with current lineup slots, injury status and valuations."""
    s = _snap()
    v = _vals(s)
    me = s.me
    return _ok(
        team=me.name, record=f"{me.wins}-{me.losses}", points_for=me.points_for,
        waiver_priority=me.waiver_priority, bench_open=s.bench_open,
        players=[
            {
                "espn_id": p.espn_id, "name": p.name, "pos": p.pos.value,
                "team": p.pro_team, "slot": me.slots.get(p.espn_id),
                "status": p.injury_status.value, "bye": p.bye_week,
                "proj_week": round(v[p.espn_id].points, 2) if p.espn_id in v else None,
                "vor": round(v[p.espn_id].vor, 2) if p.espn_id in v else None,
                "stdev": v[p.espn_id].stdev if p.espn_id in v else None,
                "missing": v[p.espn_id].missing if p.espn_id in v else ["not valued"],
            }
            for p in me.roster
        ],
    )


@mcp.tool()
def get_matchup() -> str:
    """This week's opponent, their projected total, and ours."""
    s = _snap()
    v = _vals(s)
    from core.manager import lineup as lu

    ours = lu.optimal_lineup(s.me.roster, v, s.facts.settings, week=s.week)
    opp = s.opponent
    theirs = (
        lu.optimal_lineup(opp.roster, v, s.facts.settings, week=s.week)
        if opp else None
    )
    return _ok(
        week=s.week,
        us={"team": s.me.name, "projected": round(ours.projected_points, 1)},
        opponent=(
            {"team": opp.name, "projected": round(theirs.projected_points, 1),
             "record": f"{opp.wins}-{opp.losses}"} if opp and theirs else None
        ),
        margin=round(ours.projected_points - theirs.projected_points, 1)
        if theirs else None,
    )


@mcp.tool()
def get_lineup_plan() -> str:
    """The optimal lineup, the §4.2 variance call, and the moves it would take.

    core has already done the arithmetic. Do not recompute it in prose (§10.4).
    """
    s = _snap()
    v = _vals(s)
    from core.manager import lineup as lu

    opp_proj = None
    if s.opponent:
        opp_proj = lu.optimal_lineup(
            s.opponent.roster, v, s.facts.settings, week=s.week
        ).projected_points

    plan = lu.build(
        s.me.roster, v, s.facts.settings,
        opponent_projected=opp_proj,
        current_starters=s.me.starters,
        week=s.week,
    )
    return _ok(
        projected=round(plan.projected_points, 1),
        variance_mode=plan.variance_mode,
        margin=round(plan.margin, 1) if plan.margin is not None else None,
        assignments=[
            {"slot": a.slot, "player": a.player.name if a.player else None,
             "espn_id": a.player.espn_id if a.player else None,
             "points": round(a.points, 1)}
            for a in plan.assignments
        ],
        changes=[
            {"player": p.name, "espn_id": p.espn_id, "from": frm, "to": to, "why": why}
            for p, frm, to, why in plan.changes
        ],
        notes=plan.notes,
    )


@mcp.tool()
def get_waiver_plan() -> str:
    """Waiver and free-agent candidates, scored against §5.

    Remember this league is ROLLING PRIORITY, not FAAB: the cost of a claim is
    our queue position, and free agents cost nothing at all (§5.3.2).
    """
    s = _snap()
    v = _vals(s)
    ros = _vals(s, window="ros")
    from core.gates import rate_limits
    from core.manager import waivers as w

    plan = w.build(
        s.me.roster, s.free_agents, v, s.facts.settings,
        waiver_priority=s.me.waiver_priority,
        on_waivers=s.on_waivers,
        bench_open=s.bench_open,
        current_week=s.week,
        adds_left=rate_limits.adds_left(),
        ros_valuations=ros,
    )

    from core.manager import research as R
    researched = set(R.load_all(week=s.week))

    def _cand(c):
        return {"name": c.player.name, "espn_id": c.player.espn_id,
                "pos": c.player.pos.value, "team": c.player.pro_team,
                "status": c.player.injury_status.value,
                "proj_week": round(c.valuation.points, 1),
                "weekly_gain": round(c.net_gain, 2), "ros_vor": c.ros_vor,
                "replaces": c.replaces.name if c.replaces else None,
                "drop": c.drop.name if c.drop else None,
                "drop_id": c.drop.espn_id if c.drop else None,
                "drop_tradeable": c.drop_tradeable,
                "free_agent": c.is_free_agent,
                "archetype": c.archetype, "core_verdict": c.verdict,
                "flags": c.flags, "why": c.reasons,
                "researched": c.player.espn_id in researched}

    return _ok(
        priority=plan.priority,
        adds_left_this_week=plan.adds_left,
        core_recommends={"free_adds": [c.player.name for c in plan.free_adds],
                         "claims": [c.player.name for c in plan.claims]},
        candidates=[_cand(c) for c in plan.candidates],
        notes=plan.notes,
    )


@mcp.tool()
def get_roster_shape() -> str:
    """Where the roster is fat and thin by position (D5): surplus at a
    one-slot position is trade capital, a shortage costs a starting slot on
    the first bye or injury."""
    s = _snap()
    from core.manager import roster as roster_mod

    shape = roster_mod.analyse(s.me.roster, _vals(s, window="ros"), s.facts.settings)
    return _ok(
        summary=shape.summary(),
        by_position={
            pos.value: {"have": x.have, "starters": x.starters, "cover": x.cover,
                        "delta": x.delta, "verdict": x.verdict,
                        "surplus_players": [p.name for p in x.surplus_players]}
            for pos, x in shape.by_pos.items()
        },
        notes=shape.notes,
    )


@mcp.tool()
def get_research(espn_id: int | None = None) -> str:
    """This morning's dossiers (D1, D3.1): status and practice, usage trend,
    matchup, analyst read, news, sources — and what core did with the
    multipliers. One player, or everyone researched."""
    s = _snap()
    from core.manager import research as R

    ds = R.load_all(week=s.week)
    return _ok(count=len(ds), dossiers=R.facts(ds, [espn_id] if espn_id else None))


_ON_DEMAND = {"used": 0, "cost": 0.0}


@mcp.tool()
def research_player(espn_id: int, question: str = "") -> str:
    """Research one player NOW (D9): a web pass by a research agent — status
    and practice report, usage trend, matchup, analyst read, dated news with
    sources — written as this week's dossier and folded into the valuation.

    Use it for anyone the morning pass skipped: a trade target on another
    roster, a waiver candidate you are weighing, a player whose dossier is
    missing. `question` is what you actually want to know ("is his role
    safe with X back?", "would his manager sell?"); the researcher answers
    it in `analyst_read.detail`.

    Costs ~40s and ~$0.25; capped per run. A fresh dossier from this
    morning is returned as-is unless you ask a question.
    """
    from agent import run as agent_run
    from agent.packet import weekly_dossier_packet
    from core.manager import research as R
    from core.model.priors import priors

    s = _snap()
    by_id = {p.espn_id: p for p in s.all_players()}
    pl = by_id.get(espn_id)
    if pl is None:
        return _ok(error=f"player {espn_id} is not in the league pool")

    max_age = float(priors().get("research_week.max_age_hours"))
    fresh = R.load_one(espn_id, max_age_hours=max_age)
    if fresh is not None and fresh.week == s.week and not question.strip():
        return _ok(fresh=True, dossier=R.facts({espn_id: fresh})[0])

    cap = int(priors().get("research_week.on_demand_max"))
    if _ON_DEMAND["used"] >= cap:
        return _ok(error=f"research_player cap reached ({cap} this run) — decide on "
                         "what you have, or escalate")

    v = _vals(s).get(espn_id)
    role = ("roster" if pl.espn_id in {p.espn_id for p in s.me.roster}
            else "trade" if pl.on_team_id else "waiver")
    packet = weekly_dossier_packet(pl, v, week=s.week, role=role,
                                   question=question.strip() or None)
    _ON_DEMAND["used"] += 1
    res = agent_run.run("weekly_dossier", packet, timeout=300)
    cost = float((res.usage or {}).get("total_cost_usd") or 0.0)
    _ON_DEMAND["cost"] += cost
    if not res.ok or not res.output:
        return _ok(error=f"research failed: {res.error}", used=_ON_DEMAND["used"], cap=cap)
    out = dict(res.output)
    out.setdefault("espn_id", espn_id)
    out.setdefault("name", pl.name)
    R.write(espn_id, out, week=s.week)
    d = R.load_one(espn_id, max_age_hours=max_age)
    if d is None:
        return _ok(error="dossier written but rejected by validation (no verifiable source)",
                   used=_ON_DEMAND["used"], cap=cap)
    log.info("on-demand research: %s (%s) $%.2f", pl.name, role, cost)
    return _ok(fresh=False, used=_ON_DEMAND["used"], cap=cap, cost_usd=round(cost, 2),
               note="multipliers now folded into every valuation you read from here on",
               dossier=R.facts({espn_id: d})[0])


@mcp.tool()
def get_lessons() -> str:
    """What this system has learned on previous Tuesdays (D7). A lesson that
    applies today is cited like a rule."""
    from core.state import lessons

    return _ok(lessons=lessons.read())


def _offers(s: ls_mod.LeagueState):
    """Pending offers TO us, as (PendingOffer, gauntlet.Offer) pairs.

    A player the snapshot cannot see becomes a stub with no valuation, so the
    gauntlet's §6.8.11 "complete data" gate fails on him instead of the offer
    being evaluated with him silently missing.
    """
    from core.espn import trades as tr
    from core.manager import gauntlet as G
    from core.model.schema import Player, Pos

    by_id = {p.espn_id: p for p in s.all_players()}

    def pl(i: int) -> Player:
        return by_id.get(i) or Player(espn_id=i, name=f"player {i}", pos=Pos.RB, pro_team="?")

    out = []
    for po in tr.pending_offers(client(), my_team_id=s.my_team_id, week=s.week):
        their = s.teams.get(po.from_team)
        out.append((po, G.Offer(
            offer_id=po.offer_id, from_team=po.from_team,
            incoming=[pl(i) for i in po.incoming_ids],
            outgoing=[pl(i) for i in po.outgoing_ids],
            proposed_at=po.proposed_at,
            their_roster=their.roster if their else [],
        )))
    return out


def _gauntlet(s: ls_mod.LeagueState, offer):
    """Run §6.8 on a live offer with everything the gates need from state."""
    from datetime import datetime

    from core.gates import rate_limits
    from core.manager import gauntlet as G
    from core.state import store

    v = _vals(s, window="ros")
    first_seen = datetime.fromisoformat(rate_limits.note_offer_seen(offer.offer_id))
    accepts = store.load().get("trade_accepts") or []
    this_week = rate_limits._recent(accepts, 7)
    from_them = [
        datetime.fromisoformat(e["at"]) for e in rate_limits._recent(accepts, 365)
        if e.get("from_team") == offer.from_team
    ]
    return G.run(
        offer, s.me.roster, v, s.facts.settings,
        first_seen=first_seen,
        accepts_this_week=len(this_week),
        last_accept_from_them=max(from_them) if from_them else None,
        bench_open=s.bench_open,
        playoff_weeks=tuple(s.facts.settings.playoff_weeks) or (15, 16, 17),
    )


def _offer_json(po, offer, s: ls_mod.LeagueState) -> dict:
    their = s.teams.get(po.from_team)
    return {
        "offer_id": po.offer_id,
        "from_team": po.from_team,
        "from_team_name": their.name if their else f"team {po.from_team}",
        "proposed_at": po.proposed_at.isoformat(),
        "we_receive": [{"espn_id": p.espn_id, "name": p.name, "pos": p.pos.value}
                       for p in offer.incoming],
        "we_give": [{"espn_id": p.espn_id, "name": p.name, "pos": p.pos.value}
                    for p in offer.outgoing],
    }


@mcp.tool()
def get_pending_offers() -> str:
    """Incoming trade offers proposed TO us and still open.

    Each carries what we would receive and give. Call run_gauntlet(offer_id)
    for the thirteen-gate result; the gauntlet is decided in code and you
    cannot accept anything it rejected.
    """
    s = _snap()
    try:
        pairs = _offers(s)
    except Exception as e:
        return _ok(error=f"could not read trade offers: {e}", offers=[])
    return _ok(count=len(pairs), offers=[_offer_json(po, o, s) for po, o in pairs])


@mcp.tool()
def run_gauntlet(offer_id: str) -> str:
    """The full §6.8 gauntlet on one pending offer, gate by gate.

    Read-only. Narrate the result and write the §6.8.3 sentence; the accept
    tool re-runs this in code before it does anything.
    """
    s = _snap(refresh=True)
    match = next(((po, o) for po, o in _offers(s) if po.offer_id == offer_id), None)
    if match is None:
        return _ok(error=f"no pending offer {offer_id!r}")
    po, offer = match
    r = _gauntlet(s, offer)
    return _ok(
        offer=_offer_json(po, offer, s),
        accepted=r.accepted,
        failed_on=r.failed_on,
        gates=[{"section": c.section, "name": c.name, "passed": c.passed, "detail": c.detail}
               for c in r.checks],
    )


@mcp.tool()
def get_trade_ideas() -> str:
    """Outgoing proposals core would make (§6.1–§6.7, D4, D5): both sides
    gain in ROS starting points, and what each does to our roster shape.

    Read-only. `propose_trade` is the write, and it re-runs the both-sides
    value test in code before anything is sent.
    """
    s = _snap()
    from core.gates import rate_limits
    from core.manager import trades_out as T

    v = _vals(s, window="ros")
    others = {tid: (t.name, t.roster) for tid, t in s.teams.items() if tid != s.my_team_id}
    props = T.build(s.me.roster, others, v, s.facts.settings, week=s.week)
    mv = T.market_values(s.all_players(), v, week=s.week)
    day_left, week_left = rate_limits.proposals_left()

    def _side(players):
        return [{"espn_id": x.espn_id, "name": x.name, "pos": x.pos.value,
                 "ros_vor": round(v[x.espn_id].vor, 1) if x.espn_id in v else None,
                 "market_value": mv.get(x.espn_id)} for x in players]

    return _ok(
        count=len(props),
        proposals_left_today=day_left,
        proposals_left_this_week=week_left,
        note=("our_gain is hard (§6.2); market_ratio under the floor is refused (§6.3); "
              "their_gain_advisory is our model's guess. Any offer, listed or not, goes "
              "through the same gate. Say why they accept (D9)."),
        proposals=[{
            "to_team": p.to_team, "to_team_name": p.to_team_name,
            "give": _side(p.give), "get": _side(p.get),
            "our_gain": p.our_gain, "their_gain_advisory": p.their_gain,
            "market_out": p.market_out, "market_in": p.market_in,
            "market_ratio": p.market_ratio,
            "fairness": p.fairness, "shape_effect": p.shape_effect,
            "rationale": p.rationale, "flags": p.flags, "warnings": p.warnings,
        } for p in props],
    )


@mcp.tool()
def get_rate_limits() -> str:
    """What §6.1 and §6.8.10 currently allow."""
    from core.state import store

    st = store.load()
    return _ok(
        proposals=st.get("trade_proposals", [])[-5:],
        accepts=st.get("trade_accepts", [])[-5:],
        kill_switch=kill_switch.state(),
    )


@mcp.tool()
def get_guardrails() -> str:
    """The write table in force right now (§8.2), and the kill switch state."""
    return _ok(
        kill_switch=kill_switch.state(),
        auto=["set_lineup", "waiver_claim / add_drop (3 a week, never a §5.5 drop)",
              "propose_trade (1/day, 3/week, §6.2 + market floor §6.3 in code)",
              "reject_trade",
              "accept_trade (only on a clean §6.8 gauntlet, re-run in code)"],
        never=["counter-offer", "league settings", "chat/messages",
               "anything outside our own team"],
        reminder="an action without a § citation is rejected at the schema boundary",
    )


# ════════════════════════════════════ WRITES ═════════════════════════════════


@mcp.tool()
def set_lineup(moves: list[dict], reason: str, cites: list[str]) -> str:
    """Apply start/sit changes. AUTO — reversible until kickoff.

    `moves`: [{"espn_id": int, "slot": "RB"|"BE"|...}]. Cite the sections that
    justify it, e.g. ["§4.1"] or ["§4.2"].
    """
    s = _snap(refresh=True)  # §8.3 — never act on a stale read
    action = Action(
        kind=ActionKind.SET_LINEUP,
        args={"moves": moves}, cites=cites, reason=reason,
    )

    def perform():
        from core.browser import actions as A
        from core.browser.session import EspnSession

        with EspnSession(headless=True) as sess:
            return A.set_lineup(
                sess, s.facts.settings.league_id, s.my_team_id,
                s.facts.settings.season,
                [(int(m["espn_id"]), str(m["slot"])) for m in moves],
            )

    gate, receipt = write_gate.execute(action, perform, skip_health=True)
    # No Slack here: the sweep posts ONE digest of what was done (Pearce,
    # 2026-09-05: "just what it did"). The reason lives in decisions.jsonl.
    return _ok(allowed=gate.allowed, refused_by=gate.refused_by,
               reason=gate.reason, receipt=str(receipt) if receipt else None)


@mcp.tool()
def add_drop(add_id: int, drop_id: int | None, reason: str, cites: list[str]) -> str:
    """Add a free agent or claim off waivers, dropping someone if needed. AUTO
    inside §5. Pass drop_id=None only when a bench spot is genuinely open."""
    s = _snap(refresh=True)
    by_id = {p.espn_id: p for p in s.all_players()}
    add_p, drop_p = by_id.get(add_id), by_id.get(drop_id) if drop_id else None
    if add_p is None:
        return _ok(allowed=False, reason=f"player {add_id} not found in the pool")

    # §5.5 — the one waiver rule that is a refusal, not a flag (D9): a top-N
    # player by ROS VOR is never dropped for an add. Irreversible, so in code.
    if drop_p is not None:
        from core.manager import waivers as W

        if drop_p.espn_id not in {p.espn_id for p in s.me.roster}:
            return _ok(allowed=False, refused_by="§5.4",
                       reason=f"{drop_p.name} is not on our roster")
        if drop_p.espn_id in W.protected_ids(s.me.roster, _vals(s, window="ros")):
            from core.model.priors import priors

            top_n = priors().get("waivers.never_drop_top_n")
            return _ok(allowed=False, refused_by="§5.5",
                       reason=f"{drop_p.name} is a top-{top_n} player by ROS VOR — never "
                              "dropped; trade him if you must move him")

    kind = (ActionKind.WAIVER_CLAIM if add_id in s.on_waivers else ActionKind.ADD_DROP)
    action = Action(
        kind=kind,
        args={"add": add_id, "drop": drop_id,
              "roster_has_room": s.bench_open > 0},
        cites=cites, reason=reason,
    )

    def perform():
        from core.browser import actions as A
        from core.browser.session import EspnSession

        with EspnSession(headless=True) as sess:
            return A.add_drop(
                sess, s.facts.settings.league_id, s.facts.settings.season,
                add_id, add_p.name, drop_id, drop_p.name if drop_p else None,
            )

    gate, receipt = write_gate.execute(action, perform, skip_health=True)
    if gate.allowed and receipt:
        from core.gates import rate_limits

        rate_limits.record_add(add_id, drop_id)
    return _ok(allowed=gate.allowed, refused_by=gate.refused_by,
               reason=gate.reason, receipt=str(receipt) if receipt else None)


@mcp.tool()
def propose_trade(to_team: int, give_ids: list[int], get_ids: list[int],
                  reason: str, cites: list[str], why_they_accept: str = "") -> str:
    """Send an outgoing trade offer. AUTO inside §6.1–§6.7: rate-limited
    (1/day, 3/week, 1 open per manager, no re-propose inside 14 days). Re-run
    HERE in code: our lineup must improve (§6.2), the market ratio must clear
    `trades.min_market_ratio` (§6.3), no protected asset for a package (§6.5).
    Our model's read of THEIR gain is advisory (D9).

    `why_they_accept` is required: the reason this specific human says yes —
    their hole, their bye crunch, their record, what they paid for the player.
    It is logged and graded on Tuesday against what they actually did.

    This goes to another human. Use one of the three only for an offer you
    would send with your name on it (D4.6).
    """
    if len((why_they_accept or "").strip()) < 30:
        return _ok(allowed=False, refused_by="D9",
                   reason="why_they_accept is required — say why this manager says yes")
    from core.gates import rate_limits
    from core.manager import trades_out as T

    s = _snap(refresh=True)
    them = s.teams.get(to_team)
    if them is None or to_team == s.my_team_id:
        return _ok(allowed=False, reason=f"no other team with id {to_team}")
    by_id = {p.espn_id: p for p in s.all_players()}
    give = [by_id[i] for i in give_ids if i in by_id]
    get = [by_id[i] for i in get_ids if i in by_id]
    if len(give) != len(give_ids) or len(get) != len(get_ids) or not give or not get:
        return _ok(allowed=False, reason="a player in the offer is not in the league pool")
    mine = {p.espn_id for p in s.me.roster}
    theirs = {p.espn_id for p in them.roster}
    if not all(p.espn_id in mine for p in give) or not all(p.espn_id in theirs for p in get):
        return _ok(allowed=False, reason="give must be ours and get must be theirs")

    v = _vals(s, window="ros")
    ok, why, ours, theirs_gain = T.value_check(
        s.me.roster, them.roster, give, get, v, s.facts.settings, week=s.week)
    if not ok:
        return _ok(allowed=False, refused_by="§6.2/§6.3/§6.5", reason=why)

    action = Action(
        kind=ActionKind.PROPOSE_TRADE,
        args={"to_team": to_team, "to_team_name": them.name,
              "give": give_ids, "get": get_ids,
              "give_names": [p.name for p in give], "get_names": [p.name for p in get],
              "why_they_accept": why_they_accept.strip()[:600]},
        cites=cites or ["§6.2"], reason=reason,
    )

    def perform():
        from core.browser import actions as A
        from core.browser.session import EspnSession

        with EspnSession(headless=True) as sess:
            return A.propose_trade(
                sess, s.facts.settings.league_id, s.facts.settings.season, to_team,
                [(p.espn_id, p.name) for p in give], [(p.espn_id, p.name) for p in get],
            )

    gate, receipt = write_gate.execute(
        action, perform, skip_health=True,
        predicted={"our_gain_ros": round(ours, 1), "their_gain_ros": round(theirs_gain, 1)},
    )
    if gate.allowed and receipt:
        rate_limits.record_proposal(to_team, give_ids, get_ids)
    return _ok(allowed=gate.allowed, refused_by=gate.refused_by, reason=gate.reason,
               value_check=why, receipt=str(receipt) if receipt else None)


@mcp.tool()
def reject_trade(offer_id: str, reason: str, cites: list[str]) -> str:
    """Reject an incoming offer. Always allowed — the default answer is no."""
    s = _snap(refresh=True)
    match = next(((po, o) for po, o in _offers(s) if po.offer_id == offer_id), None)
    if match is None:
        return _ok(allowed=False, reason=f"no pending offer {offer_id!r} — nothing to reject")
    _, offer = match
    names = [p.name for p in offer.incoming + offer.outgoing]
    their = s.teams.get(offer.from_team)
    action = Action(kind=ActionKind.REJECT_TRADE,
                    args={"offer_id": offer_id, "from_team": offer.from_team,
                          "from_team_name": their.name if their else f"team {offer.from_team}",
                          "get_names": [p.name for p in offer.incoming],
                          "give_names": [p.name for p in offer.outgoing]},
                    cites=cites or ["§6.8.0"], reason=reason)

    def perform():
        from core.browser import actions as A
        from core.browser.session import EspnSession

        with EspnSession(headless=True) as sess:
            return A.reject_trade(
                sess, s.facts.settings.league_id, s.facts.settings.season, offer_id, names
            )

    gate, receipt = write_gate.execute(action, perform, skip_health=True)
    return _ok(allowed=gate.allowed, reason=gate.reason,
               receipt=str(receipt) if receipt else None)


@mcp.tool()
def accept_trade(offer_id: str, reason: str, cites: list[str]) -> str:
    """Accept an incoming offer. AUTO only on a 13/13 §6.8 gauntlet pass.

    The gauntlet is re-run HERE, in code, on a fresh snapshot — the result you
    saw from run_gauntlet is not what decides. A single failed gate refuses
    the write. Acceptance posts the full gauntlet to #fantasy (§6.8.12).
    """
    from core.gates import rate_limits

    s = _snap(refresh=True)
    match = next(((po, o) for po, o in _offers(s) if po.offer_id == offer_id), None)
    if match is None:
        return _ok(allowed=False, reason=f"no pending offer {offer_id!r}")
    po, offer = match
    result = _gauntlet(s, offer)
    names = [p.name for p in offer.incoming + offer.outgoing]
    their = s.teams.get(offer.from_team)
    action = Action(
        kind=ActionKind.ACCEPT_TRADE,
        args={"offer_id": offer_id, "from_team": offer.from_team, "gauntlet": result,
              "from_team_name": their.name if their else f"team {offer.from_team}",
              "get_names": [p.name for p in offer.incoming],
              "give_names": [p.name for p in offer.outgoing]},
        cites=cites or ["§6.8"], reason=reason,
    )

    def perform():
        from core.browser import actions as A
        from core.browser.session import EspnSession

        with EspnSession(headless=True) as sess:
            return A.accept_trade(
                sess, s.facts.settings.league_id, s.facts.settings.season, offer_id, names
            )

    gate, receipt = write_gate.execute(action, perform, skip_health=True)
    gates_txt = "\n".join(
        f"{'✓' if c.passed else '✗'} {c.section} {c.name}: {c.detail}" for c in result.checks
    )
    if gate.allowed and receipt:
        rate_limits.record_accept(offer_id, offer.from_team)
        # §6.8.12 — an accept posts the moment it fires. Short: the gauntlet
        # is in decisions.jsonl.
        _notify("action", f"ACCEPTED trade from {action.args['from_team_name']}",
                f"get {', '.join(p.name for p in offer.incoming)} · "
                f"give {', '.join(p.name for p in offer.outgoing)}")
    log.info("gauntlet for %s:\n%s", offer_id, gates_txt)
    return _ok(allowed=gate.allowed, refused_by=gate.refused_by, reason=gate.reason,
               gauntlet_failed_on=result.failed_on,
               receipt=str(receipt) if receipt else None)


@mcp.tool()
def notify(level: str, title: str, body: str = "") -> str:
    """Post to Slack #fantasy. level: info | good | warn | error | action.

    Use this to surface anything a human should see — including a decision you
    deliberately did NOT take.
    """
    return _ok(sent=_notify(level, title, body))


def main() -> None:
    logging.basicConfig(level=settings().log_level)
    mcp.run()


if __name__ == "__main__":
    main()
