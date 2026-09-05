# Daily sweep (§1.3, D2 to D6)

The morning pass. This morning's research is already in the packet under
`research` and already folded into the valuations. Work in this order:

1. `get_settings`, always first (§3.1).
2. Read `us.roster`, `roster_shape`, `lessons`, and `research`. Write the
   `roster_assessment` (D5, D7.4) before touching anything.
3. `get_lineup_plan`: core's optimal lineup with the §4.2 variance call and
   the exact moves. The `changes` list is the proposal.
4. `get_waiver_plan`: candidates scored against §5, with the drop each would
   require and whether that drop is `tradeable` (D4.5). `adds_left_this_week`
   tells you how many of the three you have.
5. `get_trade_ideas`: outgoing proposals core would make, both sides' gain,
   and what each does to our roster shape. `proposals_left_today` and
   `proposals_left_this_week` are the §6.1 limits.
6. `get_pending_offers`: for each, `run_gauntlet(offer_id)`; handle per §6.8.

**Scope.** `all` is the normal sweep. `lineup` (the Thursday, Sunday and
Monday passes) means the lineup ONLY: no adds, no trades, even if you can see
them.

## Lineup (§4, D3)

If `changes` is empty, say so. If not, `set_lineup` with those moves, unless
this morning's research contradicts them: a DNP the projection has not
absorbed, a usage collapse, a matchup the dossier calls extreme. Then override
and say exactly which dossier fact you are acting on (§2.8, D1.4). A `player:
null` assignment is an unfillable slot and outranks every swap. Benching a
first-three-round pick needs the written reason of §4.5.

## Waivers (§5, D2)

Free adds cost no priority but DO cost one of the three weekly adds. A claim
costs both. Ask of every add: does he change our *starting* lineup, this week
or ROS (D2.4)? Is this a role change or one box score (D2.2)? Are we a
must-win roster this week or a strong one (D2.3)? Is the drop a player another
team would start? Then he is a trade chip, not a cut (D4.5), and you propose
a trade instead unless the add clears `season.urgent_add_weekly_gain`.

## Trades (§6, D4)

`get_trade_ideas` is where roster-shape problems get fixed. A one-slot
position holding surplus (three TEs, two QBs) is trade capital (D5.2). A good
proposal addresses a need on both sides (D4.4) and survives the group-chat test
(D4.6). `propose_trade` is a real write: it goes to the other manager. Use one
of the three only for an offer you would send with your name on it.

Incoming offers: the gauntlet decides, you narrate. Write the §6.8.3 sentence.
13/13 means `accept_trade` (re-run in code). Anything else means
`reject_trade` with the failing gate.

## Every action, the D8 contract

`reason` (the move and the number) · `short_term` · `long_term` ·
`alternative` · `evidence` (with source) · `would_be_wrong_if`. Concrete. Not
"his projection is higher" but *why*: usage, role, matchup, health, and over
what horizon.

## Escalate

Anything you want Pearce's read on goes in `escalate`: a trade you would send
but are not sure of, a stud the model wants to bench, a roster problem no tool
can fix. It is posted to him on Slack.

## Output

The actions schema. Every action fully reasoned. If nothing should happen,
`actions` is empty and `no_action_reason` says why, and `roster_assessment`
is still written.
