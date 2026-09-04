"""§P — the projection: opportunity x regressed efficiency x expected games.

The design exists because the two halves of fantasy production behave nothing
alike from one year to the next:

- **Opportunity persists.** Carries, targets and pass attempts are decided by
  coaches and depth charts, and they carry forward. Last year's target share is
  a real signal about this year's.
- **Efficiency reverts, hard** — and touchdown rate reverts hardest of all. A
  back who scored on 8% of his carries will not do it again; he regresses toward
  roughly the positional mean. Projecting last year's efficiency forward is the
  single most common amateur error, and avoiding it is most of the value here.

So neither is projected the same way. Both are shrunk toward the positional mean
by a James-Stein style weight, but with **separate** shrinkage constants that the
fitter learns from thirteen seasons:

    opp_pg  = (Σ wᵢ·gᵢ·oppᵢ + k_opp·mean_opp) / (Σ wᵢ·gᵢ + k_opp)
    eff     = (Σ wᵢ·oppᵢ·gᵢ·effᵢ + k_eff·mean_eff) / (Σ wᵢ·oppᵢ·gᵢ + k_eff)
    points  = games × opp_pg × eff

`k` is in units of the evidence it competes with: `k_opp` is a number of games,
`k_eff` a number of opportunities. A player with fewer than `k` is pulled mostly
to the mean; one with far more keeps most of his own rate. That is the whole
mechanism, and it is why a 14-parameter model can be fitted on 13 seasons without
overfitting.

**What this model cannot see** is as important as what it can: a coaching change,
a holdout, a depth-chart move, a scheme change, or a rookie with no NFL snaps.
`project` returns `confidence` alongside the number so the caller can defer to
consensus exactly where the model is blind, rather than pretending otherwise.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

from core.proj.features import Row

#: Positions the model covers. K and D/ST stay on ESPN — their week to week
#: outcome is close to noise, and no opportunity model helps.
POSITIONS = ("QB", "RB", "WR", "TE")

#: The opportunity denominator per position. QBs are dominated by dropbacks,
#: pass-catchers by targets, backs by carries plus targets (a back's receiving
#: work is real opportunity and ignoring it undervalues every third-down back).
OPP_COLS: dict[str, tuple[str, ...]] = {
    "QB": ("attempts", "carries"),
    "RB": ("carries", "targets"),
    "WR": ("targets", "carries"),
    "TE": ("targets",),
}


@dataclass
class PosParams:
    """One position's fitted constants."""

    #: Recency weights over prior seasons, most recent first.
    season_weights: list[float] = field(default_factory=lambda: [1.0, 0.5, 0.25])
    #: Shrinkage of opportunity-per-game toward the positional mean, in games.
    k_opp: float = 8.0
    #: Shrinkage of points-per-opportunity toward the positional mean, in
    #: opportunities. Much larger than k_opp — that asymmetry IS the model.
    k_eff: float = 120.0
    #: Expected games for a player with history, before the age adjustment.
    base_games: float = 15.0
    #: Games lost per year past the position's age cliff.
    age_slope: float = 0.0
    age_cliff: float = 99.0
    #: Population means, learned from the training seasons.
    mean_opp_pg: float = 0.0
    mean_eff: float = 0.0


@dataclass
class Model:
    """The fitted projection model. Serialisable, so a fit is reproducible."""

    params: dict[str, PosParams] = field(default_factory=dict)
    trained_on: list[int] = field(default_factory=list)
    #: Seasons deliberately held out — asserted at fit time.
    held_out: list[int] = field(default_factory=list)

    def to_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "params": {k: asdict(v) for k, v in self.params.items()},
            "trained_on": self.trained_on,
            "held_out": self.held_out,
        }, indent=1), encoding="utf-8")

    @classmethod
    def from_json(cls, path: Path) -> Model:
        d = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            params={k: PosParams(**v) for k, v in d["params"].items()},
            trained_on=d.get("trained_on", []),
            held_out=d.get("held_out", []),
        )


