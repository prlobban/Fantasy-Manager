# Daily sweep (§1.3)

Run the morning pass over the roster. In order:

1. `get_settings` — always first (§3.1).
2. `get_roster` and `get_matchup` — where we stand this week.
3. `get_lineup_plan` — core's optimal lineup, the §4.2 variance call, and the
   exact moves. Read the `changes` list; that is the proposal.
4. `get_waiver_plan` — candidates already scored against §5. Note the
   `skipped` list: those are decisions too, and often the right one.
5. `get_pending_offers` — for each one, `run_gauntlet(offer_id)` and handle it
   per §6.8.

**Scope.** The packet carries `scope`. `all` is the normal sweep. `lineup`
(the Sunday pass) means the lineup ONLY — no waiver moves, no trade actions,
even if you can see them; say so in `no_action_reason` if the lineup is
already optimal.

## What to actually do

**Lineup (§4).** If `changes` is empty, do nothing and say so. If it is not,
call `set_lineup` with those moves. Two things to check before you do:
- Does any assignment have `player: null`? That is an unfillable slot and it is
  a bigger problem than any swap — surface it.
- Does a change bench a first-three-rounds player? §4.5 says that needs a
  written reason. If you cannot write one, the model is probably wrong, not the
  player.

**Waivers (§5).** Free adds are free (§5.3.2) — take them if they clear §5.2.
Claims cost our queue position, so apply §5.3.1's bar for the priority we hold.
Never spend a top-3 claim on a streamer (§5.3.3). Remember every add needs a
drop with only four bench spots, and the drop's value is part of the price.

**Trades (§6.8).** The gauntlet decides; you narrate. Write the §6.8.3 sentence
in your own words — *why did this manager send this offer?* If you cannot write
a coherent one, that itself is the answer. A 13/13 pass → `accept_trade`
(which re-runs the gauntlet in code before anything happens). Anything else →
`reject_trade` citing the failed gate. Never accept anything the gauntlet
rejected; you cannot, and trying wastes a turn. Outgoing ideas come from
`get_trade_ideas`; there is no propose tool — post a good one with `notify`.

## News (§2.8)

The packet carries recent news on our players. **The model is the prior; the
news is the update.** A depth-chart change or a Wednesday DNP beats a projection
computed before it. If they disagree and two sources confirm the news, the news
wins — but say that you are overriding, and why.

## Output

Return the actions schema. Every action needs `cites`. If nothing should
happen, return an empty `actions` list and fill in `no_action_reason` — that is
a complete, correct answer on most days.
