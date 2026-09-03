---
source: Astra, 2026-09-03 (Thu). Ships into the repo as docs/build-plan.md.
status: PLAN — the engineering plan for prlobban/Fantasy-Manager. Every file, how it works, how it's tested, what Pearce does.
governs: implements docs/fantasy-playbook.md. Where they disagree, the playbook wins and this file is wrong.
---

# 🏈 Fantasy-Manager — Build Plan

**Repo:** `prlobban/Fantasy-Manager` (currently one README). **Host:** OptiPlex `jarvis`.
**Draft:** Sat 2026-09-05 11:00 CT. **Language:** Python 3.12 (what the box has).

---

## 0. The calendar, corrected

"Both halves by Saturday" is not what the calendar actually demands. The real deadlines:

| Piece                       | First moment it's needed                                                 | Build window          |
| --------------------------- | ------------------------------------------------------------------------ | --------------------- |
| **Draft**                   | Sat 09-05 11:00                                                          | **Friday.** One day.  |
| **Lineup**                  | NFL kickoff Thu 09-10 (Labor Day is 09-07); most starters lock Sun 09-13 | Sun–Wed 09-06 → 09-09 |
| **Waivers**                 | First waiver run after Week 1, ~Wed 09-16                                | 09-10 → 09-15         |
| **Trades (out + gauntlet)** | Whenever — no clock                                                      | Week 2+               |
| **Tuesday review**          | Tue 09-15                                                                | 09-10 → 09-14         |

So Friday builds the draft and *nothing else*. The manager has five more days, with no clock on any of it. This isn't a scope cut — it's the order the season forces.

---

## 1. Architecture

```
                         ┌────────────────────────────────────────────┐
                         │  agent/  (claude -p, reasoning, off-clock)  │
                         │  prompts + situation packet → JSON actions  │
                         └───────────────┬────────────────────────────┘
                                         │  MCP (stdio) — the ONLY interface
                                         │  tools = §8.2 table, nothing else
                         ┌───────────────▼────────────────────────────┐
                         │  core/  (deterministic, no LLM anywhere)    │
                         │                                            │
   ESPN read API ──────► │  espn/     model/     draft/    manager/   │
   nflverse ───────────► │  data/     gates/     state/    browser/   │
   news, ADP, Vegas ───► │                                            │
                         │  every write ──► gates/write_gate ──► browser/actions ──► ESPN UI
                         └────────────────────────────────────────────┘
```

**Three rules the layout enforces** (playbook `§10`):

1. **`core` never imports anything from `agent`.** The dependency arrow points one way.
2. **The draft loop (`core/draft/run.py`) never calls the agent.** The agent's draft-day contribution is a file it writes *before* 11:00 (`data/overrides.json`). On the clock, nothing waits on a model.
3. **The agent's only tools are `core`'s MCP tools.** `--strict-mcp-config` + an allowlist means no Bash, no WebFetch, no file access. News reaches the agent through `core` so it's cached, sourced and logged.

---

## 2. Verified today (so the plan isn't guessing)

| Fact | Source |
|---|---|
| Box: Ubuntu, Python **3.12.3**, Node 22, 15 GB RAM, 65 GB free. `claude` at `~/.npm-global/bin/claude`, **not on cron's PATH** — the support wrapper exports it. | ssh, 09-03 |
| Unattended `claude -p "/cmd" --dangerously-skip-permissions` already works on the box (support agent, hourly). **Auth is solved; reuse it.** | `~/astra-support/run-support.sh` |
| No Playwright, no Chromium, no browser deps installed on the box. | ssh |
| `claude -p` supports `--system-prompt-file`, `--output-format json`, **`--json-schema`**, `--mcp-config`, `--strict-mcp-config`, `--allowedTools`, `--max-turns`. | code.claude.com/docs/en/cli-reference |
| `espn-api`: `League(league_id, year, espn_s2, swid)`; `free_agents()`, `player_info()`, `box_scores()`, `transactions()`, **`offers_report()` (pending trade offers)**, `refresh_draft()`. Draft picks carry `playerId, teamId, roundId, roundPickNumber, overallPickNumber, autoDraftTypeId`. Projected vs actual = `statSourceId` 1 vs 0; season total = scoring period 0. **Zero POST methods.** | cwendt94/espn-api source |
| `nfl_data_py` is **archived (Sep 2025)** → use **`nflreadpy`** (Polars): `load_player_stats(seasons, summary_level="week")`, `load_injuries`, `load_snap_counts`, `load_schedules`, `load_players` (cross-platform IDs incl. ESPN), `load_ff_playerids`, `load_ff_opportunity`, `load_ff_rankings`, `load_depth_charts`. | nflreadpy docs |
| ESPN Player Queue: unlimited size; "players listed in the Player Queue will be automatically drafted if on Autopick." Mock Draft Lobby: rooms start every few minutes, plus "autodraft mocks tailored to your specific custom leagues." | ESPN support |
| Repo `prlobban/Fantasy-Manager` is **public**. | GitHub |

