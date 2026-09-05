# Draft day — Saturday 2026-09-05, 11:00 CT

10-team snake, 13 rounds, 90 s per pick. Slot is **randomised at 10:00 CT**.
Everything runs on the box. The laptop's job is the ESPN draft room, open, to
watch — and to take the wheel if it comes to that.

Every command below is run on the box:

```
ssh jarvis
cd ~/Fantasy-Manager
```

`python` means `./.venv/bin/python`. Times are CT.

---

## 09:30 — prove the system, rebuild the board

```
./.venv/bin/python scripts/healthcheck.py
./.venv/bin/python scripts/build_board.py
```

- **healthcheck** proves the ESPN cookies still work, that it sees the league
  and our team (id 8, `big P`), and reports the kill switch. Anything but
  `all healthy` → stop and fix before going further.
- **build_board** pulls ESPN's projections and consensus ranks, blends them
  (75% consensus, the setting that won the benchmark), applies last night's
  dossier overrides (±15% max, plus the Josh Jacobs veto), and writes
  `data/board.json`. **Mandatory:** the loop refuses a board older than 3 h,
  and the current one was built last night. Read the top 30 it prints — a
  wrong name at the top is easier to spot now than on the clock.

## 10:00 — the order locks

```
./.venv/bin/python scripts/healthcheck.py
```

Prints our slot. The loop re-reads the order itself and **refuses to start
before the lock** — don't try to override that.

## 10:30 — refresh the top 15, rebuild

```
./.venv/bin/python scripts/research.py --refresh-top 15
./.venv/bin/python scripts/build_board.py
```

- **research --refresh-top 15** re-runs the research agent on the 15 highest-
  value players only, so Saturday-morning practice reports and inactives are in
  the dossiers. ~$3, about a minute. Rewrites `data/overrides.json`.
- **build_board** again so the board actually carries those overrides. This
  board is now fresh enough for 11:00.

## 10:40 — start

```
scripts/draft_day.sh start
```

Arms the kill switch (`ENABLED=on`), launches the draft loop and the judge
detached so a dropped SSH doesn't kill them, and prints the tail command.

- **The loop** (`draft.py --judge shadow`) passes pre-flight, then waits for
  ESPN to open the room, retrying every 30 s, and posts to #fantasy when it's
  in. From then on, every ~2 s: read every pick in the room (API first, DOM as
  fallback) → re-rank the board, pure maths → rewrite our ESPN queue, top 8 →
  on our turn, click Draft on #1 after ESPN's 3 s inert countdown.
  **Queue before click, always**: if the click leg ever dies, ESPN autopicks
  from the top of *our* queue, never from its own list.
- **The judge** (`draft_judge.py --shadow`) is the model. In shadow it posts
  what it would have vetoed or reordered to #fantasy and **changes nothing**.
  The maths drafts. If it's quiet, the dossiers agree with the board.

## 11:00 — watch

Three views, any one is enough:

- **#fantasy** — "in the room", then every pick with its VOR and the runner-up
  we passed on, any autopick toggle it switched back off, and the final roster.
  If picks stop arriving, something is wrong.
- **Terminal** — `tail -f data/draft-live.log`. Each cycle logs the queue ops
  it landed. `scripts/draft_day.sh status` shows the switch and the processes.
- **The ESPN room on the laptop** — our Pick Queue should be full and
  re-ordered after every pick. That's the engine, visibly working.

Do **not** edit the queue by hand while the loop runs; it rewrites it every
cycle. Do not open a practice room today — one per account, it displaces the
bot's.

## If something goes wrong

| | |
|---|---|
| Loop died | The queue on ESPN's side is still right; autopick covers the pick. `scripts/draft_day.sh stop` then `start`. |
| A pick expired | ESPN flips us to Autopick and fills from our queue. The loop detects the toggle and turns it off, and says so in #fantasy. By hand: the toggle is in the Pick Queue header. |
| Room or page broken | Draft by hand in the laptop room off the top of `data/board.json`, filtered to positions still needed. A correctly ordered board with zero automation is still a competent draft (§3.9). |
| Judge misbehaving | Kill it, the loop doesn't depend on it: `pkill -f draft_judge.py`. |
| Stop everything | `scripts/draft_day.sh stop` — disarms and kills both. Every write refuses on `off`. |

## After

```
scripts/draft_day.sh stop
```

The loop posts the final roster to #fantasy and writes the decision log to
`data/drafts/<timestamp>/`. Park it until the manager goes live.

---

**Rehearsed 2026-09-04:** five practice drafts from seats 4, 2, 9, 1, 7 —
13/13 picks each, zero breaker trips, zero click failures. `scripts/practice.sh`
runs one from a random seat if you want to see it again before 10:30.
