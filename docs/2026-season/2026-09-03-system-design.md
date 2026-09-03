---
source: Astra, 2026-09-03. History file — dated, accretes, never edited.
status: HISTORY — the design record. Why the system is shaped the way it is.
---

# 2026-09-03 — Fantasy agent: system design decisions

Written the day the doctrine was drafted, from `00-inbox/Fantasy Agent.md` plus a three-round
scoping interview with Pearce. This is the "why did we do that" record. **Do not edit it later** —
if a decision is reversed after today, write a new dated file.

*(Revised once within the same session, before anything shipped: round 3 changed the project's
shape — standalone repo, two layers, everything on the box, snake confirmed, trade acceptance
authorised. The revision is folded in below rather than filed separately, because none of the
superseded version ever left draft.)*

## The situation

Draft is **Saturday 2026-09-05 at 11:00 CT**. This was written Thursday morning. **Friday is the
only build day.** The inbox spec asked for two full systems — an autonomous drafter and a
season-long manager covering lineups, waivers and trades — and Pearce declined to cut scope when
offered the option to ship the draft first.

## Research findings that shaped the design

**1. ESPN's fantasy API is read-only.** `lm-api-reads.fantasy.espn.com`, via the `espn-api` Python
library, exposes settings, rosters, matchups, draft detail, free agents and ESPN's own projections
behind the `SWID`/`espn_s2` cookies. It exposes **no write path** — not for a pick, a lineup, a
waiver claim or a trade. Every write in this system therefore goes through a logged-in browser.
That one fact drove the runtime split and the pick mechanism.

**2. ESPN's Player Queue is the autonomy backdoor.** Per ESPN's own support docs, on Autopick the
platform drafts *"the highest-ranked available player from your list"* — **checking the live queue
first**, then saved pre-ranks, then ESPN's default list. That converts autonomous drafting from
*"win a race against a 60-second clock"* into *"keep a sorted list current"* — a vastly more robust
problem, and one where failure degrades to "we still got our own #1" instead of "we got ESPN's
default."

**3. Value-based drafting is the right core model**, and replacement level has to be computed from
*this* league's starting requirements. The reason QBs go late isn't that they score less — they
score most. It's that QB12 also scores a lot, so the surplus over replacement is tiny. Same logic
generalises to waivers and trades, which is why there's one model and not four.

**4. Free data covers the gaps.** nflverse / `nfl_data_py` supplies historical weekly stats, snap
and target share, and injury history — the raw material for the durability and consistency terms
that ESPN's own projections don't expose.

## Decisions, and the reasoning

| # | Decision | Reasoning |
|---|---|---|
| 1 | **Platform: ESPN** | Pearce's league. Private → cookie auth, as his note anticipated. |
| 2 | **Scoring/format read at run time, never hardcoded** | Pearce's call, and the right one: *"agent will have to read the rules once in."* A hardcoded scoring assumption is silently wrong all season. Became `§3.1`. |
| 3 | **Both halves by Saturday** | Pearce declined the draft-first option. Recorded as his call with the risk stated. `§3.9` is the mitigation: the queue is the floor. |
| 4 | **Fully autonomous** | Pearce declined advisor-mode. Recommendation had been advise-and-click, to keep browser automation off the deadline path. |
| 5 | **Pick mechanism: queue + click** | The mitigation for #4. Both legs run; the queue is rewritten after *every* pick in the room, so an automation failure degrades to ESPN autopicking our own top choice rather than to a blown pick. `§3.3`. |
| 6 | **Everything runs on the box; laptop is a hot spare on draft day** | Round 2 had split draft-to-laptop / season-to-box. Round 3 reverted to the original spec — one host, one environment, less surface to get wrong. The hot spare keeps the only mitigation that mattered: a human who can see the draft room and grab the wheel. |
| 7 | **Lineup + waivers auto; outgoing trades auto** | Lineup is reversible until kickoff; waivers are time-sensitive, so a gate loses the player. **Outgoing trades were recommended propose-only and Pearce chose auto.** `§6.1` rate limits and `§6.3`'s group-chat test are what make that defensible. |
| 8 | **Accepting incoming trades: authorised, behind a 13-gate gauntlet** | Round 2 left this gated as unauthorised. Round 3 Pearce authorised it — *"make sure there is rules around that though that is extremely tight to prevent getting fleeced."* Hence `§6.8`. The design principle: the incoming case is **structurally adversarial** — the other manager chose the terms *and* the timing, so the default answer is NO and an offer must sweep every gate. The two load-bearing ones are `§6.8.2` (reject if their gain exceeds ours — a fantasy redraft trade is near zero-sum) and `§6.8.3` (if you can't write down why they sent it, the missing logic is never in our favour). |
| 9 | **Posture: maximise wins, regular season and playoffs. $30 buy-in.** | Pearce's words. Fed into `§2.9` and, concretely, `§4.2` — variance preference is decided per matchup, not set globally. Money on the line also means no dead-money owners to farm, and a real reputation cost. |
| 10 | **Injury-proneness is a discount, not a ban** | The spec said *"we do not want an injury prone player."* Read literally that vetoes several of the best players in football. Implemented as an availability multiplier with a short hard-veto list. `§2.5`. |
| 11 | **All draft research precomputed; the live loop is deterministic code** | The 60-second constraint. An LLM round-trip on the clock is a bug, not a slow path. `§3.2` / `§8.7`. |
| 12 | **Two layers: `core` (actions) and `agent` (reasoning)** | Pearce's architecture. It is also the right one, and it falls straight out of #11: the clock forbids an LLM in the live loop, so the deterministic half has to be a real, separable component rather than a helper module. The consequence worth stating: **every gate in `§8.2` is enforced in `core` as code, not in the agent's prompt.** An agent that can construct an arbitrary browser action has no guardrails, only suggestions. `§10`. |
| 13 | **Own repo; doctrine ships with it, vault keeps a pointer** | Pearce's call — this isn't an Astra system. Follows the vault's existing pattern for the Lane One app source: code and its docs live together outside the vault, `home.md` records that they exist and where. One home per fact. |

## What was deliberately left out

- **Auction draft logic.** Confirmed snake, so `§3` is snake-only throughout. `§3.1` asserts
  `draftType == SNAKE` on read rather than trusting this note.
- **Counter-offers.** Accept or reject only. A counter is a new outgoing proposal with a different
  risk profile, and it wasn't authorised. `§6.8.13`.
- **Any invented threshold presented as settled.** Every number in the playbook carries a
  `[v1 prior]` tag. Reasonable starting weights, not validated ones; `§7` is the loop that moves
  them.
- **A local database.** Not earned yet. Files and live reads until something forces the issue.

## The honest risk statement

Two autonomous systems, on unproven **headless** browser automation, against a read-only API, built
in one day, against a hard external deadline. Headless is the sharp edge: ESPN may serve a login
wall, a modal, or a different layout to a browser with no real user session, and none of that shows
up until you try it.

Two things keep a failure survivable:

1. **The queue** (`§3.3`) — if the click leg dies, ESPN autopicks our own #1. There is no path to
   getting ESPN's default list.
2. **The hot spare** — Pearce watching the draft room on his laptop, able to take over.

**If exactly one thing works by 11:00 Saturday, it should be the queue.** Prove it in a mock draft
Friday, headless, end to end.
