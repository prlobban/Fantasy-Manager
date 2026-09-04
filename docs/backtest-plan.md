# Backtest: run the engine on 2024 and 2025, then optimise against the result

**Goal.** Replay two completed seasons through the real draft engine, score the
resulting rosters on what actually happened, and use the gap to tune the engine —
not to admire it.

**Status.** Plan written 2026-09-04, after probing what ESPN actually serves for a
past season. Every fact in "What the data supports" below was measured, not assumed.

---

## 0. What the data supports (measured 2026-09-04)

| Question | Answer | Evidence |
|---|---|---|
| Does the league exist for 2024/2025? | Yes | `mSettings` returns "Big Johnson League": 2024 = **6 teams, 6 bench, 15 rounds**; 2025 = **10 teams, 4 bench, 13 rounds** (identical shape to 2026) |
| Are preseason projections preserved? | **Yes, and they are genuinely preseason** | Joe Mixon 2025: proj 163.5, actual 0.0. Aiyuk: proj 93.1, actual 0.0. Jayden Daniels: proj 444, actual 138. A refitted projection could not be this wrong |
| Weekly actuals? | Yes — `statSourceId=0, statSplitTypeId=1`, one call per `scoringPeriodId` | Gibbs wk5 2025 = 21.0 |
| Raw stat lines? | Yes — every row carries a `stats` dict of statId to value | Enables rescoring under any scoring map |
| **Historical ADP?** | **NO — unusable.** Every player returns `averageDraftPosition: 170.0` | The `ownership` block is live, not archived. Gibbs at 170 in 2025 is nonsense |
| Scoring drift? | 2024 == 2025. **2026 differs**: passing TD 4 to 5, and the yardage items changed id | `scoring` diff: 2026 only `{24: 0.1, 42: 0.1}`, 2025 only `{23: 0.5, 28: 1.0, 48: 1.0}` |

### The two findings that shape the design

