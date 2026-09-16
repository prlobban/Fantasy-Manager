#!/usr/bin/env python
"""Probe — and optionally re-point — every selector this system depends on.

    python scripts/discover_selectors.py --team
    python scripts/discover_selectors.py --add --heal
    python scripts/discover_selectors.py --all --heal --notify
    python scripts/discover_selectors.py --draft --headed     # a practice room

## What changed, and why (2026-09-15)

The old version of this script probed sixteen selectors, four of them on the
team page and none at all on the add/free-agent page. On 2026-09-14 the
Buccaneers D/ST claim died on `ADD_PLAYER_BUTTON`, the alert told Pearce to run
this script, and running it would have reported everything fine — it did not
look at that selector. It also always exited 1, because `LINEUP_EDIT_BUTTON` is
obsolete in-season and absence was being counted as failure.

Both are structural fixes now, not spot fixes:

* the target list comes from `core/browser/groups.py`, which is the single
  enumeration of every group, so a selector that exists is a selector that
  gets probed;
* a group marked `optional` reports as `----` and never fails the run, so the
  exit code means something again.

`--heal` makes it the same code path the unattended agent runs: discover a
replacement, write it, verify it resolves, commit it. Without `--heal` this is
strictly read-only.
"""
from __future__ import annotations

import argparse
import logging
import sys

from core.browser import groups as G
from core.browser import selfheal
from core.notify import notify

TARGETS = {"team": G.TEAM, "add": G.ADD, "trade": G.TRADE, "draft": G.DRAFT}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    for name in TARGETS:
        ap.add_argument(f"--{name}", action="store_true", help=f"probe the {name} page")
    ap.add_argument("--all", action="store_true",
                    help="every target except the draft room (which needs an open draft)")
    ap.add_argument("--heal", action="store_true",
                    help="re-point anything broken, verify it, and commit it")
    ap.add_argument("--notify", action="store_true",
                    help="post the result to Slack (what the cron pre-flight uses)")
    ap.add_argument("--headed", action="store_true", help="watch it work")
    ap.add_argument("--wait", type=int, default=6, help="seconds to let the SPA settle")
    args = ap.parse_args()

    chosen = [t for n, t in TARGETS.items() if getattr(args, n)]
    if args.all:
        chosen = [G.TEAM, G.ADD, G.TRADE]
    if not chosen:
        chosen = [G.TEAM]

    broken: list[str] = []
    healed: list[str] = []
    blocks: list[str] = []

    for target in chosen:
        print(f"\n{'=' * 70}\n{target.upper()}\n{'=' * 70}")
        try:
            rep, heals = selfheal.run(target, heal=args.heal,
                                      headless=not args.headed,
                                      settle_ms=args.wait * 1000)
        except Exception as e:
            print(f"  could not probe {target}: {e}")
            broken.append(f"{target}: page would not load ({e})")
            continue

        print(rep.text())
        for h in heals:
            print(f"  {h}")
            if h.healed:
                healed.append(f"{h.group} -> {h.candidate}")

        if rep.broken:
            names = ", ".join(p.group for p in rep.broken)
            broken.append(f"{target}: {names}")
            blocks.append(f"*{target}* — still broken: {names}"
                          + (f"\nDOM: {rep.dom}" if rep.dom else ""))

    print(f"\n{'=' * 70}")
    if healed:
        print("HEALED: " + " · ".join(healed))
    if broken:
        print("STILL BROKEN: " + " · ".join(broken))
        print("\nNothing else in the codebase contains a selector — fix these in\n"
              "core/browser/selectors.py (or accept the healed override) and re-run.")
    else:
        print("every required selector resolves")

    if args.notify and (broken or healed):
        level = "error" if broken else "warn"
        title = f"Selector probe: {len(broken)} broken" if broken \
            else "Selectors self-healed"
        body = "\n".join(blocks)
        if healed:
            body = ("Re-pointed and verified:\n" + "\n".join(f"· {h}" for h in healed)
                    + ("\n\n" + body if body else ""))
        notify(level, title, body)

    return 1 if broken else 0


if __name__ == "__main__":
    sys.exit(main())
