# Optimising the draft engine against the autopick benchmark

**Goal.** Get the engine from a mean finish of 6.5 of 10 to consistently top-2, and
build the loop that keeps it there rather than a one-off tune.

**Status.** Written 2026-09-04 after the autopick benchmark returned 16/40 and −51.5.

---

## 0. What the evidence already says

Two measurements point the same way and they are the reason this plan is not a
blind search.

**The board is the problem, not the picker.** From the benchmark's decomposition:

| Seat drafts on | vs ESPN autopick |
|---|---|
| Our raw projections | **−7.0** |
| Full engine (VOR + all roster logic) | −57.6 |
| Raw VOR order, no roster logic | −80.0 |

The picker's roster logic is worth **+22** on top of VOR. But VOR itself is worth
**−50 against simply drafting the highest projected player available.**

**Rank correlation with actual season points confirms it:**

| Ordering | 2024 | 2025 |
|---|---|---|
| Our raw projection | **+0.593** | **+0.484** |
| ESPN PPR rank | +0.503 | +0.382 |
| ESPN STANDARD rank | +0.375 | +0.378 |
| Our VOR order | +0.264 | +0.359 |

Some of that gap is structural — VOR is position-relative, so a cross-position
correlation flatters raw points. But the roster-level result is not structural, and it
says the same thing.

**So the hypothesis to test first is that VOR is over-applied in this league**, not
that it is wrong in principle. This league's scoring is unusual (5 points a passing TD,
**no points for passing yards**), which compresses the top of the QB board in a way
generic reasoning about replacement level does not anticipate.

---

## 1. The knob that follows from that

`points = vor + replacement(pos)`. So a single parameter interpolates the whole way
between the two boards without inventing a new scale:

```
base = vor + (1 - vor_weight) x replacement(pos)
```

- `vor_weight = 1.0` → `base = vor` — exactly today's engine.
- `vor_weight = 0.0` → `base = points` — raw projection order.

Default stays 1.0, so the change is inert until the sweep says otherwise. Every
VOR-proportional bonus in `picker.py` scales off `base` instead, so the whole score
moves together rather than mixing two scales.

A second knob, tested after: **`market_blend`**, mixing ESPN's consensus draft rank
into our projection. Consensus encodes role and camp information a projection model
misses, and the two signals correlate differently with outcome — blending decent
uncorrelated signals usually beats either. Only worth adding if the first knob does not
already close the gap.

---

## 2. Deliverables

| # | File | What it is |
|---|---|---|
| D1 | `core/draft/picker.py` (edit) | `base` interpolation, driven by `draft.vor_weight` |
| D2 | `priors.yaml` (edit) | `vor_weight`, default 1.0 |
| D3 | `scripts/optimize.py` | The search loop over the 40-pair benchmark |
| D4 | `tests/test_optimize.py` | The interpolation identity, and the guard below |
| D5 | Updated benchmark artifact | Before/after, honestly |

---

## 3. The loop

`scripts/optimize.py`:

1. Load both seasons once; build one board per season per config.
2. Evaluate a config = all 40 paired seats → `(wins, mean delta, mean rank)`.
3. Search one parameter at a time (coordinate descent), the same discipline as
   `backtest_tune.py`: 2 seasons is not enough to fit many parameters jointly.
4. **Accept a change only if it improves all four blocks** — 2024/STANDARD,
   2024/PPR, 2025/STANDARD, 2025/PPR. A change that wins on the average by winning
   one block and losing another is a fit to one season, and is rejected.

**The objective is mean finish**, not mean points. Points are dominated by how good a
season the field had; finish is what "top 2" actually means.

### The overfitting problem, stated honestly

Two seasons is the entire sample. A search that tries enough configurations *will*
find one that looks good on both by luck. Three guards:

- **A small, pre-declared grid.** No free-form search, no re-running until it looks good.
- **All-four-blocks improvement**, which is a much harder bar than a mean.
- **Every accepted change must have a mechanism** — a reason it should work, written
  down before the number is quoted. A parameter that helps for no articulable reason is
  noise that happens to point the right way.

If nothing clears those bars, the honest outcome is that the engine ships as it is and
the report says so.

---

## 4. What would make this genuinely solid, and is not available tonight

More seasons. 2022 and 2023 are reachable through the same `leagueHistory` endpoint if
the league existed then; that would take the sample from 2 to 4 and make the guards mean
considerably more. Worth doing before the *season*, not before tomorrow's draft.

---

## 5. Outcome (executed 2026-09-04)

**16/40 and −51.5 → 31/40 and +105.2.** Mean finish 6.45 → **3.38 of 10**; top-3
finishes 4 → 25; outright firsts 0 → 7. Every one of the four blocks improved.

| Block | Wins before | after | Mean before | after |
|---|---|---|---|---|
| 2024 · STANDARD | 2/10 | **10/10** | −49.5 | +216.4 |
| 2024 · PPR | 1/10 | **7/10** | −197.8 | +46.9 |
| 2025 · STANDARD | 5/10 | **8/10** | −2.1 | +121.8 |
| 2025 · PPR | 5/10 | **6/10** | −4.1 | +35.8 |

### Two changes accepted

1. **§2.2b consensus blend** (`core/model/market.py`, `model.market_blend: 0.75`,
   `model.market_rank_type: PPR`). Did nearly all the work. Not in the original
   plan's first knob — `vor_weight` was — and `vor_weight` was ultimately
   REJECTED, which is worth recording: the pre-declared first hypothesis was
   wrong and the pre-declared second one was right.
2. **`bench_opportunity_cost` 25 → 0.** The only grid parameter to improve all
   four blocks, and it did so against two different baselines (pre- and
   post-blend). An absolute points charge on a VOR-scaled score dominates the
   late rounds; position limits already prevent the stacking it targeted.

### Rejected, and why that matters

`vor_weight = 0.65` improved the aggregate (6.80 → 6.42) by helping 2024 and
hurting 2025 — the signature of a one-season fit. `stack_penalty = 45` did the
same post-blend (3.65 → 3.17). Both failed the all-four-blocks rule and neither
ships. The rule was fixed before any number was seen.

### A production bug the optimisation surfaced

The benchmark was **not reproducible** — identical code scored 6.45 and 7.00 in
two processes. Cause: `nflverse.injury_history_by_gsis` picked a player's primary
injury with `max(set(descs), key=descs.count)`, and a `set` of strings iterates
in `PYTHONHASHSEED` order, so tied descriptions resolved differently per process.
That flips soft-tissue vs clean-acute classification → changes the durability
discount → changes the board. **Live in production, not just the benchmark.**
Ties now break alphabetically; three fresh processes now agree exactly.

### Still open

- Mean finish 3.38 is top-3, **not** consistent top-2.
- Two seasons remains the whole sample. **2022 and 2023 are reachable from the
  same `leagueHistory` endpoint** — the single best next investment, and a
  before-the-season job.
- Three quarters of the board is now consensus. Improving our own projection
  model is the open problem that would let that weight come down.
