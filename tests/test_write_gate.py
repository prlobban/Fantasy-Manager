"""§8.2/§8.4 — the write gate. The most safety-critical tests in the repo.

Every one of these is a way the system could touch a live league when it
shouldn't. They run with health skipped (no cookies needed in CI) except where
the point of the test is the health check itself.
"""

from __future__ import annotations

import pytest

from core.gates import kill_switch, write_gate
from core.model.schema import Action, ActionKind, GateCheck, GauntletResult


@pytest.fixture
def enabled(tmp_path, monkeypatch):
    """A kill switch that is ON, isolated to this test."""

    p = tmp_path / "ENABLED"
    p.write_text("on\n", encoding="utf-8")
    monkeypatch.setattr(kill_switch, "path", lambda: p)
    return p


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    from core.state import store

    monkeypatch.setattr(store, "_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr(
        "core.state.decisions._path", lambda: tmp_path / "decisions.jsonl"
    )
    return tmp_path


def act(kind: ActionKind, **args) -> Action:
    return Action(kind=kind, args=args, cites=["§test"], reason="test")


# ── the kill switch ──────────────────────────────────────────────────────────


def test_kill_switch_off_refuses_every_write(tmp_path, monkeypatch, isolated_state):
    p = tmp_path / "ENABLED"
    p.write_text("off\n", encoding="utf-8")
    monkeypatch.setattr(kill_switch, "path", lambda: p)

    for kind in (ActionKind.SET_LINEUP, ActionKind.WAIVER_CLAIM,
                 ActionKind.PROPOSE_TRADE, ActionKind.ACCEPT_TRADE,
                 ActionKind.DRAFT_PICK, ActionKind.QUEUE_SYNC):
        r = write_gate.check(act(kind), skip_health=True)
        assert not r.allowed, kind
        assert r.refused_by == "§8.4"


def test_missing_kill_switch_file_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(kill_switch, "path", lambda: tmp_path / "nope")
    assert kill_switch.is_on() is False


def test_unreadable_kill_switch_fails_closed(tmp_path, monkeypatch):
    d = tmp_path / "adir"
    d.mkdir()
    monkeypatch.setattr(kill_switch, "path", lambda: d)  # a directory, not a file
    assert kill_switch.is_on() is False


def test_garbage_in_kill_switch_is_not_on(tmp_path, monkeypatch):
    p = tmp_path / "ENABLED"
    p.write_text("yes please\n", encoding="utf-8")
    monkeypatch.setattr(kill_switch, "path", lambda: p)
    assert kill_switch.is_on() is False


def test_notify_survives_the_kill_switch(tmp_path, monkeypatch, isolated_state):
    """A health failure is exactly when we most need to tell someone."""
    p = tmp_path / "ENABLED"
    p.write_text("off\n", encoding="utf-8")
    monkeypatch.setattr(kill_switch, "path", lambda: p)
    assert write_gate.check(act(ActionKind.NOTIFY)).allowed


# ── forbidden actions (§8.2) ─────────────────────────────────────────────────


def test_forbidden_actions_are_never_exposed(enabled, isolated_state):
    for name in write_gate.FORBIDDEN:
        assert name not in {k.value for k in ActionKind}, (
            f"{name} must not exist as an ActionKind — core should not be able "
            "to represent it, let alone perform it"
        )


# ── trade acceptance (§6.8) ──────────────────────────────────────────────────


def _gauntlet(passed: bool) -> GauntletResult:
    return GauntletResult(
        offer_id="o1",
        checks=[
            GateCheck(section="§6.8.1", name="margin", passed=True, detail=""),
            GateCheck(section="§6.8.2", name="both_sides", passed=passed, detail=""),
        ],
    )


def test_accept_without_a_gauntlet_is_refused(enabled, isolated_state):
    r = write_gate.check(
        act(ActionKind.ACCEPT_TRADE, offer_id="o1", from_team=3), skip_health=True
    )
    assert not r.allowed and r.refused_by == "§6.8"


