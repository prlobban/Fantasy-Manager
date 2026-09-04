# Benchmark: engine vs ESPN autopick

**The question.** In a 10-team league under Pearce's 2026 rules, does the engine draft a
better roster than ESPN's own autodraft would have from the same seat?

**Why this is a better test than the replay.** The 2024/2025 replay graded the engine
against nine humans who made fixed picks, and its margin turned out to depend on how
those humans were assumed to react when a target was stolen (+227 with one rule, +74 with
another — see `backtest-plan.md` §4). This design has no such knob. The opponents are an
algorithm with a published ranking, they react to a changed board the same way every
time, and the control is *the same algorithm sitting in our seat*.

**Status.** Plan written 2026-09-04. Executes immediately after.

---

## 0. How ESPN autodraft actually works, and how it is copied

Measured, not guessed. Every player in a past-season pull carries
`draftRanksByRankType`, with a **`rank` integer** under both `STANDARD` and `PPR`. All
450 players have both, in both seasons, and the STANDARD order reproduces the
`sortDraftRanks` sort order exactly. This is ESPN's own preseason board — the thing its
autodraft picks from.

ESPN's autodraft, reproduced in `autopick.py`:

1. **Queue first** — a real autodrafting team takes from its own player queue. A bot has
   no queue, so this branch does not exist. That is the pure-autopick behaviour and it is
   what "ESPN picked for me" means for someone who set nothing up.
2. **Then best available by ESPN rank**, subject to two constraints:
   - never exceed a position's roster limit (`position_limits`);
   - once rounds remaining equal the number of unfilled *starting* slots, every pick must
     fill one.

Nothing else. No VOR, no tiers, no scarcity, no bye management — that is the entire point
of the comparison.

