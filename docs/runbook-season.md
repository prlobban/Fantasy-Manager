# The season — how the manager runs, and how you run it

The manager wakes on the box by cron. You do not have to do anything for it
to run. This is what each wake-up does, what you will see, and the three
things that are yours.

Every command is on the box: `ssh jarvis && cd ~/Fantasy-Manager`.
`python` means `./.venv/bin/python`.

## The week, in CT

| When | What | What it does |
|---|---|---|
| Daily 07:00 | `research` | One research agent per player who matters today: our roster, the top waiver candidates, the trade targets. Each writes a dossier: injury designation and practice report, snap and target trend, matchup, what analysts say, dated news with sources. Validated in code; a bounded multiplier lands in the valuation. ~$8–12. |
| Daily 07:30 | `sweep` | The manager reads this morning's research, assesses the roster (shape, holes, this week's ask), then decides: lineup, adds (max 3 a week), trade proposals (max 3 a week, 1 a day), incoming offers through the gauntlet. Every action carries the six-part reasoning. Posts the assessment and every move to #fantasy. |
| Tue 07:30 | `tuesday` | Pulls last week's box score. Grades every decision as a decision, not an outcome. Writes the dated review to `docs/2026-season/`, appends lessons to `data/lessons.md` (read by every future run), proposes prior changes for you. Then the normal sweep. |
| Thu 18:30 · Sun 11:00, 15:00, 19:00 · Mon 18:30 | `lineup` | Lineup only: late news, inactives, and the late swap (§4.4). No adds, no trades. |

## What lands in #fantasy

- **07:00** research summary: dossiers written, vetoes, how many moved a number, cost.
- **07:30** the roster assessment and every action with its reason. Refusals are posted too, with the gate that refused.
- **Escalations** as a separate warning: anything the manager wants your read on.
- **Tuesday** the week's result, efficiency, lessons recorded, and the path to the review.
- **Any cron failure** as an error.

## The switch

```
cat ENABLED              # on / off
echo on  > ENABLED       # writes allowed
echo off > ENABLED       # read-and-report: every write refused, everything still posts
```

With it **off**, every wake-up still runs and still posts what it *would* do. That
is the mode it is installed in for Monday's test. A failed health check turns it
off by itself.

## The three things that are yours

1. **Read the 07:30 post.** If a reason does not convince you, it should not
   convince the system. Tell Astra; the fix goes in a prior or the doctrine, not
   in a one-off override.
2. **Apply prior changes.** Tuesday proposes; it never applies (§7.4). Change
   `priors.yaml`, date it in the operating log.
3. **Send nothing yourself while it is on.** A lineup change or an add you make by
   hand is fine, the next sweep reads the roster fresh. A trade you send by hand
   is not counted against its three; tell it.

## Running things by hand

```
./.venv/bin/python scripts/research_week.py            # this morning's dossiers (resumes)
./.venv/bin/python scripts/manage.py --no-agent        # core's plan, no model, no writes
./.venv/bin/python scripts/manage.py                   # the sweep; ENABLED=off = agent runs, every write refused
./.venv/bin/python scripts/manage.py --tuesday         # the review
./.venv/bin/python scripts/manage.py --task lineup     # lineup only
tail -f data/manager.log                               # everything cron did
tail -50 data/decisions.jsonl                          # every action WITH the number behind it
cat data/lessons.md                                    # what it has learned
ls data/research-week/                                 # this morning's dossiers
```

## What is deliberately not proven yet

- **Trades against ESPN.** `propose_trade`, `accept_trade` and `reject_trade`
  reach the browser through the write gate, but none of the three has been
  exercised against the live site, by your instruction. The first one is a
  supervised run.
- **The lineup write** has selectors verified on 2026-09-04 but has not moved a
  live lineup yet. Thursday 18:30 is its first real pass; watch #fantasy.
- **Nothing posts to league chat.** There is no tool for it (§8.2).

## If something is wrong

| | |
|---|---|
| "Log in Required" in a screenshot | Web session expired. `scripts/login.py` on the laptop, `--verify`, `scp` the session file to the box. |
| Health check failing | `scripts/healthcheck.py`. Cookies in `.env` may be dead; re-mint. |
| A bad move went out | `echo off > ENABLED`, undo it in the app, tell Astra what the reasoning got wrong. |
| It did nothing all week | `cat data/manager.log`; `crontab -l` should show the six lines. |
