---
source: Astra (drafted 2026-09-03)
status: RULES — the doctrine. Judgment calls, numbered so they can be cited.
note: sections are stable IDs. Cite them (§3.5) from the workers, the operating log and reviews.
---

# 🏈 Fantasy Playbook (the rules)

The judgment. Everything the agent would otherwise re-derive every session — and get slightly
differently every time — is written down here once.

> **Numbers marked `[v1 prior]` are starting weights, not truths.** They were chosen to be
> reasonable, not proven. `§7` is the loop that moves them. Anything marked **TBD** is an open
> question — do **not** invent a value for it.

---

## §1 — The loops

**§1.1 Pre-draft (once, before Sat 09-05 11:00 CT).** Read league settings → build the full board:
projections, VOR, tiers, durability, ADP. Everything expensive happens here. → `§3.2`

**§1.2 Draft (live).** After *every* pick by *anyone*: remove the player, recompute survival odds,
re-rank, rewrite the queue. On our turn: pick. → `§3`

**§1.3 Daily (in-season, box, morning).** Roster sweep → injury/news deltas → optimal lineup →
waiver scan → trade scan. Write actions per `§8.2`. Log everything per `§7.1`.

**§1.4 Tuesday (in-season, replaces §1.3's tail).** Grade the week: did we win, what did the bench
score, which calls were right, which priors were wrong. Scan the whole league. Write a dated
history file. → `§7`

**§1.5 The read-first rule.** Every loop starts by reading the operating log. It is the record of
what has already been decided and done. An agent that skips it re-proposes shipped work.

---

## §2 — The core model: one number, used everywhere

The draft, waivers, trades and start/sit are the **same question** — *is player A worth more to
this roster than player B?* — over different windows. One model answers all four. Do not build a
second one.

### §2.1 The window
- **Draft / trades / waiver stashes** → rest-of-season (ROS) total.
- **Start-sit / streamers** → this week only.

### §2.2 Base: projected points, in *this league's* scoring
Start from ESPN's own projection (`kona_player_info`), because that is the number computed against
the exact scoring settings in play. Blend with an independent projection where available. **Never
use a generic PPR ranking off a website** — the league's scoring is read from `mSettings`, never
assumed. (The scoring format was deliberately left unspecified: the agent reads it.)

### §2.3 Adjust for replacement level (VOR)
Value is not points. Value is **points above the worst player you'd have to start instead.**

```
VOR(player) = Availability(player) × [ ProjPoints(player) − ProjPoints(replacement) ]
```

**Availability multiplies the SURPLUS, never the total** *(corrected 2026-09-04)*. Scaling the
whole projection assumes the slot scores **zero** in the weeks a player misses. It does not — you
start a waiver-wire replacement, so the floor is replacement level. Getting this wrong charged
injury-prone players their entire projection for missed weeks and produced values that were absurd
on their face: Jayden Daniels at −103 VOR, Lamar Jackson at −37, both elite quarterbacks.

**The test that catches it, and must stay true:** the player *at* the replacement rank scores
**exactly 0.0**. That is what replacement level means. Under the old formula Mahomes, the
replacement QB, scored −63.5, because the baseline was read off raw projections while every player
was measured in availability-adjusted points — two different scales, differing by 6 points at D/ST
and 36 at QB, which silently re-ranked players *across* positions.

Replacement level is computed from *this* league, not a generic table:

```
replacement_rank(pos) = (n_teams × starters_at_pos)
                      + (n_teams × expected_flex_share(pos))
                      + 1
```

This is what stops the agent taking a QB in round 2 because he "projects highest." The top QBs
project huge and are worth little, because QB12 also projects huge.

⚠️ **Two bugs defeated this rule on exactly the case it was written for** (both found 2026-09-04,
after Pearce asked why a QB went in round 2): the scale mismatch above, and a `§3.7` roster-hole
bonus scaled by **raw projected points** rather than VOR. Quarterbacks accumulate the most raw
points and are worth the least positionally, so any term scaled by raw points hands them the
largest bonus on the board. **Nothing downstream of §2.3 may scale by raw projected points.**

### §2.4 Tiers, not ranks
Cluster by VOR gap: a tier ends where the drop to the next player exceeds `[v1 prior: 1.5×]` the
median gap inside the tier. **Decisions are made on tiers, not on the ordinal list.** The
difference between #14 and #16 is noise; the difference between the last man in a tier and the
first man out is the whole game.

### §2.5 Durability discount — a multiplier, not a ban
Pearce's note says *"we do not want an injury prone player."* Taken literally that rules out a
chunk of the elite, so it is implemented as a **discount on expected games played**, with a short
hard-veto list.

```
Availability = expected_games_played / 17
```

Inputs, from nflverse injury history + current designation:
- games missed per season over the last 3 years, weighted recent-heaviest
- **soft-tissue recurrence** (hamstring / groin / calf) — the strongest repeat signal there is;
  penalise harder than a one-off fracture, which carries almost no recurrence risk
- position + age curve (RB workload past ~27 is the sharpest cliff)
- current designation, and whether the player has practised

**Hard vetoes** (not discounts): on IR with no designated return, suspended past
`[v1 prior: week 6]`, or ruled OUT for the week in question.

**Never** treat a single freak injury as a durability pattern. Two soft-tissue events in two years
is a pattern; one broken collarbone is not.

### §2.6 Consistency — weighted *contextually*, never globally
Weekly variance matters, but **which direction it matters in depends on the matchup**, and that is
decided at `§4.2`, not here. The model carries variance as a stored attribute (stdev of weekly
scores, and a "bust rate" = share of weeks under 50% of projection). It does **not** bake a risk
preference into the base number.

### §2.7 Context multipliers (weekly window only)
- **Opponent defence** vs that specific position, recent-weighted — not season-long rank.
- **Usage trend**: snap share, target share, route participation, red-zone touches — last 3 games
  vs season. Usage predicts far better than past fantasy points do.
- **Game script**: Vegas total and spread. A big favourite runs; a big underdog throws. This flips
  the value of a RB2 and a WR3 in the same game.
- **Offense quality** and pace (plays per game).
- Weather, for outdoor games from `[v1 prior: week 12]` on.

### §2.8 News overrides the model
A depth-chart change, a backfield split announced Friday, or a "did not practise Wednesday" beats
any projection computed before it. **The model is the prior; the news is the update.** If they
disagree and the news is confirmed by two sources, the news wins.

### §2.9 The posture
Optimizing for **wins — regular season and playoffs.** Not for a pretty roster, not for floor for
its own sake. Where a call is genuinely close, take the option that wins more weeks. Where the
season's outcome is already decided (locked into a seed, or eliminated), switch the objective to
playoff weeks 15–17 and say so in the log.

**$30 buy-in.** Money is on it, which does two things: it makes `§6.3`'s reputation cost real
(people remember a fleecing when there's a pot), and it means nobody in this league is a dead-money
owner who'll accept anything. Assume every manager is paying attention.

---

## §3 — Drafting

**Draft: Saturday 2026-09-05, 11:00 CT. It is a SNAKE** (confirmed by Pearce 2026-09-03) — `§3` is
written for a snake throughout. **Scoring, roster size, team count and pick slot are all read from
ESPN (`§3.1`)** — none of them are written down here, deliberately.

**§3.1 Read the league before reasoning about it.** `mSettings` first, every time: scoring, roster
slots, team count, keeper rules, waiver type, FAAB budget, trade deadline. Hardcoding any of these
is the easiest way to lose the draft. Assert `draftType == SNAKE` on read — if that assertion ever
fails, stop and escalate rather than proceeding on rules that don't apply.

**§3.2 All research happens BEFORE 11:00.** The 60-second clock means the live loop must be
**deterministic code, not an LLM call.** Precompute: projections, VOR, tiers, durability,
consistency, ADP, positional survival curves, and a one-line written rationale per player in the
top ~200. On the clock the agent re-sorts a table it already has. It does not think.

**§3.3 The queue is the artifact.** ESPN autopicks from the top of your **live Player Queue** before
falling back to its own list. So:

- Keep the top `[v1 prior: 12]` players in the queue, in current rank order, **at all times.**
- Rewrite it after every single pick in the room — ours or anyone's.
- Then, on our turn, *also* click Draft on the top name.

**Why both:** the click is the fast path; the queue is the net. If a selector changes, the page
hangs, or the laptop hiccups, the timer expires and ESPN drafts **the agent's own #1 anyway.**
There is no state of the world in which we get ESPN's default list. **Never let the queue go stale
to save time — the queue write outranks the click.**

**§3.4 The pick rule.** Take the highest-VOR player in the **tier that is about to break**, subject
to `§3.7`. Not the highest-VOR player overall — the one whose tier won't survive the round trip.

**§3.5 Future sight = the round-trip calculation.** The formal version of "predict what will happen
in future rounds." For each position, estimate what will still be there at our *next* pick:

```
Cost(pos) = BestAvailableNow(pos) − E[ BestAvailable(pos) at our next pick ]
```

Draft the position with the **largest `Cost`** — the one where waiting hurts most. Compute
`E[BestAvailable]` from ADP survival probability across the `k` picks until our next turn, adjusted
by `§3.6`. A player 60% likely to survive is not a player you must take now.

**§3.6 Model the room, not just the board.** Track every other team's filled slots and remaining
needs. Two adjustments:
- A player at a position that three teams ahead of us still need is *less* likely to survive than
  ADP says.
- A run has started when `[v1 prior: 3 of the last 5 picks]` are one position. Runs are contagious:
  either get in front of one, or deliberately let it pass and take the position everyone just
  skipped.

**§3.7 Roster construction constraints** (hard, applied after `§3.4`):
- **ESPN's own position caps are absolute** — read from `rosterSettings.positionLimits`, never
  assumed. *This league: QB 2 · RB 4 · WR 6 · TE 3 · K 2 · D/ST 2.* A pick that would breach a cap
  is illegal, not merely unwise, and must never enter the queue.
- Never take K or D/ST before the final `[v1 prior: 2]` rounds. They are replacement-level by
  definition and streamable all season.
- One QB (two only if the format is superflex — read it, `§3.1`).
- Don't draft two starting-lineup players at the same position who share a bye week.
- **Late rounds are for upside — but bench depth is a hard budget.** *This league has 4 bench
  spots.* With a bench that shallow, a pure lottery ticket competes directly with the bye-week
  cover for a starter. Take the swing only when the roster can absorb the week it costs us;
  in a deep-bench league the balance tips the other way.
- Handcuff *our own* elite RB before handcuffing anyone else's.

**§3.8 Log every pick as it happens** — player taken, the runner-up we passed on, the reason, and
the board state. That log is what `§7` grades. Without it the Tuesday review has nothing to learn
from.

**§3.9 If anything is broken at 10:59, the queue still ships.** A correctly ordered queue with zero
automation is already a competent draft. Get that in place first; the click leg is the upgrade.

---

## §4 — Start / sit

**§4.1 The default is the optimal lineup.** Maximise total expected points across legal slots, using
the weekly window of `§2`. Run it daily; changes are free until kickoff.

**§4.2 Variance is chosen by the matchup — this is the rule that answers "consistency vs risk."**
Compare our projected total to the opponent's:

| Projected margin | Play for | Why |
|---|---|---|
| Favoured by `[v1 prior: >12 pts]` | **Floor** — take the lower-variance player even at −1.5 projected | We win unless something breaks. Remove the ways it breaks. |
| Inside `[v1 prior: ±12]` | Straight expected points | Nothing to game. |
| Underdog by `[v1 prior: >12 pts]` | **Ceiling** — take the boom/bust player even at −2 projected | Losing by 5 and losing by 30 pay the same. Buy the tail. |

**§4.3 Status rules.** Never start OUT or DOUBTFUL. QUESTIONABLE stays in only if he practised
Friday — re-check Sunday morning and late-swap. Any player without a confirmed status 90 minutes
before kickoff is benched if a startable alternative exists.

**§4.4 Late swap.** Re-run the lineup for any un-started player whose game hasn't kicked off, after
each earlier game finishes. Free points, and the single most common thing human managers forget.

**§4.5 Benching a stud requires a written reason** in the operating log. If the model wants to sit a
first-three-rounds pick, that's a signal to check the model before trusting it.

---

## §5 — Waivers & free agents

**§5.1 Read the waiver system first** (`mSettings`), every time. **This league, read 2026-09-03:
`WAIVERS_TRADITIONAL` with `isUsingAcquisitionBudget: False` — ROLLING PRIORITY, not FAAB.** There
is no bidding. 24-hour claim window, claims process every day except Tuesday, waiver order resets.
*If a future league is FAAB, a bid ladder must be written then — do not retrofit bid amounts onto a
priority system.*

**§5.2 The bar to claim.** A pickup must beat `§2` for **the player it replaces**, not the roster
average. Adding a WR5 who never starts is a nothing move that costs a roster spot — and with only
**4 bench spots** in this league, a roster spot is genuinely scarce.

### §5.3 — Rolling priority: the currency is your place in the queue

With no budget, the only cost of a claim is **dropping to the back of the waiver order.** Priority
is a one-shot asset that regenerates slowly, so the question is never *"is this player good?"* but
***"is he worth being last in line for the next one?"***

**§5.3.1 The claim ladder** `[v1 priors]` — claim only if the player clears the bar for the priority
position we currently hold:

| Our priority | Claim only for | Why |
|---|---|---|
| **Top 3** | An immediate every-week starter, or a starting-slot upgrade worth `[v1 prior: ≥3.0]` projected pts/wk | High priority is the most valuable waiver asset we own. Spending it on a streamer is the classic error. |
| **Middle (4–7)** | A starting-slot upgrade worth `[v1 prior: ≥1.5]` pts/wk, or a high-upside stash with a real path to a job | Cheap to spend, slow to regain. |
| **Bottom (8–10)** | Anything that improves the roster at all | We're last anyway; the claim costs nothing we haven't already lost. |

**§5.3.2 Free agents are free.** A player who has cleared waivers costs **no priority**. Add those
any day on the §5.2 bar alone. Never burn a claim on someone who becomes a free agent in 24 hours
unless a rival is visibly chasing him.

**§5.3.3 Never spend top-3 priority on a one-week streamer.** A week-9 D/ST is not worth the
season's best claim. Take the free-agent streamer instead, even if he's slightly worse.

**§5.3.4 Stack claims by value, not by hope.** Claims process in priority order and only the first
success consumes our position. Order them strictly by §2 value so the best available outcome is the
one we actually spend on.

**§5.4 Bench scarcity is the real budget.** Four bench spots and hard position caps (`§3.7`) mean
almost every add forces a drop. **The dropped player's value is part of the cost of the claim** —
compute the net, never the gross.

**§5.5 Never drop:** a top-`[v1 prior: 5]` player on the roster by ROS VOR; an injured player with a
return inside `[v1 prior: 4 weeks]` **if a bench spot exists** (with 4 spots it often won't, and
that is a real decision rather than an automatic hold); or the handcuff to our own RB1.

**§5.6 Daily scan, daily action.** The sweep runs daily per `§1.3`. This league processes waivers
every day but Tuesday, so a claim rarely waits for a weekly run. Free-agent adds fire any day.

---

## §6 — Trades

🔴 **Trades are the only lever here that touches other people and cannot be undone.** Pearce
authorised autonomous execution on 2026-09-03 — **both directions** — with the risk stated. The
constraints below are load-bearing; they are what makes that autonomy defensible.

**Two directions, two different problems.** Outgoing (`§6.1`–`§6.7`): we choose the terms, so the
risk is reputational — a bad offer costs us a trading partner. Incoming (`§6.8`): *they* chose the
terms and the timing, so the risk is being fleeced, and the rules are far tighter.

**§6.1 Rate limits (hard):** max **1 proposal per day**, max **3 per week**, max **1 open offer to
the same manager at a time**, and never re-propose a rejected trade to the same manager inside
`[v1 prior: 14 days]`.

**§6.2 The value test.** Propose only if the trade raises our **projected starting-lineup points
ROS** — not our total roster value. Depth that never starts is worth close to zero. Account for the
hole the trade creates and what fills it.

**§6.3 The fairness test — the group-chat rule.** *If the other manager screenshots this offer in
the league chat, does it read as a fair trade or a fleecing?* In a league of friends, reputation is
real currency: a manager who thinks you're hunting them stops trading with you for the rest of the
season, and that costs more than any single deal wins. **Every proposal must plausibly help both
sides.** Target complementary surpluses, not weakness.

**§6.4 Read the other roster before offering.** Their starting lineup, their holes, their byes,
their record. An offer that ignores what they actually need is noise, and it burns a rate-limit
slot.

**§6.5 Never trade:** a top-`[v1 prior: 3]` asset for a package of lesser parts (consolidation beats
dilution — you can only start so many), or any player whose value is temporarily suppressed by a
short-term injury.

**§6.7 Every proposal gets a log entry** with the math, written before it is sent.

---

### §6.8 — Accepting incoming trades: the anti-fleece gauntlet

Authorised autonomously by Pearce 2026-09-03, **on condition the rules are tight.** They are tight
because the incoming case is structurally different from the outgoing one: **the other manager
chose the terms, chose the timing, and knows something about why.** Every offer that lands is, by
construction, an offer someone else thinks is good for them.

**§6.8.0 The default answer is NO.** Rejecting a good trade costs a little value. Accepting a bad
one costs the season. The payoff is asymmetric, so the decision rule is asymmetric: **an offer is
rejected unless every gate below passes.** An offer that fails one gate is rejected — no
averaging, no "close enough," no overall score.

**§6.8.1 The margin gate — a positive number isn't enough.** Accept only if our projected
**starting-lineup points ROS** rises by at least `[v1 prior: +8]`. A trade that grades as +2 is
inside the model's own error bars, and inside those bars the other manager's read is more likely to
be the correct one than ours — they researched this trade, we're reacting to it.

**§6.8.2 The both-sides gate — the important one.** Run the *identical* valuation from **their**
side of the table. **Reject if their gain exceeds ours.** Not "reject if it's bad for us" — reject
if it's better for them. Fantasy trades are close to zero-sum in a redraft league; a deal where the
sender gains more is the definition of being fleeced, however good it looks in isolation.

**§6.8.3 The "why would they send this?" gate.** State, in one written sentence, the reason this
manager sent this offer. Acceptable reasons: they have a positional surplus, they have a bye-week
hole, they're punting the season, they need a QB. **If no coherent reason can be written down,
reject.** An offer we can't explain is an offer whose logic we haven't found yet — and the missing
logic is never in our favour.

**§6.8.4 The information gate.** An offer arriving within `[v1 prior: 48 hours]` of news touching
any player in it is presumed **information arbitrage** and is rejected — unless we can name that
news ourselves and have already priced it. This is the most common way a sharp manager takes a
player off a dead-money owner: offer before the report gets around.

**§6.8.5 The health gate.** Reject outright if any **incoming** player is: on IR without a
designated return, suspended, carrying an unresolved DOUBTFUL/OUT designation, or coming off an
injury without a confirmed practice. Name-brand-player-who-is-quietly-hurt is the single most
common fleece in fantasy football, and it works because the name still reads as valuable.

**§6.8.6 The consolidation gate.** Never accept the diluting side of a deal. If we send 1 and
receive 2+, the best incoming player must be within `[v1 prior: 10%]` of the outgoing player's ROS
VOR. You can only start so many players — three WR3s do not replace a WR1, no matter what the
total says. (`§6.5` says the same thing from the other direction.)

**§6.8.7 The drop-cost gate.** If accepting forces a roster drop, **the dropped player's value is
part of the price.** Recompute `§6.8.1` net of it. A 2-for-1 that forces us to cut a useful bench
piece is really a 2-for-2.

**§6.8.8 The playoff-schedule gate.** Value incoming players on ROS **including weeks 15–17** and
their remaining bye. A player whose value is concentrated in weeks we may not be playing is worth
less than his ROS total says.

**§6.8.9 The cool-down.** Never accept inside `[v1 prior: 60 minutes]` of receipt. Log the analysis,
re-pull fresh data, re-run the gauntlet, then act. This exists to defeat pressure plays — "accepting
this in the next hour only" is not a real constraint and should read as a red flag, not urgency.

**§6.8.10 Rate limits.** Max `[v1 prior: 1]` accepted trade per week. Never accept two trades from
the same manager inside `[v1 prior: 14 days]` — that's both repeat-fleece protection and collusion
optics in a league of friends.

**§6.8.11 Missing data means reject.** No projection, unconfirmed status, unknown depth-chart
situation, a rookie with no usage history — any missing input fails the gauntlet. **Never estimate
a value in order to clear a gate.**

**§6.8.12 Notify immediately.** Every acceptance posts to Pearce the moment it fires, with the full
gauntlet output — each gate, its number, pass or fail. Not a gate; a record. He should never learn
about a completed trade by opening the app.

**§6.8.13 Counter-offering is not authorised.** Accept or reject. A counter is a new outgoing
proposal and lives under `§6.1`–`§6.5`, including the rate limits.

---

## §7 — The Tuesday loop (how this playbook gets less wrong)

**§7.1 Log the prediction, not just the action.** Every decision — pick, start/sit, claim, trade —
is logged **with the number that justified it at the time.** Without the prediction there's no way
to grade the decision, only the outcome; and in a high-variance game, outcome-grading teaches the
wrong lesson.

**§7.2 Tuesday grades four things:**
1. **Result** — won or lost, and by how much.
2. **Manager efficiency** — actual starting points ÷ the best possible lineup in hindsight. The
   only number that measures *the agent*, separated from luck.
3. **Calibration** — projected vs actual, per player. Is the model biased high on a position, on
   rookies, on players coming off a big week?
4. **The league** — every other roster, their trends, who's buying and who's selling.

**§7.3 Distinguish a bad decision from a bad outcome.** Starting the 14-point projection over the
9-point projection was correct even when it scores 3. **Only move a prior on a repeated,
directional miss** — never on one loud week. A rule that changes every Tuesday is not a rule.

**§7.4 Update in one place.** A confirmed bias updates the `[v1 prior]` here, dated in the change
log. The reasoning goes in the dated history file. Never patch a number inside a worker command and
leave the playbook stale.

**§7.5 Write the dated history file** (`2026-season/YYYY-MM-DD-week-N-review.md`) and update the
operating log's watch items. History files accrete and are never edited.

---

## §8 — Guardrails

**§8.1 Lanes.** The agent owns: the draft, the lineup, waivers/free agents, and — only on a clean
sweep of the `§6.8` gauntlet — accepting an incoming trade. **Pearce owns:** league settings,
anything said to another manager in words, sending an outgoing proposal (the agent surfaces the
idea; the proposal itself is not a write `core` exposes in v1), and the kill switch.
*(Corrected 2026-09-04: an earlier line here gave incoming accepts to Pearce, contradicting the
`§8.2` table and the decision made 2026-09-03 — trades are authorised, gated by `§6.8`.)*

**§8.2 The write lane.** Reads are unlimited and unattended. Writes are exactly these — nothing
else may be written:

| Write | Gate |
|---|---|
| Draft queue + draft pick | **auto** (draft day only) |
| Lineup / start-sit | **auto** — reversible until kickoff |
| Waiver claim / free-agent add-drop, inside `§5` | **auto** |
| Outgoing trade proposal, inside `§6.1`–`§6.7` | **auto**, rate-limited — *not exposed in v1: `get_trade_ideas` surfaces them, Pearce sends* |
| Accepting an incoming trade | **auto** — *only* on a clean sweep of the `§6.8` gauntlet, with immediate notification |
| Countering an incoming trade | 🔴 **NEVER** — a counter is a new outgoing proposal (`§6.8.13`) |
| League settings · chat/messages · anything outside our own team | 🔴 **NEVER** |

**§8.2a Every row above is a named `core` function with its gate enforced in code** (`§10`). A write
the agent can reason its way into is a write that will eventually happen for a bad reason. If it
isn't in this table, `core` doesn't expose it.

**§8.3 Never act on a stale read.** Re-read the roster immediately before any write. Between the
morning sweep and the afternoon claim, a player can be rostered by someone else.

**§8.4 Kill switch.** An `ENABLED` file (`on`/`off`) gates every write — same pattern as the support
agent on the box. Off means read-and-report only. A failing health check flips it off.

**§8.5 Cookie expiry is the #1 silent failure.** `espn_s2` dies without warning, and every read then
returns a plausible-looking 401 or redirect. **Every run starts with a health check** — fetch our
own roster, assert it's ours, assert the week is right. Fail → do nothing, alert, don't guess.

**§8.6 Credentials never enter the vault.** `SWID` / `espn_s2` live in the runtime's env or `.env`,
gitignored — same rule as every other credential here.

**§8.7 On the clock, don't think.** Any live-draft path that requires an LLM round-trip is a bug
(`§3.2`). The LLM's work happens between picks and before the draft.

**§8.8 Say what you don't know.** If a projection is missing, a status unconfirmed, or the model
disagrees with itself, say so in the log. A confident wrong number is worse than a flagged gap.

---

## §9 — Open questions (do NOT invent answers)

*(Resolved 2026-09-03 by interview: snake confirmed · incoming trades authorised under the `§6.8`
gauntlet · $30 buy-in, money league.)*

*(Resolved 2026-09-03 by reading `mSettings` — **Big Johnson League**, 10 teams:)*

1. ~~Keepers or dynasty?~~ **No keepers** (`keeperCount: 0`). The ROS window in `§2.1` is correct.
2. ~~Does playoff seeding reward points-for?~~ **Yes — `playoffSeedingRule: TOTAL_POINTS_SCORED`.**
   Points-for decides seeding, so in an already-decided matchup we still play for maximum points
   rather than coasting. `§4.2`'s floor branch does **not** mean "leave points on the bench."
3. ~~Trade deadline?~~ **2026-12-02.** Unlimited trades, 24-hour revision window, 5 veto votes.
4. ~~Playoff teams and weeks?~~ **6 of 10**, 14-week regular season, 1-week matchups → **weeks
   15/16/17.** `§2.9`'s playoff pivot targets those.
5. **Waivers are rolling priority, not FAAB** — `§5.3` rewritten accordingly.
6. **90 seconds per pick**, not 60. The `§3.2` "don't think on the clock" rule stands regardless;
   the extra 30s is margin, not licence to add a model call.

**Still open:**

7. **Every `[v1 prior]` in this document is unvalidated.** `§7` is the mechanism that validates
   them; none have run yet.
8. **The 24-hour trade revision window** (new, 09-03) means an accepted trade is not instantly
   final. `§6.8` is unchanged — the gauntlet still decides — but a mistake has a recovery path that
   the section was written without knowing about.

---

## §10 — The layer boundary (`core` / `agent`)

The system is two layers, and the split is not stylistic. It exists because **the draft has a hard
clock and the writes are irreversible.**

**§10.1 `core` does; `agent` decides.** `core` is deterministic — no LLM inside it, ever. It holds
the ESPN client, the browser driver, the data pipeline, the valuation engine (`§2`), the queue
manager, the state store and the health check. `agent` is `claude -p`: it reads this doctrine and
`core`'s outputs, and makes the calls that don't reduce to a formula.

**§10.2 Anything on a clock belongs to `core` alone.** The live draft loop (`§1.2`) must complete
without waiting on a model round-trip. If a draft-day path can block on the agent, that is a bug,
not a slow path (`§3.2`, `§8.7`).

**§10.3 The agent can only do what `core` exposes.** Every write in the `§8.2` table is a named
`core` function with its gate — rate limits, the `§6.8` gauntlet, the kill switch — **enforced in
code**. None of it is enforced by the prompt. An agent that can construct an arbitrary browser
action has no guardrails at all, only suggestions.

**§10.4 The valuation engine lives in `core`, not in the agent's head.** One implementation, four
consumers (draft, start/sit, waivers, trades). If the agent is computing value in prose, the model
has forked and the two copies will disagree within a week.

**§10.5 The agent's real job is the things a formula can't do:** reading news against the model
(`§2.8`), writing the "why would they send this?" sentence (`§6.8.3`), framing an outgoing offer
(`§6.3`), the Tuesday calibration (`§7`), and escalating anomalies. Those are judgment. Everything
else is arithmetic, and arithmetic goes in `core`.

**§10.6 `core` fails closed.** A failed health check, an expired cookie, a missing projection, an
unparseable page → do nothing, log, alert. Never a fallback guess. A silent degraded mode on a live
league is worse than an outage, because nobody notices for a week.

---
