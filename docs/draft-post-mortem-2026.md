---
source: Astra, 2026-09-05 (night). Written from data/board.json, data/draft-live.log and the autopick benchmark.
status: POST-MORTEM — what the draft math got wrong, what was measured, what is proposed.
---

# The 2026 draft — post-mortem

**The result.** 13/13 picks landed from slot 9, zero errors, judge live (58 verdicts, 0 vetoes).
ESPN graded the roster **10th of 10**. Pearce: *"that same math killed the draft."*

**The roster:** Jefferson · Josh Allen · Colston Loveland · Etienne · McConkey · Swift · Burden ·
Pitts · Herbert · Hubbard · Kelce · Mevis · Browns D/ST. Two QBs, three TEs, three RBs, three WRs.

## What happened, pick by pick

| Pick | Player | VOR on our board | ADP | What the market thought |
|---|---|---|---|---|
| 9 | Jefferson WR | 104.7 | 12.6 | fine |
| 12 | **Josh Allen QB** | 113.4 (#6 overall) | 19.1 | a round early in a 1-QB league |
| 29 | **Colston Loveland TE** | 85.3 (#20 overall, "last 1 in tier 1") | 42.3 | a rookie TE, pick 42 |
| 32 | Etienne RB | 67.3 | 40.4 | fine |
| 49 | McConkey WR | 39.2 | 45.8 | fine |
| 52 | Swift RB | 23.3 (availability 0.73) | 51.8 | fine |
| 69 | Burden WR | 0.0 | 74.8 | flat board; fine |
| 72 | **Pitts TE** | 35.1 → −20 after surplus + stack | 68.7 | a second TE; dead roster spot |
| 89 | **Herbert QB** | 6.2 | 85.4 | a second QB; dead roster spot |
| 92 | Hubbard RB | −31.0 | 108.6 | fine for round 10 |
| 109 | **Kelce TE** | 6.5 → −46.7 after penalties | 94.5 | a third TE |
| 112 | Mevis K | 38.5 | 134 | fine |
| 129 | Browns D/ST | 2.2 | — | fine |

Four picks are the grade: **Allen at 12, Loveland at 29, and the three dead roster spots (Pitts,
Herbert, Kelce)**. The other nine are within a round of market.

## The three causes, from the board itself

### 1. Season-total VOR at one-starter positions ignores the wire

The replacement baseline is "the 11th-best QB's season total" (rank = teams × starters + 1).
That is one player's average. Nobody starts that player every week: at QB, TE, K and D/ST a
manager streams the best *matchup* off the wire, and the weekly max over a handful of free agents
beats any one of their averages. Season-total VOR therefore overstates every elite QB and TE, and
the board leads with **Josh Allen at #6, Bowers and McBride at #7–8, Brandon Aubrey (K) at #26**.
Only the §3.7 "no K/D/ST until the last two rounds" rule stopped a round-3 kicker.

Loveland is this effect plus a rookie's projection treated as certain: 223 projected against a
TE11 at 142 is 81 points of "value" that a market pricing rookie variance did not believe (ADP 42).

### 2. The dead rounds: every remaining starter has negative VOR, so "less negative" wins

By pick 72 the RB and WR replacement ranks (~25) are past; every remaining RB/WR is *below*
replacement. A negative VOR is never lifted by the surplus multiplier (correctly — a backup is worth
his negative VOR at best), so a bench RB at −25 stays −25. A second TE at +35 becomes
35 × 0.15 − 25 = **−20** and wins. Same mechanism for Kelce in round 11: every alternative graded
lower. The stack penalty (25 points) was tuned on the benchmark to fight exactly this and lost.

The root: **bench value is not VOR.** A bench body is worth the *expected starts* he gives us —
byes, injuries, the flex — times the gap to what the wire would give instead. At a one-starter
position that is one bye week. At RB it is several weeks. VOR says nothing about either.

### 3. The market blend maps rank onto a cross-position ladder

`market.blend` gave a player ESPN ranks 42nd "the 42nd-best projection on our board", whatever his
position. Near the top that ladder is QBs and RBs, so a TE ranked 42nd inherits a running back's
42nd-place points before a tight end's baseline is subtracted. Mechanism is wrong on its face.

## What was measured (autopick benchmark, 40 paired seats, `scripts/optimize.py`)

The benchmark drafts against nine ESPN autopick bots and scores the roster with the **hindsight
lineup each week — no waiver wire, no streaming**. Baseline: mean finish **3.38 / 10**, beats ESPN
31/40.

| Change | Mean finish | Blocks better / worse | Verdict |
|---|---|---|---|
| Per-position market ladder (`model.market_blend_by_position=1`) | **3.12** | 3 / 1 (2024-PPR 3.80 → 5.20) | Fails the all-four-blocks gate. Kept at 0. Right mechanism, insufficient sample. |
| Streaming bonus at one-starter positions, 1.5 / 2.5 / 3.5 pts/wk | 3.85 / 4.30 / 4.60 | monotonically worse | Kept at 0. **The benchmark cannot see streaming** — it never touches the wire — so it rewards the early QB/TE it should punish. |

The honest reading: **the benchmark and the ESPN grade disagree because they measure different
games.** The benchmark's game has no wire, so a QB in round 2 is genuinely worth it there. The real
league has a wire, and ESPN's grade (market-based) knows it. Tuning the picker further against this
benchmark would optimise for the wrong game.

## What changed in code tonight (in-season, where it matters now)

- **Replacement level at a one-starter position, in-season, is the best free agent on the wire**
  (`core/model/replacement.py`). Exact, no prior: once the league has rostered its starters at QB /
  TE / K / D/ST, that is who you would actually start instead. It moves every ROS VOR at those
  positions, which is what values Pitts, Herbert and Kelce honestly as trade chips and drops.
- **Market value for trades** (`core/model/market.py`): a trade-value curve from ADP decaying into
  the ROS rank. This is what "would they accept" is measured against; see D9.3.

## Proposed for the draft engine (next off-season, not tonight)

1. **A season simulator with a wire.** Each week the sim team may swap its worst starter at a
   streamable position for the best free agent at that position. Only then can the benchmark see
   the streaming premium and the cost of dead roster spots, and only then does tuning against it
   optimise the real game. Until this exists, every draft prior is tuned for a league that does not
   stream.
2. **Expected-starts bench valuation** replacing the surplus ladder, stack penalty, bench cost and
   hole weight — four hacks for one missing quantity. Value of a candidate to *this* roster =
   expected weeks he starts (byes + injury draws + flex) × (his points − wire replacement). It is
   the same question waivers already answer weekly (`weekly_gain_for`) and trades answer ROS, which
   is §10.4's "one model, four consumers" done properly.
3. **Rookie / low-history variance in the projection**: shrink a projection toward the positional
   mean by its uncertainty before VOR. Loveland at 223 was a point estimate the market discounted.
4. Re-measure the per-position ladder with a third season in the sample.

## The one-line lesson

Season-total VOR is a fine way to rank starters and a bad way to run a 13-round draft, because a
draft is thirteen roster decisions and VOR only knows about the first eight.
