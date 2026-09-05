"""The in-season manager, built 2026-09-05 to Pearce's post-draft brief.

Guards the things that would make the manager quietly wrong: the flex-starter
drop defect, the three-adds-a-week cap, trade-before-drop, roster shape on a
three-TE roster, the research bridge's clamps, and the D8 reasoning contract.
"""

from __future__ import annotations

import json

import pytest

from agent import run as agent_run
from core.manager import research as R
from core.manager import roster as roster_mod
from core.manager import trades_out, waivers
from core.model.schema import InjuryStatus, Pos
from core.model.value import value_pool
from tests.test_lineup import pl, settings_10


def _roster_3te():
    """The 09-05 draft, roughly: three TEs, three RBs."""
    return [
        pl(1, Pos.QB, 20), pl(2, Pos.QB, 17),
        pl(3, Pos.RB, 14), pl(4, Pos.RB, 11), pl(5, Pos.RB, 8),
        pl(6, Pos.WR, 15), pl(7, Pos.WR, 12), pl(8, Pos.WR, 9),
        pl(9, Pos.TE, 10), pl(10, Pos.TE, 8), pl(11, Pos.TE, 7),
    ]


def _vals(roster, window="week"):
    s = settings_10()
    if window == "week":
        return value_pool(roster, s, window="week", week=9, weeks_remaining=6, current_week=9)
    return value_pool(roster, s, window="ros", weeks_remaining=6, current_week=9)


# ── roster shape (D5) ────────────────────────────────────────────────────────


def test_three_tight_ends_read_as_surplus_at_a_one_slot_position():
    roster = _roster_3te()
    shape = roster_mod.analyse(roster, _vals(roster, "ros"), settings_10())
    te = shape.by_pos[Pos.TE]
    assert te.verdict == "surplus"
    assert te.delta == 2
    assert {p.espn_id for p in te.surplus_players} == {10, 11}   # the two worst
    assert any("one-slot position" in n for n in shape.notes)


def test_three_running_backs_read_as_short():
    roster = _roster_3te()
    shape = roster_mod.analyse(roster, _vals(roster, "ros"), settings_10())
    # 2 RB slots + ~1/3 of the flex rounds to 2, plus 2 cover = 4 wanted; have 3.
    assert shape.by_pos[Pos.RB].verdict == "short"


def test_drop_order_cuts_the_surplus_tight_end_before_a_bench_wr():
    roster = _roster_3te()
    order = roster_mod.drop_order(roster, _vals(roster, "ros"), settings_10())
    first = order[0]
    # QB2 and TE2/TE3 are all surplus at one-slot positions; any of them first.
    assert first.espn_id in (2, 10, 11)
    ranks = {p.espn_id: i for i, p in enumerate(order)}
    assert max(ranks[2], ranks[10], ranks[11]) < ranks[8]     # bench WR after all of them


# ── the flex-starter drop defect (D4) ────────────────────────────────────────


def test_a_flex_starter_is_never_a_free_drop():
    """First live run, 2026-09-05: core proposed dropping Chuba Hubbard for a
    D/ST while starting him in the flex in the same plan."""
    roster = [
        pl(1, Pos.QB, 20), pl(3, Pos.RB, 14), pl(4, Pos.RB, 11),
        pl(6, Pos.WR, 15), pl(7, Pos.WR, 12), pl(9, Pos.TE, 10),
        pl(5, Pos.RB, 9.5, name="flex starter"),       # best flex
        pl(8, Pos.WR, 6, name="bench wr"),
    ]
    v = _vals(roster)
    drop, cost, why, _ = waivers.choose_drop(roster, v, settings_10())
    assert drop.name == "bench wr"
    assert cost == 0.0


def test_weekly_gain_uses_one_optimal_lineup_including_the_flex():
    roster = [
        pl(1, Pos.QB, 20), pl(3, Pos.RB, 14), pl(4, Pos.RB, 11),
        pl(6, Pos.WR, 15), pl(7, Pos.WR, 12), pl(9, Pos.TE, 10),
        pl(5, Pos.RB, 9.5),
    ]
    cand = pl(50, Pos.WR, 12.5)
    v = _vals(roster + [cand])
    gain, replaced = waivers.weekly_gain_for(cand, v[50], roster, v, settings_10())
    # He takes the flex from the 9.5 RB: +3.0, not +0.5 against WR2.
    assert gain == pytest.approx(3.0, abs=0.05)
    assert replaced is not None and replaced.espn_id == 5


# ── trade before drop (D4.5) ─────────────────────────────────────────────────


