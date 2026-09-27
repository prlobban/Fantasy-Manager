"""What the last run reported as wrong, and the ledger of what repair did about it.

Three sources, all already written by every run:

  * the journal entry's `escalated` — the manager's own "Needs Pearce"
  * `did` outcomes that failed — a write that errored
  * the run's slice of manager.log — tracebacks and failed agent runs

Some faults are not code and are never sent to the repair agent: capacity
limits, an expired Claude login, dead ESPN cookies. Those already alert, and no
diff fixes them.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

#: A run that died of one of these needs a human or a clock, not a patch.
NOT_CODE = re.compile(
    r"session limit|usage limit|rate limit|overloaded|credit balance|"
    r"not logged in|failed to authenticate|oauth session expired|"
    r"invalid api key|claude auth login|espn_s2|log ?in required|NotLoggedIn",
    re.I,
)
_LOG_FAULT = re.compile(r"Traceback|AGENT FAILED|agent run failed|FAILED:|ActionFailed|"
                        r"Error executing tool|execution failed", re.I)


@dataclass
class Issue:
    source: str        # "escalation" | "write" | "log"
    text: str
    evidence: str = ""

    @property
    def key(self) -> str:
        """Stable-ish identity: the text with numbers and ids flattened, so
        the same fault restated with today's figures still matches."""
        flat = re.sub(r"[\d.]+", "#", self.text.lower())
        flat = re.sub(r"\s+", " ", flat)[:200]
        return hashlib.sha1(flat.encode()).hexdigest()[:12]


def log_slice(log_path: Path, offset: int) -> str:
    try:
        with log_path.open("rb") as f:
            f.seek(max(0, offset))
            return f.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def collect(journal_path: Path, log_text: str, *, since: datetime) -> list[Issue]:
    issues: list[Issue] = []

    entry = _last_journal_entry(journal_path, since)
    if entry:
        esc = (entry.get("escalated") or "").strip()
        if esc and not NOT_CODE.search(esc):
            issues.append(Issue("escalation", esc))
        for d in entry.get("did") or []:
            out = str(d.get("outcome", ""))
            if "fail" in out.lower():
                issues.append(Issue("write", f"{d.get('what')}: {out}"))

    faults = [ln for ln in log_text.splitlines() if _LOG_FAULT.search(ln)]
    if faults and not NOT_CODE.search(log_text[-4000:]):
        tb = _last_traceback(log_text)
        issues.append(Issue("log", faults[-1].strip()[:400],
                            evidence=tb or "\n".join(faults[-10:])))
    return issues


def _last_journal_entry(path: Path, since: datetime) -> dict | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            at = datetime.fromisoformat(e.get("at", ""))
        except ValueError:
            return None
        return e if at >= since else None
    return None


def _last_traceback(text: str) -> str:
    i = text.rfind("Traceback (most recent call last)")
    return text[i:i + 4000] if i >= 0 else ""


# ── ledger ───────────────────────────────────────────────────────────────────


@dataclass
class LedgerEntry:
    at: str
    status: str                    # deployed | rejected | no_fix | reverted | proven | error
    issues: list[dict] = field(default_factory=list)
    summary: str = ""
    commit: str | None = None
    base: str | None = None
    detail: str = ""
    cost_usd: float | None = None


def read_ledger(path: Path) -> list[dict]:
    try:
        return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    except (OSError, json.JSONDecodeError):
        return []


def append_ledger(path: Path, e: LedgerEntry) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(e), default=str) + "\n")


def attempts_today(ledger: list[dict], now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    day = now.date().isoformat()
    return sum(1 for e in ledger
               if str(e.get("at", "")).startswith(day)
               and e.get("status") in ("deployed", "rejected", "no_fix", "error"))


def fresh(issues: list[Issue], ledger: list[dict], *, now: datetime | None = None,
          max_tries: int = 2, window_days: int = 7) -> list[Issue]:
    """Issues worth a repair run: not tried `max_tries` times this week.

    Every attempt counts, a deployed one included: if the same fault comes
    back after its fix shipped, the fix did not work, and the second attempt
    sees the first in the ledger. After that it is Pearce's."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=window_days)
    tries: dict[str, int] = {}
    for e in ledger:
        try:
            at = datetime.fromisoformat(str(e.get("at")))
        except ValueError:
            continue
        if at < cutoff:
            continue
        for i in e.get("issues", []):
            k = i.get("key")
            tries[k] = tries.get(k, 0) + 1
    return [i for i in issues if tries.get(i.key, 0) < max_tries]


def pending_deploy(ledger: list[dict]) -> dict | None:
    """The latest deployed repair not yet proven by a clean run, if any."""
    for e in reversed(ledger):
        if e.get("status") in ("proven", "reverted"):
            return None
        if e.get("status") == "deployed":
            return e
    return None
