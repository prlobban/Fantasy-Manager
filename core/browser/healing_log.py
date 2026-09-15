"""What the selector layer has learned, and what it should do with it.

Healing one stale selector is a repair. Getting BETTER over time means the
repairs accumulate into knowledge, and three kinds are worth keeping:

1. **Which candidate actually resolved, last time.** `first_present` walks the
   candidate chain in a fixed order every run, so a group whose first two
   candidates died years ago pays for them on every single probe — and worse,
   an old candidate that starts matching something *else* after a redesign gets
   preferred over the one that has been working. Remembering the last winner
   and trying it first fixes both.

2. **Which groups keep breaking.** A group ESPN has moved three times this
   season is not a one-off; its built-in candidates are fiction, and the
   Tuesday review should say so rather than the same surprise recurring.

3. **Which overrides have earned permanence.** An override that has resolved
   on every probe for two weeks is no longer a patch, it is the truth, and
   `selectors.py` is the thing that is now wrong.

This is deliberately an append-only log rather than a rolling state file. The
system already learned (§7, and the 2026-09-08 Tuesday review) that grading
something you did not record is guesswork.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

PATH = Path(__file__).resolve().parents[2] / "data" / "selector-history.jsonl"

#: An override resolving cleanly on this many separate probes, over at least
#: PROMOTE_AFTER_DAYS, is treated as settled truth rather than a patch.
PROMOTE_AFTER_PROBES = 6
PROMOTE_AFTER_DAYS = 10

#: Breaking this often in a season means the built-in candidates for that group
#: are not worth trusting.
VOLATILE_AFTER = 3


def _append(kind: str, **fields: Any) -> None:
    rec = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "kind": kind, **fields}
    try:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        with PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, sort_keys=True) + "\n")
    except Exception as e:
        # Never fatal: losing a history line must not fail a write that worked.
        log.warning("could not append to the selector history: %s", e)


def record_break(group: str, target: str) -> None:
    """A group resolved nothing on a live page."""
    _append("break", group=group, target=target)


def record_heal(group: str, candidate: str, score: float, why: str) -> None:
    _append("heal", group=group, candidate=candidate, score=score, why=why)


def record_heal_failed(group: str, why: str) -> None:
    _append("heal_failed", group=group, why=why)


def record_resolved(group: str, candidate: str, healed: bool) -> None:
    """A group resolved, and this is the candidate that did it."""
    _append("resolved", group=group, candidate=candidate, healed=healed)


def read(limit: int = 2000) -> list[dict]:
    if not PATH.exists():
        return []
    try:
        lines = PATH.read_text(encoding="utf-8").splitlines()[-limit:]
    except Exception as e:
        log.warning("could not read the selector history: %s", e)
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


# ── 1. remember the winner ───────────────────────────────────────────────────

WINNERS = PATH.parent / "selector-winners.json" if PATH.parent else None


def _load_winners() -> dict[str, str]:
    try:
        if WINNERS and WINNERS.exists():
            d = json.loads(WINNERS.read_text(encoding="utf-8"))
            return {k: v for k, v in d.items() if isinstance(v, str)}
    except Exception as e:
        log.warning("could not read selector winners (%s) — using declared order", e)
    return {}


def last_winner(group: str) -> str | None:
    """The candidate that resolved for `group` most recently."""
    return _load_winners().get(group)


def note_winner(group: str, candidate: str) -> None:
    """Remember which candidate resolved, so it is tried first next time."""
    cur = _load_winners()
    if cur.get(group) == candidate:
        return
    cur[group] = candidate
    try:
        WINNERS.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(WINNERS.parent), suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cur, fh, indent=2, sort_keys=True)
        os.replace(tmp, WINNERS)
    except Exception as e:
        log.warning("could not record the winning selector: %s", e)


# ── 2 & 3. what the history says ─────────────────────────────────────────────


def stats() -> dict[str, dict]:
    """Per-group: how often it broke, when, and how its override is holding."""
    rows = read()
    breaks = Counter(r["group"] for r in rows if r.get("kind") == "break" and r.get("group"))
    heals = Counter(r["group"] for r in rows if r.get("kind") == "heal" and r.get("group"))
    resolved = Counter(r["group"] for r in rows
                       if r.get("kind") == "resolved" and r.get("healed"))
    last_break: dict[str, str] = {}
    for r in rows:
        if r.get("kind") == "break" and r.get("group"):
            last_break[r["group"]] = r["at"]
    out: dict[str, dict] = {}
    for g in set(breaks) | set(heals) | set(resolved):
        out[g] = {
            "breaks": breaks[g],
            "heals": heals[g],
            "override_confirmations": resolved[g],
            "last_break": last_break.get(g),
        }
    return out


def volatile() -> list[str]:
    """Groups that have broken often enough that the built-ins are untrustworthy."""
    return sorted(g for g, s in stats().items() if s["breaks"] >= VOLATILE_AFTER)


def ready_for_promotion() -> list[dict]:
    """Overrides that have held long enough to belong in selectors.py.

    Returns the evidence, not an edit — promoting means changing the declared
    candidates, and that is a code change a person should read even in a system
    that heals itself, because it is the file every future heal starts from.
    """
    from core.browser import overrides

    now = datetime.now(UTC)
    st = stats()
    out = []
    for group, entry in overrides.all_entries().items():
        if not entry.get("verified"):
            continue
        try:
            age = now - datetime.fromisoformat(entry["at"])
        except Exception:
            continue
        confirms = st.get(group, {}).get("override_confirmations", 0)
        if age >= timedelta(days=PROMOTE_AFTER_DAYS) and confirms >= PROMOTE_AFTER_PROBES:
            out.append({
                "group": group,
                "candidate": entry["candidate"],
                "days": age.days,
                "confirmations": confirms,
                "replaces": entry.get("replaces", ""),
            })
    return out


def review_notes() -> list[str]:
    """Lines for the Tuesday review (§7) — what the selector layer learned."""
    notes: list[str] = []
    st = stats()
    for g in volatile():
        s = st[g]
        notes.append(
            f"{g} has broken {s['breaks']}x — its declared candidates in "
            "selectors.py are stale fiction. Rewrite them around whatever the "
            "override currently uses rather than healing it again each time."
        )
    for p in ready_for_promotion():
        notes.append(
            f"{p['group']} has run on the healed selector {p['candidate']!r} for "
            f"{p['days']} days across {p['confirmations']} clean probes. Promote it "
            "into selectors.py as the first candidate; the override is now the "
            "truth and the declared value is the fiction."
        )
    return notes
