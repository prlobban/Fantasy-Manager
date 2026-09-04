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
    return value_pool(
        state.all_players(), state.facts.settings,
        window=window, week=state.week if window == "week" else None,
        weeks_remaining=max(1, state.facts.settings.regular_season_weeks - state.week + 1),
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
    from core.manager import waivers as w

    plan = w.build(
        s.me.roster, s.free_agents, v, s.facts.settings,
        waiver_priority=s.me.waiver_priority,
        on_waivers=s.on_waivers,
        bench_open=s.bench_open,
        current_week=s.week,
    )
    return _ok(
        priority=plan.priority,
        free_adds=[
            {"name": c.player.name, "espn_id": c.player.espn_id,
             "pos": c.player.pos.value, "weekly_gain": round(c.net_gain, 2),
             "drop": c.drop.name if c.drop else None,
             "drop_id": c.drop.espn_id if c.drop else None,
             "archetype": c.archetype, "why": c.reasons}
            for c in plan.free_adds
        ],
        claims=[
            {"name": c.player.name, "espn_id": c.player.espn_id,
             "pos": c.player.pos.value, "weekly_gain": round(c.net_gain, 2),
             "drop": c.drop.name if c.drop else None,
             "drop_id": c.drop.espn_id if c.drop else None,
             "archetype": c.archetype, "why": c.reasons}
            for c in plan.claims
        ],
        skipped=[{"name": c.player.name, "why": why} for c, why in plan.skipped[:10]],
        notes=plan.notes,
    )


@mcp.tool()
def get_pending_offers() -> str:
    """Incoming trade offers, each with its full §6.8 gauntlet result.

    The gauntlet is decided in code. Your job is to narrate it and write the
    §6.8.3 sentence — you cannot accept anything the gauntlet rejected.
    """
    try:
        offers = client().league.offers_report()
    except Exception as e:
        return _ok(error=f"could not read trade offers: {e}", offers=[])
    return _ok(
        count=len(offers),
        offers=[str(o) for o in offers],
        note="run_gauntlet(offer_id) for the full thirteen-gate result",
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
        auto=["set_lineup", "waiver_claim / add_drop", "propose_trade",
              "accept_trade (only on a clean §6.8 gauntlet)"],
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
    if gate.allowed and receipt:
        _notify("action", "Lineup set", f"{reason}\n{receipt}")
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
        _notify("action", f"{'Claimed' if kind is ActionKind.WAIVER_CLAIM else 'Added'} "
                          f"{add_p.name}",
                f"{reason}\ndropped: {drop_p.name if drop_p else '—'}\n{receipt}")
    return _ok(allowed=gate.allowed, refused_by=gate.refused_by,
               reason=gate.reason, receipt=str(receipt) if receipt else None)


@mcp.tool()
def reject_trade(offer_id: str, reason: str, cites: list[str]) -> str:
    """Reject an incoming offer. Always allowed — the default answer is no."""
    s = _snap()
    action = Action(kind=ActionKind.REJECT_TRADE, args={"offer_id": offer_id},
                    cites=cites or ["§6.8.0"], reason=reason)

    def perform():
        from core.browser import actions as A
        from core.browser.session import EspnSession

        with EspnSession(headless=True) as sess:
            return A.reject_trade(
                sess, s.facts.settings.league_id, s.facts.settings.season, offer_id
            )

    gate, receipt = write_gate.execute(action, perform, skip_health=True)
    return _ok(allowed=gate.allowed, reason=gate.reason,
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
