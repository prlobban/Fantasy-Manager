#!/usr/bin/env python
"""D6 — are the hand-written durability priors right?

    python scripts/calibrate_availability.py

`core/model/durability.py` carries age cliffs (RB 27, WR 30, TE 31, QB 36), a
3%-per-year decay past them, a 0.85 floor, and a soft-tissue/clean-acute split.
**None of it has ever been checked against an outcome.** It is the one part of
the valuation we own outright — everything else is ESPN's projection — so if it
is wrong, it is wrong on the live board today.

This measures three things over thirteen seasons of nflverse data:

  1. Does `availability()` correlate with games actually played?
  2. Is it CALIBRATED — when it says 0.8, do those players average 0.8?
  3. Are the age cliffs visible in the data at all?

Games played comes from nflverse appearances, which is the honest denominator:
a player on a roster who never dressed did not play, whatever his status said.
"""

from __future__ import annotations

import argparse
import logging
import statistics
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import polars as pl  # noqa: E402

from core.espn.client import EspnClient  # noqa: E402
from core.espn.settings import load as load_settings  # noqa: E402
from core.model import durability  # noqa: E402
from core.model.schema import InjuryStatus, Pos  # noqa: E402
from core.proj import features, nflstats  # noqa: E402

log = logging.getLogger("calib")

SEASON_GAMES = 17
POS = {"QB": Pos.QB, "RB": Pos.RB, "WR": Pos.WR, "TE": Pos.TE}


def spearman(a, b):
    from scripts.fit_projection import spearman as sp
    return sp(list(a), list(b))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--train", type=int, nargs=2, default=[2012, 2023])
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    years = list(range(args.train[0], args.train[1] + 1))
    scoring = load_settings(EspnClient()).settings.scoring
    wide = nflstats.seasons(list(range(years[0] - features.LOOKBACK, years[-1] + 1)),
                            scoring)
    bio = features.bios()

    from core.data.nflverse import injury_history_by_gsis
    hist = injury_history_by_gsis(tuple(range(years[0] - 3, years[-1] + 1)))

    rows = []
    for y in years:
        prior = wide.filter(pl.col("season") < y)
        actual = wide.filter(pl.col("season") == y)
        for r in features.build(prior, y, bio=bio, actuals=actual):
            if r.position not in POS or r.actual_games is None or not r.has_history:
                continue
            events = [e for e in hist.get(r.gsis_id, []) if e.season < y]
            dur = durability.availability(
                pos=POS[r.position], status=InjuryStatus.UNKNOWN,
                history=events, age=int(r.age) if r.age else None,
                weeks_remaining=SEASON_GAMES, current_week=1)
            rows.append((r, dur.availability, min(r.actual_games / SEASON_GAMES, 1.0)))

    log.info("%d player-seasons with prior history\n", len(rows))

    # 1 ── does it correlate at all?
    print("=== 1. correlation with games actually played ===")
    for p in sorted(POS):
        sub = [x for x in rows if x[0].position == p]
        if len(sub) < 50:
            continue
        rho = spearman([x[1] for x in sub], [x[2] for x in sub])
        prior_rho = spearman([x[0].prior_games[0] for x in sub], [x[2] for x in sub])
        print(f"  {p}  n={len(sub):<5} durability rho {rho:+.4f}   "
              f"(prior-season games alone: {prior_rho:+.4f})")

    # 2 ── is it calibrated?
    print("\n=== 2. calibration: predicted vs actual availability ===")
    buckets: dict[float, list[float]] = defaultdict(list)
    for _, pred, act in rows:
        buckets[round(pred * 20) / 20].append(act)
    print("  predicted   n     actual   error")
    for b in sorted(buckets):
        v = buckets[b]
        if len(v) < 30:
            continue
        a = statistics.fmean(v)
        print(f"    {b:.2f}    {len(v):<5} {a:.3f}   {a - b:+.3f}")
    allpred = statistics.fmean(x[1] for x in rows)
    allact = statistics.fmean(x[2] for x in rows)
    print(f"  OVERALL predicted {allpred:.3f} vs actual {allact:.3f} "
          f"({allpred - allact:+.3f})")

    # 3 ── are the age cliffs real?
    print("\n=== 3. actual availability by age, per position ===")
    for p in sorted(POS):
        by_age: dict[int, list[float]] = defaultdict(list)
        for r, _, act in rows:
            if r.position == p and r.age:
                by_age[int(r.age)].append(act)
        cells = [(a, statistics.fmean(v), len(v))
                 for a, v in sorted(by_age.items()) if len(v) >= 25]
        cliff = durability._AGE_CLIFF.get(POS[p])
        print(f"  {p} (coded cliff {cliff}): " +
              "  ".join(f"{a}:{m:.2f}" for a, m, _ in cells))
    return 0


if __name__ == "__main__":
    sys.exit(main())
