#!/usr/bin/env python
"""Build step 0: what does one dossier actually cost?

The research pass is the only workload in this system big enough to threaten a
rate-limit window, and every number in the plan's budget is an estimate. This
script replaces the estimate with a measurement before anything is built on
top of it.

It runs the `dossier` task against a handful of REAL players spanning the
shapes that cost different amounts — a clear-cut RB1 with little news, a
committee back, a rookie, a QB, a TE — then reports median tokens and wall
time, and extrapolates to the full pool.

    python scripts/measure_dossier.py                  # 5 players off the board
    python scripts/measure_dossier.py --n 3            # cheaper
    python scripts/measure_dossier.py --names "Bijan Robinson,Josh Allen"
    python scripts/measure_dossier.py --pool 80        # extrapolate to a different pool

Nothing here writes a dossier anyone will use, touches the board, or takes an
action. It spends tokens and prints a table.
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
import time
from pathlib import Path

from agent import run as agent_run
from agent.packet import dossier_packet
from core.config import settings
from core.draft import board as board_mod

log = logging.getLogger("measure")

#: Deliberately spread across the shapes that cost different amounts to
#: research: a workhorse with little news, a committee, a rookie, a QB, a TE.
#: Falls back to top-of-board order if a name is not on this year's board.
DEFAULT_SAMPLE = [
    "Bijan Robinson",
    "Tony Pollard",
    "Colston Loveland",
    "Josh Allen",
    "Travis Kelce",
]


def pick_players(bd, names: list[str], n: int):
    by_name = {p.name: (p, v) for p, v in bd.rows}
    out = []
    for want in names:
        if want in by_name:
            out.append(by_name[want])
        else:
            log.warning("not on the board, skipping: %s", want)
    for p, v in bd.rows:                       # top up from the top of the board
        if len(out) >= n:
            break
        if all(p.espn_id != q.espn_id for q, _ in out):
            out.append((p, v))
    return out[:n]


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5, help="players to measure")
    ap.add_argument("--names", type=str, default="", help="comma-separated override")
    ap.add_argument("--pool", type=int, default=80, help="pool size to extrapolate to")
    ap.add_argument("--refresh", type=int, default=15, help="10:30 refresh size")
    ap.add_argument("--dry-run", action="store_true", help="print the invocation only")
    args = ap.parse_args()

    cfg = settings()
    bd = board_mod.load()
    names = [s.strip() for s in args.names.split(",") if s.strip()] or DEFAULT_SAMPLE
    sample = pick_players(bd, names, args.n)

    print(f"model      : {cfg.claude_research_model}")
    print(f"task spec  : {agent_run.TASKS['dossier']}")
    print(f"board      : {len(bd.players)} players, built {bd.age_hours():.1f}h ago")
    print(f"measuring  : {', '.join(p.name for p, _ in sample)}\n")

    rows = []
    for i, (player, val) in enumerate(sample, 1):
        packet = dossier_packet(player, val)
        print(f"[{i}/{len(sample)}] {player.name} ({player.pos.value}) …", flush=True)
        t0 = time.monotonic()
        res = agent_run.run("dossier", packet, dry_run=args.dry_run, timeout=300)
        wall = time.monotonic() - t0

        if args.dry_run:
            continue

        u = res.usage or {}
        inp = int(u.get("input_tokens") or 0)
        out = int(u.get("output_tokens") or 0)
        cache_r = int(u.get("cache_read_input_tokens") or 0)
        cache_w = int(u.get("cache_creation_input_tokens") or 0)
        # Summed tokens read alarmingly high and mean little: cache reads are
        # billed at a fraction, and `claude -p` re-reads the Claude Code harness
        # prompt every turn. total_cost_usd is the number that decides anything.
        total = inp + out + cache_r + cache_w
        rows.append({
            "name": player.name,
            "pos": player.pos.value,
            "ok": res.ok,
            "error": res.error,
            "input": inp, "output": out,
            "cache_read": cache_r, "cache_write": cache_w,
            "total": total,
            "turns": u.get("num_turns"),
            "cost_usd": u.get("total_cost_usd"),
            "wall_s": round(wall, 1),
            "sources": len((res.output or {}).get("sources") or []),
            "multiplier": (res.output or {}).get("multiplier"),
            "veto": (res.output or {}).get("veto"),
            "confidence": (res.output or {}).get("confidence"),
            "transcript": str(res.transcript) if res.transcript else None,
        })
        flag = "ok " if res.ok else "FAIL"
        print(f"        {flag} {total:>7,} tok  {wall:>5.1f}s  turns={u.get('num_turns')}"
              f"  src={rows[-1]['sources']}  x{rows[-1]['multiplier']}"
              + (f"  — {res.error}" if res.error else ""))

    if args.dry_run or not rows:
        return 0

    ok = [r for r in rows if r["ok"]]
    med_tok = statistics.median(r["total"] for r in rows)
    med_wall = statistics.median(r["wall_s"] for r in rows)
    costs = [r["cost_usd"] for r in rows if isinstance(r["cost_usd"], int | float)]

    med_cost = statistics.median(costs) if costs else None

    print("\n" + "=" * 64)
    print(f"{len(ok)}/{len(rows)} succeeded")
    if med_cost is not None:
        print(f"median cost/dossier   : ${med_cost:>9.4f}   <-- the number that decides")
    print(f"median wall/dossier   : {med_wall:>10.1f} s")
    print(f"median tokens (summed): {med_tok:>10,.0f}  "
          f"(cache-inflated; not a billing figure)")
    print(f"turns                 : "
          f"{[r['turns'] for r in rows]}")

    pool_tok = med_tok * args.pool
    print("\nEXTRAPOLATION")
    print(f"  pool of {args.pool:<3d}          : "
          + (f"${med_cost * args.pool:>8.2f}   " if med_cost else "")
          + f"{med_wall * args.pool / 60:.0f} min serial,"
          f" {med_wall * args.pool / 60 / 6:.0f} min at 6 workers")
    if med_cost:
        print(f"  refresh of {args.refresh:<3d}       : ${med_cost * args.refresh:>8.2f}")
        print(f"  pool + refresh      : ${med_cost * (args.pool + args.refresh):>8.2f}")

    out_path = cfg.dossiers_dir / "_measurement.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "model": cfg.claude_research_model,
        "n": len(rows),
        "median_tokens": med_tok,
        "median_wall_s": med_wall,
        "pool_size": args.pool,
        "projected_pool_tokens": pool_tok,
        "rows": rows,
    }, indent=1, default=str), encoding="utf-8")
    print(f"\nwritten: {out_path}")
    return 0 if len(ok) == len(rows) else 1


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.exit(main())
