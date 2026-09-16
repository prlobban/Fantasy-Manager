#!/usr/bin/env python
"""§8.5 — pre-flight. Cron-able; exits non-zero when anything is wrong."""
from __future__ import annotations

import argparse
import logging
import sys

from agent.run import auth_status
from core.espn import health
from core.gates import kill_switch
from core.notify import notify


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-kill", action="store_true",
                    help="report only; do not flip the kill switch on failure")
    ap.add_argument("--quiet", action="store_true", help="only speak up on failure")
    ap.add_argument("--selectors", action="store_true",
                    help="also probe the browser selectors (slow: opens a real page)")
    ap.add_argument("--heal", action="store_true",
                    help="with --selectors, re-point anything stale and commit it")
    args = ap.parse_args()

    r = health.check(kill_on_fail=not args.no_kill)
    for name, ok, detail in r.checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:10} {detail}")
    # The agent's own credential, checked alongside ESPN's. Deliberately NOT
    # wired to the kill switch: with claude signed out the reasoning layer is
    # dead but `core` is fine, and killing writes would also stop a lineup the
    # optimiser can set on its own. Worth shouting about, not worth disarming
    # the system over.
    a_ok, a_detail = auth_status()
    print(f"  {'PASS' if a_ok else 'FAIL'}  {'claude':10} {a_detail}")
    print(f"\nkill switch: {kill_switch.state()}")

    # §8.5 pre-flight for the browser write path. A stale selector is invisible
    # to every other check here — the cookie is valid, the API reads fine, and
    # the write still cannot find its button (2026-09-14).
    sel_failures: list[str] = []
    if args.selectors:
        from core.browser import groups as G
        from core.browser import selfheal

        for target in (G.TEAM, G.ADD):
            try:
                rep, heals = selfheal.run(target, heal=args.heal)
            except Exception as e:
                print(f"  FAIL  {'selectors':10} {target}: {e}")
                sel_failures.append(f"selectors/{target}: page would not load ({e})")
                continue
            for h in heals:
                if h.healed:
                    print(f"  HEAL  {'selectors':10} {h.group} -> {h.candidate}")
            mark = "PASS" if rep.healthy else "FAIL"
            detail = (f"{target}: all resolve" if rep.healthy
                      else f"{target}: {', '.join(p.group for p in rep.broken)}")
            print(f"  {mark}  {'selectors':10} {detail}")
            if not rep.healthy:
                sel_failures.append(f"selectors/{target}: "
                                    + ", ".join(p.group for p in rep.broken))

    failures = r.failures + ([] if a_ok else [f"claude: {a_detail}"]) + sel_failures
    if failures:
        notify("error", "Fantasy health check FAILED", "\n".join(failures))
        return 1
    if not args.quiet:
        print("\nall healthy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
