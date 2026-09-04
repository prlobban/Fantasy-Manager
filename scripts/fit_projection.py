#!/usr/bin/env python
"""Fit the projection model on nflverse seasons, holding out 2024 and 2025.

    python scripts/fit_projection.py
    python scripts/fit_projection.py --train 2012 2023 --out data/proj-model.json

**The held-out years are refused in code, not by discipline** (see HELD_OUT).
2024 and 2025 are the only seasons where ESPN's preseason projection survives,
so they are the only seasons where "did we beat ESPN" can be asked — and the
answer is worthless if the model was fitted on them.

## The objective, and the first version of it that was wrong

The first fit minimised mean absolute error over every labelled player-season
and drove **every parameter to its grid minimum**. That is not a model finding
an optimum, it is an objective pointing the wrong way: two thirds of player
seasons belong to players nobody drafts, so MAE over all of them is minimised by
projecting everyone downward. The fix is not a wider grid.

Two things changed:

1. **Fit on the population the model is actually used on** — players whose
   PRIOR season put them in draftable range (`_relevant`). Prior-season only, so
   it adds no leakage.
2. **Split the objective by what each parameter can identify.** Shape
   parameters (shrinkage, recency, age) are fitted to maximise **within-position
   rank correlation** — ordering is what a draft consumes. Scale (`base_games`)
   cannot move a within-position ordering at all, so it is fitted afterwards, to
   MAE, which calibrates the level that cross-position VOR depends on.

Fitting scale and shape against one objective is what let a degenerate solution
score well.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.espn.client import EspnClient  # noqa: E402
from core.espn.settings import load as load_settings  # noqa: E402
from core.proj import features, model  # noqa: E402
from core.proj.features import Row  # noqa: E402

log = logging.getLogger("fit")

#: Never fitted on. These are the head-to-head test seasons.
HELD_OUT = (2024, 2025)

DEFAULT_TRAIN = (2012, 2023)

#: Roughly the draftable depth at each position in a 10-team league, widened so
#: the fit still sees the players who break out from just outside the pool.
DRAFTABLE = {"QB": 32, "RB": 60, "WR": 70, "TE": 32}

#: Shape parameters — fitted to within-position rank correlation.
SHAPE_GRID = {
    "k_opp": [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 64.0],
    "k_eff": [20.0, 60.0, 120.0, 250.0, 500.0, 1000.0, 2000.0],
    "age_cliff": [23.0, 25.0, 26.0, 27.0, 28.0, 29.0, 30.0, 99.0],
    "age_slope": [0.0, 0.25, 0.5, 1.0, 2.0],
    "recency": [[1.0, 0.0, 0.0], [1.0, 0.3, 0.1], [1.0, 0.5, 0.25],
                [1.0, 0.8, 0.6], [1.0, 1.0, 1.0]],
}

#: Scale — fitted afterwards, to MAE. Cannot affect within-position ordering.
SCALE_GRID = {"base_games": [11.0, 12.0, 13.0, 14.0, 15.0, 16.0, 17.0]}


def _rank(xs: list[float]) -> list[float]:
    """Average ranks, ties shared."""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    out = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            out[order[k]] = avg
        i = j + 1
    return out


def spearman(a: list[float], b: list[float]) -> float:
    if len(a) < 3:
        return 0.0
    ra, rb = _rank(a), _rank(b)
    ma, mb = statistics.fmean(ra), statistics.fmean(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb, strict=True))
    da = math.sqrt(sum((x - ma) ** 2 for x in ra))
    db = math.sqrt(sum((y - mb) ** 2 for y in rb))
    return num / (da * db) if da and db else 0.0


def _relevant(rows: list[Row], pos: str) -> list[Row]:
    """Rows whose PRIOR season put them in draftable range at this position.

    Defined entirely on prior-season data, so it narrows the population without
    smuggling in anything about the season being projected.
    """
    sub = [r for r in rows
           if r.position == pos and r.actual_points is not None and r.has_history]
    n = DRAFTABLE.get(pos, 50)
    by_year: dict[int, list] = {}
    for r in sub:
        by_year.setdefault(r.target_year, []).append(r)
    out = []
    for _, rs in by_year.items():
        rs.sort(key=lambda r: -(r.prior_rates[0].get("points", 0.0) * r.prior_games[0]))
        out.extend(rs[:n])
    return out


def fit_position(rows: list[Row], pos: str) -> model.PosParams:
    p = model.PosParams()
    p.mean_opp_pg, p.mean_eff = model.fit_means(rows, pos)
    sub = _relevant(rows, pos)
    if len(sub) < 30:
        log.warning("  %s: only %d relevant rows — keeping defaults", pos, len(sub))
        return p

    # Rank correlation is computed WITHIN each target year, then averaged: a
    # pooled correlation across years would partly measure which season scored
    # more, which no draft decision depends on.
    years = sorted({r.target_year for r in sub})
    groups = [[r for r in sub if r.target_year == y] for y in years]

    def shape_score(pp: model.PosParams) -> float:
        return statistics.fmean(
            spearman([model.project(r, pp).points for r in g],
                     [r.actual_points for r in g]) for g in groups if len(g) >= 5)

    def scale_score(pp: model.PosParams) -> float:
        return statistics.fmean(
            abs(model.project(r, pp).points - r.actual_points) for r in sub)

    best = shape_score(p)
    log.info("  %s: %d relevant rows over %d years — start rho %.4f",
             pos, len(sub), len(years), best)

    for _ in range(4):
        improved = False
        for name, values in SHAPE_GRID.items():
            for v in values:
                trial = model.PosParams(**dict(vars(p)))
                if name == "recency":
                    trial.season_weights = list(v)
                else:
                    setattr(trial, name, v)
                s = shape_score(trial)
                if s > best + 1e-9:
                    best, p, improved = s, trial, True
        if not improved:
            break

    scale_best = scale_score(p)
    for v in SCALE_GRID["base_games"]:
        trial = model.PosParams(**dict(vars(p)))
        trial.base_games = v
        s = scale_score(trial)
        if s < scale_best - 1e-9:
            scale_best, p = s, trial

    log.info("  %s: rho %.4f  MAE %.1f | k_opp %.0f  k_eff %.0f  games %.0f  "
             "cliff %.0f/%.2f  w %s", pos, best, scale_best, p.k_opp, p.k_eff,
             p.base_games, p.age_cliff, p.age_slope, p.season_weights)
    return p


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--train", type=int, nargs=2, default=list(DEFAULT_TRAIN),
                    metavar=("FIRST", "LAST"))
    ap.add_argument("--out", type=Path, default=Path("data/proj-model.json"))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    years = list(range(args.train[0], args.train[1] + 1))
    bad = sorted(set(years) & set(HELD_OUT))
    if bad:
        raise SystemExit(
            f"refusing to train on {bad}: those seasons are the ESPN head-to-head "
            f"test set. Fitting on them makes the comparison meaningless.")

    scoring = load_settings(EspnClient()).settings.scoring
    log.info("building training set for %d-%d ...", years[0], years[-1])
    rows = features.training_set(years, scoring)

    m = model.Model(trained_on=years, held_out=list(HELD_OUT))
    for pos in model.POSITIONS:
        m.params[pos] = fit_position(rows, pos)

    m.to_json(args.out)
    log.info("\nwrote %s", args.out)
    print(json.dumps({p: {"k_opp": v.k_opp, "k_eff": v.k_eff,
                          "base_games": v.base_games,
                          "age": [v.age_cliff, v.age_slope],
                          "w": v.season_weights}
                      for p, v in m.params.items()}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
