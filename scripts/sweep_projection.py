#!/usr/bin/env python
"""M3 — does the projection model improve the DRAFT, not just the correlation?

    python scripts/sweep_projection.py
    python scripts/sweep_projection.py --weights 0 0.15 0.3

Sweeps `model.projection_blend` over the paired-seat benchmark and applies the
gate from docs/projection-model-plan.md §1: a weight ships only if it improves
mean finish in **all four blocks** — 2024/STANDARD, 2024/PPR, 2025/STANDARD,
2025/PPR. Winning the average by winning one block and losing another is a fit
to one season.

Unlike `scripts/optimize.py`, boards are rebuilt for every weight: the blend
rewrites `proj_season` upstream of valuation, so VOR and tiers move with it and
a cached board would silently score the wrong thing.
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.backtest import arena, autopick, history, replay  # noqa: E402
from core.model.priors import overridden  # noqa: E402
from core.proj import apply as proj_apply  # noqa: E402
from core.proj import model as proj_model  # noqa: E402
from scripts import benchmark as bm  # noqa: E402

log = logging.getLogger("sweep")

BLOCKS = [(2024, "STANDARD"), (2024, "PPR"), (2025, "STANDARD"), (2025, "PPR")]
WEIGHTS = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.65, 0.8]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--weights", type=float, nargs="+", default=WEIGHTS)
    ap.add_argument("--model", type=Path,
                    default=Path("data/proj-model.json"))
    ap.add_argument("--json", type=Path,
                    default=Path("data/backtest/sweep-projection.json"))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    m = proj_model.Model.from_json(args.model)
    tf = bm.target_facts()

    # Seasons, actuals, autopick ranks and the all-autopick control: none of
    # these depend on the weight, so they are built once.
    prep, projections, ranks, base = {}, {}, {}, {}
    for year in sorted({y for y, _ in BLOCKS}):
        if year in m.trained_on:
            raise SystemExit(f"model trained on {year} — benchmarking there is circular")
        s, facts, actuals, weeks = bm.prepare(year, tf)
        prep[year] = (s, facts, actuals, weeks)
        projections[year] = proj_apply.project_pool(
            s.players, m, facts.settings.scoring, year)
        raw = json.loads((history.cache_dir(year) / "pool.json").read_text(encoding="utf-8"))
        ranks[year] = {rk: autopick.ranks_from_pool(raw, rk)
                       for rk in ("STANDARD", "PPR")}
        # The control draft depends only on ESPN's ranks, never on our board.
        plain = replay.build_board(s, facts=facts, market_ranks=bm.market_ranks(year))
        base[year] = {rk: bm.score_league(
            arena.run(plain, facts, r, engine_seat=None, teams=bm.TEAMS).rosters,
            facts, actuals, weeks, "hindsight") for rk, r in ranks[year].items()}

    rows, results = [], {}
    for w in args.weights:
        with overridden(model__projection_blend=w):
            # Rebuilt from a FRESH season each weight: both blends rewrite
            # `proj_season` in place, so reusing player objects would compound
            # one weight's blend on top of the last one's.
            boards = {}
            for year in list(prep):
                prep[year] = bm.prepare(year, tf)
                s, facts, actuals, weeks = prep[year]
                boards[year] = replay.build_board(
                    s, facts=facts, market_ranks=bm.market_ranks(year),
                    projections=projections[year])

            blocks, all_ranks, all_delta, wins = {}, [], [], 0
            for year, rk in BLOCKS:
                s, facts, actuals, weeks = prep[year]
                rr, rd, rw = [], [], 0
                for seat in range(1, bm.TEAMS + 1):
                    res = arena.run(boards[year], facts, ranks[year][rk],
                                    engine_seat=seat, teams=bm.TEAMS)
                    pts = bm.score_league(res.rosters, facts, actuals, weeks, "hindsight")
                    ours = pts[seat]
                    field = sorted(pts.values(), reverse=True)
                    rr.append(field.index(ours) + 1)
                    d = ours - base[year][rk][seat]
                    rd.append(d)
                    rw += d > 0
                blocks[f"{year}/{rk}"] = (statistics.fmean(rr), rw, statistics.fmean(rd))
                all_ranks += rr
                all_delta += rd
                wins += rw
            results[w] = blocks
            row = {"weight": w, "mean_rank": round(statistics.fmean(all_ranks), 3),
                   "wins": wins, "pairs": len(all_ranks),
                   "mean_delta": round(statistics.fmean(all_delta), 1),
                   "blocks": {k: round(v[0], 2) for k, v in blocks.items()}}
            rows.append(row)
            log.info("w=%-5s finish %.2f/10  beats ESPN %2d/%d  %+7.1f pts   %s",
                     w, row["mean_rank"], wins, len(all_ranks), row["mean_delta"],
                     "  ".join(f"{k} {v:.2f}" for k, v in row["blocks"].items()))

    print("\n" + "=" * 78)
    zero = results.get(0.0)
    if zero:
        ok = [r for r in rows if r["weight"] > 0 and all(
            results[r["weight"]][k][0] <= zero[k][0] for k in zero) and any(
            results[r["weight"]][k][0] < zero[k][0] for k in zero)]
        if ok:
            best = min(ok, key=lambda r: r["mean_rank"])
            print(f"ACCEPTED: model.projection_blend = {best['weight']}  "
                  f"finish {rows[0]['mean_rank']:.2f} -> {best['mean_rank']:.2f}  "
                  f"wins {rows[0]['wins']} -> {best['wins']}/40")
        else:
            print("NOTHING CLEARS THE BAR — no weight improves all four blocks.")
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
