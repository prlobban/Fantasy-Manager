#!/usr/bin/env python
"""Draft day. The live loop (§1.2).

    python scripts/draft.py --dry-run     # watch, plan, write nothing
    python scripts/draft.py --no-click    # maintain the queue only (§3.9 floor)
    python scripts/draft.py               # queue + click

The queue is the safety net: even with --no-click, ESPN autopicks whoever is at
the top of our queue when the timer runs out, and that is always our #1.
"""
from __future__ import annotations

import argparse
import logging
import sys

from core.draft.run import DraftConfig, run


def main() -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="no writes at all")
    ap.add_argument("--no-click", action="store_true", help="queue only, let autopick fire")
    ap.add_argument("--no-browser", action="store_true", help="API reader only; no queue")
    ap.add_argument("--poll", type=float, default=2.0)
    ap.add_argument("--depth", type=int, default=None, help="queue depth override")
    ap.add_argument("--url", type=str, default=None, help="draft room URL (practice draft)")
    ap.add_argument("--practice", action="store_true",
                    help="rehearse in a League-Specific Practice Draft: opens a room at our "
                         "real slot (or uses --url), DOM-only reads, no order lock")
    ap.add_argument("--practice-slot", type=int, default=None,
                    help="rehearse from this seat (1..teams) instead of our "
                         "current league slot. Practice only -- the real order "
                         "is randomised an hour before the draft, so one seat "
                         "is not a rehearsal.")
    ap.add_argument("--max-minutes", type=int, default=240)
    ap.add_argument("--judge", choices=("off", "shadow", "live"), default="off",
                    help="§3.10: off ignores the judge; shadow logs and posts what it "
                         "WOULD have changed without changing anything; live applies "
                         "its veto/reorder levers. Run scripts/draft_judge.py "
                         "alongside for any mode but off.")
    args = ap.parse_args()

    stats = run(DraftConfig(
        poll_seconds=args.poll,
        queue_depth=args.depth,
        click=not args.no_click,
        dry_run=args.dry_run,
        use_browser=not args.no_browser,
        max_minutes=args.max_minutes,
        draft_url=args.url,
        practice=args.practice,
        practice_slot=args.practice_slot,
        judge=args.judge,
    ))
    print(f"\ncycles={stats.cycles} picks_seen={stats.picks_seen} "
          f"queue_ops={stats.queue_ops}")
    print(f"our picks: {stats.our_picks}")
    if stats.errors:
        print(f"errors ({len(stats.errors)}): {stats.errors[:5]}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
