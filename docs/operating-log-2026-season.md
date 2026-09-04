---
source: Astra — updated every run. This is the STATE file.
status: STATE — current config, IDs, change log, watch items. The file that says what NOT to re-do.
unit: 2026-season
---

# 🏈 Operating Log — 2026 Season

**Read this before any fantasy work.** It is the record of what has already been decided, built and
shipped. Skipping it is how an agent re-proposes work that's already done.

> Rules live in `fantasy-playbook.md`. Structure lives in the README. **This file holds only the
> current position and the change log.**

---

## Config

| Field | Value | Source |
|---|---|---|
| Platform | **ESPN** | Pearce, 2026-09-03 |
| League ID | **1526991210** | Pearce, 2026-09-03 |
| Team name | **big P** | Pearce, 2026-09-03 |
| Team ID (ours) | **8** (`big P`) | resolved from `mTeam` |
| League | **Big Johnson League**, 10 teams | `mSettings` |
| Season | 2026 | — |
| Draft | **Sat 2026-09-05, 11:00 CT · SNAKE · 90s per pick** | `draftSettings` |
| Draft slot | 🔴 **RANDOMISED AT 10:00 CT, one hour before the draft.** Reads as 4 of 10 today; that is **provisional and will change**. Never cache it — `run.preflight` re-reads it and refuses to start before the lock. | Pearce 2026-09-03 · `draftSettings.pickOrder` |
| Rounds | **13** (9 starters + 4 bench; IR excluded) | `rosterSettings` |
| Scoring | **HALF PPR** (0.5/rec), 6pt rush/rec TD, **5pt pass TD**, −2 INT | `scoringSettings` |
| Position caps | QB 2 · RB 4 · WR 6 · TE 3 · K 2 · D/ST 2 | `rosterSettings.positionLimits` |
| Waivers | **WAIVERS_TRADITIONAL — rolling priority, NOT FAAB.** 24h window, processes every day but Tuesday | `acquisitionSettings` |
| Trades | enabled, unlimited, deadline **2026-12-02**, 24h revision window, 5 veto votes | `tradeSettings` |
| Playoffs | 6 of 10 teams, weeks **15–17**, seeding by **TOTAL_POINTS_SCORED** | `scheduleSettings` |
| Buy-in | **$30** | Pearce, 2026-09-03 |
| Everything else | **read at run time, never hardcoded** | `mSettings` — `§3.1` |
| Auth | `SWID` + `espn_s2` cookies | **minted 2026-09-03**, held by Pearce. Destination is `.env` on the box — `§8.6`. Never committed, never in the vault. |
| Notify target | Slack **#fantasy** `C0BUTMBSZ0W` (Lane One workspace), posting as **Polaris** from the box | Pearce, 2026-09-03 |

### Project
| Field | Value |
|---|---|
| Repo | **TBD** — its own GitHub repo, not the vault |
| Layers | `core` (deterministic actions) + `agent` (`claude -p` reasoning) — `§10` |
| Stack | Python: `espn-api`, `nfl_data_py`, `playwright` |
| Host | OptiPlex `jarvis`, headless, cron |
| Draft day | box runs it; **Pearce has the draft room open on his laptop as a hot spare** |

### Write gates in force (`§8.2`)
Lineup **auto** · waivers/FAAB **auto** · outgoing trades **auto (rate-limited)** ·
**accepting incoming trades auto — only on a clean sweep of the `§6.8` gauntlet, notify on fire** ·
countering 🔴 never · league settings / chat 🔴 never.

---

## Change log — newest first

**2026-09-03 — Doctrine drafted, then revised the same day.** Written from
`00-inbox/Fantasy Agent.md` and a three-round scoping interview. Revision in round 3 moved it from
an Astra-run system to a standalone two-layer project in its own repo, put everything on the box,
confirmed snake, and authorised autonomous trade *acceptance* behind the `§6.8` gauntlet. Nothing
built, nothing installed. Reasoning: `2026-season/2026-09-03-system-design.md`.

---

## What NOT to re-propose

- **Don't re-litigate the pick mechanism.** Queue-plus-click was chosen over queue-only and
  click-only, on purpose. `§3.3`.
- **Don't propose building a write path against the ESPN API.** There isn't one. Verified 09-03 —
  `espn-api` is read-only and no supported write endpoint exists. Browser or nothing.
- **Don't propose putting an LLM in the live draft loop.** `§3.2` / `§8.7` / `§10.2`. The 60-second
  clock is the reason the two layers exist at all.
- **Don't propose a global "play it safe" or "swing for upside" setting.** Variance is chosen per
  matchup at `§4.2`. That was a decision, not an omission.
- **Don't propose blanket-vetoing injury-prone players.** It's a discount, not a ban — `§2.5`.
- **Don't propose moving the doctrine into the vault.** It ships with the repo; the vault holds one
  pointer line.
- **Don't propose loosening `§6.8`.** The gauntlet is the condition trade acceptance was authorised
  under. Gates get tighter with evidence, not looser for convenience.
- **Don't propose a counter-offer feature.** Explicitly not authorised — `§6.8.13`.

---

## Watch items

- 🔴 **~48 hours to the draft** (Thu 09-03 → Sat 09-05 11:00 CT). Friday is the only build day, and
  `§3.9` is the floor: if only one thing ships, it's a correctly ordered queue.
- 🔴 **Cookies aren't minted and the repo doesn't exist.** Everything is blocked on these two.
- 🔴 **The queue-write path is unproven against the live ESPN UI, headless.** It is the load-bearing
  piece of the entire draft. **Prove it end-to-end Friday in a mock draft** — not at 11:00 Saturday.
  Headless is strictly harder than the laptop case: ESPN may serve a login wall, a modal, or a
  different layout without a real user agent.
- ⚠️ **`§6.8` has never run.** The first incoming offer of the season is the live test of a
  thirteen-gate rule set written in one sitting. Read its log output carefully.
- ⚠️ **No Claude auth minted for the box** — but the support agent and Daily Doc already run
  unattended `claude -p` there. Reuse that path; **verify rather than assume.**
- ⚠️ **The box's copy of anything is independent of the laptop's.** That drift has bitten before on
  this hardware.
- ⚠️ **Every `[v1 prior]` is unvalidated.** `§7` is the mechanism; it hasn't run.
