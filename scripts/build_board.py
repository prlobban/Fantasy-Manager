#!/usr/bin/env python
"""§3.2 — build the draft board. Run this BEFORE the draft, not during it.

    python scripts/build_board.py               # build + save + show the top 30
    python scripts/build_board.py --no-overrides
"""
from __future__ import annotations

import argparse
import logging
import sys

from core.draft import board as B


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", type=int, default=450)
    ap.add_argument("--no-overrides", action="store_true")
    ap.add_argument("--show", type=int, default=30)
    args = ap.parse_args()

    bd = B.build(size=args.size, apply_overrides=not args.no_overrides)
    path = B.save(bd)

    print(f"\nsaved -> {path}")
    print("coverage:", {k: int(v) for k, v in bd.coverage.items()})

    missing_proj = bd.coverage["players"] - bd.coverage["with_projection"]
    if missing_proj > bd.coverage["players"] * 0.2:
        print(f"\nWARNING: {int(missing_proj)} players have no projection")

    print(f"\nTOP {args.show} BY VOR")
    print(f"{'#':>3} {'PLAYER':24} {'POS':5} {'TM':4} {'PROJ':>7} {'VOR':>7} "
          f"{'TIER':>4} {'ADP':>6} {'AVL':>5}")
    for i, (p, v) in enumerate(bd.rows[: args.show], 1):
        print(f"{i:>3} {p.name[:24]:24} {p.pos.value:5} {p.pro_team:4} "
              f"{v.points:7.1f} {v.vor:7.1f} {v.tier:>4} "
              f"{(p.espn_adp or 0):6.1f} {v.availability:5.2f}")

    print("\nTIER BREAKS (top tier remaining, by position)")
    from collections import defaultdict
    tiers = defaultdict(lambda: defaultdict(int))
    for p, v in bd.rows:
        tiers[p.pos.value][v.tier] += 1
    for pos in ("QB", "RB", "WR", "TE", "K", "D/ST"):
        if pos in tiers:
            t = sorted(tiers[pos].items())[:4]
            print(f"  {pos:5} " + "  ".join(f"T{k}:{n}" for k, n in t))
    return 0


if __name__ == "__main__":
    sys.exit(main())
