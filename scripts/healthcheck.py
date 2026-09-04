#!/usr/bin/env python
"""§8.5 — pre-flight. Cron-able; exits non-zero when anything is wrong."""
from __future__ import annotations

import argparse
import logging
import sys

from core.espn import health
from core.gates import kill_switch
from core.notify import notify


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-kill", action="store_true",
                    help="report only; do not flip the kill switch on failure")
    ap.add_argument("--quiet", action="store_true", help="only speak up on failure")
    args = ap.parse_args()

    r = health.check(kill_on_fail=not args.no_kill)
    for name, ok, detail in r.checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name:10} {detail}")
    print(f"\nkill switch: {kill_switch.state()}")

    if not r.ok:
        notify("error", "Fantasy health check FAILED", "\n".join(r.failures))
        return 1
    if not args.quiet:
        print("\nall healthy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
