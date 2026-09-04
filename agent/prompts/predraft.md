# Pre-draft news pass (§3.2)

The board is already built: projections, VOR, tiers, durability, ADP. Your job
is the one thing it cannot do — check the top of the board against the last few
days of news, and nudge anyone the numbers have not caught up with.

## What earns an override

- A depth-chart change: a starter named, a committee broken, a rookie elevated.
- An injury that ESPN's projection has not absorbed, or a return confirmed.
- A holdout, suspension, or trade that moves a player's role.
- A beat reporter consistently signalling something the box score has not shown.

## What does NOT

- Preseason hype, "best shape of his life", camp buzz.
- A single analyst's ranking disagreeing with ESPN's projection. That is a
  different opinion, not new information.
- Anything you cannot attribute to a source.

## The bounds

Each override is a multiplier on the player's projection, and it is **clamped to
±15%** in code. You cannot rewrite the board and should not try — you are
correcting for staleness, not re-ranking football players.

One override per player. Every one needs a `reason` and a `source`. If you are
not sure, leave the player alone: the board's number is a real projection and
yours would be a guess.

Return the overrides schema. An empty list is a fine answer if the news is quiet.
