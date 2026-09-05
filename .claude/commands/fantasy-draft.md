---
description: 'Draft-day worker context — loads the runbook and the loop's rules before any draft-day work.'
---

Draft day. Read `docs/runbook-draft-day.md` first — it is the procedure, and
Pearce runs it. This file is what a Claude session needs beyond it.

**Draft: Saturday 2026-09-05, 11:00 CT.** 90 s per pick. Order randomised at
10:00 CT; `run.preflight` refuses to start before the lock. Never override that.

## The loop, in priority order (§3.2 / §8.7 / §10)

1. Read every pick in the room — API first, DOM fallback.
2. Re-rank the board. Pure code, ~10 ms, no model call.
3. **Rewrite the queue.** Before any click attempt, always. ESPN autopicks from
   the top of the live queue, so the queue is the safety net and the click is
   the optimisation, not the other way round.
4. On our turn, click Draft on the top name after ESPN's 3 s inert countdown.

Queue sync carries a circuit breaker: three consecutive refused clicks stand it
down for five cycles so a dead page cannot eat the clock. A row with no QUEUE
button is a healthy page saying no, not a failure. Drafted players are
remembered permanently and never re-searched (`core/draft/queue.py`).

## The judge (§3.10)

A separate `claude -p` process. Two levers, enforced in code: veto a candidate,
or reorder two players within the same tier. Every lever needs a § citation
and a dossier line; uncited ones are refused. **The loop never waits for it.**
The mode that decides anything is the loop's `--judge` flag; `--shadow` on the
judge process only labels its Slack posts. Shadow for the first live draft.

## Rehearsal

`scripts/practice.sh [slot]` — a League-Specific Practice Draft from a random
seat, foreground, ends disarmed. One practice room per ESPN account; a second
displaces the first. The practice pace (~3 s a pick) floors the judge budget at
zero, so the judge never fires there; `scripts/rehearse_judge.py --turns 3
--pace 45` drives it at real pace against a replayed room instead.

## Never

- Never turn the kill switch on from code. `ENABLED` is a human decision.
- Never edit the queue by hand while the loop runs.
- Never put `data/espn-session.json` or `.env` in chat, Slack or the vault.
