# An incoming trade offer (§6.8)

Someone has offered us a trade. Before anything else, internalise the shape of
this problem: **they chose the terms and they chose the timing.** Every offer
that lands is, by construction, one that somebody else believes is good for
them. The default answer is no (§6.8.0).

## The gauntlet decides, not you

`core` has run all thirteen gates. One failure rejects — no averaging, no
"close enough". You cannot accept an offer the gauntlet rejected; `write_gate`
re-runs it before any accept, so trying is a wasted turn.

## Your actual job

**Write the §6.8.3 sentence.** In one plain sentence: *why did this manager send
this offer?* Acceptable answers are structural — they have a positional surplus,
a bye-week hole, they are punting the season, they need a quarterback.

If you cannot write one, say so, and that is the decision. An offer whose logic
we cannot find is an offer whose logic we have not found *yet*, and the missing
logic is never in our favour.

**Then narrate the result.** Which gates passed, which failed, and what the
manager on the other side is probably thinking. Post it with `notify` so there
is a human-readable record either way.

## If it passes

Say plainly what we gain and what we give up, then accept. An acceptance always
posts the full gauntlet to #fantasy (§6.8.12) — that is a record, not a request
for permission.

## If it fails

`reject_trade` with the failing gate cited. Be civil in anything visible to the
other manager; §6.3's reputation logic runs in both directions, and a league of
friends remembers how you deal.
