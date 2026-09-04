"""§3.2 — the research record, and the rules that decide whether it may move.

One JSON file per player under `data/dossiers/`, written by the research pass
(`agent/research.py`) and read by two consumers: the board, which turns
multipliers and vetoes into valuation input, and the judge, which reads the
facts.

**Why validation lives here and not in the prompt.** The retired `predraft`
task asked the model for a `source` and checked only that the string was
non-empty — which a model satisfies with the word "ESPN". A claim that cannot
be checked is not evidence, and evidence that cannot be checked should not be
allowed to move a draft board. So the rules below are the ones a schema cannot
state and a prompt cannot enforce:

- a source must be a URL with a real host, or the dossier is discarded;
- a veto needs high confidence AND two independent hosts;
- a large multiplier needs two independent hosts;
- anything past its shelf life is ignored.

Nothing is ever repaired. A dossier that fails a rule loses the *claim*, not
the file — a bad multiplier falls back to 1.0 and the durability and role notes
still reach the judge, because those are readable prose a human can weigh.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from core.config import settings
from core.model.priors import priors

log = logging.getLogger(__name__)

#: A dossier may only carry a multiplier this far from 1.0 on a single source.
#: Past it, two independent hosts are required. Chosen so a routine "he is
#: fine, nothing has changed" nudge is cheap and a real claim is not.
SINGLE_SOURCE_BAND = 0.05


@dataclass
class Dossier:
    espn_id: int
    name: str
    durability: dict
    role: dict
    news_since: list[dict]
    projection_check: dict
    multiplier: float
    veto: bool
    veto_reason: str | None
    confidence: str
    sources: list[str]
    researched_at: datetime | None = None
    #: Rules this dossier failed, and what was dropped as a result. Carried so
    #: the judge and the log can see a weakened record rather than a silent one.
    demotions: list[str] = field(default_factory=list)

    @property
    def hosts(self) -> set[str]:
        out = set()
        for u in self.sources:
            h = _host(u)
            if h:
                out.add(h)
        return out

    def age_hours(self, now: datetime | None = None) -> float | None:
        if self.researched_at is None:
            return None
        return ((now or datetime.now(UTC)) - self.researched_at).total_seconds() / 3600.0

    def summary(self) -> str:
        """One line, for a Slack post or a log."""
        bits = [f"{self.durability.get('verdict')}/{self.role.get('verdict')}",
                f"x{self.multiplier:g}", self.confidence]
        if self.veto:
            bits.insert(0, "VETO")
        if self.demotions:
            bits.append(f"({len(self.demotions)} demoted)")
        return " · ".join(bits)


def _host(url: str) -> str | None:
    """The registrable-ish host of a URL, or None if it is not really a URL.

    Deliberately strict: a "source" that does not parse to an http(s) URL with
    a dotted host is not a source, it is a sentence.
    """
    try:
        u = urlparse(str(url).strip())
    except (ValueError, AttributeError):
        return None
    if u.scheme not in ("http", "https") or not u.netloc:
        return None
    host = u.netloc.split("@")[-1].split(":")[0].lower()
    if host.startswith("www."):
        host = host[4:]
    if "." not in host or host.endswith("."):
        return None
    return host


def _parse_dt(raw) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def validate(raw: dict, *, now: datetime | None = None,
             max_age_hours: float | None = None) -> tuple[Dossier | None, list[str]]:
    """Turn one raw dossier into a `Dossier`, or reject it.

    Returns (dossier, problems). A dossier is returned even when problems were
    found, with the offending claims stripped and recorded in `demotions` — the
    prose is still useful. `None` means the record was not a dossier at all.
    """
    problems: list[str] = []

    try:
        espn_id = int(raw["espn_id"])
    except (KeyError, TypeError, ValueError):
        return None, ["no usable espn_id"]

    sources = [s for s in (raw.get("sources") or []) if isinstance(s, str)]
    good = [s for s in sources if _host(s)]
    bad = [s for s in sources if not _host(s)]
    if bad:
        problems.append(f"{len(bad)} source(s) are not URLs: {bad[:2]}")
    if not good:
        # Nothing here is checkable. This is the case the old validator passed.
        return None, problems + ["no verifiable source URL"]

    d = Dossier(
        espn_id=espn_id,
        name=str(raw.get("name") or ""),
        durability=raw.get("durability") or {},
        role=raw.get("role") or {},
        news_since=[n for n in (raw.get("news_since") or []) if isinstance(n, dict)],
        projection_check=raw.get("projection_check") or {},
        multiplier=float(raw.get("multiplier") or 1.0),
        veto=bool(raw.get("veto")),
        veto_reason=raw.get("veto_reason"),
        confidence=str(raw.get("confidence") or "low"),
        sources=good,
        researched_at=_parse_dt(raw.get("researched_at")),
    )

    # Shelf life. A dossier written before the last depth-chart shake-up is
    # worse than none, because it reads as current.
    cap = (max_age_hours if max_age_hours is not None
           else float(priors().get("research.max_age_hours")))
    age = d.age_hours(now)
    if age is not None and age > cap:
        return None, problems + [f"stale: {age:.0f}h old, cap {cap:.0f}h"]

    n_hosts = len(d.hosts)

    # A veto removes a player from the board entirely. That is the largest
    # thing a research agent can do, so it carries the largest evidence bar.
    if d.veto and (d.confidence != "high" or n_hosts < 2):
        d.demotions.append(
            f"veto dropped: confidence={d.confidence}, {n_hosts} host(s), "
            "needs high + 2")
        d.veto = False
        d.veto_reason = None

    # A big multiplier on one source is one reporter's sentence moving a draft.
    # The epsilon is load-bearing: abs(1.05 - 1.0) is 0.050000000000000044, so a
    # bare > would reject the exact band edge the prompt tells the model to use.
    if abs(d.multiplier - 1.0) > SINGLE_SOURCE_BAND + 1e-9 and n_hosts < 2:
        d.demotions.append(
            f"multiplier {d.multiplier:g} -> 1.0: only {n_hosts} host(s), needs 2")
        d.multiplier = 1.0

    # The dossier must agree with itself. Observed on the first live pass: a
    # dossier read `direction: fair`, argued in prose that "no injury-based
    # adjustment is warranted", and still sent 0.95. A model that reasons its
    # way to "no change" and then changes the number anyway is not carrying
    # evidence, it is drifting, and the prose is the honest half.
    direction = str(d.projection_check.get("direction") or "").lower()
    if direction == "fair" and d.multiplier != 1.0:
        d.demotions.append(
            f"multiplier {d.multiplier:g} -> 1.0: projection_check says 'fair'")
        d.multiplier = 1.0
    elif direction == "high" and d.multiplier > 1.0:
        d.demotions.append(
            f"multiplier {d.multiplier:g} -> 1.0: says the projection is HIGH "
            "but the multiplier raises it")
        d.multiplier = 1.0
    elif direction == "low" and d.multiplier < 1.0:
        d.demotions.append(
            f"multiplier {d.multiplier:g} -> 1.0: says the projection is LOW "
            "but the multiplier cuts it")
        d.multiplier = 1.0

    if d.demotions:
        problems.extend(d.demotions)
    return d, problems


# ── on disk ──────────────────────────────────────────────────────────────────

def path_for(espn_id: int) -> Path:
    return settings().dossiers_dir / f"{espn_id}.json"


def write(espn_id: int, payload: dict) -> Path:
    """Store one raw dossier, stamping it if the model did not."""
    payload = dict(payload)
    payload.setdefault("researched_at", datetime.now(UTC).isoformat(timespec="seconds"))
    p = path_for(espn_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
    return p


def load_one(espn_id: int, **kw) -> Dossier | None:
    p = path_for(espn_id)
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("dossier %s unreadable: %s", espn_id, e)
        return None
    d, problems = validate(raw, **kw)
    if d is None:
        log.info("dossier %s rejected: %s", espn_id, "; ".join(problems))
    return d


def load_all(**kw) -> dict[int, Dossier]:
    """Every valid dossier on disk, keyed by ESPN id. Rejections are logged."""
    out: dict[int, Dossier] = {}
    rejected: list[str] = []
    d_dir = settings().dossiers_dir
    if not d_dir.exists():
        return out
    for p in sorted(d_dir.glob("*.json")):
        if p.name.startswith("_"):        # _index, _usage, _measurement
            continue
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            rejected.append(f"{p.name}: unreadable ({e})")
            continue
        d, problems = validate(raw, **kw)
        if d is None:
            rejected.append(f"{p.name}: {'; '.join(problems)}")
            continue
        out[d.espn_id] = d
    if rejected:
        _log_rejects(rejected)
        log.info("loaded %d dossiers, rejected %d", len(out), len(rejected))
    return out


def _log_rejects(lines: list[str]) -> None:
    try:
        p = settings().dossiers_dir / "_rejects.log"
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        with p.open("a", encoding="utf-8") as f:
            for line in lines:
                f.write(f"{stamp} {line}\n")
    except OSError as e:
        log.debug("could not write reject log: %s", e)


# ── the bridge to the board ──────────────────────────────────────────────────

def write_overrides(dossiers: dict[int, Dossier] | None = None) -> dict:
    """Render the dossiers into `data/overrides.json`.

    Deliberately reuses the file shape `board._apply_overrides` already reads,
    so the board keeps one input for "what the agent thinks" and the clamp
    stays where it has always been.
    """
    ds = load_all() if dossiers is None else dossiers
    items = []
    for d in ds.values():
        if d.veto:
            items.append({
                "espn_id": d.espn_id, "name": d.name, "multiplier": 1.0,
                "veto": True,
                "reason": d.veto_reason or "research veto",
                "source": d.sources[0],
            })
        elif d.multiplier != 1.0:
            items.append({
                "espn_id": d.espn_id, "name": d.name,
                "multiplier": d.multiplier,
                "reason": (d.projection_check.get("why") or "")[:400],
                "source": d.sources[0],
            })
    payload = {
        "written_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "from_dossiers": len(ds),
        "overrides": items,
    }
    p = settings().overrides_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    log.info("wrote %d overrides (%d vetoes) from %d dossiers",
             len(items), sum(1 for i in items if i.get("veto")), len(ds))
    return payload
