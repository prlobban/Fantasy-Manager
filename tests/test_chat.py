"""§8.10 — league chat in, drafts out, nothing sent without Pearce.

The tests that matter are the quiet failures: a message sent that he never
approved, a sweep woken for history or for a burst still being typed, an
approval thread read as a question, and a reply drafted over his own.
"""

from __future__ import annotations

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from core import chat_drafts
from core.espn import chat

ME = "{ME-SWID}"
JOSH = "{JOSH-SWID}"
NOW = datetime(2026, 10, 5, 18, 0, tzinfo=UTC)
NOW_MS = int(NOW.timestamp() * 1000)
MIN = 60_000


def _msg(author, text, ago_min, mid=None, type_id=0):
    return {"author": author, "content": text, "date": NOW_MS - ago_min * MIN,
            "id": mid or f"m{ago_min}{text[:3]}", "messageTypeId": type_id, "isDeleted": False}


def _raw(*dm_msgs, league_msgs=(), activity=()):
    topics = [{"id": "dm1", "type": "CHAT_DIRECT_MESSAGE", "viewableBy": [JOSH, ME],
               "messages": list(dm_msgs)}]
    if league_msgs:
        topics.append({"id": "lg", "type": "CHAT_ALL_MEMBERS", "messages": list(league_msgs)})
    if activity:
        topics.append({"id": "act", "type": "ACTIVITY_TRANSACTIONS", "messages": list(activity)})
    return {"topics": topics}


def _parse(raw):
    return chat.parse(raw, me=ME, members={JOSH: "Josh Perkowski (McKenfrey)"},
                      team_names={6: "McKenfrey", 8: "big P"},
                      player_names={3916148: "Aaron Jones"}, now_ms=NOW_MS)


# ── reading ──────────────────────────────────────────────────────────────────


def test_parses_a_dm_with_names_and_direction():
    c = _parse(_raw(_msg(JOSH, "I'll give you Aaron jones", 300),
                    _msg(ME, "let me think on it", 200)))
    t = c.topic("dm1")
    assert t.kind == "dm" and t.with_names == ["Josh Perkowski (McKenfrey)"]
    assert [ln.from_us for ln in t.lines] == [False, True]
    assert not t.awaiting_us()
    assert [ln.text for ln in c.our_lines] == ["let me think on it"]


def test_their_newer_line_means_a_reply_is_owed():
    t = _parse(_raw(_msg(ME, "thoughts?", 300), _msg(JOSH, "need a WR", 100))).topic("dm1")
    assert t.awaiting_us()


def test_system_events_and_old_lines_are_not_chat():
    c = _parse(_raw(_msg(JOSH, "ancient", 60 * 24 * 9), _msg(JOSH, "trade bookkeeping", 5, type_id=230)))
    assert c.topics == []


def test_activity_is_labelled_only_when_the_id_is_known():
    act = [{**_msg(ME, "", 30, mid="a1", type_id=178), "to": 6, "targetId": 3916148},
           {**_msg(ME, "", 25, mid="a3", type_id=179), "to": 8, "targetId": 3916148},
           {**_msg(ME, "", 20, mid="a2", type_id=188), "to": 8, "targetId": 1}]
    c = _parse(_raw(_msg(JOSH, "hi", 10), activity=act))
    assert [(a["team"], a["action"], a["player"]) for a in c.activity] == [
        ("McKenfrey", "added (FA)", "Aaron Jones"), ("big P", "dropped", "Aaron Jones")]


def test_a_failed_read_is_a_failure_not_silence(monkeypatch):
    blk = chat.packet_block(chat.Comms(error="HTTPError: 503"), pending=[])
    assert blk["unavailable"] and "conversations" not in blk


# ── drafts: posting ──────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_drafts, "_path", lambda: tmp_path / "chat_drafts.json")
    posted = []

    def fake_notify(level, title, body="", **kw):
        posted.append((title, body, kw.get("thread_ts")))
        return f"ts{len(posted)}"

    monkeypatch.setattr("core.notify.notify", fake_notify)
    return posted


BLOCK = {"conversations": [{"topic_id": "dm1", "kind": "dm", "with": ["Josh"]}]}


def test_a_draft_is_posted_for_approval_never_sent(store, monkeypatch):
    monkeypatch.setattr(chat, "send", lambda *a, **k: pytest.fail("post() must never send"))
    out = chat_drafts.post([{"topic_id": "dm1", "text": "Jones + who?", "why": "we're RB short"}], BLOCK)
    assert out == ["posted draft to Josh"]
    assert chat_drafts.pending()[0]["status"] == "pending"
    assert "needs your OK" in store[0][0]


def test_a_draft_into_an_unknown_conversation_is_refused(store):
    out = chat_drafts.post([{"topic_id": "someone-new", "text": "hey", "why": "cold open"}], BLOCK)
    assert out[0].startswith("refused") and chat_drafts.pending() == []


