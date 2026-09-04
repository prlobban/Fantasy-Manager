#!/usr/bin/env python
"""Our projection vs ESPN's, on seasons the model has never seen.

    python scripts/eval_projection.py
    python scripts/eval_projection.py --season 2025 --top 130

Both projections are compared against the SAME actuals, rescored into the 2026
league's rules, over the same player set. Three metrics, declared in
docs/projection-model-plan.md §1 before any of this was run:

  M1  Spearman over the draftable range (ESPN PPR rank <= --top). The only
      range in which a pick is ever decided.
  M2  Spearman within position. Cross-position correlation flatters any model
      that merely knows QBs outscore TEs.
  M3  the arena benchmark — not here; this script is diagnosis, scripts/
      benchmark.py is the acceptance test.

Coverage is reported, never papered over: the model needs prior NFL seasons, so
it has nothing to say about rookies. Those players are listed, not silently
scored as zero — a projection of 0 for a first-round rookie would flatter every
correlation while making the board unusable.
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import polars as pl  # noqa: E402

from core.backtest import history, rescore  # noqa: E402
from core.data.nflverse import espn_to_gsis  # noqa: E402
from core.espn.client import EspnClient  # noqa: E402
from core.espn.settings import load as load_settings  # noqa: E402
from core.model import market  # noqa: E402
from core.proj import features, model, nflstats  # noqa: E402
from scripts.fit_projection import spearman  # noqa: E402

log = logging.getLogger("eval")

MODELLED = {"QB", "RB", "WR", "TE"}


def actual_totals(year: int, scoring: dict[int, float]) -> dict[int, float]:
    """espn_id -> actual season points under the 2026 rules."""
    s = history.load(year)
    rs = rescore.rescored_weeks(s, scoring)
    return {pid: sum(wk.values()) for pid, wk in rs.weeks.items()}


def espn_projections(year: int, scoring: dict[int, float]) -> dict[int, float]:
    """espn_id -> ESPN's PRESEASON projection, rescored into 2026 rules."""
    s = history.load(year)
    return rescore.rescored_projections(s, scoring)


def our_projections(year: int, m: model.Model, scoring: dict[int, float]
                    ) -> tuple[dict[int, float], dict[int, float], list[str]]:
    """espn_id -> (points, confidence), plus the names we could not project."""
    lo = year - features.LOOKBACK
    wide = nflstats.seasons(list(range(lo, year)), scoring)
    rows = features.build(wide.filter(pl.col("season") < year), year)
    projs = model.project_all(rows, m)

    s = history.load(year)
    bridge = espn_to_gsis()
    pts: dict[int, float] = {}
    conf: dict[int, float] = {}
    missing: list[str] = []
    for p in s.players:
        if p.pos.value not in MODELLED:
            continue
        g = bridge.get(p.espn_id)
        pr = projs.get(g) if g else None
        if pr is None:
            missing.append(p.name)
            continue
        pts[p.espn_id] = pr.points
        conf[p.espn_id] = pr.confidence
    return pts, conf, missing


def evaluate(year: int, m: model.Model, scoring: dict[int, float], top: int,
             rank_type: str) -> dict:
    s = history.load(year)
    raw = json.loads((history.cache_dir(year) / "pool.json").read_text(encoding="utf-8"))
    ranks = market.ranks_from_raw(raw, rank_type)

    actual = actual_totals(year, scoring)
    espn = espn_projections(year, scoring)
    ours, conf, missing = our_projections(year, m, scoring)

    pos_of = {p.espn_id: p.pos.value for p in s.players}
    name_of = {p.espn_id: p.name for p in s.players}

    # The draftable range is defined by where players were RANKED preseason,
    # not by how they finished. Defining it on outcome would be hindsight.
    pool = [pid for pid, r in ranks.items()
            if r <= top and pos_of.get(pid) in MODELLED and pid in actual]
    covered = [pid for pid in pool if pid in ours]

    def m1(proj: dict[int, float], ids: list[int]) -> float:
        return spearman([proj.get(i, 0.0) for i in ids], [actual[i] for i in ids])

    def m2(proj: dict[int, float], ids: list[int]) -> float:
        out = []
        for p in sorted(MODELLED):
            sub = [i for i in ids if pos_of.get(i) == p]
            if len(sub) >= 5:
                out.append(spearman([proj.get(i, 0.0) for i in sub],
                                    [actual[i] for i in sub]))
        return statistics.fmean(out) if out else 0.0

    res = {
        "season": year, "rank_type": rank_type, "top": top,
        "pool": len(pool), "covered": len(covered),
        "uncovered_names": [name_of.get(i, str(i)) for i in pool if i not in ours][:12],
        "m1_espn": round(m1(espn, covered), 4),
        "m1_ours": round(m1(ours, covered), 4),
        "m2_espn": round(m2(espn, covered), 4),
        "m2_ours": round(m2(ours, covered), 4),
        "mean_confidence": round(statistics.fmean(
            [conf[i] for i in covered]) if covered else 0.0, 3),
        "missing_from_nflverse": len(missing),
    }
    # A blend of the two, at a few weights — the hypothesis being that two
    # partly-independent signals beat either alone.
    for w in (0.25, 0.4, 0.5, 0.6, 0.75):
        ladder = sorted((espn.get(i, 0.0) for i in covered), reverse=True)
        order = sorted(covered, key=lambda i: -ours.get(i, 0.0))
        implied = {pid: ladder[min(k, len(ladder) - 1)] for k, pid in enumerate(order)}
        mixed = {i: (1 - w) * espn.get(i, 0.0) + w * implied[i] for i in covered}
        res[f"m1_blend{w}"] = round(m1(mixed, covered), 4)
        res[f"m2_blend{w}"] = round(m2(mixed, covered), 4)
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--season", type=int, nargs="+", default=[2024, 2025])
    ap.add_argument("--top", type=int, default=130)
    ap.add_argument("--rank-type", default="PPR")
    ap.add_argument("--model", type=Path, default=Path("data/proj-model.json"))
    ap.add_argument("--json", type=Path, default=Path("data/backtest/proj-eval.json"))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    m = model.Model.from_json(args.model)
    if set(args.season) & set(m.trained_on):
        raise SystemExit(f"model was trained on {sorted(set(args.season) & set(m.trained_on))}"
                         " — evaluating on it would be meaningless")
    scoring = load_settings(EspnClient()).settings.scoring

    out = []
    for y in args.season:
        r = evaluate(y, m, scoring, args.top, args.rank_type)
        out.append(r)
        print(f"\n=== {y} · top {r['top']} {r['rank_type']} · "
              f"{r['covered']}/{r['pool']} covered ===")
        print(f"  M1 draftable-range   ESPN {r['m1_espn']:+.4f}   ours {r['m1_ours']:+.4f}")
        print(f"  M2 within-position   ESPN {r['m2_espn']:+.4f}   ours {r['m2_ours']:+.4f}")
        for w in (0.25, 0.4, 0.5, 0.6, 0.75):
            print(f"     blend {w:<5}        M1 {r[f'm1_blend{w}']:+.4f}"
                  f"        M2 {r[f'm2_blend{w}']:+.4f}")
        print(f"  mean confidence {r['mean_confidence']}   "
              f"no projection for: {', '.join(r['uncovered_names'][:6])}")

    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