---

## 3. The repo — every file

```
Fantasy-Manager/
├── README.md                        MAP — from the doctrine. Components, sources, how it's operated.
├── CLAUDE.md                        Context for any Claude Code session in this repo. Short: points at docs/, restates §10 + the write table.
├── pyproject.toml                   Deps + entry points. espn-api, nflreadpy, polars, playwright, mcp, pydantic, pyyaml, pytest.
├── .env.example                     Every env var, no values.
├── .gitignore                       .env, data/, screenshots/, *.log, .venv/
│
├── docs/                            The doctrine, unchanged from the vault draft.
│   ├── fantasy-playbook.md          RULES (§1–§10)
│   ├── operating-log-2026-season.md STATE
│   ├── build-plan.md                this file
│   └── 2026-season/                 HISTORY (accretes)
│       └── 2026-09-03-system-design.md
│
├── .claude/commands/
│   ├── fantasy.md                   Primer. Load, orient, stop.
│   ├── fantasy-draft.md             Draft-day RUNBOOK for a human (Pearce or Astra on the laptop): pre-flight checks, start the loop, what to watch, how to take over.
│   └── fantasy-manage.md            Manager runbook: run a sweep by hand, read the log, flip the switch.
│
├── priors.yaml                      EVERY [v1 prior] from the playbook as a named key. §7.4's "update in one place" is literally this file.
│
├── core/
│   ├── __init__.py
│   ├── config.py                    Loads .env → typed Settings (league_id, team_id, year, cookies, paths, notify target). Fails loud if anything's missing.
│   │
│   ├── espn/
│   │   ├── client.py                One League() instance + raw view fetch: get_view(views=[...], filter={...}). Retries, timeouts, and the 401/redirect sniff that feeds health.py.
│   │   ├── settings.py              mSettings → LeagueSettings (frozen dataclass): scoring rules, roster slots + counts, team_count, draft_type, waiver_type, faab_budget, trade_deadline, playoff weeks, keeper flag. Asserts draft_type == SNAKE (§3.1).
│   │   ├── players.py               kona_player_info → PlayerPool: espn_id, name, pos, team, eligible slots, ROS projection, weekly projections, ownership %, ESPN ADP, injury status, bye. Top ~400 by ownership.
│   │   ├── league_state.py          Rosters (all teams), current matchup, standings, free agents, pending offers (offers_report), recent transactions. One snapshot object, timestamped.
│   │   └── health.py                §8.5. Fetch our roster; assert team_id matches, week matches, response isn't a login page. Returns HealthResult. Any failure → gates.kill_switch.off(reason).
│   │
│   ├── data/
│   │   ├── nflverse.py              nflreadpy loaders (cached to data/cache/ as parquet, refreshed daily): weekly stats 2023–25, injuries 2023–25, snap counts, schedules 2026, ff_playerids. Exposes join_key(espn_id).
│   │   ├── adp.py                   ESPN ADP (primary — it's the population we're drafting against) + Fantasy Football Calculator (secondary). Returns adp, adp_stdev per player.
│   │   ├── vegas.py                 ESPN public scoreboard → spread + total per game, current week. No auth.
│   │   └── news.py                  Fetch + normalise news items for a set of players (source, url, published_at, text). Cached. NO judgment — that's the agent's.
│   │
│   ├── model/                       THE valuation engine. Pure functions, no I/O. One implementation, four consumers (§2, §10.4).
│   │   ├── priors.py                Loads priors.yaml into a typed Priors object. Nothing else reads the yaml.
│   │   ├── replacement.py           §2.3: replacement_rank(pos, settings) → the rank; replacement_points(pos, pool, settings).
│   │   ├── vor.py                   §2.3–2.4: vor(pool, settings, window) and tiers(pool_by_pos) using the gap rule.
│   │   ├── durability.py            §2.5: availability(player, injury_history) → multiplier ∈ (0,1] + hard_veto reasons.
│   │   ├── variance.py              §2.6: weekly stdev, bust_rate from weekly stats.
│   │   ├── context.py               §2.7: weekly multipliers from opponent-vs-pos, usage trend, Vegas, pace.
│   │   ├── value.py                 THE function: value(player, window, settings, ctx) → Valuation(points, vor, tier, availability, stdev, bust_rate, components{}). Every consumer calls this and nothing else.
│   │   └── schema.py                Pydantic models: Player, Valuation, LeagueSettings, RosterSlot, Action, GauntletResult, DecisionRecord.
│   │
│   ├── draft/
│   │   ├── board.py                 §3.2: build_board() → data/board.json. Runs pre-draft. Pulls pool + nflverse + ADP, computes value() for everyone, applies data/overrides.json (agent's news pass, capped at ±priors.override_cap), writes the ranked board with a one-line rationale per player.
│   │   ├── survival.py              §3.5: p_available(player, picks_until_our_turn, room) from ADP + adp_stdev, adjusted by room needs.
│   │   ├── room.py                  §3.6: RoomModel — per-team filled slots + remaining needs, run detection (3 of last 5), our pick positions for the whole draft from slot + team_count.
│   │   ├── picker.py                §3.4/3.5/3.7: rank(board, room, my_roster) → ordered list. Pure. Computes Cost(pos) per position, applies tier-break logic, then roster constraints (no K/DST early, QB count, bye collisions, upside-in-late-rounds).
│   │   ├── reader.py                Draft state. ApiReader: mDraftDetail polled every 2s → picks. DomReader: parse the draft-room pick history from the page. Same interface. **DOM is what the mock draft validates; API gets its first live test Saturday with DOM as fallback.**
│   │   ├── queue.py                 §3.3: sync(target_top_n) — diff-based. Reads current queue from DOM, computes add/remove/reorder ops, executes only the delta. Never rebuilds from scratch on the clock.
│   │   └── run.py                   THE LIVE LOOP. Deterministic. See §4 below.
│   │
│   ├── browser/
│   │   ├── session.py               Playwright Chromium: persistent context, cookies injected from .env, realistic UA/viewport, screenshot-on-exception to data/screenshots/, one retry then fail closed.
│   │   ├── selectors.py             ALL selectors in one file, with a comment per selector saying which mock-draft session it was verified in. When ESPN ships a redesign, this is the only file that changes.
│   │   └── actions.py               The physical writes: draft_player(id), queue_add/remove/move, set_lineup(slot_map), add_drop(add_id, drop_id), waiver_claim(add_id, drop_id, bid), propose_trade(...), accept_trade(offer_id), reject_trade(offer_id). Each returns a Receipt with a screenshot path. **Nothing here checks a gate — that's write_gate's job, and actions.py is only ever called through it.**
│   │
│   ├── manager/
│   │   ├── lineup.py                §4: optimal_lineup(roster, valuations, settings, matchup) — assignment over legal slots, then the §4.2 variance swap based on projected margin. Returns the slot map + a diff vs current + reasons.
│   │   ├── waivers.py               §5: candidates (FA pool vs our worst starter/bench by value), bid from the §5.3 ladder, §5.4 budget floor, §5.5 never-drop list.
│   │   ├── trades_out.py            §6.1–6.7: candidate generation from complementary surpluses, §6.2 starting-lineup delta both sides, §6.3 fairness check, rate-limit check. Returns proposals for the agent to frame.
│   │   ├── gauntlet.py              §6.8: run(offer, state) → GauntletResult with all 13 gates, each pass/fail with its number. Pure. One fail = reject. Missing data = fail (§6.8.11).
│   │   └── review.py                §7: efficiency (actual ÷ hindsight-optimal), calibration (proj vs actual by pos/archetype), league scan. Produces the numbers the Tuesday prompt narrates.
│   │
│   ├── gates/
│   │   ├── kill_switch.py           ENABLED file (on/off + reason + timestamp). off() is called by health failures; on() only by a human.
│   │   ├── rate_limits.py           §6.1 / §6.8.10 counters in data/state.json: proposals today/this week, per-manager open offers, last-rejected-by-manager dates, accepts this week.
│   │   └── write_gate.py            §8.2 AS CODE. check(action) → allow/deny(reason). Order: kill switch → fresh health check → fresh roster re-read (§8.3) → action-specific rule (rate limit / gauntlet / lineup-lock) → then and only then browser.actions. Every decision logged.
│   │
│   ├── state/
│   │   ├── store.py                 data/state.json (small, current) + data/cache/*.parquet. No database until earned.
│   │   └── decisions.py             §7.1: append-only data/decisions.jsonl — every action WITH the prediction that justified it, the § cited, and the alternative passed on.
│   │
│   ├── notify.py                    One function: notify(level, title, body, receipt=None). Target TBD (see §9). Used by health failures, every write, every gauntlet result, draft completion.
│   │
│   └── mcp_server.py                FastMCP server exposing core to the agent. The tool list IS the write table — see §5 below.
│
├── agent/
│   ├── prompts/
│   │   ├── system.md                Role + output contract + refusal path. At run time the playbook is INLINED verbatim below it (build_prompt.py). Never summarised.
│   │   ├── predraft.md              Fri/Sat AM: given board + news, produce overrides.json. Bounded: multiplier ∈ [1−cap, 1+cap], one reason, one source per override.
│   │   ├── daily.md                 §1.3: given the packet, decide lineup/waiver/trade actions. Calls tools.
│   │   ├── tuesday.md               §1.4/§7: narrate review.py's numbers, name directional misses, propose (not apply) prior changes.
│   │   └── incoming_trade.md        §6.8.3: write the "why would they send this" sentence; narrate the gauntlet result; call accept/reject.
│   ├── schemas/
│   │   ├── actions.json             --json-schema for daily/incoming: {actions:[{tool, args, cites:[§], reason, confidence}], no_action_reason?}
│   │   ├── overrides.json           --json-schema for predraft
│   │   └── review.json              --json-schema for tuesday
│   ├── build_prompt.py              system.md + docs/fantasy-playbook.md + priors.yaml → one system prompt file. Run every invocation so the prompt can't drift from the doctrine.
│   ├── packet.py                    Situation packet builder: everything the agent needs as ONE JSON object from core (roster w/ valuations, opponent, candidates w/ numbers, news items, rate-limit state, gates in force, last review's calibration notes). The agent never computes a number.
│   ├── run.py                       Invokes claude -p with: --system-prompt-file, --mcp-config agent/mcp.json, --strict-mcp-config, --allowedTools "mcp__fantasy__*", --json-schema, --max-turns, --output-format json. Parses output, RE-VALIDATES against the schema (never trust the model's shape), logs the full transcript to data/agent-runs/.
│   └── mcp.json                     {"mcpServers": {"fantasy": {"command": ".venv/bin/python", "args": ["-m", "core.mcp_server"]}}}
│
├── scripts/
│   ├── setup_box.sh                 venv, pip install, playwright install chromium, ENABLED=off, cron lines, PATH export. Idempotent.
│   ├── mint_cookies.md              The 6-step how-to for SWID/espn_s2 (Chrome → DevTools → Application → Cookies → espn.com).
│   ├── build_board.py               → core.draft.board.build_board(); prints top 30 + tier breaks for a sanity read.
│   ├── draft.py                     → core.draft.run. Flags: --mock <url> (DOM reader only), --dry-run (no queue writes, no click), --no-click (queue only).
│   ├── manage.py                    → daily sweep. Flags: --dry-run, --tuesday, --task {lineup,waivers,trades,review}.
│   ├── discover_selectors.py        Opens a draft room (headed, on the laptop), dumps candidate selectors for queue/draft/pick-history, saves DOM snapshot. Friday's first job.
│   ├── healthcheck.py               → core.espn.health. Cron-able.
│   └── simulate_draft.py            → tests' simulator as a CLI: N sims, prints our roster VOR vs baseline.
│
├── tests/
│   ├── fixtures/
│   │   ├── mSettings.json, kona_player_info.json, mRoster.json, mDraftDetail.json   ← recorded from the real league Friday
│   │   ├── league_12_ppr.json, league_10_half.json                                    ← synthetic settings
│   │   └── offers/ fleece_hurt_star.json, fleece_2for1.json, fleece_news_window.json, fair_surplus_swap.json …
│   ├── test_settings.py             Parses fixtures → correct slots/scoring/team_count; asserts on SNAKE.
│   ├── test_replacement.py          Known-answer: 12 teams, 2RB+1FLEX → RB replacement rank.
│   ├── test_vor_tiers.py            Tier breaks land where the gap rule says; ties don't split tiers.
│   ├── test_durability.py           Soft-tissue recurrence discounts harder than a fracture; one freak injury ≠ pattern; IR-no-return = veto.
│   ├── test_picker.py               No K/DST before final 2 rounds; QB count; bye collisions rejected; late-round upside rule.
│   ├── test_survival.py             p_available monotone in picks-until-turn; room needs lower survival.
│   ├── test_draft_sim.py            12-team snake, 11 ADP-noise bots, 200 sims: roster always legal; our roster VOR beats ADP-bot baseline in ≥70% (a sanity floor, not a truth).
│   ├── test_lineup.py               Optimal assignment; §4.2 floor/ceiling swap flips with margin; OUT never starts.
│   ├── test_gauntlet.py             Every fixture in offers/: fleeces fail on the named gate; the fair swap passes all 13.
│   ├── test_write_gate.py           ENABLED=off refuses every write; rate limits trip; stale-roster re-read happens.
│   ├── test_queue_diff.py           Given current vs target queue, the op list is minimal and order-correct.
│   └── test_agent_output.py         Golden packets → schema-valid outputs; every action cites a §; unknown tool names rejected.
│
└── data/  (gitignored)              board.json · overrides.json · state.json · decisions.jsonl · cache/ · screenshots/ · agent-runs/
```

