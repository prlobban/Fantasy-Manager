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


# ── 2026-10-13: a 429 that read as a schema bug ──────────────────────────────

# The real envelope and stderr from data/agent-runs/20261013T123005-tuesday.json,
# trimmed. The run died on the weekly limit; stderr held two Ajv strictTypes
# WARNINGS (the same two every review since 09-08 printed and succeeded with),
# and the runner named those as the cause.
WEEKLY_LIMIT = json.dumps({
    "type": "result", "subtype": "success", "is_error": True,
    "duration_ms": 493, "num_turns": 1, "total_cost_usd": 0,
    "terminal_reason": "api_error", "api_error_status": 429,
    "result": "You've hit your weekly limit · resets 8am (America/Chicago)",
})
AJV_WARNINGS = (
    'strict mode: use allowUnionTypes to allow union type keyword at '
    '"#/properties/prior_proposals/items/properties/from" (strictTypes)\n')


class _Popen:
    def __init__(self, stdout, stderr, rc):
        self._out, self._err, self.returncode = stdout, stderr, rc

    def __call__(self, *a, **k):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def communicate(self, *a, **k):
        return self._out, self._err


def _run_with(monkeypatch, tmp_path, stdout, stderr="", rc=1):
    import dataclasses
    cfg = dataclasses.replace(agent_run.settings(), data_dir=tmp_path)
    monkeypatch.setattr(agent_run, "settings", lambda *a, **k: cfg)
    monkeypatch.setattr(agent_run.subprocess, "Popen", _Popen(stdout, stderr, rc))
    return agent_run.run("weekly_dossier", {"player": "test"})


def test_a_429_is_capacity_not_a_code_fault(monkeypatch, tmp_path):
    res = _run_with(monkeypatch, tmp_path, WEEKLY_LIMIT, AJV_WARNINGS)
    assert not res.ok
    assert res.error.startswith("capacity:")      # what stops a research pass
    assert "weekly limit" in res.error
    assert "strict mode" not in res.error


def test_repair_does_not_chase_a_capacity_failure():
    from core.repair.issues import NOT_CODE
    assert NOT_CODE.search("You've hit your weekly limit · resets 8am")
    assert NOT_CODE.search("capacity: rate limit (429): no message")


def test_the_envelope_names_the_cause_over_stderr_warnings(monkeypatch, tmp_path):
    out = json.dumps({"type": "result", "is_error": True, "result": "Prompt is too long"})
    res = _run_with(monkeypatch, tmp_path, out, AJV_WARNINGS)
    assert res.error == "claude exited 1: Prompt is too long"


def test_stderr_still_speaks_when_there_is_no_envelope(monkeypatch, tmp_path):
    res = _run_with(monkeypatch, tmp_path, "", "Error: unknown option '--bogus'\n")
    assert res.error == "claude exited 1: Error: unknown option '--bogus'"


# ── 2026-11-11: the model said "rate limit", the CLI did not ─────────────────

# The shape of data/agent-runs/20261111T133015-daily.json, trimmed: a clean
# run whose summary quoted §6.1's daily trade cap. It was discarded as capacity.
def _answered(summary: str) -> str:
    answer = {"actions": [], "summary": summary}
    return json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "terminal_reason": "completed", "api_error_status": None,
        "result": json.dumps(answer), "structured_output": answer,
    })


def test_a_clean_answer_that_quotes_a_marker_is_not_capacity(monkeypatch, tmp_path):
    out = _answered("no adds and no trades, both refused on value and the daily rate limit.")
    res = _run_with(monkeypatch, tmp_path, out, rc=0)
    assert not (res.error or "").startswith("capacity:")


def test_a_clean_answer_that_quotes_login_is_not_auth(monkeypatch, tmp_path):
    out = _answered("the beat writer said to please run /login on the team app")
    res = _run_with(monkeypatch, tmp_path, out, rc=0)
    assert not (res.error or "").startswith("auth:")


def test_a_flagged_capacity_notice_still_trips_without_a_429(monkeypatch, tmp_path):
    out = json.dumps({"type": "result", "is_error": True,
                      "result": "Claude AI usage limit reached|1794420000"})
    res = _run_with(monkeypatch, tmp_path, out, rc=0)
    assert res.error == "capacity: usage limit"


def test_a_plain_text_capacity_notice_still_trips(monkeypatch, tmp_path):
    res = _run_with(monkeypatch, tmp_path, "Claude AI usage limit reached\n", rc=0)
    assert res.error == "capacity: usage limit"


def test_running_out_of_turns_names_itself(monkeypatch, tmp_path):
    # data/agent-runs/20261111T130012-weekly_dossier.json, trimmed.
    out = json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": True,
                      "terminal_reason": "max_turns",
                      "errors": ["Reached maximum number of turns (7)"]})
    res = _run_with(monkeypatch, tmp_path, out)
    assert res.error == "claude exited 1: Reached maximum number of turns (7)"


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
