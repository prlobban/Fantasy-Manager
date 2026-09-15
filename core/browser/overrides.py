"""Healed selectors — the layer consulted BEFORE the built-in candidates.

A self-healing loop has to write down what it learned somewhere. The obvious
place is `selectors.py` itself, and that is the wrong place: those constants are
a mix of bare strings and multi-line implicit concatenations, so a source
patcher would be the most fragile component of the system meant to make the
system less fragile. A bad regex there does not break one selector, it breaks
the import and takes the whole agent down.

So healed candidates live here, in one JSON file:

    {"ADD_PLAYER_BUTTON": {"candidate": "button.add-action-btn-v2",
                           "at": "2026-09-15T14:02:11Z",
                           "why": "score 3.0: title='Add', in a player row",
                           "verified": true}}

Three properties fall out of that choice and all three matter:

1. **The built-ins are never destroyed.** `candidates()` returns the override
   FIRST and the original candidate chain after it, so a heal that guessed
   wrong still degrades to whatever used to work rather than to nothing.
2. **The diff is one readable file.** `git log core/browser/overrides.json`
   is the complete history of what the agent taught itself.
3. **Reverting is deleting a line.** No merge, no code review.

§10.6 still holds: nothing here fails open. A corrupt or unreadable overrides
file is logged and ignored, which returns the system to exactly its built-in
behaviour rather than to no behaviour.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.browser import groups as G

log = logging.getLogger(__name__)

PATH = Path(__file__).with_name("overrides.json")

#: An override that has never survived a re-probe is not trusted as the first
#: candidate. `heal()` only writes verified=True after the patched selector
#: resolved on a live page, so an unverified entry can only come from a crash
#: mid-write or a hand edit.
_cache: dict[str, dict[str, Any]] | None = None


def _load() -> dict[str, dict[str, Any]]:
    global _cache
    if _cache is not None:
        return _cache
    try:
        raw = json.loads(PATH.read_text(encoding="utf-8")) if PATH.exists() else {}
        if not isinstance(raw, dict):
            raise ValueError(f"overrides.json is a {type(raw).__name__}, not an object")
        _cache = {k: v for k, v in raw.items() if isinstance(v, dict) and v.get("candidate")}
    except Exception as e:
        # Fail closed to BUILT-IN behaviour, never to no behaviour.
        log.error("overrides.json unreadable (%s) — using built-in selectors only", e)
        _cache = {}
    return _cache


def reload() -> None:
    """Drop the cache. Called after a heal so the next lookup sees the patch."""
    global _cache
    _cache = None


def get(group: str) -> str | None:
    """The healed candidate for `group`, if one is on file and verified."""
    e = _load().get(group)
    if not e or not e.get("verified", False):
        return None
    return str(e["candidate"])


def all_entries() -> dict[str, dict[str, Any]]:
    return dict(_load())


def candidates(group: str, builtin: str | None = None) -> tuple[str, ...]:
    """Every candidate for `group`, healed first, built-ins after.

    This is the function the action layer calls. The built-in chain is always
    appended — a heal adds a way to find the element, it never removes one.
    """
    if builtin is None:
        g = G.get(group)
        builtin = g.builtin if g else ""
    chain: list[str] = []
    healed = get(group)
    if healed:
        chain.append(healed)
    for c in (builtin or "").split(","):
        c = c.strip()
        if c and c not in chain:
            chain.append(c)
    return tuple(chain)


def record(group: str, candidate: str, *, why: str, verified: bool) -> None:
    """Write a healed candidate to disk. Atomic — a torn write here would be
    read as a corrupt file on the next run and silently drop every heal."""
    if group not in G.BY_NAME:
        raise ValueError(f"unknown selector group {group!r} — add it to groups.py first")
    data = dict(_load())
    data[group] = {
        "candidate": candidate,
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "why": why,
        "verified": verified,
        "replaces": G.BY_NAME[group].builtin,
    }
    PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, PATH)
    finally:
        Path(tmp).unlink(missing_ok=True)
    reload()
    log.info("override recorded: %s -> %s (verified=%s)", group, candidate, verified)


def forget(group: str) -> bool:
    """Remove a healed candidate, returning the group to its built-ins."""
    data = dict(_load())
    if group not in data:
        return False
    del data[group]
    PATH.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    reload()
    log.info("override dropped: %s", group)
    return True


def commit(group: str, candidate: str, why: str) -> bool:
    """Commit overrides.json on the box.

    An unattended agent that changes its own behaviour and leaves no trace is
    the thing that makes autonomy unreviewable. This is what makes
    `git log core/browser/overrides.json` the complete answer to "what has it
    taught itself, and when".

    Never fatal: a heal that worked but could not be committed is still a heal,
    and dying here would turn a recovered write into a failed one.
    """
    import subprocess

    try:
        subprocess.run(["git", "add", str(PATH)], cwd=str(PATH.parents[2]),
                       check=True, capture_output=True, timeout=30)
        msg = (f"selfheal: {group} -> {candidate}\n\n{why}\n\n"
               "Written by the self-healing loop after the candidate was verified\n"
               "to resolve on a live page. Revert by deleting the entry from\n"
               "core/browser/overrides.json; the built-in candidates are untouched.")
        r = subprocess.run(["git", "commit", "-m", msg], cwd=str(PATH.parents[2]),
                           capture_output=True, timeout=30, text=True)
        if r.returncode:
            log.warning("could not commit the heal: %s", (r.stdout or r.stderr).strip())
            return False
        return True
    except Exception as e:
        log.warning("could not commit the heal: %s", e)
        return False