---

## 4. How each loop runs

### 4.1 Pre-draft (Fri, again Sat ~09:30)
```
build_board.py
  espn.settings ─► espn.players ─► data.nflverse (join on espn_id) ─► data.adp
  ─► model.value(window=ROS) for all ─► vor + tiers ─► durability ─► board.json
agent/run.py --task predraft
  packet = {board top 200, news per player (last 7d)}
  claude -p … --json-schema overrides.json  ─► data/overrides.json  (bounded multipliers + reason + source)
build_board.py --apply-overrides  ─► final board.json
```
Saturday's rebuild catches Friday-night injury news. The agent's pass is *before* 11:00 and is the last time a model touches anything until the draft ends.

### 4.2 Draft (Sat 11:00) — `core/draft/run.py`
```
preflight: health ✓ · ENABLED=on · board.json fresh (<3h) · browser opens room · our slot read
loop (every ~2s):
  picks  = reader.read()                       # API primary, DOM fallback; same shape
  if new picks:
      board.remove(picks); room.update(picks)
      ranked = picker.rank(board, room, my_roster)          # <100 ms, pure
      queue.sync(ranked[:N])                                # diff only; N from priors
      decisions.log(state, ranked[:5])
  if room.on_the_clock == us:
      actions.draft_player(ranked[0])                       # the fast path
      (queue is already correct → if this fails, ESPN autopicks ranked[0] on timer)
  if reader.complete(): break
postflight: full roster read-back · diff vs decision log · notify(summary) · write history file
```
Time budget per tick: read <1s, rank <0.1s, queue delta typically 1–3 ops <5s. The 60s clock is never close.

