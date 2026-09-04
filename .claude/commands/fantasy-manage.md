---
description: 'Season manager runbook — run a sweep by hand, read the log, flip the switch.'
---

The in-season loop. Normally runs itself on the box at 07:30 CT; this is how to
drive it by hand or work out what it did.

## Run a sweep

```
python scripts/manage.py --dry-run --no-agent   # core's plan only, no model, no writes
python scripts/manage.py --dry-run              # + the agent, still no writes
python scripts/manage.py                        # live
python scripts/manage.py --tuesday              # the weekly review (§7)
```

**Start with `--dry-run --no-agent`.** That prints core's own lineup and waiver
plans with no model involved. If those look wrong, the agent cannot fix it and
the bug is in `core`.

## Reading the output

**LINEUP** — the optimal assignment, the §4.2 variance mode, and the moves.
- `playing for floor` means we are favoured by 12+; `ceiling` means we are
  behind by 12+. Neither means leaving points on the bench: playoff seeding here
  is TOTAL_POINTS_SCORED, so points-for always counts.
- `— EMPTY —` in any slot is more urgent than any swap.

**WAIVERS** — remember this league is **rolling priority, not FAAB**.
- `FREE ADD` costs nothing at all (§5.3.2) — those clear a low bar on purpose.
- `CLAIM` spends our queue position. The bar scales with how good that position
  is (§5.3.1).
- The `skip` lines are decisions too. "No net starting-lineup gain" usually
  means the pickup would never actually start.

## The switch

```
cat ENABLED           # on / off
echo on  > ENABLED    # allow writes
echo off > ENABLED    # read-and-report only
```
A failed health check flips it off by itself and posts to #fantasy. **Nothing in
`core` ever turns it back on** — that is a human decision.

## When something looks wrong

```
python scripts/healthcheck.py        # is ESPN still answering as us?
tail -50 data/decisions.jsonl        # every action WITH the number that justified it
ls -lt data/screenshots | head       # every write leaves a screenshot
ls -lt data/agent-runs | head        # full transcripts
```

**"Log in Required" in a screenshot** means the saved web session has expired.
The API cookies are not enough for the web UI. Fix:
```
python scripts/login.py
python scripts/login.py --verify
scp data/espn-session.json ironman@192.168.4.43:~/Fantasy-Manager/data/
```

## Cadence

| When | What |
|---|---|
| Daily 07:30 CT | full sweep — lineup, waivers, trades |
| Sunday 11:00 CT | lineup only, catching late news |
| After each Sunday window | late swap (§4.4) |
| Tuesday | the review (§7) — writes a dated history file |
