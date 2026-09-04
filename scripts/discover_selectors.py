#!/usr/bin/env python
"""Point core/browser/selectors.py at whatever ESPN is actually rendering.

Run this against a LIVE page — a practice draft for the draft-room selectors,
the team page for the lineup ones. It tries every candidate in selectors.py,
reports which resolve, and dumps the DOM so new ones can be written.

    python scripts/discover_selectors.py --draft --headed
    python scripts/discover_selectors.py --team

Headed is usually right: you want to see what the page is doing.
"""
from __future__ import annotations

import argparse
import logging
import sys

from core.browser import selectors as S
from core.browser.session import EspnSession
from core.config import settings

DRAFT_TARGETS = {
    "DRAFT_PICK_ROW": S.DRAFT_PICK_ROW,
    "DRAFT_ON_CLOCK": S.DRAFT_ON_CLOCK,
    "DRAFT_TIMER": S.DRAFT_TIMER,
    "DRAFT_PLAYER_ROW": S.DRAFT_PLAYER_ROW,
    "DRAFT_BUTTON": S.DRAFT_BUTTON,
    "DRAFT_SEARCH": S.DRAFT_SEARCH,
    "QUEUE_CONTAINER": S.QUEUE_CONTAINER,
    "QUEUE_ROW": S.QUEUE_ROW,
    "QUEUE_ADD_BUTTON": S.QUEUE_ADD_BUTTON,
    "QUEUE_REMOVE_BUTTON": S.QUEUE_REMOVE_BUTTON,
}
TEAM_TARGETS = {
    "LINEUP_EDIT_BUTTON": S.LINEUP_EDIT_BUTTON,
    "LINEUP_SLOT_ROW": S.LINEUP_SLOT_ROW,
    "LINEUP_MOVE_BUTTON": S.LINEUP_MOVE_BUTTON,
    "LINEUP_SAVE_BUTTON": S.LINEUP_SAVE_BUTTON,
}


def probe(page, targets: dict[str, str]) -> int:
    found = 0
    print(f"\n{'NAME':24} {'N':>4}  CANDIDATE THAT MATCHED")
    for name, group in targets.items():
        hit, count = None, 0
        for cand in [c.strip() for c in group.split(",")]:
            try:
                n = page.locator(cand).count()
            except Exception:
                continue
            if n:
                hit, count = cand, n
                break
        found += bool(hit)
        print(f"{name:24} {count:>4}  {hit or '*** NONE MATCHED ***'}")
    return found


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--draft", action="store_true", help="probe the draft room")
    ap.add_argument("--team", action="store_true", help="probe the team page")
    ap.add_argument("--url", type=str, default=None)
    ap.add_argument("--headed", action="store_true")
    ap.add_argument("--wait", type=int, default=8, help="seconds to let the SPA settle")
    args = ap.parse_args()

    cfg = settings()
    s = EspnSession(headless=not args.headed)
    s.start()
    try:
        if args.url:
            url = args.url
        elif args.draft:
            url = f"/football/draft?leagueId={cfg.league_id}&seasonId={cfg.season}"
        else:
            url = f"/football/team?leagueId={cfg.league_id}&seasonId={cfg.season}"

        s.goto(url)
        s.page.wait_for_timeout(args.wait * 1000)
        s.dismiss_overlays()

        targets = DRAFT_TARGETS if args.draft else TEAM_TARGETS
        found = probe(s.page, targets)
        print(f"\n{found}/{len(targets)} selector groups resolved")

        dom = s.dump_dom("selector-discovery")
        shot = s.screenshot("selector-discovery")
        print(f"DOM  -> {dom}\nshot -> {shot}")
        print("\nEdit core/browser/selectors.py for anything that says NONE MATCHED,")
        print("then re-run. Nothing else in the codebase contains a selector.")
        return 0 if found == len(targets) else 1
    finally:
        s.close()


if __name__ == "__main__":
    sys.exit(main())
