---
source: Astra, 2026-09-05 (Sat, post-draft). Pearce's brief, verbatim intent.
status: PLAN — the in-season manager, built to the original spec (00-inbox/Fantasy Agent.md) plus the 09-05 additions.
governs: implements docs/fantasy-playbook.md §1.3, §1.4, §4–§7. The playbook wins where they disagree.
---

# The manager — build plan

**The brief (Pearce, 2026-09-05, after an ESPN draft grade of 10th/10):** *"the manager agent
better clutch up."* Complete the original spec: wake every morning, do its own research first
(injury reports, news, usage, analyst reads), then optimise — lineup, waivers, trades — with
concrete reasoning for every move. Hard limits so it has to think: **3 waiver adds a week, 3 trade
proposals a week.** Don't drop a player who could be traded for value. Fix the roster shape (three
TEs, one needed). Every Tuesday, grade the week's decisions and learn. Don't trust ESPN's raw
projection. Ping on Slack when human input is needed.

## Deliverables

| # | Deliverable | Where | Done when |
|---|---|---|---|
| D1 | **The doctrine** — a sourced in-season management doctrine the agents load every run | `docs/fantasy-doctrine.md` | Inlined into the system prompt for decide-tasks; every principle has a source |
| D2 | **Morning research** — one dossier per rostered player + top waiver/trade candidates, in-season shape: status, practice, usage trend, matchup, analyst consensus, and a bounded weekly multiplier | `agent/prompts/weekly_dossier.md`, `agent/schemas/weekly_dossier.json`, `scripts/research_week.py` | Runs before the sweep; multipliers land in the week valuation via `PlayerContext`, clamped |
| D3 | **Roster shape** — surplus / shortage per position against starters + flex + bye cover; drives drops (surplus first), trades (give surplus, get shortage) and the "3 TEs" fix | `core/manager/roster.py` | Waiver drop prefers surplus; trade ideas target shape |
| D4 | **Fixes** — the flex-starter drop defect; drop cost computed from ONE optimal lineup | `core/manager/waivers.py` | Test: a flex starter costs his points |
| D5 | **Weekly caps** — 3 adds/week, 3 proposals/week (1/day), enforced in `write_gate`, visible in the packet | `core/gates/rate_limits.py`, `priors.yaml` | Fourth add of the week refused by §5.7 |
| D6 | **Trade before drop** — a droppable player with real ROS value is flagged `tradeable`; the sweep must try a trade or justify the drop | `waivers.py`, `trades_out.py`, prompts | Waiver plan carries the flag; daily prompt enforces |
| D7 | **Outgoing trades** — `propose_trade` as a gated write (rate-limited, §6.2/§6.3 checked in code), browser action written fail-closed, **untested by instruction** | `core/browser/actions.py`, `core/mcp_server.py` | Tool exists, gates refuse on limits, not exercised live |
| D8 | **Reasoning contract** — every action carries `short_term`, `long_term`, `alternative`, `evidence`; validated in code, not asked for in prose | `agent/schemas/actions.json`, `agent/run.py`, `agent/prompts/daily.md` | An action missing any field is rejected |
| D9 | **Tuesday** — box scores → efficiency, calibration, decision grading → agent narrates → dated history file + `data/lessons.md` (the memory the daily run reads) | `scripts/manage.py --tuesday`, `core/manager/review.py`, `core/state/lessons.py` | History file written; lessons fed into the next daily packet |
| D10 | **Schedule** — cron on the box: research + sweep daily, Tuesday review, lineup passes Thu/Sun/Mon | `scripts/cron_manage.sh`, crontab | Installed with `ENABLED=off` (read-and-report) for Monday's test |
| D11 | **Runbook + tests** | `docs/runbook-season.md`, `tests/` | Green |
| D12 | **Reasoning drives** *(09-05, second brief)* — core annotates, the agent decides: every waiver candidate with `flags` + `core_verdict` instead of a hidden `skipped`; trade ideas carry `market_ratio` (ADP decaying into ROS rank) and `their_gain` is advisory; `why_they_accept` required on every proposal; `research_player` lets the sweep research any player mid-decision | `core/manager/waivers.py`, `core/manager/trades_out.py`, `core/model/market.py`, `core/mcp_server.py` | Hard in code = cap · room · §5.5 · §6.1 · §6.2 · market floor · §6.5 · gauntlet. Everything else a flag |
| D13 | **Slack says what, the log says why** — one sentence + one line per move; full reasoning in `data/reasoning/` | `scripts/manage.py` | No paragraphs in #fantasy |
| D14 | **Draft post-mortem** — why 10th/10, measured | `docs/draft-post-mortem-2026.md` | Two candidate fixes benchmarked; the simulator's blind spot named |

## Cadence (CT)

| When | Task | Writes |
|---|---|---|
| Daily 07:00 | `research_week.py` — roster + candidates | none (dossiers) |
| Daily 07:30 | `manage.py` — full sweep | lineup, adds (cap 3/wk), proposals (cap 3/wk, 1/day), incoming trades |
| Tue 07:30 | `manage.py --tuesday` then the sweep | history file, lessons |
| Thu 18:30 | `manage.py --task lineup` | lineup (TNF lock) |
| Sun 11:00 · 15:00 · 19:00 | `manage.py --task lineup` | lineup (late news, late swap §4.4) |
| Mon 18:30 | `manage.py --task lineup` | lineup (MNF lock) |

## Not done, by instruction

Trades are not exercised against ESPN and nothing posts to league chat. The browser paths for
propose/accept/reject are written fail-closed and unverified until Pearce says otherwise.
