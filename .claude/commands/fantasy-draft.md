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

**Friday 22:00 — the research pass** *(its own rate-limit window, deliberately)*
```
python scripts/research.py
```
One agent per player over the ADP 1-90 pool, ~6 min at 6 workers, ~$20. Writes
`data/dossiers/` and `data/overrides.json`. **Run it the night before, not on
Saturday morning:** it is expensive and variable-length, the judge is cheap and
time-critical, and an overrun in the same window puts you at the limit at 11:00
with no recovery. It resumes, so a rate limit costs a wait, not a restart.

Read a few dossiers before bed. They are prose, and a wrong one is obvious.

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

**10:30 — refresh the top of the board** *(after the order lock)*
```
python scripts/research.py --refresh-top 15
python scripts/build_board.py
```
Saturday practice reports are the one thing that genuinely breaks an overnight
dossier. ~$3, under a minute.

**10:15 — start the loop (on the box)**
```
python scripts/draft.py                        # queue + click
python scripts/draft.py --judge shadow         # + the judge, watching only
python scripts/draft.py --no-click             # queue only, the safest mode
```

**Then, in a second terminal, the judge:**
```
python scripts/draft_judge.py --shadow
```
It reads the loop's clock file and writes verdicts the loop *ignores* in
shadow. You see every verdict and every "would have changed the pick" in
#fantasy, on a real draft, with nothing at risk. **Shadow is the recommended
setting for the first live draft** — the judge has never driven one.

To let it drive: `--judge live` on the loop and drop `--shadow` on the judge.
Its worst case is bounded by construction — veto and within-tier reorder only,
so the floor is "we take the maths' #2".
It passes pre-flight, then waits for ESPN to open the room — the draft URL
bounces to the home page until then — retrying every 30 s and posting to
#fantasy when it is in. The queue fills within ~20 s of joining. Have the
draft room open on the laptop too. Not to drive it — to see it.

## What the judge can and cannot do

Two levers, **enforced in code, not asked for in the prompt**: veto a
candidate, or reorder two players **within the same tier**. It cannot promote
across tiers, cannot act on anything not in a dossier, and cannot touch a
player outside the 15 it was shown. Every lever carries a § citation and the
dossier line behind it; uncited ones are refused.

It runs as a separate process and **the loop never waits for it.** No verdict,
a stale verdict, or a verdict whose every lever was refused all end the same
way: the maths drafts. It is killed the instant you are on the clock.

If it is quiet, that is the expected outcome — it means the dossiers agree with
the board.

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

## Two ESPN behaviours to know

- **Autopick after a miss.** If a pick ever expires, ESPN flips the team to
  Autopick and fills every later turn instantly from the top of our queue.
  The loop detects the toggle each cycle and switches it back off (and says
  so in #fantasy). If you are driving by hand, the toggle is in the Pick
  Queue header.
- **The 3-second countdown.** For the first ~3 s of our turn every DRAFT
  button is inert and shows `:03`. The loop waits for it. Do not read a
  quiet three seconds as a stall.

## If something breaks

**The loop dies.** The queue is still correct on ESPN's side. Autopick will make
sensible picks. Restart it: `python scripts/draft.py`.

**The queue looks wrong.** Stop the loop, run with `--dry-run` to see what it
thinks the queue should be, and fix it by hand in the draft room.

**Everything is broken.** Draft manually off `data/board.json` — the top of that
file, filtered to positions we still need, is a perfectly good cheat sheet.
`§3.9`: a correctly ordered queue with zero automation is already a competent
draft.

**The judge is misbehaving.** Kill that process. The loop carries on drafting
the maths — it does not depend on it. Or restart it with `--shadow`.

**Kill it entirely:** `echo off > ENABLED`. Every write refuses immediately.

## After

The loop posts a final roster to #fantasy and writes the decision log. Then:
```
echo off > ENABLED
```
Park it until the manager goes live.
