#!/usr/bin/env python
"""Search the picker's parameters against the autopick benchmark.

    python scripts/optimize.py                    # the declared grid
    python scripts/optimize.py --param vor_weight
    python scripts/optimize.py --config draft.vor_weight=0.4   # score one config

The objective is **mean finish** across all 40 paired seats, not mean points:
points are dominated by how good a season the field had, finish is what "top 2"
means.

**The acceptance bar is deliberately hard.** Two seasons is the entire sample, so
a search that tries enough configurations will find one that looks good by luck.
A change is accepted only if it improves ALL FOUR blocks — 2024/STANDARD,
2024/PPR, 2025/STANDARD, 2025/PPR. Winning the average by winning one block and
losing another is a fit to one season, and is rejected on sight.

Boards are built once per season and reused for every configuration, because
these parameters live entirely inside `picker.rank`; valuation, VOR and tiers are
upstream of them and cannot move.
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.backtest import arena, autopick, history, replay  # noqa: E402
from core.model.priors import overridden, priors  # noqa: E402
from scripts import benchmark as bm  # noqa: E402

log = logging.getLogger("optimize")

BLOCKS = [(2024, "STANDARD"), (2024, "PPR"), (2025, "STANDARD"), (2025, "PPR")]

#: Declared up front, not grown while looking at results. Each entry needs a
#: mechanism written in the plan before it earns a place here.
GRID: dict[str, list[float]] = {
    "vor_weight": [0.0, 0.2, 0.35, 0.5, 0.65, 0.8, 1.0],
    "stack_penalty": [0.0, 15.0, 25.0, 45.0],
    "bench_opportunity_cost": [0.0, 15.0, 25.0, 45.0],
    "hole_weight": [0.0, 0.15, 0.35],
}


@dataclass
class Score:
    """One configuration's result over the whole benchmark."""

    mean_rank: float
    wins: int
    pairs: int
    mean_delta: float
    #: block -> (mean rank, wins, mean delta)
    blocks: dict[str, tuple[float, int, float]]

    def line(self) -> str:
        return (f"finish {self.mean_rank:.2f}/10   beats ESPN {self.wins:>2}/{self.pairs}"
                f"   {self.mean_delta:+7.1f} pts")

    def improves_every_block(self, other: Score) -> bool:
        """Strictly better finish in all four blocks — the anti-overfit gate."""
        return all(self.blocks[k][0] <= other.blocks[k][0] for k in other.blocks) and \
            any(self.blocks[k][0] < other.blocks[k][0] for k in other.blocks)


class Harness:
    """Seasons, boards and autopick baselines — all the expensive parts, once."""

    def __init__(self) -> None:
        tf = bm.target_facts()
        self.per_season = {}
        for year in sorted({y for y, _ in BLOCKS}):
            s, facts, actuals, weeks = bm.prepare(year, tf)
            board = replay.build_board(s, facts=facts,
                                       market_ranks=bm.market_ranks(year))
            raw = json.loads((history.cache_dir(year) / "pool.json")
                             .read_text(encoding="utf-8"))
            ranks = {rk: autopick.ranks_from_pool(raw, rk)
                     for rk in ("STANDARD", "PPR")}
            base = {}
            for rk, r in ranks.items():
                ctrl = arena.run(board, facts, r, engine_seat=None, teams=bm.TEAMS)
                base[rk] = bm.score_league(ctrl.rosters, facts, actuals, weeks,
                                           "hindsight")
            self.per_season[year] = (s, facts, actuals, weeks, board, ranks, base)

    def evaluate(self) -> Score:
        blocks: dict[str, tuple[float, int, float]] = {}
        all_ranks: list[int] = []
        all_delta: list[float] = []
        wins = 0
        for year, rk in BLOCKS:
            s, facts, actuals, weeks, board, ranks, base = self.per_season[year]
            r_ranks, r_delta, r_wins = [], [], 0
            for seat in range(1, bm.TEAMS + 1):
                res = arena.run(board, facts, ranks[rk], engine_seat=seat,
                                teams=bm.TEAMS)
                pts = bm.score_league(res.rosters, facts, actuals, weeks, "hindsight")
                ours = pts[seat]
                field = sorted(pts.values(), reverse=True)
                r_ranks.append(field.index(ours) + 1)
                d = ours - base[rk][seat]
                r_delta.append(d)
                r_wins += d > 0
            key = f"{year}/{rk}"
            blocks[key] = (statistics.fmean(r_ranks), r_wins, statistics.fmean(r_delta))
            all_ranks += r_ranks
            all_delta += r_delta
            wins += r_wins
        return Score(mean_rank=statistics.fmean(all_ranks), wins=wins,
                     pairs=len(all_ranks), mean_delta=statistics.fmean(all_delta),
                     blocks=blocks)


