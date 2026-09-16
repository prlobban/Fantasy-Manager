"""The agent's memory of its own runs, and the pending-claim ledger.

Both exist because of 2026-09-15: a sweep placed a waiver claim at 15:04 and
the run seven minutes later escalated asking whether the add should be
refunded, because nothing told it what it had just done.
"""
from __future__ import annotations

import json

import pytest

from core.gates import rate_limits
from core.state import journal


@pytest.fixture
def jpath(tmp_path, monkeypatch):
    p = tmp_path / "journal.jsonl"
    monkeypatch.setattr(journal, "path", lambda: p)
    return p


# ── the journal ──────────────────────────────────────────────────────────────

def test_a_run_is_remembered(jpath):
    journal.record(week=2, task="daily", scope="waivers",
                   summary="claimed the Bucs", did=[{"what": "claim", "outcome": "pending"}],
                   escalated="add_drop is broken", open_threads=["confirm the claim Wednesday"])
    runs = journal.read()
    assert len(runs) == 1
    assert runs[0]["summary"] == "claimed the Bucs"
    assert runs[0]["did"][0]["outcome"] == "pending"


def test_escalations_are_deduped(jpath):
    """Three identical 'add_drop is broken' messages went out in one afternoon."""
    for _ in range(3):
        journal.record(week=2, task="daily", scope="all", summary="s",
                       escalated="add_drop is broken at the browser layer")
    assert len(journal.recent_escalations()) == 1


def test_open_threads_accumulate_without_duplicates(jpath):
    journal.record(week=2, task="daily", scope="all", summary="s",
                   open_threads=["confirm the Bucs claim", "McConkey rib Friday"])
    journal.record(week=2, task="daily", scope="lineup", summary="s",
                   open_threads=["confirm the Bucs claim"])
    assert journal.open_threads().count("confirm the Bucs claim") == 1


def test_packet_block_tells_the_agent_how_to_read_it(jpath):
    journal.record(week=2, task="daily", scope="all", summary="s")
    block = journal.for_packet()
    assert set(block) == {"runs", "already_escalated", "open_threads", "how_to_use"}
    assert "pending" in block["how_to_use"]


def test_a_torn_line_does_not_blind_the_agent(jpath):
    journal.record(week=2, task="daily", scope="all", summary="first")
    with jpath.open("a", encoding="utf-8") as fh:
        fh.write("{ not json\n")
    journal.record(week=2, task="daily", scope="all", summary="second")
    assert [e["summary"] for e in journal.read()] == ["first", "second"]


def test_recording_never_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(journal, "path", lambda: tmp_path / "no" / "such" / "x.jsonl")
    monkeypatch.setattr(journal.Path, "mkdir", lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    journal.record(week=1, task="t", scope="s", summary="x")  # must not raise


# ── the ledger ───────────────────────────────────────────────────────────────

@pytest.fixture
def ledger(tmp_path, monkeypatch):
    from core.state import store

    p = tmp_path / "state.json"
    p.write_text(json.dumps({}), encoding="utf-8")
    monkeypatch.setattr(store, "_path", lambda: p)
    return p


def test_a_claim_is_pending_not_landed(ledger):
    rate_limits.record_add(-16027, -16030, pending=True)
    assert len(rate_limits.pending_adds()) == 1
    # It still HOLDS a slot: we committed to it and cannot un-commit.
    assert rate_limits.adds_this_week() == 1


def test_a_free_agent_add_is_landed(ledger):
    rate_limits.record_add(17372, None)
    assert rate_limits.pending_adds() == []
    assert rate_limits.adds_this_week() == 1


def test_legacy_rows_without_status_count_as_landed(ledger):
    from core.state import store

    store.append("roster_adds", {"at": store.now_iso(), "add": 1, "drop": None})
    assert rate_limits.adds_this_week() == 1
    assert rate_limits.pending_adds() == []


def test_a_claim_that_lands_is_reconciled(ledger):
    rate_limits.record_add(-16027, -16030, pending=True)
    changed = rate_limits.reconcile({-16027}, set())
    assert changed and changed[0]["outcome"] == "landed"
    assert rate_limits.pending_adds() == []
    assert rate_limits.adds_this_week() == 1


def test_a_claim_still_on_waivers_stays_pending(ledger):
    rate_limits.record_add(-16027, -16030, pending=True)
    assert rate_limits.reconcile(set(), {-16027}) == []
    assert len(rate_limits.pending_adds()) == 1


def test_a_lost_claim_refunds_the_add(ledger):
    """Priority 9 of 10 — most claims lose. §5.3.1: priority is only spent on a
    successful claim, and the weekly add must work the same way."""
    rate_limits.record_add(-16027, -16030, pending=True)
    assert rate_limits.adds_this_week() == 1
    changed = rate_limits.reconcile(set(), set())   # neither ours nor on waivers
    assert changed and changed[0]["outcome"] == "lost"
    assert rate_limits.adds_this_week() == 0, "a lost claim must not keep costing an add"


def test_reconcile_is_idempotent(ledger):
    rate_limits.record_add(-16027, -16030, pending=True)
    rate_limits.reconcile(set(), set())
    assert rate_limits.reconcile(set(), set()) == []
