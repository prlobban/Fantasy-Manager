"""The situation packet: everything the agent needs, as one JSON object.

§10.4 — the agent never computes a number. Anything it might want to work out
arrives here already computed by `core`, so there is exactly one implementation
of the model and the two halves cannot disagree.

Deliberately includes the things a model is tempted to skip: what is MISSING
from each valuation, what the rate limits currently allow, and what the gates
would refuse. A model that can see the constraints does not have to guess at
them.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from core.espn import league_state as ls_mod
from core.gates import kill_switch
from core.model.priors import priors
from core.model.value import value_pool
from core.state import store

log = logging.getLogger(__name__)


def _player_row(p, v) -> dict[str, Any]:
    return {
        "espn_id": p.espn_id,
        "name": p.name,
        "pos": p.pos.value,
        "team": p.pro_team,
        "status": p.injury_status.value,
        "bye": p.bye_week,
        "proj": round(v.points, 2) if v else None,
        "vor": round(v.vor, 2) if v else None,
        "tier": v.tier if v else None,
        "stdev": v.stdev if v else None,
        "bust_rate": v.bust_rate if v else None,
        "availability": v.availability if v else None,
        # §8.8 — the model is told what it does NOT know.
        "missing": v.missing if v else ["not valued"],
    }


def dossier_packet(player, val, *, adp: float | None = None) -> dict[str, Any]:
    """§3.2 — one player, for one research agent.

    Deliberately tiny. This packet is sent once per player in the pool, and
    every field in it is paid for on every turn of that agent's loop. It
    carries what the researcher needs to know who he is and what the model
    currently believes — nothing about the draft, the league, or us.
    """
    row = _player_row(player, val)
    row["adp"] = adp if adp is not None else player.espn_adp
    return {
        "task": "dossier",
        "as_of": datetime.now(UTC).date().isoformat(),
        "player": row,
        "what_the_model_believes": (
            f"ESPN projects {row['proj']} points; the engine values him at "
            f"{row['vor']} over replacement, tier {row['tier']}, with an "
            f"availability discount of {row['availability']}."
        ),
    }


def weekly_dossier_packet(player, val, *, week: int, role: str) -> dict[str, Any]:
    """D1 / D3.1 — one player, one morning, for one research agent.

    `role` tells the researcher why we care (roster / waiver / trade) so the
    analyst read is aimed at the right question — start-sit for a starter,
    role and volume for a waiver target.
    """
    row = _player_row(player, val)
    return {
        "task": "weekly_dossier",
        "as_of": datetime.now(UTC).date().isoformat(),
        "nfl_week": week,
        "why_we_care": role,
        "player": row,
        "what_the_model_believes": (
            f"ESPN projects {row['proj']} points this week; the engine values him at "
            f"{row['vor']} over replacement for the week, tier {row['tier']}, status "
            f"{row['status']}."
        ),
    }


def judge_packet(plan, room, *, for_overall: int, budget_s: float,
                 dossiers: dict, board_by_id: dict, recent_picks: list) -> dict[str, Any]:
    """§3.10 — one pick's worth of context for the judge.

    Carries `core`'s ranking with every adjustment named, each candidate's
    survival odds, and the dossier for each. The levers are stated in the
    packet as well as the prompt so the model cannot claim not to know them,
    and `dossier: null` is sent explicitly — an unknown player must read as
    unknown, not as absent.
    """
    from core.draft.survival import p_survives

    n = int(priors().get("judge.candidates"))
    picks_until = plan.picks_until_next

    cands = []
    for i, c in enumerate(plan.top(n), 1):
        pid = c.player.espn_id
        d = dossiers.get(pid)
        cands.append({
            "rank": i,
            **_player_row(c.player, c.valuation),
            "adp": c.player.espn_adp,
            "score": round(c.score, 2),
            "adjustments": {k: round(v, 2) for k, v in c.reasons.items()},
            "note": c.note,
            "p_survives_to_our_next_pick": round(
                p_survives(c.player.espn_adp, c.player.adp_stdev, picks_until), 3),
            "dossier": ({
                "durability": d.durability,
                "role": d.role,
                "news_since": d.news_since,
                "projection_check": d.projection_check,
                "multiplier_applied": d.multiplier,
                "confidence": d.confidence,
                "sources": d.sources,
                "demoted_claims": d.demotions,
            } if d else None),
        })

    have = {p.value: n_ for p, n_ in room.my_positions.items()}
    return {
        "task": "judge",
        "for_overall": for_overall,
        "round": plan.round_num,
        "picks_until_our_next_turn": picks_until,
        "budget_seconds": round(budget_s),
        "levers": {
            "veto": True,
            "reorder_within_tier": True,
            "promote_across_tiers": False,
            "max_vetoes": int(priors().get("judge.max_vetoes_per_turn")),
            "max_reorders": int(priors().get("judge.max_reorders_per_turn")),
        },
        "our_roster": have,
        "run_on": plan.run_on.value if plan.run_on else None,
        "position_outlooks": {
            pos.value: {
                "cost_of_waiting": round(o.cost, 2),
                "best_now": round(o.best_now, 2),
                "expected_next": round(o.expected_next, 2),
                "top_tier": o.top_tier,
                "top_tier_remaining": o.top_tier_remaining,
            }
            for pos, o in plan.outlooks.items()
        },
        "candidates": cands,
        "last_picks_in_the_room": [
            {"overall": pk.overall, "name": pk.name,
             "pos": pk.pos.value if pk.pos else None}
            for pk in recent_picks[-5:]
        ],
        "dossier_coverage": (
            f"{sum(1 for c in cands if c['dossier'])}/{len(cands)} candidates "
            "have a dossier"),
    }


def build(task: str, state: ls_mod.LeagueState | None = None,
          *, scope: str = "all") -> dict[str, Any]:
    """`scope` narrows a daily run: "lineup" (the Sunday pass) omits the
    waiver plan and tells the agent to leave waivers and trades alone."""
    from core.gates import rate_limits
    from core.manager import research as R
    from core.manager import roster as roster_mod
    from core.state import lessons

    st = state or ls_mod.snapshot()
    weeks_left = max(1, st.facts.settings.regular_season_weeks - st.week + 1)

    # This morning's research, folded into BOTH windows in code (D1.4): the
    # weekly multiplier is a named §2.7 term, the ROS one is the bounded
    # override. The agent then reads the facts, not just the number.
    dossiers = R.load_all(week=st.week)
    vals = value_pool(
        st.all_players(), st.facts.settings, window="week", week=st.week,
        weeks_remaining=weeks_left, current_week=st.week,
        contexts=R.contexts(dossiers, window="week"),
    )
    ros_vals = value_pool(
        st.all_players(), st.facts.settings, window="ros",
        weeks_remaining=weeks_left, current_week=st.week,
        contexts=R.contexts(dossiers, window="ros"),
        override_cap=float(priors().get("model.override_cap")),
    )

    me = st.me
    shape = roster_mod.analyse(me.roster, ros_vals, st.facts.settings)
    day_left, week_left = rate_limits.proposals_left()
    packet: dict[str, Any] = {
        "task": task,
        "scope": scope,
        "as_of": st.taken_at.isoformat(),
        "league": {
            "name": st.facts.settings.name,
            "teams": st.facts.settings.team_count,
            "week": st.week,
            "scoring": f"{st.facts.settings.ppr_value} PPR",
            "starting_slots": {
                s.name: s.count for s in st.facts.settings.starting_slots
            },
            "bench": st.facts.settings.bench_count,
            "position_caps": {p.value: n for p, n in st.facts.position_limits.items()},
            "waivers": (
                "ROLLING PRIORITY (not FAAB)"
                if not st.facts.is_faab else f"FAAB, budget {st.facts.settings.faab_budget}"
            ),
            "playoff_weeks": st.facts.settings.playoff_weeks,
            "playoff_seeding": st.facts.playoff_seeding_rule,
            "trade_deadline": st.facts.settings.trade_deadline,
        },
        "us": {
            "team": me.name,
            "record": f"{me.wins}-{me.losses}",
            "points_for": me.points_for,
            "waiver_priority": me.waiver_priority,
            "bench_open": st.bench_open,
            "roster": [
                {**_player_row(p, vals.get(p.espn_id)),
                 "slot": me.slots.get(p.espn_id),
                 "ros_proj": round(ros_vals[p.espn_id].points, 1) if p.espn_id in ros_vals else None,
                 "ros_vor": round(ros_vals[p.espn_id].vor, 1) if p.espn_id in ros_vals else None}
                for p in me.roster
            ],
        },
        "roster_shape": {
            "summary": shape.summary(),
            "by_position": {
                pos.value: {"have": s.have, "starters": s.starters, "cover": s.cover,
                            "delta": s.delta, "verdict": s.verdict,
                            "surplus_players": [p.name for p in s.surplus_players]}
                for pos, s in shape.by_pos.items()
            },
            "notes": shape.notes,
        },
        "research": {
            "dossiers": R.facts(dossiers),
            "coverage": f"{sum(1 for p in me.roster if p.espn_id in dossiers)}/{len(me.roster)} "
                        f"of our roster researched this morning; "
                        f"{len(dossiers)} dossiers total",
            "note": "multipliers already applied to the valuations above (D1.4); read the facts",
        },
        "lessons": lessons.read(),
        "guardrails": {
            "kill_switch": kill_switch.state(),
            "writes_allowed": [
                "set_lineup", "add_drop", "propose_trade", "accept_trade (13/13 gauntlet only)",
                "reject_trade", "notify",
            ],
            "never": ["counter-offer", "league settings", "chat", "other teams"],
            "note": ("an action without a § citation or the six D8 reasoning fields "
                     "is rejected before it executes"),
        },
        "rate_limits": {
            "adds_left_this_week": rate_limits.adds_left(),
            "proposals_left_today": day_left,
            "proposals_left_this_week": week_left,
            "recent_adds": (store.load().get("roster_adds") or [])[-3:],
            "recent_proposals": (store.load().get("trade_proposals") or [])[-3:],
            "recent_accepts": (store.load().get("trade_accepts") or [])[-3:],
        },
        "thresholds": priors().as_dict(),
    }

    if st.opponent:
        from core.manager import lineup as lu

        opp_plan = lu.optimal_lineup(
            st.opponent.roster, vals, st.facts.settings, week=st.week
        )
        packet["opponent"] = {
            "team": st.opponent.name,
            "record": f"{st.opponent.wins}-{st.opponent.losses}",
            "projected": round(opp_plan.projected_points, 1),
            "roster": [
                _player_row(p, vals.get(p.espn_id)) for p in st.opponent.roster
            ],
        }

    if task in ("daily", "tuesday"):
        from core.manager import lineup as lu
        from core.manager import waivers as w

        opp_proj = packet.get("opponent", {}).get("projected")
        plan = lu.build(
            me.roster, vals, st.facts.settings,
            opponent_projected=opp_proj,
            current_starters=me.starters,
            week=st.week,
        )
        packet["lineup_plan"] = {
            "projected": round(plan.projected_points, 1),
            "variance_mode": plan.variance_mode,
            "margin": round(plan.margin, 1) if plan.margin is not None else None,
            "assignments": [
                {"slot": a.slot,
                 "player": a.player.name if a.player else None,
                 "espn_id": a.player.espn_id if a.player else None,
                 "points": round(a.points, 1)}
                for a in plan.assignments
            ],
            "changes": [
                {"player": p.name, "espn_id": p.espn_id, "from": f, "to": t, "why": why}
                for p, f, t, why in plan.changes
            ],
            "notes": plan.notes,
        }

    if task in ("daily", "tuesday") and scope in ("all", "waivers"):
        from core.manager import waivers as w

        wplan = w.build(
            me.roster, st.free_agents, vals, st.facts.settings,
            waiver_priority=me.waiver_priority,
            on_waivers=st.on_waivers,
            bench_open=st.bench_open,
            current_week=st.week,
            adds_left=rate_limits.adds_left(),
            ros_valuations=ros_vals,
        )

        def _cand(c):
            return {"name": c.player.name, "espn_id": c.player.espn_id,
                    "pos": c.player.pos.value, "gain_per_week": round(c.net_gain, 2),
                    "replaces": c.replaces.name if c.replaces else None,
                    "drop": c.drop.name if c.drop else None,
                    "drop_id": c.drop.espn_id if c.drop else None,
                    "drop_tradeable": c.drop_tradeable,
                    "archetype": c.archetype, "why": c.reasons}

        packet["waiver_plan"] = {
            "priority": wplan.priority,
            "adds_left_this_week": wplan.adds_left,
            "free_adds": [_cand(c) for c in wplan.free_adds],
            "claims": [_cand(c) for c in wplan.claims],
            # The skips are decisions too, and often the right one.
            "skipped": [{"name": c.player.name, "why": why}
                        for c, why in wplan.skipped[:10]],
            "notes": wplan.notes,
        }

    if task in ("daily", "tuesday") and scope in ("all", "trades"):
        from core.manager import trades_out as T

        others = {tid: (t.name, t.roster) for tid, t in st.teams.items()
                  if tid != st.my_team_id}
        props = T.build(me.roster, others, ros_vals, st.facts.settings)
        packet["trade_ideas"] = {
            "proposals_left_today": day_left,
            "proposals_left_this_week": week_left,
            "ideas": [{
                "to_team": p.to_team, "to_team_name": p.to_team_name,
                "give": [{"espn_id": x.espn_id, "name": x.name, "pos": x.pos.value} for x in p.give],
                "get": [{"espn_id": x.espn_id, "name": x.name, "pos": x.pos.value} for x in p.get],
                "our_gain": p.our_gain, "their_gain": p.their_gain,
                "fairness": p.fairness, "shape_effect": p.shape_effect,
                "rationale": p.rationale, "warnings": p.warnings,
            } for p in props],
        }

    if task == "tuesday":
        from core.espn.client import client
        from core.manager import tuesday as tue

        packet["review"] = tue.build_section(client(), st, ros_vals)

    return packet


def main() -> int:
    import argparse
    import json

    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("task", default="daily", nargs="?")
    args = ap.parse_args()
    print(json.dumps(build(args.task), indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
