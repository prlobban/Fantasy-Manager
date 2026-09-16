"""Slack #fantasy, inbound. The half of the channel that did not exist.

`notify.py` is a speaker: it posts and nothing reads back. So a reply typed
into #fantasy — a question, a steer, "don't trade Skattebo" — went nowhere and
was not even logged. This is the other direction: every run pulls what Pearce
said since the last run and hands it to the agent in the packet (§8.9, D10).

Two rules shape the implementation:

1. **It never raises.** Same contract as `notify` — a Slack outage must not
   take down a sweep that can set a lineup perfectly well without it.
2. **But silence is never faked.** A failure comes back as `error`, and the
   packet says so out loud. "Pearce said nothing" and "we could not find out
   whether Pearce said anything" are different facts, and an agent that cannot
   tell them apart will read an outage as consent (§8.8).

Thread replies count. Answering the morning digest in its own thread is the
obvious way to ask "why did you bench Allen?", and `conversations.history`
does not return thread replies — so any recent message carrying replies is
followed. Without that, the most natural place to type is the one place the
agent would never look.

The cursor only advances when a run finishes (`mark_read`). A crash mid-sweep
re-reads the same messages next time; the alternative is a question that is
consumed and never answered.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from core.config import settings

log = logging.getLogger(__name__)

# How far back to look for thread parents. A reply can land under a digest
# posted days ago; anything older than this is stale as an instruction anyway.
THREAD_LOOKBACK_S = 48 * 3600

# The agent reads these in a prompt. Twenty is already more than a morning's
# worth; past that something has gone wrong and truncating is the right answer.
MAX_MESSAGES = 20
MAX_CHARS = 1200


@dataclass(frozen=True)
class Message:
    ts: str
    text: str
    at: str
    in_thread: bool

    def as_dict(self) -> dict:
        return {"ts": self.ts, "at": self.at, "text": self.text,
                "in_thread": self.in_thread}


@dataclass(frozen=True)
class Inbox:
    messages: list[Message] = field(default_factory=list)
    error: str | None = None
    cursor: str = "0"

    @property
    def ok(self) -> bool:
        return self.error is None


def _state_path() -> Path:
    return settings().data_dir / "slack_inbox.json"


def cursor() -> str:
    """Last message timestamp we have handed to the agent."""
    p = _state_path()
    if not p.exists():
        return "0"
    try:
        return str(json.loads(p.read_text(encoding="utf-8")).get("last_ts") or "0")
    except Exception as e:
        log.warning("inbox cursor unreadable (%s) — treating as fresh", e)
        return "0"


def mark_read(ts: str) -> None:
    """Advance the cursor. Called AFTER a run succeeds, never before."""
    if not ts or ts == "0":
        return
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text(json.dumps(
            {"last_ts": str(ts), "read_at": datetime.now(UTC).isoformat()},
            indent=1) + "\n", encoding="utf-8")
        log.info("inbox: cursor -> %s", ts)
    except Exception as e:
        log.warning("inbox: could not persist cursor: %s", e)


def _suppressed() -> bool:
    """A test must never reach the live channel.

    Same guard `notify` carries, for the same reason: running the suite on the
    box, where the token resolves, once put junk into #fantasy. pytest sets
    PYTEST_CURRENT_TEST for every test it runs, so this needs no cooperation
    from the tests. It is a function so the inbox's own tests can exercise the
    body by patching it — which is the only reason anything should.
    """
    return bool(os.environ.get("PYTEST_CURRENT_TEST")
                or os.environ.get("FANTASY_NO_SLACK_INBOX"))


def _token() -> str | None:
    cfg = settings()
    if not cfg.slack_token_file:
        return None
    p = Path(cfg.slack_token_file)
    if not p.exists():
        log.warning("slack token file %s not found — inbox disabled", p)
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))["bot_token"]
    except Exception as e:
        log.warning("could not read slack token: %s", e)
        return None


def _call(method: str, token: str, **params) -> dict:
    url = f"https://slack.com/api/{method}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def _is_human(m: dict) -> bool:
    """Polaris's own posts are not input. Nor are joins, pins or edits.

    A bot reading its own output as instruction is a feedback loop, and this
    one would be a feedback loop with write access to a money league.
    """
    if m.get("bot_id") or m.get("app_id"):
        return False
    if m.get("subtype") and m["subtype"] != "thread_broadcast":
        return False
    return bool(m.get("user") and (m.get("text") or "").strip())


def _clean(text: str) -> str:
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = " ".join(text.split())
    return text[:MAX_CHARS]


def _msg(m: dict, *, in_thread: bool) -> Message:
    ts = str(m["ts"])
    at = datetime.fromtimestamp(float(ts), UTC).isoformat(timespec="seconds")
    return Message(ts=ts, text=_clean(m.get("text") or ""), at=at, in_thread=in_thread)


def read(since: str | None = None) -> Inbox:
    """Everything a human said in #fantasy since the cursor. Never raises."""
    last = str(since if since is not None else cursor())

    if _suppressed():
        return Inbox(cursor=last)

    cfg = settings()
    token = _token()
    if not token or not cfg.slack_channel_id:
        return Inbox(error="slack not configured (token file or channel id missing)",
                     cursor=last)

    try:
        # Reach back far enough to find thread parents, then filter by cursor.
        # Compare timestamps as floats, not strings: Slack's ts is fixed-width
        # today and a lexical compare happens to work, which is exactly the
        # kind of accident that breaks silently later.
        since_f = float(last or 0)
        oldest = min(since_f or 0.0, time.time() - THREAD_LOOKBACK_S)
        hist = _call("conversations.history", token,
                     channel=cfg.slack_channel_id, oldest=f"{oldest:.6f}", limit=100)
        if not hist.get("ok"):
            err = hist.get("error", "unknown")
            if err in ("missing_scope", "not_in_channel"):
                # The two failures that are configuration, not weather. Say
                # which, because "slack failed" sends you reading logs.
                err = (f"{err} — the Polaris app needs channels:history and to be "
                       f"a member of the channel")
            return Inbox(error=f"conversations.history: {err}", cursor=last)

        found: list[Message] = []
        for m in hist.get("messages", []):
            if float(m["ts"]) > since_f and _is_human(m):
                found.append(_msg(m, in_thread=False))
            # A reply under an older digest is the natural way to answer it,
            # and history does not return replies. Follow the thread.
            if int(m.get("reply_count") or 0) > 0:
                rep = _call("conversations.replies", token,
                            channel=cfg.slack_channel_id, ts=m["ts"], limit=50)
                if not rep.get("ok"):
                    log.warning("inbox: replies for %s failed: %s", m["ts"],
                                rep.get("error"))
                    continue
                for r in rep.get("messages", []):
                    if r["ts"] != m["ts"] and float(r["ts"]) > since_f and _is_human(r):
                        found.append(_msg(r, in_thread=True))

        found.sort(key=lambda x: float(x.ts))
        if len(found) > MAX_MESSAGES:
            log.warning("inbox: %d messages, keeping the newest %d",
                        len(found), MAX_MESSAGES)
            found = found[-MAX_MESSAGES:]
        return Inbox(messages=found, cursor=last)
    except Exception as e:
        log.warning("inbox read failed: %s", e)
        return Inbox(error=f"{type(e).__name__}: {e}", cursor=last)


def main() -> int:
    """`python -m core.inbox` — what the next sweep would see. Reads only."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    box = read()
    print(f"cursor: {box.cursor}")
    if box.error:
        print(f"ERROR: {box.error}")
        return 1
    if not box.messages:
        print("nothing new")
        return 0
    for m in box.messages:
        print(f"  {m.at}{' [thread]' if m.in_thread else ''}  {m.text}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
