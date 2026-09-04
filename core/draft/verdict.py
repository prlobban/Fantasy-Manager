"""§3.10 — what the judge said, and what it is actually allowed to do.

The judge writes a file; the loop reads a file. This module is the gate between
them, and every limit in it is enforced here rather than in the prompt — §10.3:
a rule the model is merely *told* is a suggestion, and the whole reason the
draft loop is deterministic is that suggestions are not good enough on a clock.

Four things are checked, in order of how much damage they prevent:

1. **Freshness.** A verdict names the pick it was written for. If the room has
   moved on, it is ignored — a stale verdict is a decision about a board that
   no longer exists.
2. **Scope.** A lever may only touch a candidate the judge was actually shown.
3. **Tier.** A reorder must be within a tier. Cross-tier promotion is the one
   lever deliberately not granted, and this is where that is true rather than
   merely asked for.
4. **Counts and citations.** Capped per turn; an uncited lever is refused the
   same way an uncited action is (§8.2a).

A rejected instruction never invalidates the rest of the verdict. The judge
getting one thing wrong should cost that one thing.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from core.model.priors import priors

log = logging.getLogger(__name__)


@dataclass
class Lever:
    kind: str                    # "veto" | "reorder"
    espn_id: int
    name: str
    reason: str
    cites: list[str]
    dossier_fact: str
    above_espn_id: int | None = None


@dataclass
class Verdict:
    for_overall: int
    agree: bool
    summary: str
    vetoes: list[Lever] = field(default_factory=list)
    reorders: list[Lever] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    #: Instructions refused, and why. Logged and posted — a judge whose levers
    #: are being thrown away is a judge someone needs to look at.
    rejected: list[str] = field(default_factory=list)
    written_at: datetime | None = None

    @property
    def acts(self) -> bool:
        return bool(self.vetoes or self.reorders)

    def describe(self) -> str:
        if not self.acts:
            return "agree"
        bits = []
        for lv in self.vetoes:
            bits.append(f"VETO {lv.name}")
        for lv in self.reorders:
            bits.append(f"{lv.name} above #{lv.above_espn_id}")
        return " · ".join(bits)


def _lever(raw: dict, kind: str) -> Lever | None:
    try:
        return Lever(
            kind=kind,
            espn_id=int(raw["espn_id"]),
            name=str(raw.get("name") or ""),
            reason=str(raw.get("reason") or ""),
            cites=[str(c) for c in (raw.get("cites") or [])],
            dossier_fact=str(raw.get("dossier_fact") or ""),
            above_espn_id=(int(raw["above_espn_id"])
                           if raw.get("above_espn_id") is not None else None),
        )
    except (KeyError, TypeError, ValueError):
        return None


def parse(raw: dict, *, plan=None, for_overall: int | None = None,
          dossiers: dict | None = None) -> Verdict:
    """Validate one raw verdict against the plan it claims to be about.

    `plan` is the PickPlan the judge was shown. Pass it and the scope and tier
    rules apply; omit it (tests, replay) and only the shape is checked.
    """
    p = priors()
    max_v = int(p.get("judge.max_vetoes_per_turn"))
    max_r = int(p.get("judge.max_reorders_per_turn"))
    n_cand = int(p.get("judge.candidates"))

    v = Verdict(
        for_overall=int(raw.get("for_overall") or 0),
        agree=bool(raw.get("agree")),
        summary=str(raw.get("summary") or ""),
        flags=[str(f) for f in (raw.get("flags") or [])],
    )
    ts = raw.get("written_at")
    if ts:
        try:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            v.written_at = dt if dt.tzinfo else dt.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            pass

    # 1. Freshness — a verdict for a pick we have already made is not a
    #    late opinion, it is an answer to a different question.
    if for_overall is not None and v.for_overall != for_overall:
        v.rejected.append(
            f"whole verdict ignored: written for pick {v.for_overall}, "
            f"we are on {for_overall}")
        return Verdict(for_overall=for_overall, agree=True,
                       summary=v.summary, rejected=v.rejected)

    # 2. Scope — only the candidates the judge was shown, and only the top N.
    shown: dict[int, object] = {}
    tiers: dict[int, int | None] = {}
    if plan is not None:
        for c in plan.top(n_cand):
            shown[c.player.espn_id] = c
            tiers[c.player.espn_id] = c.valuation.tier

    def in_scope(lv: Lever) -> bool:
        if plan is None:
            return True
        if lv.espn_id not in shown:
            v.rejected.append(
                f"{lv.kind} {lv.name or lv.espn_id}: not in the top {n_cand} "
                "candidates it was shown")
            return False
        return True

    def cited(lv: Lever) -> bool:
        # §8.2a — an uncited lever is refused, not fixed up.
        if not lv.cites:
            v.rejected.append(f"{lv.kind} {lv.name or lv.espn_id}: no § citation")
            return False
        if not lv.dossier_fact:
            v.rejected.append(
                f"{lv.kind} {lv.name or lv.espn_id}: no dossier_fact — a lever "
                "must rest on the record, not on the model's own recall")
            return False
        return True

    for raw_lv in (raw.get("veto") or []):
        lv = _lever(raw_lv, "veto")
        if lv is None:
            v.rejected.append("veto: unparseable")
            continue
        if not in_scope(lv) or not cited(lv):
            continue
        if len(v.vetoes) >= max_v:
            v.rejected.append(f"veto {lv.name}: over the cap of {max_v}")
            continue
        v.vetoes.append(lv)

    for raw_lv in (raw.get("reorder") or []):
        lv = _lever(raw_lv, "reorder")
        if lv is None or lv.above_espn_id is None:
            v.rejected.append("reorder: unparseable")
            continue
        if not in_scope(lv) or not cited(lv):
            continue
        if plan is not None:
            if lv.above_espn_id not in shown:
                v.rejected.append(
                    f"reorder {lv.name}: target {lv.above_espn_id} not in the "
                    "candidate list")
                continue
            # 3. The lever that does not exist. Tiers are the model's statement
            #    about who is interchangeable; crossing one is re-ranking.
            t_from, t_to = tiers.get(lv.espn_id), tiers.get(lv.above_espn_id)
            if t_from != t_to:
                v.rejected.append(
                    f"reorder {lv.name}: tier {t_from} over tier {t_to} — "
                    "cross-tier promotion is not a lever the judge holds")
                continue
            if lv.espn_id == lv.above_espn_id:
                v.rejected.append(f"reorder {lv.name}: above itself")
                continue
        if len(v.reorders) >= max_r:
            v.rejected.append(f"reorder {lv.name}: over the cap of {max_r}")
            continue
        v.reorders.append(lv)

    # A verdict that says "agree" and then acts is contradicting itself; the
    # levers are the load-bearing half, so keep them and correct the flag.
    if v.acts:
        v.agree = False

    if v.rejected:
        log.warning("verdict for #%d: %d instruction(s) refused: %s",
                    v.for_overall, len(v.rejected), "; ".join(v.rejected))
    return v


def apply(plan, verdict: Verdict | None):
    """Return a new PickPlan with the verdict's levers applied.

    Never mutates the plan it is given: shadow mode ranks once and compares
    both, so the un-judged plan has to survive intact.
    """
    if verdict is None or not verdict.acts:
        return plan

    from dataclasses import replace

    cands = list(plan.candidates)

    vetoed = {lv.espn_id: lv for lv in verdict.vetoes}
    if vetoed:
        kept = []
        for c in cands:
            lv = vetoed.get(c.player.espn_id)
            if lv is None:
                kept.append(c)
                continue
            log.info("judge vetoed %s: %s", c.player.name, lv.reason)
        cands = kept

    for lv in verdict.reorders:
        src = next((i for i, c in enumerate(cands)
                    if c.player.espn_id == lv.espn_id), None)
        dst = next((i for i, c in enumerate(cands)
                    if c.player.espn_id == lv.above_espn_id), None)
        if src is None or dst is None:
            continue
        c = cands.pop(src)
        if src < dst:
            dst -= 1
        # Record the size of the override so §7 can grade it: how much score
        # the judge was willing to overrule.
        below = cands[dst] if dst < len(cands) else None
        if below is not None:
            c.reasons["agent_reorder"] = round(below.score - c.score, 2)
        c.note = (c.note + " · " if c.note else "") + f"judge: {lv.reason}"[:200]
        cands.insert(dst, c)
        log.info("judge moved %s above %s", lv.name, lv.above_espn_id)

    return replace(plan, candidates=cands)


# ── on disk ──────────────────────────────────────────────────────────────────

def dir_for(draft_dir: Path) -> Path:
    return draft_dir / "verdicts"


def path_for(draft_dir: Path, overall: int) -> Path:
    return dir_for(draft_dir) / f"{overall}.json"


def write(draft_dir: Path, overall: int, payload: dict) -> Path:
    p = path_for(draft_dir, overall)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(payload)
    payload.setdefault("written_at", datetime.now(UTC).isoformat(timespec="seconds"))
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")
    tmp.replace(p)          # atomic: the loop must never read a half-written file
    return p


def read(draft_dir: Path, overall: int, *, plan=None) -> Verdict | None:
    """The verdict for this pick, if one landed in time and survived the gate."""
    p = path_for(draft_dir, overall)
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("verdict %s unreadable: %s", overall, e)
        return None
    return parse(raw, plan=plan, for_overall=overall)
