"""The agent's memory of its own runs.

Every sweep is a cold `claude -p`. It gets the league's state and none of its
own history, so it re-derives its situation from scratch two or three times a
day and cannot tell "this is new" from "I did this seven minutes ago".

2026-09-15 is the clean example. At 15:04 a sweep placed a waiver claim for the
Buccaneers D/ST. At 15:11 the next run read the ledger, saw an add recorded,
read the roster, saw no Buccaneers, and escalated:

    "we appear to have paid for a transaction that did not land ... should the
     rate-limit ledger have that add refunded before Sunday?"

Nothing was wrong. Waivers process Wednesday. The run had the *data* — the
packet already carried `recent_adds` — and no *memory*: a flat row saying an
add happened at 15:04 with no outcome, no status, and no record that a previous
run had already placed and explained it.

The same blindness produced the day's other pattern: the identical
"add_drop is broken" escalation three times in a row, and through Week 1 the
same Herbert+Pitts→Wilson trade regenerated in four consecutive sweeps, because
no run could see that the last one had already decided it.

## Why a journal and not a resumed session

The obvious alternative is to keep one long-lived conversation (SDK session
resumption) instead of a fresh `-p` each time. That is the wrong shape here: a
cron job running two or three times a day for a five-month season would grow a
single context without bound, re-paying for it on every turn, and the thing we
actually need — "what did I decide about McConkey, and when?" — is a *query*,
not a transcript to scroll.

Durable structured memory injected into a fresh context is cheaper, queryable,
survives a model or runtime change, and is reviewable by a human. It also works
identically under `claude -p` or the Agent SDK, so this is not a bet on either.

## What is worth remembering

Not the transcript. Four things, and each one exists to stop a specific failure
already seen in this league:

* **what I did, and how it turned out** — stops "did that write land?"
* **what I decided NOT to do, and why** — stops re-proposing the same trade
* **what I escalated** — stops asking Pearce the same question three times
* **what I left open for the next run** — the handoff a cold start cannot infer
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

#: Entries older than this are dropped from the packet rendering. Long enough
#: to cover a waiver cycle and a trade cooldown (§6.1's 14 days is handled by
#: rate_limits, not here); short enough that the agent reads its recent past
#: rather than the season.
WINDOW_DAYS = 10

#: How many entries to render. A sweep is two or three a day, so this is
#: roughly the last three days of runs.
RENDER_LIMIT = 8


def path() -> Path:
    from core.config import settings

    return settings().data_dir / "journal.jsonl"


def record(*, week: int, task: str, scope: str, summary: str,
           did: list[dict] | None = None, declined: str = "",
           escalated: str = "", open_threads: list[str] | None = None) -> None:
    """Append one run to the journal. Never fatal — losing a memory must not
    fail a sweep that otherwise worked."""
    entry = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "week": week,
        "task": task,
        "scope": scope,
        "summary": (summary or "").strip()[:600],
        "did": did or [],
        "declined": (declined or "").strip()[:400],
        "escalated": (escalated or "").strip()[:400],
        "open": [str(o)[:300] for o in (open_threads or [])][:6],
    }
    try:
        p = path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
    except Exception as e:
        log.warning("could not write the run journal: %s", e)


def read(limit: int = 40) -> list[dict]:
    p = path()
    if not p.exists():
        return []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()[-limit:]
    except Exception as e:
        log.warning("could not read the run journal: %s", e)
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except Exception:
            continue  # one torn line must not blind the agent to the rest
    return out


def _recent(entries: list[dict], days: int) -> list[dict]:
    cutoff = datetime.now(UTC) - timedelta(days=days)
    out = []
    for e in entries:
        try:
            if datetime.fromisoformat(e["at"]) >= cutoff:
                out.append(e)
        except Exception:
            continue
    return out


def recent(days: int = WINDOW_DAYS, limit: int = RENDER_LIMIT) -> list[dict]:
    return _recent(read(), days)[-limit:]


def recent_escalations(days: int = 3) -> list[str]:
    """What has already been put to Pearce, so it is not asked again.

    Three identical "add_drop is broken" escalations went out on 2026-09-15
    because no run could see the previous one.
    """
    seen: list[str] = []
    for e in _recent(read(), days):
        esc = (e.get("escalated") or "").strip()
        if esc and esc not in seen:
            seen.append(esc)
    return seen


def open_threads(days: int = WINDOW_DAYS) -> list[str]:
    """Everything a previous run said the next one should check."""
    out: list[str] = []
    for e in _recent(read(), days):
        for o in e.get("open") or []:
            if o not in out:
                out.append(o)
    return out[-10:]


def for_packet() -> dict[str, Any]:
    """The memory block handed to the agent.

    Deliberately shaped as answers rather than a log: the agent should be able
    to read "you already escalated this" without reconstructing it from
    timestamps.
    """
    entries = recent()
    return {
        "runs": [
            {
                "at": e.get("at"),
                "task": f"{e.get('task')}/{e.get('scope')}",
                "summary": e.get("summary"),
                "did": e.get("did"),
                "declined": e.get("declined"),
            }
            for e in entries
        ],
        "already_escalated": recent_escalations(),
        "open_threads": open_threads(),
        "how_to_use": (
            "This is YOUR record of YOUR previous runs — read it before deciding "
            "anything is new. (1) A write listed in `did` with outcome 'pending' "
            "has NOT changed the roster yet and is not a failure. (2) Do not "
            "re-raise anything in `already_escalated` unless the situation "
            "materially changed — say 'still open' instead. (3) Do not re-propose "
            "what a recent run explains in `declined`; if you disagree, say what "
            "new evidence changed it. (4) `open_threads` is what a previous run "
            "asked you to check — work them before opening new ones."
        ),
    }
