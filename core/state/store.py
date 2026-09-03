"""data/state.json — small, current, and the only mutable shared state.

No database until one is earned. Everything here is either a counter the rate
limits need (§6.1, §6.8.10) or a marker the loops need to avoid repeating work.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from core.config import settings

log = logging.getLogger(__name__)

_DEFAULT: dict[str, Any] = {
    "version": 1,
    "trade_proposals": [],      # [{"at": iso, "to_team": int, "offer_hash": str}]
    "trade_rejections": [],     # [{"at": iso, "by_team": int, "offer_hash": str}]
    "trade_accepts": [],        # [{"at": iso, "offer_id": str}]
    "offers_first_seen": {},    # offer_id -> iso, for the §6.8.9 cool-down
    "last_lineup_set": None,
    "last_waiver_run": None,
    "draft_complete": False,
}


def _path() -> Path:
    return settings().state_path


def load() -> dict[str, Any]:
    p = _path()
    if not p.exists():
        return dict(_DEFAULT)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log.error("state.json unreadable (%s) — starting from defaults", e)
        return dict(_DEFAULT)
    merged = dict(_DEFAULT)
    merged.update(data)
    return merged


def save(state: dict[str, Any]) -> None:
    """Atomic write. A half-written state file after a crash would silently
    reset rate limits, which is the one thing this file must never do."""
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=1, default=str)
        os.replace(tmp, p)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise


def update(**kw: Any) -> dict[str, Any]:
    s = load()
    s.update(kw)
    save(s)
    return s


def append(key: str, item: Any, *, cap: int = 500) -> dict[str, Any]:
    s = load()
    lst = list(s.get(key) or [])
    lst.append(item)
    s[key] = lst[-cap:]
    save(s)
    return s


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def today() -> str:
    return date.today().isoformat()
