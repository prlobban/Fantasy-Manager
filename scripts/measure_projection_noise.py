#!/usr/bin/env python
"""How much signal is actually in an ESPN weekly projection?

Measured on 2025, weeks 1-14, over the players a manager would plausibly
start (weekly projection >= 5.0). The question this answers: is a +/-25%
narrative multiplier a meaningful adjustment, or is it noise applied with
false confidence?
"""

import statistics as st
from collections import defaultdict

from core.backtest import history as H

c = H._client_for(2025)
POS = {1: "QB", 2: "RB", 3: "WR", 4: "TE", 5: "K", 16: "D/ST"}
rows = defaultdict(list)

for wk in range(1, 15):
    f = {"players": {"filterStatus": {"value": ["FREEAGENT", "WAIVERS", "ONTEAM"]},
                     "limit": 180, "offset": 0,
                     "sortPercOwned": {"sortAsc": False, "sortPriority": 1}}}
    try:
        d = c.get_view("kona_player_info", filters=f, params={"scoringPeriodId": wk})
    except Exception as e:
        print("week", wk, "failed:", e)
        continue
    for e in d.get("players") or []:
        p = e.get("player") or {}
        pos = POS.get(int(p.get("defaultPositionId", -1)))
        if not pos:
            continue
        proj = act = None
        for s in p.get("stats") or []:
            if int(s.get("statSplitTypeId", -1)) != 1:
                continue
            if int(s.get("scoringPeriodId") or 0) != wk:
                continue
            if int(s.get("statSourceId", -1)) == 1:
                proj = float(s.get("appliedTotal") or 0)
            elif int(s.get("statSourceId", -1)) == 0:
                act = float(s.get("appliedTotal") or 0)
        if proj and proj >= 5.0 and act is not None:
            rows[pos].append(act / proj)

hdr = "{:>5} {:>6} {:>8} {:>7} {:>7} {:>9} {:>10}".format(
    "POS", "n", "median", "p25", "p75", "bust<50%", "boom>150%")
print(hdr)
print("-" * len(hdr))

allr = []
for pos in ("QB", "RB", "WR", "TE", "K", "D/ST"):
    r = sorted(rows.get(pos, []))
    if len(r) < 30:
        continue
    allr += r
    bust = sum(1 for x in r if x < 0.5) / len(r)
    boom = sum(1 for x in r if x > 1.5) / len(r)
    p25, p75 = r[len(r) // 4], r[3 * len(r) // 4]
    print(f"{pos:>5} {len(r):>6} {st.median(r):>8.2f} {p25:>7.2f} "
          f"{p75:>7.2f} {bust:>8.0%} {boom:>9.0%}")

allr.sort()
bust = sum(1 for x in allr if x < 0.5) / len(allr)
boom = sum(1 for x in allr if x > 1.5) / len(allr)
lo, hi = allr[len(allr) // 4], allr[3 * len(allr) // 4]
print(f"{'ALL':>5} {len(allr):>6} {st.median(allr):>8.2f} {lo:>7.2f} "
      f"{hi:>7.2f} {bust:>8.0%} {boom:>9.0%}")

# The comparison that matters: the research multiplier's full range against
# the spread of the thing it is adjusting.
print()
print("research week_multiplier range: 0.75 - 1.25  (+/-25%)")
print(f"actual p25-p75 spread of actual/projected: {lo:.2f} - {hi:.2f}")
