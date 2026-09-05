# Fantasy-Manager — context for any Claude session in this repo

An autonomous agent that drafts and manages an ESPN fantasy football team. Runs
unattended on the OptiPlex box (`jarvis`). **Live league, real money ($30 buy-in),
irreversible writes.**

## Read before you reason

- **`docs/fantasy-playbook.md` is the law.** Numbered §1–§10. Cite sections, don't
  paraphrase them — paraphrase is how a rule quietly changes.
- **`docs/operating-log-2026-season.md` is the state.** Config, change log, and an
  explicit list of settled questions **not to reopen**.
- **`docs/build-plan.md`** is the engineering plan: every file, why it exists.
- **`docs/runbook-draft-day.md`** is draft day, step by step. `/fantasy-draft` loads it.
- `/fantasy` is the primer. It loads, orients, and stops.

## The two layers (§10)

**`core/` does. `agent/` decides. Anything on a clock belongs to `core` alone.**

- `core/` is deterministic. **No LLM call may ever appear inside it.**
- `core/` never imports from `agent/`. The dependency arrow points one way.
- The live draft loop never waits on a model. The agent's draft-day contribution is
  a file it writes *before* the draft starts.
- The agent's only capability is `core`'s MCP tool list, which **is** the write
  table in §8.2. If it isn't on that table, `core` doesn't expose it.

## The writes, and their gates (§8.2)

| Write | Gate |
|---|---|
| Draft queue + pick | auto, draft day only, core-internal |
| Lineup | auto — reversible until kickoff |
| Waiver claim / add-drop | auto, inside §5 |
| Outgoing trade proposal | auto, rate-limited (§6.1) |
| **Accepting an incoming trade** | auto **only** on 13/13 of the §6.8 gauntlet |
| Counter-offer · league settings · chat | **never** |

Every write goes through `core/gates/write_gate.py`. Nothing calls
`core/browser/actions.py` directly.

## Hard rules

1. **Never hardcode a league setting.** Scoring, roster slots, team count, waiver
   type, FAAB budget all come from `mSettings` at run time (§3.1).
2. **Never hardcode a threshold.** Every tunable lives in `priors.yaml` (§7.4).
3. **ESPN's API is read-only.** `espn-api` has no POST methods. Every write is
   Playwright against the web UI. Don't go looking for a write endpoint.
4. **Fail closed** (§10.6). Bad health check, expired cookie, missing projection,
   unparseable page → do nothing, log, alert. Never a fallback guess.
5. **Log the prediction, not just the action** (§7.1). A decision without the number
   that justified it can't be graded.
6. **This repo is public.** No credentials, no league data, no screenshots in git.
   `.gitignore` is deny-by-default; add to it before you add a new output path.

## Layout

```
core/espn/     read client, settings, players, league state, health
core/data/     nflverse (nflreadpy — NOT the archived nfl_data_py)
core/model/    the valuation engine. Pure functions. One model, four consumers (§2)
core/proj/     our own projection model — fitted, measured, OFF (projection_blend 0.0)
core/draft/    board, survival, room, picker, reader, queue, clock, verdict, run
core/backtest/ replay real seasons; the 40-seat arena vs ESPN autopick
core/browser/  Playwright session, selectors, actions
core/manager/  lineup, waivers, trades_out, gauntlet, review (review is not yet wired)
core/gates/    kill switch, rate limits, write_gate
core/state/    store, decisions log, draft log
agent/         prompts, schemas, packet builder, claude -p runner
scripts/       entry points; draft_day.sh and practice.sh are the box's two buttons
docs/          playbook (law) · operating log (state) · runbook-draft-day · the *-plan measurements
```

## Testing

`pytest` runs everything that needs no credentials. `-m live` hits real ESPN,
`-m browser` drives a real browser. The draft simulator exercises the *same*
picker code the live loop uses — there is no test-only path.
