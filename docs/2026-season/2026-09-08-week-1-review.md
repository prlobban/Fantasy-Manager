# Week 1 review — 2026-09-08

**Result:** No result. Week 1 has not been played — kickoff is Sunday 2026-09-13, both teams are 0-0, and every player in the box score (started and benched) shows 0.0 actual points. The packet's "won: false, margin 0.0" is the absence of a game, not a loss.

**Efficiency:** 0.0 (actual 0.0 / best 0.0, 0.0 left on bench)

**Read:** Manager efficiency is undefined this week: actual 0.0 / best-possible 0.0 reported as 0.0% is an artifact of grading an unplayed week, and 0.0 points left on the bench means no games, not a perfect lineup. So the only thing that measures the agent this Tuesday is the decision log, and it splits cleanly. The wire and trade work is sound: the agent found the single largest ROS lever on the roster (two dead one-slot surpluses in Herbert and Pitts converted into a startable WR) and correctly identified the D/ST stream as the biggest weekly gain in the packet. The lineup work is not: five consecutive sweeps benched Josh Allen for Justin Herbert on a 1.9-point weekly edge against an 82-point ROS gap, and two put a TE in the flex on a 0.09-point edge with a startable RB on the bench. Both errors were stopped by Pearce hard-coding §4.7 and §4.6, not by the agent catching itself — an agent that needs the rule written into code twice in one week is over-trusting the weekly projection and under-reading its own playbook. Everything was refused at the gate (kill switch off all week), so none of it cost points.

## Decisions graded

- **no_outcome_yet** — set_lineup (5 sweeps, 09-05 through 09-06): start Justin Herbert at QB over Josh Allen on a ~1.9-point weekly projection edge: BAD DECISION on what was known at the time. Allen's ROS VOR is +73.4 against Herbert's -8.6 — an 82-point gap where §4.7's stud_ros_gap is 20 — and the weekly gap of 1.9 is a quarter of the 8.0 stud_bench_margin and well inside the ~6-point QB weekly error bar (D3.6). The Houston matchup evidence was real but a matchup read is not a DNP or a usage collapse, which is the only kind of fact D9.2 lets override the default. Every one of the five writes was refused by the kill switch, so no points were lost, and §4.7/§4.6 are now enforced in code — this week's lineup_plan correctly has Allen at QB.
- **no_outcome_yet** — set_lineup (09-06 07:33 and 11:02): put Kyle Pitts in the RB/WR/TE flex over Travis Kelce on a 0.09-point edge, with Chuba Hubbard on the bench: BAD DECISION. §4.6 makes the flex RB/WR and admits a TE only when no startable RB or WR remains; Hubbard and Burden were both available, so the slot was never legally Pitts's. Worse, the margin cited to justify it — 0.09 points — is pure model noise, so a rule was broken to buy nothing. The Monday-night late-scratch argument for preferring Pitts over Kelce was a legitimate mechanism, but it argues between two TEs, not for a TE in the flex at all.
- **no_outcome_yet** — set_lineup (09-06 15:03): spend an on-demand research_player call to re-check the Herbert dossier's matchup, which had carried the 2025 Sao Paulo Chiefs opener as this week's opponent: GOOD DECISION, wrong conclusion. Spending a research call to verify a fact the lineup turned on is exactly D9.4, and it caught a genuine data error — LAC hosts Arizona, not KC. The catch generalises: five dossiers in today's packet carry the same stale-opponent warning. The decision it fed was still wrong for the §4.7 reason above, but the process that produced it is the one to keep.
- **no_outcome_yet** — propose_trade (4 sweeps): Justin Herbert + Kyle Pitts to GLOBO GYM PURPLE COBRAS for Garrett Wilson: GOOD DECISION as an idea, sloppy as a process. It converts two players who cannot enter our lineup in any week — a QB2 behind Allen and a TE3 behind Loveland — into a weekly starter for +51.9 ROS starting points at a 0.91 market ratio, clearing the §6.3 floor without touching a protected asset. The process failure is that the identical proposal was regenerated in four separate sweeps: with the kill switch on, §1.5 says the log should have been read first, and this would have consumed the entire 3/week cap on one idea. All four were refused.
- **no_outcome_yet** — propose_trade: choosing the Wilson package over the flagged alternative of Pitts alone to 'rick' for Bhayshul Tuten: DEFENSIBLE BUT PROBABLY WRONG ON SHAPE. The Wilson deal is worth ~3.5x more in raw ROS starting points, which is why it was chosen, but core flagged that it leaves RB short 1 while making WR a surplus, and D2.4 says need over name. The Tuten idea (+15.0, ratio 0.87, no flags, shape_score 3) fixes the RB shortage and the TE surplus in one move and costs only Pitts, keeping Herbert as a second chip. With Swift questionable and only three RBs, the shape argument was underweighted.
- **no_outcome_yet** — add_drop: add Jaguars D/ST, drop Travis Kelce, keep Browns D/ST: RIGHT PLAYER, WRONG DROP. Streaming JAX over CLE is the correct read and the largest weekly gain in the packet (+4.6/wk, clearing the 4.0 urgent bar): Jacksonville is a home favourite against a Watson-led offence, Cleveland is a +9 road underdog. But D6.3 is explicit that a stream replaces — the drop for a D/ST add is the incumbent defence, and dropping Kelce instead would have left us rostering two D/STs, neither of which can be traded or both started, on a bench with zero open spots.
- **no_outcome_yet** — notify x10 (09-04 and 09-05): 'draft complete' posted repeatedly, including one run reporting 2,569 cycles: NOT A MANAGER DECISION, and an operational anomaly worth naming. Ten identical completion notices from what look like repeated practice/draft runs pollute the decision log that §7.1 exists to grade, and the 2,569-cycle entry against a nominal 25-33 suggests one run looped. It cost nothing this week, but a log full of non-decisions makes the Tuesday loop harder every week it accretes.