def test_a_marginal_add_is_held_when_the_drop_has_trade_value(monkeypatch):
    from core.model import priors as P

    roster = _roster_3te()
    ros = _vals(roster, "ros")
    # Make the drop candidate (TE3) obviously tradeable and the add marginal.
    with P.overridden(**{"season.trade_instead_of_drop_min_vor": -100.0,
                         "season.urgent_add_weekly_gain": 4.0}):
        fa = pl(60, Pos.WR, 9.6)      # +0.6 over the flex
        fv = _vals(roster + [fa])
        plan = waivers.build(roster, [fa], fv, settings_10(), waiver_priority=8,
                             bench_open=0, current_week=9, ros_valuations=ros)
    assert not plan.claims and not plan.free_adds
    assert any("D4.5" in why for _, why in plan.skipped)


def test_an_urgent_add_overrides_trade_before_drop():
    from core.model import priors as P

    roster = _roster_3te()
    ros = _vals(roster, "ros")
    with P.overridden(**{"season.trade_instead_of_drop_min_vor": -100.0,
                         "season.urgent_add_weekly_gain": 4.0}):
        fa = pl(60, Pos.RB, 20.0)     # a clear starter
        fv = _vals(roster + [fa])
        plan = waivers.build(roster, [fa], fv, settings_10(), waiver_priority=8,
                             bench_open=0, current_week=9, ros_valuations=ros)
    assert plan.free_adds and plan.free_adds[0].player.espn_id == 60


# ── the weekly cap (§5.7) ────────────────────────────────────────────────────


def test_adds_left_caps_the_plan(monkeypatch):
    roster = _roster_3te()
    ros = _vals(roster, "ros")
    fas = [pl(60 + i, Pos.RB, 18 - i) for i in range(5)]
    fv = _vals(roster + fas)
    plan = waivers.build(roster, fas, fv, settings_10(), waiver_priority=8,
                         bench_open=0, current_week=9, adds_left=1, ros_valuations=ros)
    assert len(plan.free_adds) + len(plan.claims) <= 1
    assert plan.adds_left == 1