### 4.3 Daily (box, cron 07:30 CT) — `scripts/manage.py`
```
health ✓ → ENABLED? → league_state snapshot → valuations(window=week) →
lineup.py diff · waivers.py candidates · trades_out.py candidates · pending offers → gauntlet.py
→ packet.py → agent/run.py --task daily
   agent reads packet, reasons, calls MCP tools: set_lineup / waiver_claim / propose_trade / accept_trade / reject_trade / notify
   every tool → write_gate.check → (allow) browser.actions → receipt → decisions.log → notify
```
Additional cron: Sun 11:00 CT lineup-only pass (late news), and lineup-only after each Sunday window (§4.4 late swap) — `manage.py --task lineup`.

### 4.4 Tuesday — `manage.py --tuesday`
review.py computes efficiency/calibration/league scan → packet → `tuesday.md` → agent narrates, names directional misses, **proposes** prior changes as a diff to priors.yaml → written to `docs/2026-season/YYYY-MM-DD-week-N-review.md` + operating log watch items. Prior changes are applied by Pearce (or by Astra on his word), not by the agent — §7.3.

---

## 5. The MCP surface (= the write table, §8.2)

| Tool | Kind | Gate inside core |
|---|---|---|
| `get_settings`, `get_roster`, `get_matchup`, `get_free_agents`, `get_valuations`, `get_news`, `get_pending_offers`, `get_rate_limits`, `get_decision_log`, `get_gauntlet_result` | read | none |
| `set_lineup(slot_map)` | write | kill switch · health · re-read · lock-time check |
| `add_drop(add, drop)` / `waiver_claim(add, drop, bid)` | write | + §5.4 floor · §5.5 never-drop · bid ≤ ladder max |
| `propose_trade(to_team, give[], get[])` | write | + §6.1 rate limits · §6.2 delta>0 · §6.3 both-sides check |
| `accept_trade(offer_id)` | write | + **§6.8 gauntlet must be 13/13 PASS** · §6.8.9 cool-down · §6.8.10 cap |
| `reject_trade(offer_id)` | write | kill switch only |
| `notify(level, title, body)` | write | none |

