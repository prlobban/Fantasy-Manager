"""§7 / D7 — what the system has learned, in a form the next run can read.

`data/lessons.md` is the manager's memory. The Tuesday review appends to it;
the daily packet carries its tail. It is the only place a lesson lives, so
there is exactly one thing to prune when a lesson turns out wrong.

A lesson is one dated line with a MECHANISM (D7.3): "benched X on a Friday DNP;
he was inactive" is a lesson. "we were right about X" is not.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from core.config import settings

log = logging.getLogger(__name__)

_HEADER = (
    "# Lessons — what the manager has learned\n\n"
    "One dated line per lesson, with the mechanism. Appended by the Tuesday review;\n"
    "read by every daily run. Prune a line only when it has been shown wrong.\n\n"
)


def path() -> Path:
    return settings().data_dir / "lessons.md"


def read(limit: int = 40) -> list[str]:
    p = path()
    if not p.exists():
        return []
    lines = [ln.rstrip() for ln in p.read_text(encoding="utf-8").splitlines()
             if ln.startswith("- ")]
    return lines[-limit:]


def append(week: int, lessons: list[str], *, kind: str = "lesson") -> int:
    """Append lessons for a week. Returns how many were written."""
    lessons = [ln.strip() for ln in lessons if ln and ln.strip()]
    if not lessons:
        return 0
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if not p.exists():
        p.write_text(_HEADER, encoding="utf-8")
    stamp = datetime.now(UTC).date().isoformat()
    with p.open("a", encoding="utf-8") as f:
        for ln in lessons:
            f.write(f"- {stamp} wk{week} [{kind}] {ln}\n")
    log.info("lessons: appended %d for week %d", len(lessons), week)
    return len(lessons)