def test_the_gate_refuses_a_fourth_add(tmp_path, monkeypatch):
    from core.gates import rate_limits, write_gate
    from core.model.schema import Action, ActionKind
    from core.state import store

    monkeypatch.setattr(store, "_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(write_gate.kill_switch, "is_on", lambda: True)
    for i in range(3):
        rate_limits.record_add(100 + i, None)
    assert rate_limits.adds_left() == 0
    a = Action(kind=ActionKind.ADD_DROP, args={"add": 1, "drop": 2, "roster_has_room": False},
               cites=["§5.2"], reason="x")
    g = write_gate.check(a, skip_health=True)
    assert not g.allowed and g.refused_by == "§5.7"


# ── trades: shape-driven (D5.2) ──────────────────────────────────────────────


def test_a_surplus_tight_end_generates_an_idea_without_a_hole_on_their_side():
    """The old generator needed the other team to have ZERO TEs. Nobody does."""
    ours = _roster_3te()
    theirs = [
        pl(21, Pos.QB, 19), pl(23, Pos.RB, 13), pl(24, Pos.RB, 12), pl(25, Pos.RB, 12),
        pl(26, Pos.WR, 14), pl(27, Pos.WR, 11), pl(28, Pos.TE, 4, name="their bad TE"),
        pl(29, Pos.RB, 10),
    ]
    v = _vals(ours + theirs, "ros")
    props = trades_out.build(ours, {2: ("them", theirs)}, v, settings_10())
    assert props, "a TE they would start for an RB we need should be an idea"
    p = props[0]
    assert any(x.pos is Pos.TE for x in p.give)
    assert p.our_gain > 0 and p.their_gain > 0
    assert "TE" in p.shape_effect


def test_value_check_refuses_a_one_sided_offer():
    ours = _roster_3te()
    theirs = [pl(21, Pos.QB, 19), pl(23, Pos.RB, 13), pl(24, Pos.RB, 12),
              pl(26, Pos.WR, 14), pl(27, Pos.WR, 11), pl(28, Pos.TE, 12)]
    v = _vals(ours + theirs, "ros")
    give = [next(p for p in ours if p.espn_id == 11)]     # our worst TE
    get = [next(p for p in theirs if p.espn_id == 23)]    # their RB1
    ok, why, *_ = trades_out.value_check(ours, theirs, give, get, v, settings_10())
    assert not ok and "§6.3" in why


# ── the research bridge ──────────────────────────────────────────────────────


def _raw(**over):
    base = {
        "espn_id": 5, "name": "p", "week": 9,
        "status": {"designation": "healthy", "practice": "full", "detail": ""},
        "usage": {"trend": "rising", "detail": "snaps 78%"},
        "matchup": {"read": "good", "detail": ""},
        "analyst_read": {"consensus": "start", "detail": ""},
        "news_since": [{"date": "2026-11-01", "headline": "h", "url": "https://espn.com/x"}],
        "week_multiplier": 1.2, "ros_multiplier": 1.0,
        "veto": False, "confidence": "high",
        "sources": ["https://www.espn.com/a", "https://www.nfl.com/b"],
    }
    base.update(over)
    return base


def test_a_weekly_multiplier_is_clamped_and_lands_as_a_named_context_term():
    d, _ = R.validate(_raw(week_multiplier=1.9))
    assert d.week_multiplier == pytest.approx(1.25)
    ctx = R.contexts({5: d}, window="week")
    assert ctx[5].multipliers["research_week"] == pytest.approx(1.25)


def test_a_big_weekly_move_on_one_host_is_dropped():
    d, _ = R.validate(_raw(week_multiplier=1.2, sources=["https://www.espn.com/a"]))
    assert d.week_multiplier == 1.0
    assert d.demotions


def test_an_out_designation_cannot_carry_a_boost():
    d, _ = R.validate(_raw(status={"designation": "out", "practice": "dnp", "detail": ""},
                           week_multiplier=1.1))
    assert d.week_multiplier == 1.0


def test_a_veto_needs_an_unstartable_designation():
    d, _ = R.validate(_raw(veto=True, veto_reason="x"))
    assert d.veto is False
    d2, _ = R.validate(_raw(veto=True, veto_reason="x",
                            status={"designation": "ir", "practice": "dnp", "detail": ""}))
    assert d2.veto is True
    assert R.contexts({5: d2}, window="week")[5].news_veto == "x"


def test_a_stale_dossier_is_ignored():
    d, problems = R.validate(_raw(researched_at="2026-01-01T00:00:00+00:00"))
    assert d is None and any("stale" in p for p in problems)


def test_unsourced_research_is_thrown_away():
    d, problems = R.validate(_raw(sources=["ESPN"]))
    assert d is None


# ── the D8 reasoning contract ────────────────────────────────────────────────


def _action(**over):
    a = {
        "tool": "set_lineup", "args": {}, "cites": ["§4.1"],
        "reason": "Start X over Y at flex: core projects 12.4 vs 9.1 this week.",
        "short_term": "About three more expected points this week.",
        "long_term": "No roster change; Y stays as bye cover for week 11.",
        "alternative": "Starting Y for his floor; rejected, we are underdogs.",
        "evidence": "Dossier: X 81% snaps, 24% targets last 3 (pff.com).",
        "would_be_wrong_if": "X is downgraded to out on Sunday morning.",
    }
    a.update(over)
    return a


def test_a_fully_reasoned_action_passes():
    payload = {"summary": "s", "roster_assessment": "fine", "actions": [_action()]}
    assert agent_run._validate(payload, "daily") == []


def test_an_action_without_long_term_reasoning_is_rejected():
    payload = {"summary": "s", "roster_assessment": "fine",
               "actions": [_action(long_term="")]}
    probs = agent_run._validate(payload, "daily")
    assert any("long_term" in p for p in probs)


def test_his_projection_is_higher_is_not_a_reason():
    payload = {"summary": "s", "roster_assessment": "fine",
               "actions": [_action(reason="His projection is higher.")]}
    probs = agent_run._validate(payload, "daily")
    assert any("'reason'" in p for p in probs)


def test_the_sweep_must_assess_the_roster_even_with_no_action():
    payload = {"summary": "s", "roster_assessment": "", "actions": [],
               "no_action_reason": "optimal"}
    probs = agent_run._validate(payload, "daily")
    assert any("roster_assessment" in p for p in probs)


def test_a_notify_is_exempt_from_the_contract():
    payload = {"summary": "s", "roster_assessment": "fine",
               "actions": [{"tool": "notify", "args": {}, "cites": ["§8.8"], "reason": "x"}]}
    assert agent_run._validate(payload, "daily") == []


def test_a_thin_lesson_is_rejected_on_tuesday():
    payload = {"result": "won", "efficiency_read": "ok", "calibration": [],
               "decision_grades": [], "lessons": ["we were right"], "prior_proposals": []}
    probs = agent_run._validate(payload, "tuesday")
    assert any("lesson 0" in p for p in probs)


def test_the_schemas_agree_with_the_validator():
    """The JSON schema and _validate must name the same reasoning fields."""
    s = json.loads((agent_run.SCHEMAS / "actions.json").read_text(encoding="utf-8"))
    required = set(s["properties"]["actions"]["items"]["required"])
    assert set(agent_run.REASONING_FIELDS) <= required
    assert "propose_trade" in s["properties"]["actions"]["items"]["properties"]["tool"]["enum"]


def test_the_doctrine_is_inlined_for_deciding_tasks():
    prompt = agent_run.build_system_prompt("daily")
    assert "THE DOCTRINE" in prompt and "D4.5" in prompt
    assert "D4.5" not in agent_run.build_system_prompt("weekly_dossier")


def test_ir_players_do_not_count_toward_shape():
    roster = _roster_3te()
    roster[10] = roster[10].model_copy(update={"injury_status": InjuryStatus.IR})
    shape = roster_mod.analyse(roster, _vals(roster, "ros"), settings_10())
    assert shape.by_pos[Pos.TE].delta == 1
