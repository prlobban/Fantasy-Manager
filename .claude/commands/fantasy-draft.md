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

## The rehearsal — a League-Specific Practice Draft

The selectors were verified against live practice rooms on 2026-09-04 and the
whole loop has been run end to end in one. Re-run it any time to re-prove the
room (ESPN can redesign overnight):

```
echo on > ENABLED
python scripts/draft.py --practice --max-minutes 14
echo off > ENABLED
```

`--practice` opens a practice room itself, at our current league slot, reads
picks from the **DOM only** (the league's draft record knows nothing about a
practice room), skips the 10:00 order lock, and otherwise runs exactly the
Saturday loop: queue sync, click, Slack posts, decision log. The practice
clock is 30 s per pick, so the whole thing takes ~10 minutes.

Two things about the practice room, so nothing reads as a bug:
- Other teams are ESPN auto-teams seated at random. Picks are attributed to
  teams by the **real league's** order, so "room demand" is approximate. Our
  own slot is exact, which is what matters.
- Add `--dry-run` to watch without writing (no kill switch needed).

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
