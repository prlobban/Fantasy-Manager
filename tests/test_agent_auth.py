"""The second credential — and why a silent one cost two days.

§8.5 exists because "espn_s2 dies without warning". The Claude session has
exactly that property and had no check at all. It expired 2026-09-09, and from
then on every task died in ~45ms with exit 1, an EMPTY stderr, and the real
sentence — "Failed to authenticate: OAuth session expired and could not be
refreshed" — on stdout, inside the JSON envelope, written only to a transcript.

Slack therefore said `claude exited 1:` twenty-eight times, $0.00, 0 min. The
alert fired perfectly and named nothing. These tests pin the three fixes: the
envelope's own message reaches the error string, an auth failure is its own
class so a pass can stop on it, and there is a pre-flight that answers the
question before 28 players are attempted.
"""

from __future__ import annotations

import json

from agent import run as agent_run

# The real envelope from data/agent-runs/20260911T123004-daily.json, trimmed.
EXPIRED = json.dumps({
    "type": "result", "subtype": "success", "is_error": True,
    "duration_ms": 45, "num_turns": 1, "total_cost_usd": 0,
    "terminal_reason": "api_error",
    "result": "Failed to authenticate: OAuth session expired and could not be refreshed",
})


# ── the envelope's message ───────────────────────────────────────────────────

def test_the_cli_message_is_read_out_of_stdout():
    assert agent_run._cli_message(EXPIRED) == (
        "Failed to authenticate: OAuth session expired and could not be refreshed")


def test_a_non_json_run_has_no_message_rather_than_a_crash():
    assert agent_run._cli_message("Killed\n") is None
    assert agent_run._cli_message("") is None
    assert agent_run._cli_message("[1, 2, 3]") is None


def test_a_blank_result_is_not_a_message():
    assert agent_run._cli_message(json.dumps({"result": "   "})) is None


# ── pre-flight ───────────────────────────────────────────────────────────────

class _Proc:
    def __init__(self, stdout: str, stderr: str = ""):
        self.stdout, self.stderr, self.returncode = stdout, stderr, 0


def test_signed_out_is_a_failure_that_names_the_fix(monkeypatch):
    monkeypatch.setattr(agent_run.subprocess, "run",
                        lambda *a, **k: _Proc(json.dumps(
                            {"loggedIn": False, "authMethod": "none"})))
    ok, detail = agent_run.auth_status()
    assert not ok
    assert "claude auth login" in detail


def test_signed_in_reports_the_account(monkeypatch):
    monkeypatch.setattr(agent_run.subprocess, "run",
                        lambda *a, **k: _Proc(json.dumps(
                            {"loggedIn": True, "email": "a@b.com",
                             "authMethod": "claude.ai", "subscriptionType": "pro"})))
    ok, detail = agent_run.auth_status()
    assert ok
    assert "a@b.com" in detail


def test_a_missing_cli_is_a_failure_not_an_exception(monkeypatch):
    def boom(*a, **k):
        raise OSError("No such file or directory: 'claude'")
    monkeypatch.setattr(agent_run.subprocess, "run", boom)
    ok, detail = agent_run.auth_status()
    assert not ok
    assert "could not run" in detail


def test_unreadable_status_fails_closed(monkeypatch):
    """§10.6 — an answer we cannot parse is not a pass."""
    monkeypatch.setattr(agent_run.subprocess, "run",
                        lambda *a, **k: _Proc("<html>proxy error</html>"))
    ok, _ = agent_run.auth_status()
    assert not ok
