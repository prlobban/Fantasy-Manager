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

**2026-09-12 — the first in-season write attempt: five defects in the browser layer, and a
partial write the gate logged as nothing.** Week 1 replaced the preseason team page every selector
was verified against on 09-08. The 07:30 sweep reasoned correctly and lost all three writes in the
browser; the agent was handed only `Error executing tool` and escalated blind.

Found and fixed (`b085235`):
1. **No "Edit Lineup" button exists in-season** — the page is permanently editable, twelve MOVE
   buttons already on it. `_need()` on that button aborted the lineup write before its first move.
2. **`LINEUP_SAVE_BUTTON` matched the OneTrust cookie dialog's hidden Submit** — the one element it
   found on the live page. A "successful save" was a consent click.
3. **The page and the read API disagree on slot names** — API `RB/WR/TE` / `BE`, page `FLEX` /
   `Bench`. The flex move was cancelled as "no destination slot offering HERE".
4. **`set_lineup` matched rows by ESPN id only** — a D/ST has no headshot and a negative id, so the
   Jaguars were skipped silently. The add modal already had the name fallback; the lineup didn't.
5. 🔴 **The gate recorded a partial write as nothing.** ESPN commits an add the instant the roster
   has room: the Jaguars D/ST went on at 07:31, the drop of the Browns then failed, and `add_drop`
   reported total failure. So the roster carried **two defences into game week**, and `record_add`
   never fired — the week read **0 of 3 adds spent** when one was gone.

Also built: **`drop_player`**, which the system never had (an add that commits on its own had no way
to finish the job; flow verified live). Every write tool now returns the failure reason instead of
letting FastMCP flatten it, and `set_lineup` verifies against the **read API**, not a banner.

**Live state corrected:** lineup applied and API-confirmed — Garrett Wilson to the flex, Jaguars
D/ST starting, Browns benched. The add was **back-recorded** to the rate limiter: 1 of 3 spent, 2
left. Health 6/6, 346 tests, ruff clean. **Not pushed** — the commit is local to the box.

⚠️ **The Browns D/ST is still rostered.** The stream's drop leg never ran and dropping is
irreversible, so it was left as Pearce's call. Nothing needs it this week: Jacksonville starts and
Cleveland sits on the bench.

**2026-09-11 — two days of sweeps ran without a brain, and the alert never said so.**
The box's Claude OAuth session expired **2026-09-09** (`~/.claude/.credentials.json`:
`expiresAt: 0`, refresh token dead; `claude auth status` → `loggedIn: false`). From then on every
`claude -p` died in ~45ms with exit 1 and an **empty stderr**, while the CLI's own explanation sat
on stdout inside the JSON envelope: *"Failed to authenticate: OAuth session expired and could not
be refreshed."* Slack therefore reported `claude exited 1:` twenty-eight times at $0.00 and 0 min,
and each sweep fell through to `⚠ no research this morning — deciding on projections alone (D3.1)`.
ESPN health was green throughout and writes stayed live, so `core` kept working; it was the
judgment layer that was gone.

**Three separate defects made an expired login look like a modelling problem:**

1. **The error string was built from `stderr`**, which `--output-format json` leaves empty.
   `_cli_message()` now reads the envelope's `result`, and both the exit-code path and the auth
   path use it.
2. **An auth failure was not a class**, so the `capacity:` halt in `agent/research.py` and
   `scripts/research_week.py` never fired and all 28 players were attempted against a dead
   credential. The prefix is now `auth:` and both passes stop on either.
3. **`§8.5` never checked the second credential.** It checks ESPN's cookie exhaustively *because
   "espn_s2 dies without warning"* — the Claude session has exactly that property and had no check
   at all. `agent.run.auth_status()` asks `claude auth status --json` (free, offline, instant) and
   `scripts/healthcheck.py` reports it. **Deliberately not wired to the kill switch:** signed out,
   `core` is still healthy and can still set a lineup on its own arithmetic, and disarming writes
   would take that away too.

Also: `scripts/cron_manage.sh` puts the failing log lines in the Slack alert instead of
"see data/manager.log on the box". An alert that fires correctly and names nothing is an outage.

**The remaining fix is a human one:** `claude auth login` on the box. Nothing in the repo can
renew it.

**2026-09-08 — the first live sweep wrote nothing, and the gates were not why.**
Tuesday's 07:30 sweep decided three writes — stream the Jaguars D/ST over the Browns, add
Jordan Mason over Travis Kelce, offer Herbert + Pitts to GLOBO GYM for Garrett Wilson — and
**all three failed in the browser layer with `ActionFailed`, not a §8.4 refusal.** The agent
caught it itself, escalated, and did not retry. Four bugs, all in `core/browser/`, all now
fixed and verified against the live page:

