"""Slack #fantasy, posting as Polaris from the box.

Polaris is the BOX identity. Nothing on the laptop should post as Polaris — a
Polaris message means "the autonomous agent did something," and chatter wearing
that name trains the reader to ignore the signal that matters.

Never raises. A failed notification must not take down a draft loop, so every
error is logged and swallowed — but it is ALWAYS logged, because a silent
notifier is worse than none.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.parse
import urllib.request
from pathlib import Path

from core.config import settings

log = logging.getLogger(__name__)

LEVEL_ICON = {
    "info": "",
    "good": "✅ ",
    "warn": "⚠️ ",
    "error": "🔴 ",
    "action": "🤖 ",
}


def _token() -> str | None:
    cfg = settings()
    if not cfg.slack_token_file:
        return None
    p = Path(cfg.slack_token_file)
    if not p.exists():
        log.warning("slack token file %s not found — notifications disabled", p)
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))["bot_token"]
    except Exception as e:
        log.warning("could not read slack token: %s", e)
        return None


def notify(level: str, title: str, body: str | None = "", *,
           receipt: str | None = None, thread_ts: str | None = None) -> str | None:
    """Post to #fantasy. Returns the message timestamp, or None.

    The timestamp is what makes threading possible: pass it back as
    `thread_ts` and the next post is a reply instead of another top-level
    message. A draft produces 110+ opponent picks, and a channel that scrolls
    that fast is a channel nobody reads (§3.8).

    Still falsy on failure, so `if notify(...)` reads exactly as it did when
    this returned a bool.

    `body=None` is accepted and treated as empty. It used to raise here —
    inside a function whose contract is that it never does — which killed the
    research pass after it had already done all its work.
    """
    # A test must never reach the live channel. The judge tests call
    # consult_judge() for real, which posts a shadow diff, and running the
    # suite on the box — where the token DOES resolve — put two junk "Shadow ·
    # pick 17" messages into #fantasy on 2026-09-04. pytest sets
    # PYTEST_CURRENT_TEST for every test it runs, so this needs no cooperation
    # from the tests themselves, which is the point: the guard has to hold for
    # tests nobody remembered to check.
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("FANTASY_NO_NOTIFY"):
        log.debug("notify suppressed (test context): [%s] %s", level, title)
        return None

    body = body or ""
    icon = LEVEL_ICON.get(level, "")
    text = f"{icon}*{title}*"
    if body:
        text += f"\n{body}"
    if receipt:
        text += f"\n_receipt: {receipt}_"

    # Always log locally, whether or not Slack works. The log is the record;
    # Slack is the convenience.
    log.info("NOTIFY [%s] %s | %s", level, title, body.replace("\n", " ")[:400])

    cfg = settings()
    token = _token()
    if not token or not cfg.slack_channel_id:
        return None

    try:
        fields = {"channel": cfg.slack_channel_id, "text": text}
        if thread_ts:
            fields["thread_ts"] = thread_ts
        data = urllib.parse.urlencode(fields).encode()
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage",
            data=data,
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read().decode())
        if not resp.get("ok"):
            log.warning("slack rejected the message: %s", resp.get("error"))
            return None
        return resp.get("ts")
    except Exception as e:
        log.warning("slack notify failed: %s", e)
        return None
