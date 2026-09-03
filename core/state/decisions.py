"""§7.1 — log the prediction, not just the action.

Append-only JSONL. Every decision carries the number that justified it at the
time, so Tuesday (§7) can grade the DECISION rather than the outcome. In a
high-variance game, grading outcomes teaches the wrong lesson: starting the
14-point projection over the 9-point one was correct even when it scores 3.

Never edited, never rewritten. A correction is a new line.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.config import settings
from core.model.schema import ActionKind, DecisionRecord, GateResult

log = logging.getLogger(__name__)


def _path() -> Path:
    return settings().decisions_path


def record(
    kind: ActionKind | str,
    *,
    cites: list[str],
    reason: str,
    predicted: dict[str, float] | None = None,
    alternative: dict | None = None,
    executed: bool = False,
    gate: GateResult | None = None,
    receipt: str | None = None,
    extra: dict[str, Any] | None = None,
) -> DecisionRecord:
    rec = DecisionRecord(
        at=datetime.now(UTC),
        kind=ActionKind(kind) if isinstance(kind, str) else kind,
        cites=cites,
        reason=reason,
        predicted=predicted or {},
        alternative=alternative,
        executed=executed,
        gate=gate,
        receipt=receipt,
    )
    payload = rec.model_dump(mode="json")
    if extra:
        payload["extra"] = extra

    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, default=str) + "\n")
    return rec


def read_all(limit: int | None = None) -> list[dict]:
    p = _path()
    if not p.exists():
        return []
    out = []
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # a truncated final line after a crash is survivable
    return out[-limit:] if limit else out


def since(when: datetime) -> list[dict]:
    return [d for d in read_all() if _at(d) and _at(d) >= when]


def _at(d: dict) -> datetime | None:
    try:
        v = datetime.fromisoformat(d["at"])
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    except Exception:
        return None
