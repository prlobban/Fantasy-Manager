---
description: 'Fantasy primer — load the map + playbook + operating log + data sources so the agent
is grounded before any fantasy football work, instead of reasoning from scratch about scoring,
value, the layer boundary, or what has already been built.'
---

Orient before a session that touches the fantasy agent. **This is a primer, not a worker — it loads
context and stops.** It never drafts, never sets a lineup, never touches a write.

**Why this exists:** the value model, the pick mechanism, the layer boundary and the trade gates
were all decided once, with reasoning, on 2026-09-03. An agent that starts cold re-derives them
slightly differently every session — a different replacement level, a different injury policy, a
different opinion on whether it may accept a trade. This system writes to a live league with money
in it. A drifted rule doesn't produce a statement to correct; it produces a move you can't undo.

## Args
- **bare** — overview: README + playbook. Default.
- **`season`** *(e.g. `2026-season`)* — also read that season's operating log: config, IDs, and
  what NOT to re-propose.
- **`live`** — also pull current state off ESPN (roster, matchup, free agents, draft state).

## The grounding rule

**Read the system before you reason about it.**

Three things are already written down, and none should be reconstructed from memory:

1. **The rules are in the playbook**, numbered. Cite them (`§3.3`, `§6.8.2`) rather than restating
   them in your own words — paraphrase is how a rule quietly changes.
2. **The live numbers are in ESPN, not in any doc.** Scoring, roster slots, team count, waiver type,
   projections, who's rostered. The doctrine deliberately holds none of these. Read them.
3. **What's already been decided and built is in the operating log** — including an explicit list of
   settled questions not to reopen.

## Steps

1. **Read the map and the rules. Always.**
   - `README.md` — components, the two layers, sources, runtime
   - `docs/fantasy-playbook.md` — the numbered rules
2. **State the layer boundary before anything else** (`§10`). `core` does, `agent` decides, and
   anything on a clock belongs to `core` alone. Every write is a named `core` function with its gate
   enforced in code — **not** something to be reasoned into.
3. **Name the data sources and what each answers** — don't restate values from memory:
   - **ESPN read API** (`espn-api`, `SWID`+`espn_s2`) — league settings & scoring, rosters,
     matchups, draft state, free agents, ESPN's own projections. Authoritative for anything
     league-specific.
   - **ESPN web UI** (Playwright) — the *only* write path. The API writes nothing.
   - **nflverse / `nfl_data_py`** — historical stats, usage, injury history.
   - **News / WebSearch** — depth charts, practice reports, game statuses. Beats the model (`§2.8`).
   - **ADP** — what the room thinks; the input to `§3.5`. **Vegas** — game script (`§2.7`).
4. **Restate the guardrails, short:** lanes (`§8.1`), the write table (`§8.2`), the `§6.8` gauntlet
   for incoming trades, the kill switch (`§8.4`), and the health check that must pass before any
   write (`§8.5`).
5. **If a season arg:** read `docs/operating-log-<season>.md` — config, change log, watch items, and
   the "what NOT to re-propose" list.
6. **If `live`:** pull current ESPN state. If auth fails or the tool is down, **say so plainly and
   continue on docs** — never infer a roster. `core` fails closed (`§10.6`); so does this.
7. **Print a tight orientation and STOP:**

   > **Oriented on fantasy** ({scope}).
   > **Model:** {2–3 lines — VOR against this league's replacement level, tiers not ranks,
   > durability as a discount, variance chosen per matchup}
   > **Mechanism:** {queue + click; ESPN API is read-only, the browser is the only write path;
   > `core` deterministic / `agent` reasoning}
   > **Pointers loaded:** {README OK, playbook OK, sources named}
   > **State:** {last change, open watch items — if a season was given}
   > **Gates:** {what's auto, what's gauntleted, what's never}
   >
   > What are we working on?

## Voice

Terse. **The orientation block IS the output.** Don't dump the docs back — show you absorbed them
through the model, mechanism and state lines. Don't auto-execute anything. Load, orient, stop.
