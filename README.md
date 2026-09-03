---
source: Astra (drafted 2026-09-03 from 00-inbox/Fantasy Agent.md + a three-round scoping interview)
status: MAP — the fantasy system's map. Navigation and structure only; holds no live state by design.
note: ships INTO the fantasy repo as its doctrine. The vault keeps only a pointer.
---

# 🏈 Fantasy Agent — README (the map)

An autonomous agent that **drafts** the team and then **manages it all season** — lineups, waivers,
trades — on ESPN. Its own project, its own GitHub repo, running unattended on the OptiPlex box.

> 🗺️ **This is the map, not the rules and not the state.** Judgment lives in the playbook. Current
> config and what's already been done live in the operating log. Nothing here is a number, a status,
> or a date.

⚠️ **This is not an Astra system.** Astra drafted the doctrine; it does not run the agent, and the
vault is not its home. The vault holds one pointer line and nothing else.

---

## The two layers

The whole architecture is one boundary, and it exists because **the draft has a 60-second clock and
the writes are irreversible.**

### 🔧 `core` — the actions
Deterministic. **No LLM anywhere inside it.** Every capability the system has is a named function
here, and each one carries its own gate.

- ESPN read client (`espn-api` + raw `lm-api-reads` views)
- Browser write driver (Playwright/Chromium, ESPN cookies) — **the only write path that exists**
- Data pipeline: nflverse / `nfl_data_py`, ADP, Vegas lines
- **The valuation engine** — projections → VOR → tiers → durability → context. One model,
  four consumers (`§2`)
- Draft queue manager (`§3.3`) and the live re-rank loop
- State store, run log, health check, kill switch

### 🧠 `agent` — the reasoning
`claude -p` on the box. Reads the doctrine and `core`'s outputs, makes the calls that don't reduce
to a formula, then invokes `core` actions by name.

- Interpreting news, beat reports, coach-speak against the model (`§2.8`)
- Judgment on trades — framing an offer, reading an incoming one (`§6`)
- The Tuesday review and prior calibration (`§7`)
- Anomalies, and anything where the model disagrees with itself

### The boundary rule
**`core` does; `agent` decides — and anything on a clock belongs to `core` alone.**
The live draft loop never waits on the agent (`§3.2`, `§8.7`, `§10`). The agent can only do what
`core` exposes as a function, which means the write table in `§8.2` is enforced in code, not in a
prompt.

---

## Components

| Piece | Location | Role |
|---|---|---|
| **Primer** | `.claude/commands/fantasy.md` | Loads map + playbook + operating log, orients, **stops**. Never executes. |
| **Draft worker** | `.claude/commands/fantasy-draft.md` *(not built)* | Draft-day loop. Mostly a thin driver over `core` — the clock lives here. |
| **Manager worker** | `.claude/commands/fantasy-manage.md` *(not built)* | Daily sweep: lineup, waivers, trades. Tuesday runs the review variant. |
| **Playbook** | `docs/fantasy-playbook.md` | The RULES. Numbered so everything else can cite them (`§3.5`). |
| **Operating log** | `docs/operating-log-2026-season.md` | The STATE. Config, IDs, change log, watch items, **what NOT to re-propose.** |
| **History** | `docs/2026-season/YYYY-MM-DD-<what>.md` | Dated reviews, postmortems, the Tuesday passes. **Accrete; never edited.** |
| **`core`** | `core/` | The action layer, above. |
| **`agent`** | `agent/` | The reasoning layer, above. |

**The unit is a season.** `2026-season` is the first. A second league or a second year gets its own
operating log and history folder, sharing this playbook.

**Stack: Python.** Not a preference — `espn-api` and `nfl_data_py` are both Python, and Playwright
has a first-class Python binding. Fighting that costs a day we don't have.

---

## Sources — and what each one answers

**Nothing in this doctrine hand-copies a live number.** The docs hold rules, structure and IDs; the
numbers live in the sources below and `core` reads them at run time.

| Source | Auth | Answers |
|---|---|---|
| **ESPN read API** (`lm-api-reads.fantasy.espn.com`, via `espn-api`) | `SWID` + `espn_s2` cookies | League settings & scoring (`mSettings`), rosters (`mRoster`), matchups (`mMatchup`), draft state (`mDraftDetail`), free agents, and **ESPN's own weekly + ROS projections** (`kona_player_info`). Authoritative for anything league-specific. |
| **ESPN web UI** (Playwright) | same cookies | **Every write.** The read API writes nothing — see below. |
| **nflverse / `nfl_data_py`** | none | Historical weekly stats, snap counts, target share, and **injury history** — the raw material for durability and consistency scoring. |
| **News** (WebSearch/WebFetch, beat reporters, official injury reports) | none | Depth-chart changes, practice participation Wed/Thu/Fri, game-day statuses. What projections lag on. |
| **ADP** (Fantasy Football Calculator, ESPN draft ranks) | none | Where the *room* thinks a player goes — the input to "will he survive to my next pick?" (`§3.5`). |
| **Vegas** (total + spread) | none | Game script (`§2.7`). |

### 🔴 The hard constraint that shapes everything

**ESPN's fantasy API is read-only.** There is no supported write endpoint for a draft pick, a lineup
change, a waiver claim, or a trade. Every write goes through the browser as a logged-in user. That
single fact is why `core` owns a Playwright driver, and why the draft is built around the queue
(`§3.3`) rather than around clicking fast.

---

## How it's operated

**Everything runs on the OptiPlex box (`jarvis`)** — headless, unattended, `claude -p` on a cron.
Same host as the support agent and the Daily Doc; reuse that Claude auth path rather than minting a
second one (**verify, don't assume**).

**Draft day adds one thing: Pearce has the draft room open on his laptop as a hot spare.** He can
see what the agent is doing and take the wheel. It costs nothing and it removes the only
single-point-of-failure that matters — a headless browser hitting a login wall or a modal at 11:00
with nobody watching.

Credentials (`SWID`, `espn_s2`) live in the repo's gitignored `.env` on the box, **never committed
and never in the vault.**

---

## Infra prerequisites

| Item | State |
|---|---|
| GitHub repo created, box has a clone | **TBD** |
| ESPN `SWID` + `espn_s2` cookies minted, in `.env` on the box | **TBD** — blocks everything |
| Long-lived Claude auth on the box for unattended `claude -p` | **Likely already solved** — the support agent and Daily Doc run this way on `jarvis`. Reuse; verify. |
| Python venv: `espn-api`, `nfl_data_py`, `playwright` + Chromium | **TBD** |
| Headless Chromium proven against the live ESPN draft room | **TBD** — the load-bearing unknown |
| Kill switch file (`ENABLED` on/off), mirroring `~/astra-support/ENABLED` | **TBD** — `§8.4` |

---

## Roadmap

1. **Pre-draft (Fri 09-04):** repo + venv, cookies, league read working, board precomputed (`§3.2`),
   **queue write proven end-to-end in a mock draft.**
2. **Draft day (Sat 09-05, 11:00 CT):** the draft worker, box-run, laptop watching.
3. **Week 1:** the manager worker — lineup first, then waivers.
4. **After a Tuesday review or two:** trades, and the prior-calibration loop (`§7`).