**1. Dead ADP is solved by `mDraftDetail`.** The league's *actual draft results* for
2024 and 2025 are retrievable. That is strictly better than simulated ADP bots: the
other teams make the picks they really made, in the order they really made them. It
also yields a **pseudo-ADP** (a player's real pick number) to feed `survival.py`,
which is otherwise dead in the water without ADP.

**2. Scoring drift forces a choice.** ESPN's `appliedTotal` for 2025 is in *2025*
scoring. Two runs, both reported:

- **Native** — each season under its own settings and scoring. Self-consistent, zero
  reconstruction risk, and the honest "would it have won that league."
- **2026-normalised** — same seasons rescored under 2026 settings. Answers the
  question he actually cares about: how does this engine do in *my* league.

Normalised mode requires a rescorer, and a rescorer that is wrong is worse than no
backtest. So it ships **gated on a reproduction test** (Step 2).

---

## 1. Deliverables

| # | File | What it is |
|---|---|---|
| D1 | `core/backtest/history.py` | Season loader: pool + preseason projections + weekly actuals + real draft results, cached to disk |
| D2 | `core/backtest/rescore.py` | Recompute fantasy points from raw stat lines under an arbitrary scoring map |
| D3 | `core/backtest/replay.py` | The draft replay: engine in our slot, real picks for the other teams, documented fallback when a real pick is gone |
| D4 | `core/backtest/score.py` | Season scoring: week-by-week lineups to points, three lineup policies |
| D5 | `scripts/backtest.py` | The runner. `--season 2025 --mode native\|normalised --slot N` |
| D6 | `tests/test_backtest.py` | Rescorer reproduction test, replay invariants, leakage guards |
| D7 | `data/backtest/<season>/` | Cached raw pulls + per-run results JSON |
| D8 | `90-agent-output/fantasy-backtest-report.html` | The findings, published as an artifact |

---

## 2. Steps

### Step 1 — `history.py`: pull and freeze the seasons (D1)

One module, four pulls per season, all cached to `data/backtest/<season>/`:

- `settings.json` — `mSettings` for that season (`core.espn.settings.load` already
  reads whatever season is configured).
- `pool.json` — `kona_player_info`, ~400 players, carrying `proj_season`
  (src1/split0), position, pro team, and the raw season stat line.
- `weeks/<n>.json` — `kona_player_info` with `scoringPeriodId=n` for n in 1..18,
  carrying each player's actual weekly `appliedTotal` **and** raw `stats`.
- `draft.json` — `mDraftDetail`: every pick, in order, with team id and player id.

**Bye weeks** come from the weekly pull (a rostered player with no stat row that
week whose pro team also has none). **Injury status at draft time is not
recoverable** — it is left `UNKNOWN`, which is the honest state and means
durability §2.5 runs on injury *history* only. A limitation to state in the report,
not to paper over.

Cache is keyed by season and never expires: a completed season does not change.

### Step 2 — `rescore.py`, gated on reproduction (D2)

`points(stats: dict[int, float], scoring: dict[int, float]) -> float`.

**The gate:** recompute every 2025 weekly line under *2025* scoring and compare to
ESPN's own `appliedTotal`.

- Agreement within **0.01 on at least 99% of lines** → the rescorer is trusted, and
  normalised mode ships.
- Anything less → normalised mode is **disabled**, `--mode normalised` errors out,
  and the report says only native numbers exist, and why.

This is the discipline of the last build session applied up front: the
plausible-looking number nobody checked is what costs you.

### Step 3 — `replay.py`: the draft (D3)

For a season, a slot, and a settings object:

1. Build the board with `core.draft.board` on the historical pool, with
   `espn_adp` = the player's real pick number from `draft.json` (pseudo-ADP) and
   `adp_stdev` = the existing default. This is what makes `survival.py` and the
   cost-of-waiting model function at all.
2. Walk the real pick order. For every pick:
   - **our slot** → `picker.rank(...)`, take `plan.best`;
   - **any other team** → the player they actually took, if still available;
     otherwise **the fallback**: that team's next real pick's player, else the
     highest preseason projection legal under their roster caps. Every fallback is
     logged and counted — a replay with a high fallback rate is a weaker result and
     the report says so.
3. Emit the roster plus the full `reasons` dict for each of our picks, so a bad
   pick can be traced to the term that caused it.

**No dossiers, no judge.** Those act on live news about a moment that has passed;
running them on 2025 with 2026 knowledge is pure hindsight. The backtest measures
the **deterministic engine**, which is the part that can be honestly measured — and
the report says exactly that.

**Slot sensitivity:** the runner sweeps every slot (`--slot all`), because one draft
position is one sample and the engine's behaviour genuinely differs at pick 1 vs
pick 10.

### Step 4 — `score.py`: what the roster was actually worth (D4)

Score weeks 1..(regular season) under the season's settings, three ways:

| Policy | What it measures |
|---|---|
| **Hindsight optimal** | Best legal lineup each week knowing the results. The ceiling the roster contained — this grades the **draft** alone |
| **Engine lineup** | `core/manager/lineup.py` choosing on projections only. Grades draft + start/sit |
| **Naive** | Highest preseason season-projection at each slot, set once. The baseline any human beats by accident |

Plus: head-to-head record against the other replayed rosters on the real schedule,
and points-per-week stdev — the consistency question from earlier today, finally
measurable.

**Comparison set.** Our replayed roster is scored against **all other teams in the
same replay**, so the engine is graded relative to the field it actually drafted
against, not against an absolute points number that means nothing across seasons.

### Step 5 — `scripts/backtest.py` (D5) and the tests (D6)

Runner flags: `--season`, `--mode`, `--slot N|all`, `--policy`, `--json out.json`.

Tests that must exist:

- rescorer reproduces ESPN (the Step 2 gate, as a test);
- **leakage guard**: the board built for a replay contains no field derived from
  `actual_week` — asserted by construction, because this is the one bug that makes
  a backtest lie in the flattering direction;
- replay invariants: no player drafted twice, every roster legal under position
  limits, fallback rate reported;
- a fixture-based end-to-end on a tiny synthetic season, so the suite stays offline
  and fast.

### Step 6 — Optimise (the actual point)

Only after the harness is trusted. The tunables are the coefficients in
[`picker.py`](../core/draft/picker.py) — scarcity 0.35, tier-break 0.12, room-demand
0.10/0.02, run-join 0.06, `bench_cost`, `stack_penalty` — plus the flex shares in
`replacement.py` and the durability curve in `durability.py`.

Method, in order:

1. **Baseline** — every slot × both seasons at the current coefficients. This
   number is the thing to beat, and it is written down before anything is tuned.
2. **One-at-a-time sensitivity** — sweep each coefficient alone against
   hindsight-optimal points. A coefficient that does nothing across its whole range
   is dead weight and gets deleted, not tuned.
3. **Change only what the evidence supports.** Two seasons × 10 slots is ~26 drafts
   of signal: enough to find a coefficient that is badly wrong, nowhere near enough
   to justify fine-tuning. Anything inside the noise band stays where it is.
4. **Guard against overfitting:** tune on 2024, verify on 2025, report both. A
   change that only helps the season it was tuned on is rejected.

**Every accepted change lands as a diff plus the before/after number in the report.**

---

## 3. What this cannot tell us

Stated up front so the report cannot overclaim:

- **The research and judge layers are not measured.** They act on live news; there
  is no honest way to replay them.
- **Opponents do not react.** They make their real picks; they do not adapt to ours.
  The fallback rate quantifies how far the replay drifted from reality.
- **Injury status at draft time is missing**, so §2.5 sees history but not "he is
  QUESTIONABLE today."
- **Two seasons is a small sample.** 2024 is a *6-team* league — a genuinely
  different game with far shallower replacement level. It is reported separately and
  never averaged with 2025.

---

## 4. Outcome (executed 2026-09-04)

All eight deliverables shipped. Findings in
`90-agent-output/fantasy-backtest-report.html`; raw numbers in
`data/backtest/baseline-normalised.json` and `sweep-2026rules.json`.

**Result.** Under 2026 scoring the engine beat the human who really held the seat in
**15 of 16 seats** — 2025 mean finish 1.80 of 10 (+227 pts avg, top-3 in 10/10), 2024
1.83 of 6 (+151). It captures 92% of its own roster's ceiling setting lineups on
projections alone.

**Tuning: no change is justified.** Six of eight coefficients are INERT across their
full range; `scarcity_weight` was REJECTED (its best value wins combined but loses a
season); `stack_penalty` is already at the grid's best. Reason, measured: at the
extremes these weights change 3-5 of 13 picks but mostly REORDER the same players
across rounds — roster overlap with the baseline stays at 93-96%. The edge comes from
VOR against a league-specific replacement level and the hard legality rules, not from
the soft weights.

**A hypothesis that failed, kept on the record.** 12/16 seats draft a QB2 and 15/16 a
TE2 in a one-QB, one-TE league. Pushing the single-start surplus discount from 0.15 to
0.05 to 0.0 moved roster shape from 5.2 to 5.0 QB+TE picks and points within noise. The
habit is not costing points; it stays.

**Deviations from the plan above.**

- Pool ordering is `sortDraftRanks`, not ownership — ownership is an END-of-season
  figure, so pool membership itself would carry hindsight. It also covers the room
  better: all 220 real picks land inside the top 450.
- Historical seasons are served from ESPN's `leagueHistory` endpoint, where
  `filterStatus` matches nothing (2024 returned 0 players with it, 450 without).
- Head-to-head is ALL-PLAY, not the real schedule: 13 games against 9 opponents is
  mostly opponent luck, and the engine is being graded on the roster it built.
- Normalised mode borrows 2026's SCORING only, never its structure — and rescores
  projections as well as actuals. Both were bugs first (see the report).

**Still open.** Waiver-wire simulation. Every roster here is frozen at the draft, which
understates every human who actually managed their team, and it is the largest single
caveat on the margin.