def test_accept_with_a_failed_gauntlet_is_refused(enabled, isolated_state):
    r = write_gate.check(
        act(ActionKind.ACCEPT_TRADE, offer_id="o1", from_team=3,
            gauntlet=_gauntlet(False)),
        skip_health=True,
    )
    assert not r.allowed and r.refused_by == "§6.8"


def test_accept_with_a_clean_gauntlet_still_waits_out_the_cooldown(enabled, isolated_state):
    """§6.8.9 — a passing gauntlet is necessary, not sufficient."""
    r = write_gate.check(
        act(ActionKind.ACCEPT_TRADE, offer_id="o1", from_team=3,
            gauntlet=_gauntlet(True)),
        skip_health=True,
    )
    assert not r.allowed and r.refused_by == "§6.8.10"
    assert "cool-down" in r.reason


# ── outgoing trade rate limits (§6.1) ────────────────────────────────────────


def test_proposal_rate_limit_trips_after_the_daily_cap(enabled, isolated_state):
    from core.gates import rate_limits

    a = act(ActionKind.PROPOSE_TRADE, to_team=3, give=[1], get=[2])
    assert write_gate.check(a, skip_health=True).allowed

    rate_limits.record_proposal(3, [1], [2])
    r = write_gate.check(
        act(ActionKind.PROPOSE_TRADE, to_team=4, give=[9], get=[8]), skip_health=True
    )
    assert not r.allowed and r.refused_by == "§6.1"


def test_rejected_offer_cannot_be_reproposed(enabled, isolated_state):
    from core.gates import rate_limits

    rate_limits.record_rejection(3, [1], [2])
    r = write_gate.check(
        act(ActionKind.PROPOSE_TRADE, to_team=3, give=[1], get=[2]), skip_health=True
    )
    assert not r.allowed
    assert "rejected within" in r.reason


def test_offer_hash_ignores_player_order(isolated_state):
    from core.gates import rate_limits

    assert rate_limits.offer_hash([1, 2], [3], 5) == rate_limits.offer_hash([2, 1], [3], 5)
    assert rate_limits.offer_hash([1, 2], [3], 5) != rate_limits.offer_hash([1, 2], [4], 5)


# ── execute() plumbing ───────────────────────────────────────────────────────


def test_refused_action_never_calls_the_performer(tmp_path, monkeypatch, isolated_state):
    p = tmp_path / "ENABLED"
    p.write_text("off\n", encoding="utf-8")
    monkeypatch.setattr(kill_switch, "path", lambda: p)

    called = []
    gate, receipt = write_gate.execute(
        act(ActionKind.SET_LINEUP), lambda: called.append(1), skip_health=True
    )
    assert not gate.allowed
    assert called == [], "the browser must never be touched behind a closed gate"
    assert receipt is None


def test_dry_run_allows_but_does_not_perform(enabled, isolated_state):
    called = []
    gate, receipt = write_gate.execute(
        act(ActionKind.SET_LINEUP), lambda: called.append(1),
        skip_health=True, dry_run=True,
    )
    assert gate.allowed and called == [] and receipt is None


def test_successful_execute_logs_a_decision_with_its_prediction(enabled, isolated_state):
    from core.state import decisions

    gate, receipt = write_gate.execute(
        act(ActionKind.SET_LINEUP), lambda: "receipt-1",
        predicted={"projected_gain": 4.2}, skip_health=True,
    )
    assert gate.allowed and receipt == "receipt-1"
    recs = decisions.read_all()
    assert recs and recs[-1]["executed"] is True
    # §7.1 — the prediction must be on the record, or Tuesday can't grade it.
    assert recs[-1]["predicted"]["projected_gain"] == 4.2


def test_failed_execution_is_logged_then_reraised(enabled, isolated_state):
    from core.state import decisions

    def boom():
        raise RuntimeError("selector not found")

    with pytest.raises(RuntimeError):
        write_gate.execute(act(ActionKind.SET_LINEUP), boom, skip_health=True)

    recs = decisions.read_all()
    assert recs[-1]["executed"] is False
    assert "selector not found" in recs[-1]["gate"]["reason"]