def sweep(h: Harness, param: str, values: list[float], baseline: Score) -> list[dict]:
    rows = []
    committed = priors().get(f"draft.{param}")
    for v in values:
        with overridden(**{f"draft__{param}": v}):
            sc = h.evaluate()
        ok = sc.improves_every_block(baseline)
        rows.append({"param": param, "value": v, "mean_rank": round(sc.mean_rank, 3),
                     "wins": sc.wins, "mean_delta": round(sc.mean_delta, 1),
                     "all_blocks": ok, "committed": v == committed,
                     "blocks": {k: round(x[0], 2) for k, x in sc.blocks.items()}})
        log.info("  %s=%-6s %s  %s%s", param, v, sc.line(),
                 "ALL BLOCKS IMPROVE" if ok else "",
                 "  <- committed" if v == committed else "")
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--param", nargs="*", default=None)
    ap.add_argument("--config", nargs="*", default=None,
                    help="score one config, e.g. draft.vor_weight=0.4")
    ap.add_argument("--json", type=Path, default=Path("data/backtest/optimize.json"))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    if args.config:
        # The harness is built INSIDE the override, so a prior that acts at
        # board-build time (the market blend, replacement level) is scored
        # too — not only the picker weights. Found 2026-09-05 when two blend
        # modes scored identically because the boards were built first.
        over = {}
        for kv in args.config:
            k, v = kv.split("=")
            over[k.replace(".", "__")] = float(v)
        with overridden(**over):
            log.info("loading seasons and boards under %s...", args.config)
            h = Harness()
            sc = h.evaluate()
        print(f"\n{args.config}\n  {sc.line()}")
        for k, x in sorted(sc.blocks.items()):
            print(f"    {k:<16} finish {x[0]:.2f}  wins {x[1]}/10  {x[2]:+7.1f}")
        return 0

    log.info("loading seasons and boards...")
    h = Harness()
    baseline = h.evaluate()
    log.info("BASELINE  %s", baseline.line())
    for k, x in sorted(baseline.blocks.items()):
        log.info("    %-16s finish %.2f  wins %d/10  %+7.1f", k, x[0], x[1], x[2])

    out = []
    for param in (args.param or list(GRID)):
        if param not in GRID:
            log.warning("no grid for %s", param)
            continue
        log.info("\nsweeping draft.%s", param)
        out.extend(sweep(h, param, GRID[param], baseline))

    print("\n" + "=" * 76)
    winners = [r for r in out if r["all_blocks"]]
    if winners:
        best = min(winners, key=lambda r: r["mean_rank"])
        print(f"ACCEPTED: draft.{best['param']} = {best['value']}  "
              f"finish {baseline.mean_rank:.2f} -> {best['mean_rank']:.2f}  "
              f"wins {baseline.wins} -> {best['wins']}/40")
    else:
        print("NOTHING CLEARS THE BAR — no value improves all four blocks.")
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(
        {"baseline": {"mean_rank": baseline.mean_rank, "wins": baseline.wins,
                      "mean_delta": baseline.mean_delta,
                      "blocks": {k: list(v) for k, v in baseline.blocks.items()}},
         "rows": out}, indent=1), encoding="utf-8")
    print(f"wrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