**Not exposed, ever:** draft writes (core-internal), counter-offers, league settings, chat, arbitrary browser actions. If the agent asks for something not on this list, the CLI refuses it before core even sees it.

---

## 6. Prompt engineering — how the agent prompts are built

**Principle: the model reasons; it never computes, and it never remembers.** Everything numeric arrives in the packet; every rule arrives in the system prompt; every action leaves as validated JSON.

1. **Doctrine as system prompt, verbatim.** `build_prompt.py` concatenates `system.md` + the full playbook + `priors.yaml` every run. Not a summary — summaries drift, and a drifted rule on a live league is a move you can't undo. Cost is ~15k tokens of cached prefix; fine.
2. **Structured input.** One JSON packet, schema'd, built by `core`. Roster rows already carry `value()` output. News items carry `source`, `published_at`, `url`. Rate-limit state and gates-in-force are in the packet so the model can't claim ignorance.
3. **Structured output, enforced twice.** `--json-schema` at the CLI, then `core` re-validates. Each action: `{tool, args, cites: ["§4.2"], reason, confidence}`. **An action without a `§` citation is rejected.** This makes every decision auditable against the doctrine, and it stops the model inventing rules.
4. **No-action is a first-class answer.** The schema has `no_action_reason`. The system prompt says: *under uncertainty, prefer no action (§8.8); the cost of a missed marginal move is small, the cost of a wrong write is not.*
5. **Tool-only capability.** `--strict-mcp-config --allowedTools "mcp__fantasy__*"`. No Bash, no web, no files. The model literally cannot do anything that isn't on the table in §5.
6. **Two-pass inside one run.** `daily.md` instructs: draft your actions → then re-read §8.8 and §6.8 and list what you're uncertain about → then finalise. `--max-turns` bounds it. Cheap self-critique catches overconfidence.
7. **The gauntlet is narrated, not decided, by the model.** `gauntlet.py` decides. The model writes the §6.8.3 sentence, explains the result in English, and calls `accept_trade` / `reject_trade` — where `accept_trade` re-runs the gauntlet in code anyway. The model can't accept a trade the code rejects.
8. **Few-shot from our own history.** After week 2, the last Tuesday review's calibration notes go into the packet so the model knows where the model has been wrong.
9. **Golden packets.** Every recorded packet + its expected action classes lives in `tests/fixtures/`. Any prompt change re-runs them. A prompt edit that flips a golden is a regression, not an improvement, until someone says otherwise.
10. **Transcripts are kept.** `data/agent-runs/<ts>.json` — the full `claude -p` output for every run. That is the raw material for §7.

