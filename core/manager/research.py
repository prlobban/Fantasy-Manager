"""The morning research (D1, D3.1) — one in-season dossier per player, and the
bridge from those facts into the valuation.

Same discipline as the pre-draft dossiers (core/draft/dossiers.py): every claim
needs a real URL, a big move needs two hosts, the record must agree with
itself, and anything past its shelf life is ignored. Nothing is repaired; a
claim that fails a rule is dropped and the prose still reaches the agent.

Two consumers:
  - `contexts()` turns the bounded multipliers into PlayerContext for
    core.model.value, so "don't trust ESPN's raw projection" is a number the
    engine applies rather than a sentence the agent argues.
  - `facts()` renders the dossiers for the packet, so the agent reads the
    practice report, the usage line and the analyst read, with sources.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from core.config import settings
from core.draft.dossiers import _host, _parse_dt
from core.model.priors import priors
from core.model.value import PlayerContext

log = logging.getLogger(__name__)

_UNSTARTABLE = {"out", "ir", "suspended"}


@dataclass
class WeekDossier:
    espn_id: int
    name: str
    week: int
    status: dict
    usage: dict
    matchup: dict
    analyst_read: dict
    news_since: list[dict]
    week_multiplier: float
    ros_multiplier: float
    veto: bool
    veto_reason: str | None
    confidence: str
    sources: list[str]
    researched_at: datetime | None = None
    demotions: list[str] = field(default_factory=list)

    @property
    def hosts(self) -> set[str]:
        return {h for h in (_host(u) for u in self.sources) if h}

    def age_hours(self, now: datetime | None = None) -> float | None:
        if self.researched_at is None:
            return None
        return ((now or datetime.now(UTC)) - self.researched_at).total_seconds() / 3600

    def summary(self) -> str:
        bits = [
            f"{self.status.get('designation')}/{self.status.get('practice')}",
            f"usage {self.usage.get('trend')}",
            f"matchup {self.matchup.get('read')}",
            f"analysts {self.analyst_read.get('consensus')}",
            f"wk x{self.week_multiplier:g}",
        ]
        if self.ros_multiplier != 1.0:
            bits.append(f"ros x{self.ros_multiplier:g}")
        if self.veto:
            bits.insert(0, "VETO")
        if self.demotions:
            bits.append(f"({len(self.demotions)} demoted)")
        return " · ".join(bits)


def _clamp(x: float, cap: float) -> float:
    return max(1.0 - cap, min(1.0 + cap, x))


def validate(raw: dict, *, now: datetime | None = None,
             max_age_hours: float | None = None) -> tuple[WeekDossier | None, list[str]]:
    problems: list[str] = []
    try:
        espn_id = int(raw["espn_id"])
    except (KeyError, TypeError, ValueError):
        return None, ["no usable espn_id"]

    sources = [s for s in (raw.get("sources") or []) if isinstance(s, str)]
    good = [s for s in sources if _host(s)]
    if not good:
        return None, problems + ["no verifiable source URL"]

    p = priors()
    week_cap = float(p.get("season.week_override_cap"))
    ros_cap = float(p.get("model.override_cap"))

    d = WeekDossier(
        espn_id=espn_id,
        name=str(raw.get("name") or ""),
        week=int(raw.get("week") or 0),
        status=raw.get("status") or {},
        usage=raw.get("usage") or {},
        matchup=raw.get("matchup") or {},
        analyst_read=raw.get("analyst_read") or {},
        news_since=[n for n in (raw.get("news_since") or []) if isinstance(n, dict)],
        week_multiplier=_clamp(float(raw.get("week_multiplier") or 1.0), week_cap),
        ros_multiplier=_clamp(float(raw.get("ros_multiplier") or 1.0), ros_cap),
        veto=bool(raw.get("veto")),
        veto_reason=raw.get("veto_reason"),
        confidence=str(raw.get("confidence") or "low"),
        sources=good,
        researched_at=_parse_dt(raw.get("researched_at")),
    )

    cap = (max_age_hours if max_age_hours is not None
           else float(p.get("research_week.max_age_hours")))
    age = d.age_hours(now)
    if age is not None and age > cap:
        return None, problems + [f"stale: {age:.0f}h old, cap {cap:.0f}h"]

    n_hosts = len(d.hosts)
    designation = str(d.status.get("designation") or "").lower()

    # A veto keeps a player out of the lineup and off the add list. Evidence,
    # not adjectives: the designation must say he cannot play, and two hosts.
    if d.veto and (designation not in _UNSTARTABLE or n_hosts < 2):
        d.demotions.append(
            f"veto dropped: designation={designation!r}, hosts={n_hosts} (needs out/ir/suspended and 2)")
        d.veto = False
        d.veto_reason = None

    # A big weekly move on one source is one reporter's sentence moving a
    # lineup. ±10% on one host; past that, two hosts.
    if abs(d.week_multiplier - 1.0) > 0.10 + 1e-9 and n_hosts < 2:
        d.demotions.append(
            f"week_multiplier {d.week_multiplier:g} -> 1.0: only {n_hosts} host(s), needs 2")
        d.week_multiplier = 1.0
    if abs(d.ros_multiplier - 1.0) > 0.05 + 1e-9 and n_hosts < 2:
        d.demotions.append(
            f"ros_multiplier {d.ros_multiplier:g} -> 1.0: only {n_hosts} host(s), needs 2")
        d.ros_multiplier = 1.0

    # A ROS multiplier is for role changes; a matchup read must not touch it.
    if d.ros_multiplier != 1.0 and str(d.usage.get("trend") or "unknown") == "stable" \
            and not d.news_since:
        d.demotions.append(
            f"ros_multiplier {d.ros_multiplier:g} -> 1.0: usage stable and no news — "
            "nothing here is a role change")
        d.ros_multiplier = 1.0

    # Self-consistency: a player reported out cannot carry a weekly boost.
    if designation in _UNSTARTABLE and d.week_multiplier > 1.0:
        d.demotions.append(
            f"week_multiplier {d.week_multiplier:g} -> 1.0: designation {designation}")
        d.week_multiplier = 1.0

    problems.extend(d.demotions)
    return d, problems


# ── on disk ──────────────────────────────────────────────────────────────────

def directory() -> Path:
    return settings().data_dir / "research-week"


def path_for(espn_id: int) -> Path:
    return directory() / f"{espn_id}.json"


def write(espn_id: int, payload: dict, *, week: int) -> Path:
    payload = dict(payload)
    payload.setdefault("researched_at", datetime.now(UTC).isoformat(timespec="seconds"))
    payload["week"] = week
    p = path_for(espn_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
    return p


def load_one(espn_id: int, **kw) -> WeekDossier | None:
    p = path_for(espn_id)
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("week dossier %s unreadable: %s", espn_id, e)
        return None
    d, problems = validate(raw, **kw)
    if d is None:
        log.info("week dossier %s rejected: %s", espn_id, "; ".join(problems))
    return d


def load_all(*, week: int | None = None, **kw) -> dict[int, WeekDossier]:
    out: dict[int, WeekDossier] = {}
    d_dir = directory()
    if not d_dir.exists():
        return out
    rejected = 0
    for p in sorted(d_dir.glob("*.json")):
        if p.name.startswith("_"):
            continue
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            rejected += 1
            continue
        d, _ = validate(raw, **kw)
        if d is None or (week is not None and d.week and d.week != week):
            rejected += 1
            continue
        out[d.espn_id] = d
    log.info("week research: %d dossiers loaded, %d rejected", len(out), rejected)
    return out


# ── the bridge to the valuation ──────────────────────────────────────────────

def contexts(dossiers: dict[int, WeekDossier], *, window: str) -> dict[int, PlayerContext]:
    """PlayerContext per player, for the given window.

    Week: the multiplier goes in as a named §2.7 context term, so §7 can see
    exactly which term was biased. ROS: the bounded news override.
    """
    out: dict[int, PlayerContext] = {}
    for pid, d in dossiers.items():
        ctx = PlayerContext()
        if d.veto:
            ctx.news_veto = d.veto_reason or "research veto"
        if window == "week":
            if d.week_multiplier != 1.0:
                ctx.multipliers["research_week"] = d.week_multiplier
        else:
            if d.ros_multiplier != 1.0:
                ctx.news_override = d.ros_multiplier
                ctx.news_reason = (d.usage.get("detail") or "")[:200]
        out[pid] = ctx
    return out


def facts(dossiers: dict[int, WeekDossier], ids: list[int] | None = None) -> list[dict]:
    """The dossiers as the agent reads them: the facts, the sources, and
    what core did with the numbers."""
    rows = []
    for pid, d in dossiers.items():
        if ids is not None and pid not in ids:
            continue
        rows.append({
            "espn_id": pid,
            "name": d.name,
            "status": d.status,
            "usage": d.usage,
            "matchup": d.matchup,
            "analyst_read": d.analyst_read,
            "news_since": d.news_since[:4],
            "applied": {
                "week_multiplier": d.week_multiplier,
                "ros_multiplier": d.ros_multiplier,
                "veto": d.veto,
            },
            "confidence": d.confidence,
            "sources": d.sources[:4],
            "demoted_claims": d.demotions,
        })
    return rows
