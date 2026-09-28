"""The guard between an unattended fix and the live league.

Every test here is a way a plausible repair could do damage and still look
green: editing its own guard, weakening a test, adding a write tool, shipping
a fix with no reproduction.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from core.repair import guard
from core.repair import issues as I


def _diff(path: str, added: list[str] = (), removed: list[str] = (), *, deleted=False) -> str:
    head = f"diff --git a/{path} b/{path}\n"
    if deleted:
        head += "deleted file mode 100644\n"
    body = "--- a/x\n+++ b/x\n@@ -1 +1 @@\n"
    body += "".join(f"-{r}\n" for r in removed) + "".join(f"+{a}\n" for a in added)
    return head + body


NEW_TEST = _diff("tests/test_x.py", ["def test_the_bug_stays_fixed():", "    assert True"])


def test_a_clean_fix_with_a_new_test_passes_the_diff_rules():
    v = guard.check_diff(_diff("core/manager/waivers.py", ["x = 1"], ["x = 0"]) + NEW_TEST)
    assert v.ok, v.problems
    assert v.new_tests == ["tests/test_x.py::test_the_bug_stays_fixed"]


def test_the_gates_are_never_touched():
    v = guard.check_diff(_diff("core/gates/write_gate.py", ["pass"]) + NEW_TEST)
    assert not v.ok and "protected" in v.problems[0]


def test_the_guard_cannot_edit_itself():
    for p in ("core/repair/guard.py", "scripts/repair.py", "scripts/cron_manage.sh"):
        assert guard.is_protected(p), p


def test_doctrine_priors_prompts_and_the_kill_switch_are_protected():
    for p in ("docs/fantasy-playbook.md", "priors.yaml", "agent/prompts/daily.md",
              "agent/schemas/actions.json", "ENABLED", ".env", "core/espn/health.py",
              "core/manager/gauntlet.py"):
        assert guard.is_protected(p), p
    assert not guard.is_protected("core/manager/waivers.py")


def test_tests_only_grow():
    """The cheapest way to make a suite pass is to delete the failing assert."""
    v = guard.check_diff(_diff("tests/test_lineup.py", ["    assert x"], ["    assert y"])
                         + NEW_TEST + _diff("core/a.py", ["x"]))
    assert not v.ok and any("tests only grow" in p for p in v.problems)


def test_deleting_a_test_file_is_refused():
    v = guard.check_diff(_diff("tests/test_old.py", [], ["def test_a(): pass"], deleted=True)
                         + NEW_TEST)
    assert not v.ok


def test_a_fix_with_no_regression_test_is_refused():
    v = guard.check_diff(_diff("core/a.py", ["x = 1"]))
    assert not v.ok and any("regression test" in p for p in v.problems)


def test_a_redesign_is_not_a_fix():
    v = guard.check_diff(_diff("core/a.py", [f"x{i} = 1" for i in range(guard.MAX_CHANGED_LINES + 1)])
                         + NEW_TEST)
    assert not v.ok


SURFACE = '''
@mcp.tool()
def get_roster(): ...

@mcp.tool()
def set_lineup(moves): ...

def _helper(): ...
'''


def test_the_write_table_is_read_by_ast():
    assert guard.tool_surface(SURFACE) == {"get_roster", "set_lineup"}


def test_adding_a_write_tool_is_refused():
    fixed = SURFACE + "\n@mcp.tool()\ndef accept_everything(): ...\n"
    assert guard.check_surface(SURFACE, fixed)


def test_removing_a_tool_is_refused_and_changing_its_body_is_not():
    assert guard.check_surface(SURFACE, SURFACE.replace("@mcp.tool()\ndef get_roster", "def get_roster"))
    assert guard.check_surface(SURFACE, SURFACE.replace("(moves): ...", "(moves):\n    return 1")) == []


# ── issues ───────────────────────────────────────────────────────────────────


def test_operational_faults_are_not_sent_to_repair(tmp_path):
    j = tmp_path / "journal.jsonl"
    now = datetime.now(UTC)
    j.write_text(json.dumps({"at": now.isoformat(),
                             "escalated": "claude is NOT logged in on this host"}) + "\n")
    assert I.collect(j, "", since=now - timedelta(minutes=5)) == []


def test_an_escalation_and_a_traceback_are_collected(tmp_path):
    j = tmp_path / "journal.jsonl"
    now = datetime.now(UTC)
    j.write_text(json.dumps({
        "at": now.isoformat(), "escalated": "WR slot left empty",
        "did": [{"what": "reject trade", "outcome": "refused execution failed"}],
    }) + "\n")
    log = "INFO fine\nTraceback (most recent call last)\n  File x\nKeyError: 3\n"
    got = I.collect(j, log, since=now - timedelta(minutes=5))
    assert [i.source for i in got] == ["escalation", "write", "log"]


def test_an_old_journal_entry_is_not_this_runs_fault(tmp_path):
    j = tmp_path / "journal.jsonl"
    old = datetime.now(UTC) - timedelta(days=1)
    j.write_text(json.dumps({"at": old.isoformat(), "escalated": "stale"}) + "\n")
    assert I.collect(j, "", since=datetime.now(UTC) - timedelta(minutes=5)) == []


def test_the_same_fault_restated_with_new_numbers_has_the_same_key():
    a = I.Issue("escalation", "Broncos D/ST 3.8 vs Giants 6.7")
    b = I.Issue("escalation", "Broncos D/ST 3.9 vs Giants 6.8")
    assert a.key == b.key


def test_two_tries_a_week_then_it_is_pearces():
    i = I.Issue("escalation", "x broke")
    now = datetime.now(UTC)
    one = {"at": now.isoformat(), "status": "rejected", "issues": [{"key": i.key}]}
    assert I.fresh([i], [one]) == [i]
    assert I.fresh([i], [one, dict(one, status="deployed")]) == []
    old = dict(one, at=(now - timedelta(days=8)).isoformat())
    assert I.fresh([i], [old, old]) == [i]


def test_a_deploy_is_pending_until_proven_or_reverted():
    d = {"status": "deployed", "commit": "abc"}
    assert I.pending_deploy([d]) == d
    assert I.pending_deploy([d, {"status": "proven"}]) is None
    assert I.pending_deploy([d, {"status": "reverted"}]) is None
    assert I.pending_deploy([d, {"status": "no_fix"}]) == d


def test_the_daily_cap_counts_attempts_not_proofs():
    now = datetime.now(UTC)
    led = [{"at": now.isoformat(), "status": s} for s in ("deployed", "proven", "rejected")]
    assert I.attempts_today(led, now) == 2


def test_the_managers_own_words_are_not_a_crash(tmp_path):
    """2026-09-27: the first live run sent a Polaris chat reply to the repair
    agent as a fault — it said the trade 'fails' a gate."""
    log = ('}INFO NOTIFY [info] Re: your message | rejected on 09-18 under §6.8.0, '
           'which FAILED: six gates failed\n'
           '  "escalate": "set_lineup execution failed twice",\n')
    assert I.collect(tmp_path / "none.jsonl", log, since=datetime.now(UTC)) == []


def test_a_real_crash_marker_is_still_a_fault(tmp_path):
    log = "2026-09-27T07:33:59-05:00 ⚠️ sweep FAILED (rc=1)\n"
    got = I.collect(tmp_path / "none.jsonl", log, since=datetime.now(UTC))
    assert [i.source for i in got] == ["log"]