---

## 7. Testing

| Layer | What | When |
|---|---|---|
| **Unit** (pytest) | Everything in `core/model`, `draft/picker`, `draft/survival`, `manager/*`, `gates/*` against recorded + synthetic fixtures. Known-answer tests for replacement rank and tier breaks. | Every commit |
| **Draft simulator** | 12-team snake, 11 ADP-noise bots, 200 runs. Roster legality every time; our VOR vs baseline ≥70%. Also runs the *same* picker code the live loop uses — no test-only path. | Friday, and after any picker/prior change |
| **Selector discovery** | `discover_selectors.py` on the laptop, headed, in an ESPN mock room. Fills `browser/selectors.py`. | **Friday first thing** |
| **Mock draft, headless, on the box** — three passes | (1) `--dry-run`: DOM reader tracks a full mock, zero writes. (2) `--no-click`: queue sync only; verify ESPN autopicks our #1 every time the clock runs out. (3) full: queue + click. **Success = 15+ rounds, every pick was our ranked[0], zero ESPN-default picks.** Then a chaos pass: kill the click leg mid-draft; confirm the queue carries it. | **Friday PM** |
| **League practice draft** (Pearce's) | The only place the *API* reader (`mDraftDetail`) can be tested live before Saturday, because mock-lobby rooms have no league ID. If the league practice draft is schedulable Friday, it's worth more than three mock-lobby runs. | Friday, if it exists |
| **Chaos: cookie death** | Fixture returns ESPN's login page → `health.py` fails → `ENABLED` flips off → `notify` fires → every write refused. | Friday |
| **Manager dry-run** | `manage.py --dry-run` Sun/Mon with `ENABLED=off`: prints every action it *would* take, with citations. Read them like a code review. | Sun 09-06 → Wed 09-09 |
| **Gauntlet fixtures** | Six named fleeces + one fair swap. Each fleece must fail on the gate it's designed for. | Every commit |
| **Golden packets** | Recorded daily packets → expected action classes. | Every prompt change |
| **Live, gated** | Week 1 lineup live Thu 09-10 with notify on every write. First waiver run Wed 09-16. First trade proposal week 2. | Rolling |

---

## 8. Build order

**Friday 09-04**
- **AM (P0):** repo scaffold · `config` · `espn/client, settings, players` · `data/nflverse, adp` · `model/*` · `draft/board` · `draft/picker` · `draft/survival, room` · unit tests + simulator green.
- **AM, in parallel on the laptop:** `discover_selectors.py` in a mock room → `selectors.py`.
- **PM (P0):** `browser/session, actions(draft only)` · `draft/reader (DOM)` · `draft/queue` · `draft/run` · `gates/kill_switch` · `state/decisions` · `notify` · **three headless mock passes on the box** · `fantasy-draft.md` runbook.
- **PM (P1):** `draft/reader (API)` against the league practice draft if it exists · `health.py` · cookie-death chaos test.

**Saturday 09-05**
- 09:00 rebuild board (fresh injuries/projections) · agent predraft pass · apply overrides · health ✓ · ENABLED=on.
- 10:45 loop running against the real room, `--no-click` for the first pick if nerves say so, then full. Pearce has the room open on the laptop.
- After: postflight, history file, ENABLED=off.

**Sun 09-06 → Wed 09-09 (P2):** `manager/lineup` · `gates/write_gate, rate_limits` · `browser/actions(set_lineup)` · `mcp_server` · `agent/*` (system, daily, packet, run, schemas) · golden packets · dry-runs daily · cron installed.

**Thu 09-10:** lineup live.

**Thu 09-10 → Tue 09-15 (P3):** `manager/waivers` · `actions(add_drop, waiver_claim)` · `manager/review` · `tuesday.md` · first Tuesday review 09-15 · first waiver run 09-16.

**Week 2 (P4):** `manager/trades_out` · `manager/gauntlet` + fixtures · `actions(propose/accept/reject)` · `incoming_trade.md`.

---

## 9. What Pearce does

1. **Mint the cookies, put them on the box.** Chrome → logged into ESPN → DevTools → Application → Cookies → `https://www.espn.com` → copy `SWID` (keep the braces) and `espn_s2` (long). On the box: `~/Fantasy-Manager/.env` → `ESPN_SWID=…` and `ESPN_S2=…`. **Never paste them in chat, never commit them.**
2. **League ID + team name** — league ID is in the league URL (`leagueId=…`). Team name is enough; `core` resolves the ID.
3. ~~**The practice draft.**~~ **Resolved 09-03:** it's the **league Practice Draft** button beside the draft countdown — an on-demand mock using *our* league's real settings, real 12 teams and real team names, against ESPN bots. Better than the public lobby for DOM testing because the room is laid out exactly like Saturday's. **Assume it does NOT populate `mDraftDetail`** (practice results aren't written to the league's draft record), so it validates the DOM reader, the queue sync and the click leg — not the API reader. It's on-demand and repeatable, which means Friday can run the three passes back to back without waiting for a lobby.
4. **One sudo command on the box** — Playwright's Chromium needs system libs: `sudo $HOME/Fantasy-Manager/.venv/bin/playwright install-deps chromium`. I can't run sudo over the key.
5. ~~**Notification target.**~~ **Resolved 09-03:** Slack **#fantasy** `C0BUTMBSZ0W` in the Lane One workspace. The box posts as **Polaris** using `~/.slack-laneone/token.json`, same as the support agent's alert path. `notify.py` is a thin wrapper over `chat.postMessage`. **Astra on the laptop never posts there** — Polaris is the box identity and lives only on the box.
6. **Repo visibility.** It's **public**. That means your league-mates can read your trade gauntlet, your valuations, and your decision log's *rules* (not the data — that's gitignored). In a $30 money league of friends, I'd make it private. Your call.
7. **Saturday 10:45:** draft room open on the laptop. You don't do anything unless the loop says so.

## 10. What I need from you to start (blocking)

| # | Item | Blocks |
|---|---|---|
| 1 | Cookies in `.env` on the box | everything |
| 2 | League ID + team name | everything |
| 3 | What the practice draft is + when | Friday PM plan |
| 4 | The one `sudo` line run | headless browser on the box |
| 5 | Notify target | `notify.py` |
| 6 | Public/private call | nothing technical — but decide before I push |
| 7 | **Go** to clone to `C:\Users\daysh\Documents\Fantasy-Manager` and push to `main` directly | the scaffold |

## 11. Risks I can't engineer away

- **Headless ESPN.** A bot-detection wall, a "verify you're human," or a layout that only renders with a real session. Mitigation is the three mock passes + the laptop hot spare; there is no code fix if ESPN simply refuses headless.
- **The API reader is untested until Saturday** unless the league practice draft exists. DOM fallback covers it.
- **ESPN queue semantics under autopick.** Support docs say it drafts from the queue; whether it *also* enforces roster-slot limits from the queue (skipping a third QB, say) is unverified. Picker respects limits anyway, so the queue never contains an illegal pick.
- **`[v1 prior]` values are guesses.** The simulator will tell us if any are absurd; only the season tells us if they're good.
