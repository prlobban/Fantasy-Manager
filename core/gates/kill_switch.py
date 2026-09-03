"""§8.4 — the master write switch.

A file, not a flag in code, so a human can stop the agent from a phone over SSH
without editing anything. Same pattern as ~/astra-support/ENABLED on the box.

Fails CLOSED: an unreadable or missing file means OFF. A switch that defaults to
"go" when it can't read itself is not a safety mechanism.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from core.config import settings

log = logging.getLogger(__name__)


def path() -> Path:
    return settings().enabled_file


def is_on() -> bool:
    try:
        first = path().read_text(encoding="utf-8").strip().splitlines()[0]
    except (OSError, IndexError):
        return False
    return first.strip().lower() == "on"


def state() -> str:
    """Full file contents — the 'off' reason lives on lines 2+."""
    try:
        return path().read_text(encoding="utf-8").strip()
    except OSError:
        return "off (file unreadable)"


def turn_off(reason: str) -> None:
    """Called by health failures (§8.5) and by any unrecoverable write error."""
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    try:
        path().write_text(f"off\n{stamp} {reason}\n", encoding="utf-8")
        log.error("KILL SWITCH OFF: %s", reason)
    except OSError as e:
        log.critical("could not write kill switch (%s) — writes still refused", e)


def turn_on(who: str = "human") -> None:
    """Only ever called by a person. Nothing in core turns itself back on."""
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    path().write_text(f"on\n{stamp} enabled by {who}\n", encoding="utf-8")
    log.warning("kill switch ON (by %s)", who)
