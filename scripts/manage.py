#!/usr/bin/env python
"""The season manager (§1.3, §1.4). Runs daily on the box.

    python scripts/manage.py --dry-run          # print what it WOULD do
    python scripts/manage.py                    # the daily sweep
    python scripts/manage.py --tuesday          # the weekly review (§7)
    python scripts/manage.py --task lineup      # one slice only
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from agent import run as agent_run
from agent.packet import build as build_packet
from core.espn import health, league_state
from core.gates import kill_switch
from core.notify import notify

log = logging.getLogger("manage")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--tuesday", action="store_true")
    ap.add_argument("--task", choices=["lineup", "waivers", "trades", "all"], default="all")
    ap.add_argument("--no-agent", action="store_true",
                    help="print core's plans without invoking the model")
    args = ap.parse_args()

    h = health.check(kill_on_fail=not args.dry_run)
    print("health:", h.summary())
    if not h.ok:
        print("FAILED:", "; ".join(h.failures))
        return 1

    if not kill_switch.is_on() and not args.dry_run:
        print(f"kill switch is off ({kill_switch.state()[:80]}) — read-only")
        args.dry_run = True

    task = "tuesday" if args.tuesday else "daily"
    st = league_state.snapshot()
    packet = build_packet(task, st)

    # core's view first, always. If this looks wrong, the model cannot fix it.
    if lp := packet.get("lineup_plan"):
        print(f"\nLINEUP  projected {lp['projected']} · playing for "
              f"{lp['variance_mode']}" + (f" · margin {lp['margin']:+}"
                                          if lp.get("margin") is not None else ""))
        for a in lp["assignments"]:
            print(f"  {a['slot']:9} {(a['player'] or '— EMPTY —'):24} {a['points']:6.1f}")
        for c in lp["changes"]:
            print(f"  CHANGE {c['player']}: {c['from']} -> {c['to']}  ({c['why']})")
        if not lp["changes"]:
            print("  (already optimal — no moves)")

    if wp := packet.get("waiver_plan"):
        print(f"\nWAIVERS priority {wp['priority']}")
        for c in wp["free_adds"]:
            print(f"  FREE ADD {c['name']:24} +{c['gain_per_week']}/wk  "
                  f"drop {c['drop'] or '—'}")
        for c in wp["claims"]:
            print(f"  CLAIM    {c['name']:24} +{c['gain_per_week']}/wk  "
                  f"drop {c['drop'] or '—'}  ({c['archetype']})")
        if not wp["free_adds"] and not wp["claims"]:
            print("  (nothing clears the bar)")
        for s in wp["skipped"][:4]:
            print(f"  skip     {s['name']:24} {s['why']}")

    if args.no_agent:
        return 0

    res = agent_run.run(task, packet, dry_run=args.dry_run)
    if not res.ok:
        print(f"\nAGENT FAILED: {res.error}")
        notify("error", "Fantasy manager: agent run failed", str(res.error)[:400])
        return 1

    print("\nAGENT")
    print(json.dumps(res.output, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