def test_a_newer_draft_supersedes_the_old_one(store):
    chat_drafts.post([{"topic_id": "dm1", "text": "first", "why": "x" * 10}], BLOCK)
    chat_drafts.post([{"topic_id": "dm1", "text": "second", "why": "x" * 10}], BLOCK)
    assert [d["text"] for d in chat_drafts.pending()] == ["second"]


# ── drafts: reading Pearce's answer ──────────────────────────────────────────


def _h(text, ts):  # a human thread reply
    return {"user": "UPEARCE", "text": text, "ts": str(ts)}


@pytest.mark.parametrize("parent,replies,want", [
    ({}, [], ("wait", None)),
    ({"reactions": [{"name": "white_check_mark", "users": ["UPEARCE"]}]}, [], ("send", None)),
    ({"reactions": [{"name": "x", "users": ["UPEARCE"]}]}, [], ("drop", None)),
    ({}, [_h("send: Jones and Addison for Likely?", 1)], ("send", "Jones and Addison for Likely?")),
    ({}, [_h("Send", 1)], ("send", None)),
    ({}, [_h("nah", 1)], ("wait", None)),          # not a recognised answer
    ({}, [_h("no", 1)], ("drop", None)),
    # the bot's own ✅ is not approval
    ({"reactions": [{"name": "white_check_mark", "users": ["UBOT"]}]}, [], ("wait", None)),
    # ✅ and ❌ together is a contradiction
    ({"reactions": [{"name": "white_check_mark", "users": ["UPEARCE"]},
                    {"name": "x", "users": ["UPEARCE"]}]}, [], ("wait", None)),
    # a typed edit outranks the reaction
    ({"reactions": [{"name": "white_check_mark", "users": ["UPEARCE"]}]},
     [_h("send: different words", 5)], ("send", "different words")),
    # the latest typed answer wins
    ({}, [_h("send", 1), _h("no", 2)], ("drop", None)),
    # bot posts in the thread are not answers
    ({}, [{"bot_id": "B1", "text": "send", "ts": "3"}], ("wait", None)),
])
def test_decide(parent, replies, want):
    assert chat_drafts.decide(parent, replies, bot_user="UBOT") == want


# ── the watcher ──────────────────────────────────────────────────────────────


def _watch():
    p = Path(__file__).resolve().parents[1] / "scripts" / "watch_league.py"
    spec = importlib.util.spec_from_file_location("watch_league", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_first_tick_seeds_and_wakes_nothing():
    w = _watch()
    c = _parse(_raw(_msg(JOSH, "need a WR", 60)))
    reasons, s = w.decide({}, comms=c, offer_ids=["o1"], now=NOW)
    assert reasons == [] and s["handled_ms"] == NOW_MS - 60 * MIN and s["seen_offers"] == ["o1"]


def test_a_settled_unanswered_message_wakes_the_sweep_once():
    w = _watch()
    c = _parse(_raw(_msg(ME, "thoughts?", 120), _msg(JOSH, "need a WR", 10)))
    seed = {"handled_ms": NOW_MS - 120 * MIN, "seen_offers": []}
    reasons, s = w.decide(seed, comms=c, offer_ids=[], now=NOW)
    assert reasons and "waiting on a reply" in reasons[0]
    again, _ = w.decide(s, comms=c, offer_ids=[], now=NOW)
    assert again == []


def test_a_burst_still_being_typed_waits():
    w = _watch()
    c = _parse(_raw(_msg(JOSH, "Good morning", 3), _msg(JOSH, "would you…", 1)))
    reasons, s = w.decide({"handled_ms": 0, "seen_offers": []}, comms=c, offer_ids=[], now=NOW)
    assert reasons == [] and s["handled_ms"] == 0


def test_nothing_fires_when_pearce_already_answered():
    w = _watch()
    c = _parse(_raw(_msg(JOSH, "I'll give you Aaron jones", 300), _msg(ME, "let me think", 200)))
    reasons, s = w.decide({"handled_ms": 0, "seen_offers": []}, comms=c, offer_ids=[], now=NOW)
    assert reasons == [] and s["handled_ms"] == NOW_MS - 300 * MIN


def test_a_new_offer_wakes_the_sweep_and_a_failed_read_concludes_nothing():
    w = _watch()
    reasons, s = w.decide({"handled_ms": 0, "seen_offers": ["o1"]}, comms=None,
                          offer_ids=["o1", "o2"], now=NOW)
    assert reasons == ["trade: 1 new incoming offer(s)"]
    reasons, s2 = w.decide(s, comms=None, offer_ids=None, now=NOW)
    assert reasons == [] and s2["seen_offers"] == ["o1", "o2"]


def test_spacing_and_daily_cap():
    w = _watch()
    recent = {"triggers": [(NOW - timedelta(minutes=5)).isoformat()]}
    assert w.allowed(recent, NOW)[0] is False
    full = {"triggers": [(NOW - timedelta(minutes=30 * i + 30)).isoformat() for i in range(10)]}
    assert w.allowed(full, NOW) == (False, "daily cap 10 reached")
    assert w.allowed({}, NOW) == (True, "")
