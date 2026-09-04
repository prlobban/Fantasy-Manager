# Building a projection model that beats ESPN

**Goal.** Replace `points = ESPN_projection × availability` with a projection we own,
fit on many seasons of opportunity data, and prove it beats ESPN's number on seasons it
was never fitted on.

**Written** 2026-09-04, after the autopick benchmark reached 31/40 with 75% of the board
coming from ESPN's consensus ranks. That weight is the problem this plan attacks.

---

## 0. The sample audit, done first — and it changes the plan

The instruction was "double or triple the sample." I probed the endpoint before planning
around it. **The ESPN sample cannot be tripled.** Measured:

| Season | League exists | Preseason projections | Draft ranks | Weekly actuals |
|---|---|---|---|---|
| 2021 | ❌ "League does not exist" | — | — | — |
| 2022 | ❌ "League does not exist" | — | — | — |
| 2023 | ✅ (4 teams) | ❌ **rows exist, all 0.0** | ✅ 60/60 | ✅ |
| 2024 | ✅ (6 teams) | ✅ 60/60 | ✅ | ✅ |
| 2025 | ✅ | ✅ | ✅ | ✅ |

2023's `statSourceId=1, statSplitTypeId=0` rows are present but zeroed — McCaffrey,
Lamb, Hill, Chase all return 0.0 against a 2024 pull that returns 429.3 for the same
player. ESPN has purged projection *values* that far back. The league itself begins in
2023.

**So the honest reading: ESPN-projection sample is hard-capped at 2 seasons.** No amount
of work changes that. But the sample that matters can be tripled twice over, in two
different places:

**(a) Training sample → nflverse, 2011–2023. Thirteen seasons, not three.** A projection
model does not need ESPN. It needs opportunity and production, which `nflreadpy` carries
back past 2010, verified: `carries`, `targets`, `receptions`, `target_share`,
`air_yards_share`, `wopr`, `racr`, EPA, plus snap counts from 2012. This is a **7× sample
increase over the current backtest**, and it is the sample the model is actually fitted on.

**(b) Benchmark sample → 2023 joins the arena. 40 paired seats → 60.** 2023 lacks only
ESPN's *projections*. It has draft ranks (which is what the autopick opponents consume)
and weekly actuals (which is what the scorer consumes). The moment we have our own
projection, 2023 stops needing ESPN's — so the season that is useless to the *old*
engine becomes usable by the *new* one. That is a real 50% increase in benchmark seats.

**(c) The head-to-head against ESPN stays at 2024 + 2025**, because those are the only
seasons where ESPN's number exists to lose to. **This is a feature, not a concession:**
fitting on 2011–2023 and testing on 2024–2025 means the comparison is genuinely
out-of-sample. The old backtest fitted and tested on the same two years.

Net: **train 13 seasons, benchmark 3, head-to-head 2, and the head-to-head is now
honestly held out.**

---

## 1. What the model has to beat, and on what metric

**Fix the metric before fitting anything.** Rank correlation across all 450 players is
dominated by calls nobody gets wrong. Three metrics, declared now:

| Metric | Why |
|---|---|
| **M1 — Spearman, top 130 only** | The draftable range. The only place a pick is decided. |
| **M2 — Spearman within position** | Cross-position correlation flatters any model that merely knows QBs outscore TEs. |
| **M3 — the 60-seat arena benchmark** | The one that pays. Everything else is diagnosis. |

**M3 is the acceptance metric. M1/M2 are for diagnosis only.** A model that wins M1 and
loses M3 does not ship — the draft is the product, not the correlation.

**The bar, declared before any number is seen:** the model ships only if it beats the
ESPN-projection baseline on **M3 in all four ESPN blocks** (2024/STANDARD, 2024/PPR,
2025/STANDARD, 2025/PPR) — the same all-blocks gate that rejected twelve candidates
during the last optimisation, including my own first hypothesis. 2023 is reported but
does **not** vote, because it has no ESPN baseline to pair against.

---

## 2. The model

Not a from-scratch points projection. **Opportunity × efficiency, each handled
differently**, because they behave differently year over year:

```
projected_points = E[games] × E[opportunity per game] × E[points per opportunity]
```

- **Opportunity persists.** Targets, carries, routes, snap share are the stable part.
  Project them from prior-year opportunity, age, and depth-chart movement.
- **Efficiency regresses, hard.** Points per target, yards per carry and especially
  **TD rate** revert to the positional mean. A player who scored on 8% of carries
  regresses toward ~4%. Projecting last year's efficiency forward is the single most
  common amateur error and the one this design exists to avoid.
