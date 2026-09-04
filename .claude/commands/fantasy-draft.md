---
description: 'Draft-day runbook — pre-flight, start the loop, what to watch, how to take over.'
---

Draft day. **Read the whole thing before 10:00; the clock is not the place to
learn it.**

Draft: **Saturday 2026-09-05, 11:00 CT.** 90 seconds per pick.

> ⚠️ **A bare `python` does not work.** On Windows the Microsoft Store alias
> intercepts it. Everywhere below, `python` means `.\.venv\Scripts\python.exe`
> on the laptop and `./.venv/bin/python` on the box. See `RUNNING.md`.

## 🔴 The order is randomised at 10:00

One hour before the draft. Everything about our slot changes then — which picks
we own, how long the gaps are, who we plan around. `run.preflight` **refuses to
start** before the lock, on purpose. Do not override it.

## Friday — the Practice Draft (selectors)

The draft-room selectors are the one untested surface. ESPN's practice room is
the only place they can be checked before Saturday.

1. Click **Practice Draft** on the team page; copy the room's URL from the tab.
2. Probe the selectors against it:
   ```
   python scripts/discover_selectors.py --draft --headed --url "<practice url>"
   ```
   Anything `NONE MATCHED` gets re-pointed in `core/browser/selectors.py`, then
   re-run until every group resolves.
3. Watch the loop read the room, with no writes:
   ```
   python scripts/draft.py --dry-run --url "<practice url>"
   ```
   Expect `pick N: <name>` lines as the practice room fills, and `queue sync:
   N ops` (planned, not executed).

Two limits of the practice room, so nothing below reads as a bug:
- The API does not record practice picks, so the loop reads the **DOM only**,
  and it attributes each pick to a team by the **real league's** pick order.
  If the practice room seats us differently, "our turn" will be off. That is
  fine — the goal is proving the reader and the queue plan, not the pick.
- `--dry-run` skips the 10:00 order lock and the kill switch on purpose.

## Timeline

**09:30 — rebuild the board**
```
python scripts/build_board.py
```
Catches overnight injury news. Sanity-check the top 30: if a name at the top
looks obviously wrong, that is the moment to find out.

**09:45 — the agent's news pass** *(optional; the board is fine without it)*
```
python -m agent.run predraft
python scripts/build_board.py        # re-run to apply the overrides
```
Overrides are clamped to ±15%. If it wants to move someone further, that is the
model being wrong, not the cap.

**10:00 — the order locks. Re-read it.**
```
python scripts/healthcheck.py
```
Then confirm our slot and pick numbers are what the loop reports.

**10:45 — start the loop**
```
python scripts/draft.py --no-click     # queue only, safest
python scripts/draft.py                # queue + click
```
Have the draft room open on the laptop too. Not to drive it — to see it.

## What the loop does, in priority order

1. Reads every pick in the room (API first, DOM as fallback).
2. Re-ranks the board — pure code, ~10ms, no model call (§3.2/§8.7).
3. **Rewrites the queue.** This happens BEFORE any click attempt, always.
4. On our turn, clicks Draft on the top name.

**Why that order matters:** ESPN autopicks from the top of the live queue. If
the click leg breaks — a selector changes, the page hangs — the timer expires
and ESPN drafts **our own #1 anyway**. There is no path to ESPN's default list.

## What to watch

- Each of our picks posts to **#fantasy** with the pick, its VOR, and the
  runner-up we passed on. If those stop arriving, something is wrong.
- `queue_ops` climbing every cycle means the queue is being maintained.
- Errors are logged and the loop continues, deliberately: a broken cycle does
  not blow a pick, it just means autopick handles that one.

## If something breaks

**The loop dies.** The queue is still correct on ESPN's side. Autopick will make
sensible picks. Restart it: `python scripts/draft.py`.

**The queue looks wrong.** Stop the loop, run with `--dry-run` to see what it
thinks the queue should be, and fix it by hand in the draft room.

**Everything is broken.** Draft manually off `data/board.json` — the top of that
file, filtered to positions we still need, is a perfectly good cheat sheet.
`§3.9`: a correctly ordered queue with zero automation is already a competent
draft.

**Kill it entirely:** `echo off > ENABLED`. Every write refuses immediately.

## After

The loop posts a final roster to #fantasy and writes the decision log. Then:
```
echo off > ENABLED
```
Park it until the manager goes live.