## Calibration

- Calibration is not computable this week. All 13 observations have actual = 0.0 because the games have not been played; the reported -11.53 'projections ran HIGH' bias is that artifact and must not be read as a model bias or carried forward.

## Lessons

- Do not grade a week whose games have not kicked off: an actual of 0.0 with an unplayed schedule reads to the review code as a total miss, and it produced a fake 0% manager efficiency and a fake -11.5 projection bias that would have moved a prior on nothing.
- Never bench a healthy stud on a weekly projection edge: Allen-over-Herbert was an 82-point ROS VOR gap against a 1.9-point weekly gap, and the weekly number's error bar at QB is wider than the gap it was being asked to decide (§4.7, D3.6).
- A stream replaces its incumbent: when adding this week's D/ST or K, the drop is last week's D/ST or K, never the roster's general drop candidate, or the bench carries two bodies that cannot both start (D6.3).
- Read the decision log before proposing: the same Herbert+Pitts-for-Wilson offer was regenerated in four consecutive sweeps, and with writes enabled that repetition alone would have spent the entire 3-proposals-a-week cap on one idea (§1.5, §6.1).
- Morning dossiers carry stale opponents — five in today's packet flag last season's schedule as this week's game — so when a start/sit or a matchup multiplier turns on the opponent, verify the opponent before the projection (D9.4).

## League

- GLOBO GYM PURPLE COBRAS — stacked at RB (Jonathan Taylor 90.1, Saquon Barkley 77.0) and holding Garrett Wilson 48.6 as their WR2/3; no QB in their top four by market, which is the hole Herbert fills. Primary trade partner: Herbert + Pitts for Wilson, ratio 0.91.
- rick — WR-heavy at the top (Puka Nacua 91.7, Drake London 67.4) with Kyren Williams and Breece Hall behind them, and holds Bhayshul Tuten. Pitts-for-Tuten is the cleanest shape fix on the board: fixes our RB shortage and TE surplus in one move, no flags, and costs us nothing that can start.
- I'm a BoLiever — holds Rashee Rice, suspended through Week 6, so they are a receiver short for six weeks with Amon-Ra St. Brown alone at the top. They also hold Bucky Irving, the RB core wants. No QB in their top four. Best combined buyer of both our surpluses; the Irving idea costs 1.34 of market, so lead with Herbert, not a sweetener.
- Purdy Mouths — RB-rich (James Cook 83.4, Derrick Henry 73.4) with no QB or TE in the top four. A second QB market for Herbert if GLOBO GYM passes.
- Mile High Magic — has Lamar Jackson, so not a Herbert market; the core-generated Odunze offer sends 1.67 of market value for a player whose dossier has him DNP with a leg injury. Do not send it.
- Jeremy's Buster Call (Trey McBride) and Josh Wasowski (Brock Bowers) — both set at TE. Neither is a buyer for Pitts or Kelce; do not waste a slot there.
- Shakir It Off (Week 1 opponent) — carries two tight ends (Ferguson, LaPorta) and Drake Maye, so no market for either of our surpluses; the Jeanty idea would cost Loveland and Swift for +9.4, which core flags as marginal.
- Amon Drugz — deepest roster in the league by market (Chase, Hampton, A.J. Brown, Jeremiyah Love). Nothing we hold interests them; skip.

## Prior proposals (Pearce applies)

- none

## Watch

- D'Andre Swift QUESTIONABLE (cramp 09-03, returned to practice 09-07, no official designation yet) — he is a starting RB on a roster already one RB short. Wednesday's Bears practice report decides whether the RB hole is theoretical or live this week.
- Luther Burden III is in the flex at QUESTIONABLE with a groin injury, zero preseason snaps and a month of missed practice — even if active, the snap-share ramp is a real risk. Watch Friday; Hubbard is the fallback despite his own hamstring tag.
- Bench is 0 open and every one of our three weekly adds forces a drop; Kelce (0.2 ROS VOR) is the only genuinely cheap cut, and once he is gone the next add costs something real.
- Waiver priority is 2 (top-3 band) — §5.3.1 means a claim must be an every-week starter or a +3.0/wk starting upgrade. The current candidate list contains nothing that clears it; do not spend it on a streamer (§5.3.3).
- Etienne is confirmed a three-down bell cow with Kamara out 4+ weeks and Chandler on IR — the buy-low window on him has closed; treat his valuation as real, not optimistic.
- Bye cluster in Week 7: Allen, McConkey and Herbert all off. If Herbert is traded, Week 7 needs a streamed QB planned two weeks out (D5.4).
- Five dossiers in this packet carry stale-schedule warnings; core's matchup multipliers are only as good as the opponent field feeding them.

_raw: {'won': False, 'our_points': 0.0, 'their_points': 0.0, 'margin': 0.0}_