- **Games played is its own model** — that is §5, and it is the part we already
  half-own.

The regression strength (how far each efficiency term is pulled to the mean, and how
much prior-year opportunity carries) is **fitted, not guessed** — that is what 13 seasons
buys.

---

## 3. Deliverables

| # | File | What it is |
|---|---|---|
| **D0** | this doc §0 | Sample audit. **Done.** |
| **D1** | `core/proj/nflstats.py` | nflverse → league-scored player-seasons. Verified against ESPN's own arithmetic before use. |
| **D2** | `core/proj/features.py` | Player-season feature rows and (year N → year N+1) training pairs. Leakage-checked. |
| **D3** | `core/proj/model.py` | The opportunity × regressed-efficiency projection. Pure, no I/O. |
| **D4** | `scripts/fit_projection.py` | Fits on 2011–2023, writes `data/proj-model.json`. |
| **D5** | `scripts/eval_projection.py` | M1/M2 on held-out 2024–2025, ESPN vs ours, per position. |
| **D6** | `core/model/durability.py` (edit) | Availability calibrated against actual games played, not hand-written. |
| **D7** | `core/backtest/` (edit) | 2023 admitted to the arena. 40 → 60 seats. |
| **D8** | `core/draft/board.py` (edit) | `model.projection_source` prior. **Default `espn` — the new model is inert until it clears §1's bar.** |
| **D9** | `tests/test_proj*.py` | Leakage, regression identity, scoring bridge, calibration. |
| **D10** | Artifact + build log | Before/after, honestly, including if it fails. |

---

## 4. Leakage — the thing that will silently ruin this

A projection model trained on data from the season it projects will look magnificent and
be worthless. Three hard rules, enforced in code, not in discipline:

1. **`features.py` may only read seasons strictly before the target season.** Asserted,
   with a test that feeds it a leaked row and expects a raise.
2. **The fit never touches 2024 or 2025.** Enforced by the fitter refusing those years.
3. **Snap/target shares are prior-year.** A player's 2025 target share is not knowable in
   August 2025.

Rule 2 is the one that makes the head-to-head meaningful, and it is the one that is
tempting to break when the number disappoints.

---

## 5. Availability, calibrated

The age cliffs (RB 27, WR 30, TE 31, QB 36), the 3%/year decay and the 0.85 floor in
`durability.py` are hand-written priors that **have never been checked against an
outcome.** nflverse gives actual games played for 13 seasons. Fit the curve, or confirm
the guess and say so. This is the one component we already own, so it is the cheapest
real win on the list.

---

## 6. Stopping conditions

Run until the model beats ESPN on §1's bar, **or** until one of these:

- **The draft is tomorrow at 11:00 CT.** `model.projection_source` defaults to `espn`,
  so a half-finished model cannot reach the live board. **Non-negotiable.**
- **The model wins M1/M2 but loses M3.** Then it does not ship, and the report says the
  projection got better while the draft did not — which is a real and interesting result.
- **Nothing clears the bar.** Then the honest outcome is that ESPN's projection plus
  consensus is hard to beat with 13 seasons of public data, and the report says that
  rather than lowering the bar until something passes.

---

## 7. Outcome (executed 2026-09-04)

### The headline: the model does not beat ESPN, and does not ship

`model.projection_blend` stays **0.0**. The live board is byte-identical to the
one measured yesterday (Gibbs, Bijan, Smith-Njigba, St. Brown, Chase, Allen,
Bowers, Nacua 8th) and tomorrow's draft is unaffected.

**M3, the acceptance metric — mean finish over 40 paired seats:**

| `projection_blend` | finish | beats ESPN | mean pts |
|---|---|---|---|
| **0.0 (shipped)** | **3.38** | **31/40** | **+105.2** |
| 0.1 | 3.73 | 27/40 | +85.3 |
| 0.2 | 4.53 | 27/40 | +40.7 |
| 0.3 | 3.90 | 26/40 | +60.6 |
| 0.4 | 4.15 | 26/40 | +66.4 |
| 0.5 | 5.25 | 19/40 | −1.1 |
| 0.65 | 5.80 | 15/40 | −29.9 |
| 0.8 | 7.53 | 8/40 | −95.0 |

Every positive weight is worse, and it degrades roughly monotonically — the
signature of a genuinely weaker signal, not a tuning miss. w=0.0 reproduces the
previous benchmark exactly (3.38 / 31 / +105.2), which is what confirms the
wiring is truly inert when off rather than accidentally helping.

