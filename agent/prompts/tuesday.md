# Tuesday review (§1.4, §7, D7)

core has computed the numbers and pulled last week's decisions. You interpret
them, grade them, and write down what to keep and what never to repeat.

## The four things (§7.2)

1. **Result**: won or lost, by how much.
2. **Manager efficiency**: actual starting points divided by the best lineup
   available in hindsight. The only number that measures *the agent*,
   separated from luck. A loss at 98% is a good week; a win at 70% is a
   warning.
3. **Calibration**: projected vs actual by position. Direction, not magnitude.
4. **The league**: every roster, who is buying, who is selling, who has a bye
   crunch or a hole where we hold surplus (D7.4). That list is next week's
   trade targets.

## Grade every decision (D7.1)

The packet carries `decisions_last_week`: each move with the number that
justified it and the alternative passed on. For each one say whether it was a
**good decision** given what was known at the time, independent of how it
scored. Starting the 14-point projection over the 9 was right even when it
scored 3. A move that worked for a reason we did not have is not a good
decision.

## Lessons (D7.3)

Write at most six, one sentence each, **with the mechanism**. "Benched X on a
Friday DNP; he was inactive. Practice reports beat projections" is a lesson.
"We were right about X" is not. Lessons are appended to the system's memory
and read by every future run, so a wrong lesson does damage: write one only
when you can name the mechanism.

## Priors (§7.3, §7.4)

**Only propose moving a prior on a repeated, directional miss.** One loud week
is noise. Say which `priors.yaml` key, from what to what, and the pattern. Most
weeks this is empty. You propose; Pearce applies.

Return the review schema.
