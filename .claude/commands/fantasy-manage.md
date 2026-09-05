---
description: 'Season manager context — the cadence, the limits, the reasoning contract, and how to drive a sweep by hand.'
---

The in-season loop. It runs itself on the box by cron (`scripts/cron_manage.sh`);
`docs/runbook-season.md` is the procedure and Pearce runs it. This file is what
a Claude session needs beyond it.

`python` means `./.venv/bin/python` on the box, `.\.venv\Scripts\python.exe` on
the laptop.

## The cadence (CT)

| When | Task | Writes |
|---|---|---|
| 07:00 daily | `research_week.py` — a dossier per rostered player, waiver candidate, trade target | none |
| 07:30 daily | `manage.py` — assessment, lineup, adds, proposals, incoming offers | lineup · add (3/wk) · propose (3/wk, 1/day) · accept/reject |
| 07:30 Tue | `manage.py --tuesday`, then the sweep | history file, `data/lessons.md` |
| Thu 18:30 · Sun 11/15/19 · Mon 18:30 | `manage.py --task lineup` | lineup only |

## What is enforced in code, not in the prompt (D9: everything else is a flag)

- **§5.7** three roster adds per rolling week — `write_gate` refuses the fourth.
- **§5.5** a top-N player by ROS VOR is never the drop — `add_drop` refuses.
- **§6.1** three proposals a week, one a day, one open per manager, no re-propose
  inside 14 days. **§6.2** our lineup must improve, **§6.3** the market ratio
  (`trades.min_market_ratio`, from `core/model/market.py`) must clear the floor,
  **§6.5** no protected asset for a package — all re-run inside `propose_trade`.
  Their model gain is ADVISORY. `why_they_accept` is required.
- **D8** every action carries `reason · short_term · long_term · alternative ·
  evidence · would_be_wrong_if`, or `agent/run.py` rejects the whole reply.
- **D1.4** the morning dossiers' multipliers (±25% week, ±15% ROS, two hosts for a
  big move) land in `value_pool` via `core/manager/research.py`. The agent reads
  the facts; it does not re-derive the number.
- **D9.4** `research_player(espn_id, question)` runs a `weekly_dossier` from
  inside the MCP server, capped by `research_week.on_demand_max` per run.

Flags, not refusals: §5.3.1 the priority ladder, §5.3.3 streamer rule, D4.5
trade-before-drop, D5 shape effects — `waivers.Candidate.flags`,
`trades_out.Proposal.flags`.

## Run by hand

```
python scripts/research_week.py --roster        # cheap: our 13 only
python scripts/manage.py --no-agent             # core's plan, no model, no writes
python scripts/manage.py                        # the sweep; ENABLED=off = agent runs, writes refused
python scripts/manage.py --tuesday              # the §7 review
```

Start with `--no-agent`. If core's plan looks wrong, the agent cannot
fix it and the bug is in `core`.

## Reading what it did

```
tail -f data/manager.log             # every cron run
ls -t data/reasoning/ | head         # the full reasoning per run (Slack only gets the what)
tail -50 data/decisions.jsonl        # every action WITH the number behind it
cat data/lessons.md                  # the memory
ls -lt data/agent-runs | head        # full transcripts
ls -lt data/screenshots | head       # every write leaves a screenshot
ls data/research-week/               # this morning's dossiers
```

## Not yet proven

`propose_trade`, `accept_trade`, `reject_trade` and `set_lineup` are written
fail-closed behind the gate and have not moved a live ESPN page yet (trades by
Pearce's instruction). The first live write of each is a supervised run: watch
#fantasy and the screenshot.
