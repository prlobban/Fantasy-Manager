"""Chat replies: the agent drafts, Pearce approves, core sends (§8.10).

A line in league chat goes out under Pearce's name to his friends. So the
agent never holds the pen: it writes `chat_drafts` in its output, this module
posts each one to #fantasy, and the watcher (scripts/watch_league.py, every
five minutes) sends a draft only once Pearce has answered it there:

    ✅ reaction, or a thread reply "send"/"yes"   → sent exactly as drafted
    thread reply "send: <text>"                   → <text> is sent instead
    ❌ reaction, or a thread reply "no"/"drop"     → dropped

Nothing else sends. There is no MCP tool for it, the agent's output can only
create a pending draft, and `approve()` reads the approval off Slack itself —
a prompt cannot talk its way past a check that never asks the model.

A draft that waits 48 hours expires: a reply to a negotiation two days stale
is worse than none. A newer draft for the same conversation supersedes the old
one, so there is never a stale draft sitting behind a live one.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from core.config import settings

log = logging.getLogger(__name__)

EXPIRE_AFTER = timedelta(hours=48)
MAX_TEXT = 500
YES_REACTIONS = {"white_check_mark", "heavy_check_mark", "ballot_box_with_check", "+1"}
NO_REACTIONS = {"x", "no_entry", "no_entry_sign", "-1"}
YES_WORDS = {"send", "send it", "yes", "y", "yep", "go", "ok", "okay", "approve", "approved"}
NO_WORDS = {"no", "n", "nope", "drop", "drop it", "skip", "don't", "dont", "cancel", "kill"}


def _path() -> Path:
    return settings().data_dir / "chat_drafts.json"


def load() -> list[dict]:
    try:
        return json.loads(_path().read_text(encoding="utf-8")).get("drafts") or []
    except FileNotFoundError:
        return []
    except Exception as e:
        log.warning("chat drafts unreadable (%s) — treating as empty", e)
        return []


def save(drafts: list[dict]) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps({"drafts": drafts[-200:]}, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, p)


def pending() -> list[dict]:
    return [d for d in load() if d.get("status") == "pending"]


def for_packet() -> list[dict]:
    return [{"topic_id": d["topic_id"], "to": d.get("to"), "text": d["text"],
             "drafted_at": d.get("created")} for d in pending()]


def thread_ts_set() -> set[str]:
    """Slack threads that belong to drafts. The inbox must not read an
    approval ("send") as a question for the next sweep to answer."""
    return {d["slack_ts"] for d in load() if d.get("slack_ts")}


def _now() -> datetime:
    return datetime.now(UTC)


# ── from the agent's output to #fantasy ─────────────────────────────────────


def post(drafts: list[dict], block: dict) -> list[str]:
    """Validate the agent's drafts against what was actually read, then post.

    Returns one line per draft for the log. A draft into a conversation the
    packet did not contain is refused: the agent can only answer, never start
    a thread with someone it has not heard from.
    """
    from core.notify import notify

    known = {c["topic_id"]: c for c in block.get("conversations") or []}
    store = load()
    out: list[str] = []
    for d in drafts or []:
        tid = str(d.get("topic_id") or "")
        text = " ".join(str(d.get("text") or "").split())
        conv = known.get(tid)
        if not conv:
            out.append(f"refused: topic {tid!r} is not a conversation in this run's packet")
            continue
        if not text or len(text) > MAX_TEXT:
            out.append(f"refused: text empty or over {MAX_TEXT} chars")
            continue
        for old in store:
            if old.get("status") == "pending" and old.get("topic_id") == tid:
                old["status"] = "superseded"
                old["resolved_at"] = _now().isoformat()
                if old.get("slack_ts"):
                    notify("info", "Superseded", "A newer draft for this conversation is below.",
                           thread_ts=old["slack_ts"])
        to = ", ".join(conv.get("with") or []) or "?"
        where = "DM" if conv.get("kind") == "dm" else "league chat"
        why = " ".join(str(d.get("why") or "").split())[:300]
        body = (f"*To {to}* ({where})\n>{text}\n"
                + (f"_why: {why}_\n" if why else "")
                + "✅ to send · reply `send: <your words>` to send yours instead · ❌ to drop")
        ts = notify("info", "💬 Chat draft — needs your OK", body)
        store.append({
            "id": uuid.uuid4().hex[:12], "topic_id": tid, "to": to, "kind": conv.get("kind"),
            "text": text, "why": why, "slack_ts": ts, "status": "pending" if ts else "unposted",
            "created": _now().isoformat(),
        })
        out.append(f"posted draft to {to}" if ts else f"draft to {to} NOT posted (Slack down)")
    save(store)
    return out


# ── reading Pearce's answer ──────────────────────────────────────────────────


def decide(parent: dict, replies: list[dict], *, bot_user: str | None) -> tuple[str, str | None]:
    """("send", text | None) · ("drop", None) · ("wait", None).

    Pure: the Slack thread in, the decision out. `text` None means "as drafted".
    The newest human signal wins — a ✅ followed by "send: <edit>" sends the edit.
    """
    signals: list[tuple[float, str, str | None]] = []
    reacted: set[str] = set()
    for r in parent.get("reactions") or []:
        users = [u for u in r.get("users") or [] if u != bot_user]
        if not users:
            continue
        name = r.get("name", "").split("::")[0]
        if name in YES_REACTIONS:
            reacted.add("send")
        elif name in NO_REACTIONS:
            reacted.add("drop")
    # Reactions carry no timestamp, so they rank below any typed reply — and
    # both ✅ and ❌ on one draft is a contradiction, which means wait.
    if len(reacted) == 1:
        signals.append((0.0, reacted.pop(), None))
    for m in replies:
        if m.get("bot_id") or m.get("app_id") or not m.get("user") or m.get("user") == bot_user:
            continue
        raw = (m.get("text") or "").strip()
        low = raw.lower().rstrip(".!")
        ts = float(m.get("ts") or 0)
        if low.startswith(("send:", "edit:")):
            edit = raw.split(":", 1)[1].strip()
            if edit:
                signals.append((ts, "send", edit))
        elif low in YES_WORDS:
            signals.append((ts, "send", None))
        elif low in NO_WORDS:
            signals.append((ts, "drop", None))
    if not signals:
        return "wait", None
    _, verdict, text = max(signals, key=lambda s: s[0])
    return verdict, text


def _slack(method: str, **params) -> dict:
    from core import inbox

    token = inbox._token()
    if not token:
        raise RuntimeError("no slack token")
    return inbox._call(method, token, **params)


def process() -> list[str]:
    """One watcher tick: expire, then send or drop whatever Pearce answered."""
    from core.espn import chat
    from core.gates import kill_switch
    from core.notify import notify

    if os.environ.get("PYTEST_CURRENT_TEST"):
        return []
    store = load()
    if not any(d.get("status") == "pending" for d in store):
        return []
    cfg = settings()
    try:
        bot_user = _slack("auth.test").get("user_id")
    except Exception as e:
        return [f"slack unavailable: {e}"]

    log_lines: list[str] = []
    for d in store:
        if d.get("status") != "pending":
            continue
        if _now() - datetime.fromisoformat(d["created"]) > EXPIRE_AFTER:
            d.update(status="expired", resolved_at=_now().isoformat())
            notify("info", "Expired", "Not sent — 48h without an answer.", thread_ts=d["slack_ts"])
            log_lines.append(f"expired draft to {d['to']}")
            continue
        try:
            rep = _slack("conversations.replies", channel=cfg.slack_channel_id,
                         ts=d["slack_ts"], limit=50)
        except Exception as e:
            log_lines.append(f"could not read thread for draft {d['id']}: {e}")
            continue
        msgs = rep.get("messages") or []
        if not msgs:
            continue
        verdict, edit = decide(msgs[0], msgs[1:], bot_user=bot_user)
        if verdict == "wait":
            continue
        if verdict == "drop":
            d.update(status="dropped", resolved_at=_now().isoformat())
            notify("info", "Dropped", "Not sent.", thread_ts=d["slack_ts"])
            log_lines.append(f"dropped draft to {d['to']}")
            continue
        text = edit or d["text"]
        if not kill_switch.is_on():
            notify("warn", "Not sent", "The kill switch is off, so nothing goes to ESPN. "
                   "Turn it on and ✅ again, or paste it yourself:\n>" + text,
                   thread_ts=d["slack_ts"])
            d.update(status="refused", resolved_at=_now().isoformat())
            log_lines.append(f"refused (kill switch) draft to {d['to']}")
            continue
        try:
            sent = chat.send(d["topic_id"], text)
            d.update(status="sent", sent_text=sent, edited=bool(edit),
                     resolved_at=_now().isoformat())
            notify("good", "Sent", f"Posted to {d['to']}" + (" (your edit)." if edit else "."),
                   thread_ts=d["slack_ts"])
            log_lines.append(f"SENT to {d['to']}: {sent[:80]}")
        except Exception as e:
            d.update(status="failed", error=str(e)[:300], resolved_at=_now().isoformat())
            notify("error", "Send failed", f"{str(e)[:200]}\nPaste it yourself:\n>{text}",
                   thread_ts=d["slack_ts"])
            log_lines.append(f"FAILED to send to {d['to']}: {e}")
        _record(d)
    save(store)
    return log_lines


def _record(d: dict) -> None:
    """Every send attempt lands in decisions.jsonl, like every other write."""
    try:
        p = settings().decisions_path
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "at": _now().isoformat(), "kind": "chat_send",
                "executed": d.get("status") == "sent",
                "extra": {"args": {"topic_id": d["topic_id"], "to": d.get("to"),
                                   "text": d.get("sent_text") or d.get("text"),
                                   "edited": d.get("edited", False)}},
                "gate": {"approved_by": "pearce (slack)", "error": d.get("error")},
            }) + "\n")
    except Exception as e:
        log.warning("could not record chat send: %s", e)
