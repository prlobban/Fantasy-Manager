# Fantasy-Manager

An autonomous agent that drafts and manages an ESPN fantasy football team.
Runs unattended on the OptiPlex box (`jarvis`). Live league, real money,
irreversible writes.

## The one boundary

**`core/` does. `agent/` decides. Anything on a clock belongs to `core` alone.**

- `core/` is deterministic, no LLM anywhere inside it. Every write is a named
  function behind `core/gates/write_gate.py` and the `ENABLED` kill switch.
- `agent/` is `claude -p`. It reads the doctrine and `core`'s outputs and can
  only act through `core`'s MCP tool list (`core/mcp_server.py`).
- The live draft loop never waits on a model. The agent's draft-day input is
  the dossiers it wrote the night before, clamped to ±15% (`model.override_cap`).

ESPN's API is read-only. Every write is Playwright against the web UI, which is
why the draft is built around the **queue** rather than around clicking fast:
ESPN autopicks from the top of our queue, so a correct queue with the click leg
dead is still a competent draft.

## Where things are

| | |
|---|---|
| `docs/fantasy-playbook.md` | the rules, numbered §1–§10 — cite, don't paraphrase |
| `docs/operating-log-2026-season.md` | league config, IDs, change log, settled questions |
| `docs/runbook-draft-day.md` | draft day, step by step |
| `docs/runbook-season.md` | **the season: what wakes when, what lands in Slack, what is yours** |
| `docs/fantasy-doctrine.md` | the craft, D1–D8, sourced; inlined into every deciding agent run |
| `docs/build-plan.md` | every file and why it exists |
| `docs/*-plan.md` | the measurements: backtest, autopick benchmark, draft optimisation, projection model |
| `priors.yaml` | every tunable, each citing its § — nothing hardcodes a threshold |
| `core/model/` | the valuation engine: projections → VOR → tiers → durability |
| `core/draft/` | board, survival, room, picker, queue, judge verdicts, the loop |
| `core/proj/` | our own projection model — built, measured, **off** (`projection_blend: 0.0`) |
| `core/backtest/` | replay real seasons; the 40-seat arena vs ESPN autopick |
| `core/manager/` | in-season: lineup, waivers, trades, the incoming-trade gauntlet |
| `scripts/` | the entry points (below) |

## Running it

`python` on its own works on neither machine. Laptop: `.\.venv\Scripts\python.exe`
(or `.\.venv\Scripts\Activate.ps1` once per terminal). Box: `./.venv/bin/python`.

```
ssh jarvis && cd ~/Fantasy-Manager          # the box (~/.ssh/config on the laptop)
```

| What | Command |
|---|---|
| Health check | `scripts/healthcheck.py` |
| Build the board | `scripts/build_board.py` |
| Research pass (dossiers → overrides) | `scripts/research.py` · `--refresh-top 15` after the order lock |
| Practice draft, random seat | `scripts/practice.sh [slot]` |
| **Draft day** | `scripts/draft_day.sh start\|status\|stop` |
| Morning research (in-season) | `scripts/research_week.py` |
| Daily sweep, no writes | `scripts/manage.py --dry-run --no-agent` |
| Tuesday review | `scripts/manage.py --tuesday` |
| Cron entry point | `scripts/cron_manage.sh research\|sweep\|tuesday\|lineup` |
| Benchmark vs ESPN autopick | `scripts/benchmark.py` |
| Re-find ESPN selectors | `scripts/discover_selectors.py --draft --headed` (laptop only) |
| Tests | `python -m pytest -q` |

### The ESPN session

`scripts/login.py` opens a browser, you log in, it saves `data/espn-session.json`.
`--verify` proves it headless. `scp` it to the box. It is a **live credential**:
gitignored, never in Slack, email or the vault. When #fantasy reports
"Log in Required", repeat.

### The kill switch

`ENABLED` holds `on` or `off`. Every write refuses on `off`. It ships off,
nothing in `core` ever turns it on, and it is machine-local (gitignored).
Box: `echo on > ENABLED` / `echo off > ENABLED`.

## Testing

`pytest` runs everything that needs no credentials. `-m live` hits real ESPN,
`-m browser` drives a real browser. The draft simulator and the arena exercise
the *same* picker the live loop uses; there is no test-only path.

This repo is public. `.gitignore` is deny-by-default; add to it before you add
a new output path.
