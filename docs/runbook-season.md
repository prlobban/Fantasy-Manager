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
| Daily 07:30 | `sweep` | **Every day, Sunday included.** The manager reads this morning's research, assesses the roster (shape, holes, this week's ask), then decides: lineup, adds (max 3 a rolling week), trade proposals (max 3 a week, 1 a day), incoming offers through the gauntlet. Every action carries the six-part reasoning. Posts the assessment and every move to #fantasy. |
| Tue 07:30 | `tuesday` | Pulls last week's box score. Grades every decision as a decision, not an outcome. Writes the dated review to `docs/2026-season/`, appends lessons to `data/lessons.md` (read by every future run), proposes prior changes for you. Then the normal sweep. |
| Sun 11:00 | `sweep` | A second full sweep on game day, an hour before the early kickoffs — so a stream or an add decided after 07:30 can still land for this week. Same caps. |
| Thu 18:30 · Sun 15:00, 19:00 · Mon 18:30 | `lineup` | Lineup only: late news, inactives, and the late swap (§4.4). No adds, no trades. |

## What lands in #fantasy

Short, by design. What it did, not why.

- **07:00** one line: dossiers written, vetoes, cost.
- **07:30** one sentence, then one line per move:
  `✅` done · `⛔` refused (with the gate) · `would:` what it would have done with the switch off.
  Example:
  ```
  Week 2 sweep · READ-ONLY
  Roster is RB-thin and TE-heavy; one add and one offer would fix both.
  would: lineup: Chuba Hubbard → RB/WR/TE, Luther Burden III → BE
  would: add Tyrone Tracy Jr., drop Travis Kelce
  would: offer to GLOBO GYM: give Kyle Pitts Sr., Justin Herbert / get Garrett Wilson
  ```
- **Lineup passes** (Thu/Sun/Mon) post only when something changed.
- **Needs Pearce** as a separate warning, two sentences and the ask.
- **Tuesday** one line: result, efficiency, lessons, and the path to the review.
- **Any cron failure** as an error.

**The why** is in `data/reasoning/<date>-<task>.md` on the box: the roster assessment, every
action's six reasoning fields, `why_they_accept` on offers, what was refused and by which gate.
`data/decisions.jsonl` has the same per write, with the numbers.

## The switch

```
cat ENABLED              # on / off
echo on  > ENABLED       # writes allowed
echo off > ENABLED       # read-and-report: every write refused, everything still posts
```

With it **off**, every wake-up still runs and still posts what it *would* do.
**It has been on since Mon 2026-09-07 09:30 CT.** A failed health check turns it
off by itself.

## The three things that are yours

1. **Read the 07:30 post.** If a move surprises you, open that morning's
   `data/reasoning/` file. If the reason does not convince you, it should not
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
ls -t data/reasoning/ | head                           # the why, per run, readable
cat data/lessons.md                                    # what it has learned
ls data/research-week/                                 # this morning's dossiers
```

## How it decides (the short version)

`core` computes; the agent decides (D9). The agent sees every waiver candidate
with core's number and core's objections as flags, every trade idea with our
gain and the market read, and it may act against a flag with a written reason.
It can research any player itself mid-decision (`research_player`, capped at
six a run). What it cannot reason past: three adds a week, roster room, never
dropping a top-5 player, the proposal limits, our lineup must improve, the
market-ratio floor on offers, no top-3 asset for a package, and the trade
gauntlet on accepts.

## What is proven, and what is not

**Proven 2026-09-08:** the add/drop path — search, ENTER, the icon `+`, the drop
modal, an enabled Continue — driven to one click short of commit and cancelled.
The Propose Trade page opens, ticks all three players and assembles the offer.

**Not proven:**

- **The trade SEND button**, the one click past "Continue". Verifying it means
  sending a real offer, so it needs your word first.
- **`accept_trade` / `reject_trade`** — no incoming offer has arrived yet.
- **`set_lineup`** has verified selectors (2026-09-04) but has still not moved a
  live lineup: every pass since the switch went on has found nothing to change.
- **Nothing posts to league chat.** There is no tool for it (§8.2).

The first live pass of each is a supervised run: watch #fantasy and the
screenshot.

## If something is wrong

| | |
|---|---|
| "Log in Required" in a screenshot | Web session expired. `scripts/login.py` on the laptop, `--verify`, `scp` the session file to the box. |
| Health check failing | `scripts/healthcheck.py`. Cookies in `.env` may be dead; re-mint. |
| A bad move went out | `echo off > ENABLED`, undo it in the app, tell Astra what the reasoning got wrong. |
| It did nothing all week | `cat data/manager.log`; `crontab -l` should show the six lines. |
