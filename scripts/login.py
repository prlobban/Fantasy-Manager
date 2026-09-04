#!/usr/bin/env python
r"""ONE-TIME: log into ESPN by hand and save the web session.

Why this exists
---------------
SWID + espn_s2 authenticate ESPN's fantasy **API**. They do NOT authenticate the
**web app**. Verified 2026-09-03: with perfectly valid cookies, fantasy.espn.com
renders "Log in Required" where the roster should be, behind a MyDisney modal.

Every write in this system goes through the web UI, because ESPN publishes no
write API. So the browser needs a real MyDisney session, and that session cannot
be minted from the two API cookies. A human logs in once; Playwright saves the
resulting cookies + localStorage to data/espn-session.json; every headless run
after that reuses it.

Usage
-----
A bare `python` does not work on Windows — the Store alias intercepts it. Call
the venv's interpreter directly, and run these ONE AT A TIME: the first opens a
browser and then waits for you.

    .\.venv\Scripts\python.exe scripts\login.py            # Windows
    ./.venv/bin/python scripts/login.py                    # Linux / the box

    .\.venv\Scripts\python.exe scripts\login.py --verify   # headless re-check

The saved file is gitignored. It is a live credential — treat it like a password.

Sessions expire. When #fantasy reports "Log in Required", run this again.
"""

from __future__ import annotations

import argparse
import logging
import sys

from core.browser.session import ESPN_BASE, EspnSession, NotLoggedIn
from core.config import settings

log = logging.getLogger("login")


def do_login() -> int:
    cfg = settings()
    print("\n" + "=" * 72)
    print("  A Chromium window is opening. In it:")
    print("    1. Log into ESPN with your MyDisney account.")
    print("    2. Wait until you can see your fantasy team.")
    print("    3. Come back here and press ENTER.")
    print("=" * 72 + "\n")

    s = EspnSession(headless=False, use_saved_session=False)
    s.start()
    try:
        s.page.goto(ESPN_BASE + team_url(cfg), wait_until="domcontentloaded")
        input("Press ENTER once you are logged in and can see your team... ")

        s.page.goto(ESPN_BASE + team_url(cfg), wait_until="domcontentloaded")
        s.page.wait_for_timeout(4000)
        s.dismiss_overlays()

        body = (s.page.inner_text("body") or "").lower()
        if "log in required" in body or "enter your email" in body:
            print("\n[x] Still logged out. Nothing saved. Try again.")
            return 1

        path = s.save_session()
        print(f"\n[ok] Session saved to {path}")
        print("\nNext, one at a time:")
        print(r"  .\.venv\Scripts\python.exe scripts\login.py --verify")
        print(r"  scp data\espn-session.json "
              "ironman@192.168.4.43:~/Fantasy-Manager/data/")
        print("\nThis file is a live credential — treat it like a password.")
        return 0
    finally:
        s.close()


def team_url(cfg) -> str:
    """The team page REQUIRES teamId.

    Without it ESPN renders "Invalid Team ID" — a page that is fully
    authenticated (your team name is in the header, the whole nav is live) but
    contains no roster. That reads exactly like a dead session and is not one.
    Cost us a false alarm on 2026-09-04.
    """
    from core.espn.client import client

    url = f"/football/team?leagueId={cfg.league_id}&seasonId={cfg.season}"
    try:
        return f"{url}&teamId={client().my_team_id}"
    except Exception:
        return url


def do_verify() -> int:
    cfg = settings()
    s = EspnSession(headless=True)
    if not s.has_saved_session():
        print(f"[x] No saved session at {s.storage_state_path}. Run without --verify first.")
        return 1
    s.start()
    try:
        s.goto(team_url(cfg))
        s.page.wait_for_timeout(2500)
        body = (s.page.inner_text("body") or "").lower()
        shot = s.screenshot("verify-session")
        if "log in required" in body:
            print(f"[x] Session is dead — ESPN still says 'Log in Required'. See {shot}")
            return 1
        if "invalid team id" in body or "invalid league" in body:
            print(f"[x] Wrong URL, not a bad session — ESPN says invalid id. See {shot}")
            return 1
        print(f"[ok] Headless session works. Screenshot: {shot}")
        return 0
    except NotLoggedIn as e:
        print(f"[x] {e}")
        return 1
    finally:
        s.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verify", action="store_true", help="headless check of the saved session")
    args = ap.parse_args()
    return do_verify() if args.verify else do_login()


if __name__ == "__main__":
    sys.exit(main())