@dataclass
class Projection:
    points: float
    games: float
    opp_per_game: float
    eff: float
    #: 0..1 — how much evidence stands behind this number. Low means the model
    #: is guessing the positional mean and the caller should prefer consensus.
    confidence: float
    reason: str = ""


def _opportunity(rates: dict[str, float], pos: str) -> float:
    return sum(rates.get(c, 0.0) for c in OPP_COLS.get(pos, ()))


def project(row: Row, p: PosParams) -> Projection:
    """Project one player-season. Pure arithmetic on prior-season features."""
    pos = row.position
    w = p.season_weights

    # ── opportunity per game, shrunk by how many games back it ──────────────
    num = den = 0.0
    for i, g in enumerate(row.prior_games[:len(w)]):
        if g <= 0:
            continue
        opp_pg = _opportunity(row.prior_rates[i], pos)
        num += w[i] * g * opp_pg
        den += w[i] * g
    opp_pg = (num + p.k_opp * p.mean_opp_pg) / (den + p.k_opp)
    games_of_evidence = den

    # ── points per opportunity, shrunk by how many opportunities back it ────
    enum = eden = 0.0
    for i, g in enumerate(row.prior_games[:len(w)]):
        if g <= 0:
            continue
        opps = _opportunity(row.prior_rates[i], pos) * g
        if opps <= 0:
            continue
        eff = (row.prior_rates[i].get("points", 0.0) * g) / opps
        enum += w[i] * opps * eff
        eden += w[i] * opps
    eff = (enum + p.k_eff * p.mean_eff) / (eden + p.k_eff)

    # ── expected games ──────────────────────────────────────────────────────
    games = p.base_games
    if row.age is not None and row.age > p.age_cliff:
        games -= p.age_slope * (row.age - p.age_cliff)
    games = max(1.0, min(17.0, games))

    # Evidence is measured against the shrinkage constant itself: a player with
    # k_opp games of history is exactly half his own rate and half the mean.
    conf = games_of_evidence / (games_of_evidence + p.k_opp) if p.k_opp > 0 else 1.0

    return Projection(
        points=max(0.0, games * opp_pg * eff),
        games=games,
        opp_per_game=opp_pg,
        eff=eff,
        confidence=round(conf, 3),
        reason=f"{games_of_evidence:.0f} weighted games of history",
    )


def project_all(rows: list[Row], m: Model) -> dict[str, Projection]:
    out: dict[str, Projection] = {}
    for r in rows:
        p = m.params.get(r.position)
        if p is None:
            continue
        out[r.gsis_id] = project(r, p)
    return out


# ── population means ─────────────────────────────────────────────────────────


def fit_means(rows: list[Row], pos: str) -> tuple[float, float]:
    """Opportunity-weighted positional means.

    Weighted by games and opportunity respectively, so a player with two carries
    does not drag the mean as hard as a bell-cow — the mean is the thing every
    low-evidence player is shrunk toward, so a mean polluted by scrubs would
    push every uncertain projection down.
    """
    num = den = enum = eden = 0.0
    for r in rows:
        if r.position != pos:
            continue
        for i, g in enumerate(r.prior_games):
            if g <= 0:
                continue
            opp_pg = _opportunity(r.prior_rates[i], pos)
            num += g * opp_pg
            den += g
            opps = opp_pg * g
            if opps > 0:
                enum += r.prior_rates[i].get("points", 0.0) * g
                eden += opps
    return (num / den if den else 0.0), (enum / eden if eden else 0.0)


def mae(rows: list[Row], m: Model) -> float:
    """Mean absolute error in season points, over rows that have a label."""
    errs = []
    for r in rows:
        p = m.params.get(r.position)
        if p is None or r.actual_points is None:
            continue
        errs.append(abs(project(r, p).points - r.actual_points))
    return sum(errs) / len(errs) if errs else math.inf