1. **The free-agent search filters on ENTER.** `fill()` alone left the entire FA list rendered
   under an autocomplete dropdown, so the row scan for the target found nothing. The draft-room
   code has known this since 09-04; the waiver path never learned it.
2. **The Add control is an icon button with no text** — `title="Add"`, `.add-action-btn`.
   `button:has-text('Add')` matched zero elements.
3. **`_row_for_player` matched the ESPN id in the headshot URL** — which a D/ST does not have.
   The drop was silently skipped and a disabled Continue clicked: a write that would have
   reported success while the roster never changed. Now falls back to the name and **fails
   closed** if the drop row is missing.
4. **The trade page's footer button reads "Continue"**, not "Review Trade". Everything before it
   worked: the Propose Trade page opened, all three players ticked, the offer was assembled.

Also learned, and worth carrying: **Playwright's `has_text=` regex runs in JS, not Python.**
A pattern spanning two cells (`Browns` … `D/ST`, separated by newlines and tabs) matches
nothing and raises no error. Row matching now reads `inner_text()` in Python.

Verified 2026-09-08 by driving the fixed add path to one click short of commit — search →
ENTER → `+` → select the Browns D/ST as the drop → Continue **enabled** → Cancel. Nothing was
committed.

**Then, on Pearce's go: the first real trade of the season went out.** 09:36 CT — Justin
Herbert + Kyle Pitts Sr. to GLOBO GYM PURPLE COBRAS for Garrett Wilson. Sent through the gated
tool, not by hand, so §6.1–§6.5 ran, the rate limit was recorded and the decision logged.
`§6.2 us +51.9 · market ratio 0.91 · their model gain +6.1`. ESPN confirmed it: "Your trade offer
has been confirmed and sent", Pending Moves 1. `propose_trade` now only reports *verified* when
that banner is on the page.

Still unproven: **`set_lineup`** (every pass since the switch has found no change to make) and
**`accept_trade` / `reject_trade`** (no incoming offer yet).

**2026-09-07 09:30 CT — 🔴 THE SWITCH IS ON.** `ENABLED=on` on the box, on Pearce's instruction.
Every write is now live against the real league: lineup, add/drop, outgoing proposals, and accepts
that clear the §6.8 gauntlet. Health was green at the flip. **Still unproven against live ESPN:**
`set_lineup` (selectors verified 2026-09-04, never used to move a real lineup), `propose_trade`,
`accept_trade`, `reject_trade`. The first firing of each is the test. Kill: `echo off > ENABLED`.

**2026-09-05 (night) — Reasoning drives; the math annotates (D9).** Pearce's second brief after
the first read-only sweep: *"mostly reasoning backing the engine rather than straight math."* The
waiver plan now returns every candidate with `flags` instead of hiding the skips; trade ideas carry
a market ratio (`core/model/market.py`, ADP → ROS rank over `trades.market_adp_decay_weeks`) and
their model gain is advisory; `propose_trade` requires `why_they_accept` and refuses below
`trades.min_market_ratio` 0.8; `research_player` lets the sweep research anyone mid-decision
(`research_week.on_demand_max` 6). Slack trimmed to what was done; the why lives in
`data/reasoning/`. New priors: `waivers.candidates_shown` 20, `waivers.upside_shown` 8,
`trades.gettables_per_team` 8, `model.market_blend_by_position` 0 (measured, see below),
`model.streaming_bonus_per_week` all 0.0 (measured worse — the benchmark has no wire).
In-season replacement level at a one-starter position is now the best free agent on the wire
(`core/model/replacement.py`), no prior involved. Draft post-mortem: `docs/draft-post-mortem-2026.md`.

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
- 🔴 **The box's Claude login expires, and when it does the agent layer is simply gone.** It went on
  2026-09-09 and cost two days (change log, 09-11). `scripts/healthcheck.py` now names it in one
  line; the renewal — `claude auth login` on the box — is manual and nothing here can automate it.
  **The support agent and the Daily Doc share that credential**, so the check is not fantasy's
  alone.
⚠️ **The rest of this list is pre-draft and stale** — it was written 2026-09-03 and has not been
  revised since the draft happened on 09-05. Read it as history until someone rewrites it.
- ⚠️ **The box's copy of anything is independent of the laptop's.** That drift has bitten before on
  this hardware.
- ⚠️ **Every `[v1 prior]` is unvalidated.** `§7` is the mechanism; it hasn't run.