> **Which ranking?** The league is half-PPR (0.5), which sits between ESPN's two published
> boards, and they differ enough to matter (McCaffrey 2024: #1 STANDARD, #12 PPR). So the
> benchmark runs **both**, and both are reported. Picking the one that makes autopick look
> worse would be the easiest way to fake this result, so it is not available.

---

## 1. The design

A clean synthetic league rather than the real historical ones:

- **10 teams, snake, 2026 settings** — his actual roster shape and scoring, 13 rounds.
  2024's real league was 6 teams, which is a different game; using 2026's shape for both
  years makes them comparable and makes the answer about *his* league.
- **Player pool, projections and results from 2024 / 2025**, all rescored to 2026 scoring
  by the existing `rescore` module (validated: reproduces ESPN exactly on every offensive
  line, and 150/150 on 2026's own projections).
- **No research, no judge.** Both act on live news that cannot be reconstructed for a past
  season, and Pearce has explicitly cut them for this. The deterministic engine is what is
  being measured.

**The control is the whole trick.** For each year and ranking:

- Run one **all-autopick** draft. Deterministic, so it yields the baseline roster for all
  ten seats at once.
- Then run **ten more drafts**, one per seat, with the engine in that seat and nine
  autopick bots around it.
- Compare **the same seat, engine vs autopick**. That isolates the engine's contribution
  with the seat, the pool, the scoring and the opponents all held fixed.

2 years × 2 rankings × 10 seats = **40 paired comparisons**, plus 4 control drafts.

Autopick is deterministic, so each pair is one clean sample with no simulation noise —
the variation across the 40 comes from seat and season, which is the variation that
matters.

---

## 2. Deliverables

| # | File | What it is |
|---|---|---|
| D1 | `core/backtest/autopick.py` | The ESPN autodraft bot + its ranking source |
| D2 | `core/backtest/arena.py` | 10-team snake draft, any mix of engine and bot seats |
| D3 | `scripts/benchmark.py` | The runner: `--season 2024 2025 --ranking both` |
| D4 | `tests/test_autopick.py` | Bot fidelity, arena invariants, control determinism |
| D5 | `core/draft/run.py` (edit) | Slack pick post trimmed to just our picks |
| D6 | `90-agent-output/fantasy-autopick-benchmark.html` | Results, published |

---

## 3. Steps

**Step 1 — `autopick.py`.** `rank_of(player)` from `draftRanksByRankType`, and
`pick(available, roster, rounds_left, settings, limits)` implementing §0. Pure, no I/O.

**Step 2 — `arena.py`.** Build the 10-team league; snake order; each seat is either
`"engine"` (calls `picker.rank` through the existing board) or `"autopick"`. Returns every
team's roster. Reuses `core.backtest.score` for scoring, unchanged.

**Step 3 — `benchmark.py`.** Control draft, then ten engine drafts, per year per ranking.
Report per seat: engine points, autopick points, delta, and rank among the ten. Headline:
how many of the 40 pairs the engine wins, and by how much.

**Step 4 — the Slack trim (D5).** Pearce wants only "what pick was made for us" in
`#fantasy` — no cost-of-waiting table, no scored top 3, no reasoning. Keep the round
threading (it is what makes the channel readable) and keep the judge's own posts, which
are a separate process and already opt-in.

**Step 5 — report.** Same treatment as the last one.

---

## 4. The stop-and-fix rule

Pearce's instruction, taken literally: **if a major or critical bug surfaces mid-run,
stop, fix it, then re-run and report before/after.** So the runner writes results
incrementally and every anomaly gets checked rather than averaged away. Specifically
watching for:

- an engine roster that is illegal, short, or unfillable;
- the engine losing badly and consistently to autopick at a particular seat, which would
  point at a real defect rather than variance;
- autopick producing an implausible roster, which would mean the bot is wrong and the
  whole comparison is void.

---

## 5. What I need from Pearce

Nothing to start — the data is already cached and no credentials or decisions are
required. Two things to flag rather than block on:

1. **The Slack trim changes tomorrow's live output.** It ships unless he says otherwise.
2. **If the engine loses to autopick**, that is the finding and it gets reported as-is,
   the night before the draft. Worth knowing that is a possible outcome of asking.

---

## 6. Outcome (executed 2026-09-04)

Report: `90-agent-output/fantasy-autopick-benchmark.html`.
Numbers: `data/backtest/benchmark-final.json`.

**Verdict: the engine does not beat ESPN autopick.** 16 of 40 paired seats,
**mean −51.5 points**. It wins 2025 (13/20, +20.7) and loses 2024 (3/20, −123.6).

| Block | Wins | Mean |
|---|---|---|
| 2025 · PPR | 7/10 | +21.1 |
| 2025 · STANDARD | 6/10 | +20.3 |
| 2024 · STANDARD | 2/10 | −49.5 |
| 2024 · PPR | 1/10 | −197.8 |

**Mechanism.** In 2024 the engine drafted the same four players from *every*
seat — McCaffrey, Chase, Mahomes, LaPorta — because ESPN's PPR board ranks
Mahomes 240th and LaPorta 111th, so no bot ever takes them. The two boards
barely overlap, the engine reliably gets its guys, and in 2024 its guys were
wrong (McCaffrey played four games). The engine **concentrates**: it collects a
lot of whichever player it disagrees with consensus about, which pays when it is
right and costs double when it is not.

**Hypotheses tested and killed** — the finding does not rest on the first
plausible story:

- *Not VOR.* A seat drafting raw VOR order does worse (−80) than the full picker
  (−58); replacement-level subtraction is worth **+53**.
- *Not injury history.* Off: −46.0. On: −51.5. Both lose.
- *Not early QB.* Deferring QBs makes it **worse** (−135). With 5 pts a passing
  TD and no points for passing yards, the high QB valuation is correct.
- *Not the scorer or the bot.* Lineups verified slot by slot; both ESPN rankings
  verified to place 4–5 QBs in the top 50.

### Two defects fixed mid-run, per the stop-and-fix rule

1. **The harness had no bye weeks** (ESPN does not serve a past season's bye
   schedule), so the engine's bye logic was inert while the scorer still zeroed a
   player on bye — a handicap against a bot with no bye logic to disable. Byes are
   now derived from the weekly data; all 32 teams detected in both seasons.
   *Before → after on the affected block: 7/10 +21 → 5/10 −4. The fix made the
   engine look worse, which is how you know it was a fix.*
2. **A sign error in `picker.py`, live since it was written.** The bye-collision
   penalty was `score -= w * val.vor`, which on a NEGATIVE vor *adds* — a
   collision was a bonus for a below-replacement player. Unreachable until this
   benchmark supplied real byes. Fixed to `max(val.vor, 0)`; the weight itself
   sweeps inert, so the sign was the whole bug.

### Also shipped

`_pick_post` trimmed to the player, round, VOR and roster counts. The
cost-of-waiting table and scored top 3 still go to `events.jsonl`.

### The honest next test

Rosters here are frozen at the draft. The engine's real claim is season-long
management — waivers, start/sit, trades — and none of it is in this number.
