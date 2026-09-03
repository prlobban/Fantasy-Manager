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


def notify(level: str, title: str, body: str = "", *, receipt: str | None = None) -> bool:
    """Post to #fantasy. Returns whether it landed."""
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
        return False

    try:
        data = urllib.parse.urlencode(
            {"channel": cfg.slack_channel_id, "text": text}
        ).encode()
        req = urllib.request.Request(
            "https://slack.com/api/chat.postMessage",
            data=data,
            headers={"Authorization": f"Bearer {token}"},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            resp = json.loads(r.read().decode())
        if not resp.get("ok"):
            log.warning("slack rejected the message: %s", resp.get("error"))
            return False
        return True
    except Exception as e:
        log.warning("slack notify failed: %s", e)
        return False
