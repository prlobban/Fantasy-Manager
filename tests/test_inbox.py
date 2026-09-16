"""§8.9 / D10 — the inbound half of #fantasy.

The tests that matter here are not "does it parse a message". They are the
three ways this feature goes wrong quietly: reading its own posts as
instructions, reporting an outage as silence, and consuming a message the run
then failed to act on.
"""

from __future__ import annotations

import json

import pytest

from core import inbox


@pytest.fixture(autouse=True)
def _no_pytest_shortcut(monkeypatch):
    # `read()` no-ops under pytest so a stray test can never hit the real
    # channel. These tests exercise the body, so the guard is lifted and the
    # HTTP layer is stubbed instead.
    monkeypatch.setattr(inbox, "_suppressed", lambda: False)
    monkeypatch.setattr(inbox, "_token", lambda: "xoxb-test")


def _stub(monkeypatch, history, replies=None):
    replies = replies or {}

    def fake(method, token, **params):
        if method == "conversations.history":
            return {"ok": True, "messages": history}
        if method == "conversations.replies":
            return {"ok": True, "messages": replies.get(params["ts"], [])}
        raise AssertionError(method)

    monkeypatch.setattr(inbox, "_call", fake)


def test_reads_human_messages_since_the_cursor(monkeypatch):
    _stub(monkeypatch, [
        {"ts": "100.0", "user": "U1", "text": "old news"},
        {"ts": "200.0", "user": "U1", "text": "don't trade Skattebo"},
    ])
    box = inbox.read(since="150.0")
    assert box.ok
    assert [m.text for m in box.messages] == ["don't trade Skattebo"]


def test_polaris_never_reads_its_own_posts(monkeypatch):
    # The feedback loop this guard exists to prevent: the digest is not input.
    _stub(monkeypatch, [
        {"ts": "300.0", "bot_id": "B1", "text": "✅ set lineup"},
        {"ts": "301.0", "subtype": "channel_join", "user": "U1", "text": "joined"},
        {"ts": "302.0", "user": "U1", "text": "why did you bench Allen?"},
    ])
    box = inbox.read(since="0")
    assert [m.text for m in box.messages] == ["why did you bench Allen?"]


def test_thread_replies_are_found(monkeypatch):
    # history does not return replies. Answering the digest in its thread is
    # the most natural thing to do, so it has to work.
    _stub(monkeypatch,
          [{"ts": "400.0", "bot_id": "B1", "text": "Week 2 sweep", "reply_count": 2}],
          {"400.0": [
              {"ts": "400.0", "bot_id": "B1", "text": "Week 2 sweep"},
              {"ts": "401.0", "bot_id": "B1", "text": "a bot reply"},
              {"ts": "402.0", "user": "U1", "text": "start Tuten instead"},
          ]})
    box = inbox.read(since="399.0")
    assert [(m.text, m.in_thread) for m in box.messages] == [("start Tuten instead", True)]


def test_messages_come_back_in_time_order(monkeypatch):
    _stub(monkeypatch,
          [{"ts": "500.0", "user": "U1", "text": "second", "reply_count": 1},
           {"ts": "450.0", "user": "U1", "text": "first"}],
          {"500.0": [{"ts": "510.0", "user": "U1", "text": "third"}]})
    box = inbox.read(since="0")
    assert [m.text for m in box.messages] == ["first", "second", "third"]


def test_an_outage_is_an_error_not_silence(monkeypatch):
    # The whole point: the agent must be able to tell "he said nothing" from
    # "we could not find out whether he said anything" (§8.8).
    monkeypatch.setattr(inbox, "_call",
                        lambda *a, **k: {"ok": False, "error": "missing_scope"})
    box = inbox.read(since="0")
    assert not box.ok
    assert "channels:history" in box.error
    assert box.messages == []


def test_a_raised_exception_is_caught_and_reported(monkeypatch):
    def boom(*a, **k):
        raise TimeoutError("slack is down")

    monkeypatch.setattr(inbox, "_call", boom)
    box = inbox.read(since="0")
    assert not box.ok and "slack is down" in box.error


def test_unconfigured_slack_reports_rather_than_pretending(monkeypatch):
    monkeypatch.setattr(inbox, "_token", lambda: None)
    box = inbox.read(since="0")
    assert not box.ok and "not configured" in box.error


def test_truncates_a_flood(monkeypatch):
    _stub(monkeypatch, [{"ts": f"{600 + i}.0", "user": "U1", "text": f"m{i}"}
                        for i in range(40)])
    box = inbox.read(since="0")
    assert len(box.messages) == inbox.MAX_MESSAGES
    assert box.messages[-1].text == "m39"


def test_cursor_round_trips(tmp_path, monkeypatch):
    monkeypatch.setattr(inbox, "_state_path", lambda: tmp_path / "slack_inbox.json")
    assert inbox.cursor() == "0"
    inbox.mark_read("700.5")
    assert inbox.cursor() == "700.5"
    assert json.loads((tmp_path / "slack_inbox.json").read_text())["last_ts"] == "700.5"


def test_mark_read_ignores_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(inbox, "_state_path", lambda: tmp_path / "slack_inbox.json")
    inbox.mark_read("800.0")
    inbox.mark_read("")
    inbox.mark_read("0")
    assert inbox.cursor() == "800.0"


def test_a_corrupt_cursor_reads_fresh_rather_than_crashing(tmp_path, monkeypatch):
    p = tmp_path / "slack_inbox.json"
    p.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(inbox, "_state_path", lambda: p)
    assert inbox.cursor() == "0"


def test_a_fresh_cursor_asks_for_the_lookback_window_not_zero(monkeypatch):
    # Slack rejects oldest=0 outright (invalid_ts_oldest), and if it did not,
    # a first run would have dredged the entire channel. Caught on the box.
    seen = {}

    def fake(method, token, **params):
        seen[method] = params
        return {"ok": True, "messages": []}

    monkeypatch.setattr(inbox, "_call", fake)
    inbox.read(since="0")
    assert float(seen["conversations.history"]["oldest"]) > 0


def test_an_old_cursor_wins_over_the_lookback_floor(monkeypatch):
    # A cursor older than the floor must still be honoured, or a run that was
    # down for three days silently drops what he said on day one.
    seen = {}

    def fake(method, token, **params):
        seen[method] = params
        return {"ok": True, "messages": []}

    monkeypatch.setattr(inbox, "_call", fake)
    inbox.read(since="1000.0")
    assert float(seen["conversations.history"]["oldest"]) == 1000.0