2024/STANDARD *improves* at low weight (1.60 → 1.30 → 1.00). That is one block
out of four, and the all-blocks gate exists precisely to reject it.

### Diagnosis: the model works, it is just beaten

On held-out 2024/2025, within-position Spearman over ESPN's top 130:

| Ordering | 2024 M1 | 2024 M2 | 2025 M1 | 2025 M2 |
|---|---|---|---|---|
| ESPN projection | **+0.588** | **+0.546** | **+0.586** | +0.334 |
| **our model** | +0.409 | +0.294 | +0.479 | +0.288 |
| prior-season ppg | +0.439 | +0.222 | +0.456 | +0.206 |
| prior-season total | +0.341 | +0.153 | +0.390 | +0.232 |
| prior opportunity/g | +0.454 | +0.096 | +0.474 | +0.184 |

**The model beats every naive baseline on M2 in both years** — the
opportunity/efficiency structure is doing real work — but ESPN beats it. What
ESPN has and we do not is depth charts, coaching changes, holdouts and rookie
scouting. Thirteen seasons of opportunity data does not substitute for knowing
who the starter is in August.

### An unexpected result worth keeping

**The shipped board scores far WORSE on rank correlation than ESPN's raw
projection** — M1 0.419 vs 0.588 (2024), 0.427 vs 0.586 (2025) — while winning
the draft decisively. Adding our model *improves* M1 at every weight and makes
M3 worse.

So M1/M2 are poor proxies for M3, which is exactly why §1 declared M3 the
acceptance metric before any of this ran. **A projection metric would have
shipped this model.**

### Two production bugs found on the way

1. **statId 8 = passing yards per 25, scored at 1.0.** The league DOES score
   passing yards (0.04/yd); the earlier claim that it does not was wrong.
   ESPN's own totals always included it, so the live board was never affected —
   but the QB reasoning built on it was. Found because QB agreement in the
   nflverse bridge sat at 68/621 until the bucket was modelled.
2. **`PlayerContext.age` is never populated anywhere in production.**
   `board.py:95` builds it with injury history only, so `durability._AGE_CLIFF`
   (RB 27, WR 30, TE 31, QB 36) and the 3%/year decay have **never executed on
   a live player**. Wiring age in was tested and **fails the gate** (3.38 →
   3.83), so it stays out — now by measurement rather than by accident.

A third, contained to this work: nflverse serves `birth_date` as a **String**,
so an `isinstance(date)` check silently discarded all 24,800 of them. The first
fit therefore "chose" no age effect because there was no age.

### The durability model does not predict availability

Over 5,326 player-seasons, `availability()` correlates with games actually
played at **+0.09 / −0.05 / −0.05 / −0.01** (QB/RB/TE/WR) — noise. Prior-season
games alone manages +0.66 / +0.35 / +0.49 / +0.39. It also overstates: predicted
0.909 vs actual 0.758 on established starters.

**But removing it also fails the gate** (3.38 → 3.60, 31 → 29 wins), so it
stays. It is not measuring what it claims to measure, and it is still earning
its place — an uncomfortable result reported rather than resolved.

Caveat recorded: among established starters, RB availability is flat from 22 to
28 (0.73–0.76) and only drops at 29, which does not support a cliff at 27. The
positive age-availability correlation among survivors is survivorship bias, so
the data disproves the coded cliff without establishing the right one.

### On the sample

Trained on **2012–2023 (12 seasons, 10,011 player-seasons)** — a 6× increase
over the 2-season backtest, and the head-to-head is now genuinely out of sample,
which the previous optimisation was not.

The nflverse bridge reproduces ESPN's own arithmetic on **99.8% (2024) and 99.6%
(2025)** of weekly lines; the residual is return and recovery TDs, which
nflverse attributes differently and which no projection reaches anyway.

**2023 was NOT added to the arena.** It has draft ranks and actuals but its
preseason projections are zeroed, so `market.blend` has an all-zero ladder to
blend into and the board collapses. Making it work needs the board seeded from
our model — a materially different board from the one under test, which would
confound the comparison rather than strengthen it. Left undone deliberately.

### What would actually move this

Not more parameters. The gap is information ESPN has and we do not:

- **Depth chart and role.** Who is the starter, who lost their competition.
- **Team change**, which is knowable in August and currently unused.
- **Rookies**, 8–11 of every top 130, whom the model cannot see at all —
  `draft_pick` is loaded but not yet used as a feature.
- **A second market** (FantasyPros ECR, Underdog ADP) remains the cheapest
  remaining win, and needs historical snapshots to be backtestable.
