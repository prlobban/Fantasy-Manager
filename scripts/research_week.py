#!/usr/bin/env python
"""The morning research (D1, D3.1): one in-season dossier per player who
matters today — everyone on our roster, the top waiver candidates, and the
trade targets core surfaced. Runs BEFORE the sweep so the sweep decides on
this morning's facts, not last week's projection.

    python scripts/research_week.py             # roster + candidates, resuming
    python scripts/research_week.py --roster    # roster only (cheap)
    python scripts/research_week.py --limit 3   # a taste

Same shape as the pre-draft pass (agent/research.py): one agent per player,
parallel, resumable, every dossier validated in code before it can move a
number.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from agent import run as agent_run
from agent.packet import weekly_dossier_packet
from agent.research import PassStats, _record_usage, summarise
from core.espn import league_state
from core.manager import research as R
from core.manager import trades_out, waivers
from core.model.priors import priors
from core.model.value import value_pool
from core.notify import notify

log = logging.getLogger("research_week")


def targets(st: league_state.LeagueState) -> list[tuple[object, object, str]]:
    """(player, valuation, role) for everyone worth a dossier this morning."""
    p = priors()
    settings = st.facts.settings
    wk = st.week
    vals = value_pool(st.all_players(), settings, window="week", week=wk,
                      weeks_remaining=max(1, settings.regular_season_weeks - wk + 1),
                      current_week=wk)
    ros = value_pool(st.all_players(), settings, window="ros",
                     weeks_remaining=max(1, settings.regular_season_weeks - wk + 1),
                     current_week=wk)
    out: dict[int, tuple] = {}
    for pl in st.me.roster:
        out[pl.espn_id] = (pl, vals.get(pl.espn_id), "roster")

    n_w = int(p.get("research_week.waiver_candidates"))
    wp = waivers.build(st.me.roster, st.free_agents, vals, settings,
                       waiver_priority=st.me.waiver_priority, on_waivers=st.on_waivers,
                       bench_open=st.bench_open, current_week=wk, ros_valuations=ros,
                       max_claims=n_w)
    for c in (wp.free_adds + wp.claims + [s for s, _ in wp.skipped])[:n_w]:
        out.setdefault(c.player.espn_id, (c.player, c.valuation, "waiver"))

    n_t = int(p.get("research_week.trade_targets"))
    others = {tid: (t.name, t.roster) for tid, t in st.teams.items() if tid != st.my_team_id}
    for pr in trades_out.build(st.me.roster, others, ros, settings, max_proposals=n_t):
        for pl in pr.get:
            out.setdefault(pl.espn_id, (pl, vals.get(pl.espn_id), "trade"))
    return list(out.values())


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--roster", action="store_true", help="our roster only")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--no-resume", action="store_true")
    args = ap.parse_args(argv)

    p = priors()
    st = league_state.snapshot()
    wk = st.week
    todo = targets(st)
    if args.roster:
        todo = [t for t in todo if t[2] == "roster"]
    if args.limit:
        todo = todo[:args.limit]

    max_age = float(p.get("research_week.max_age_hours"))
    if not args.no_resume:
        fresh = R.load_all(week=wk, max_age_hours=max_age)
        before = len(todo)
        todo = [t for t in todo if t[0].espn_id not in fresh]
        log.info("resuming: %d already fresh, %d to do", before - len(todo), len(todo))

    workers = args.workers or int(p.get("research_week.workers"))
    st_ = PassStats()
    log.info("week %d research: %d players · %d workers", wk, len(todo), workers)

    def work(t):
        pl, val, role = t
        t0 = time.monotonic()
        try:
            res = agent_run.run("weekly_dossier", weekly_dossier_packet(pl, val, week=wk, role=role),
                                timeout=300)
        except Exception as e:  # a worker must not kill the pool
            res = agent_run.AgentResult(False, "weekly_dossier", None, "", error=str(e))
        return pl, res, time.monotonic() - t0

    halted = False
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(work, t): t for t in todo}
        for fut in as_completed(futures):
            pl, res, wall = fut.result()
            st_.attempted += 1
            st_.cost_usd += _record_usage(pl.espn_id, pl.name, res, wall)
            if res.ok and res.output:
                out = dict(res.output)
                out.setdefault("espn_id", pl.espn_id)
                out.setdefault("name", pl.name)
                R.write(pl.espn_id, out, week=wk)
                d = R.load_one(pl.espn_id, max_age_hours=max_age)
                if d is None:
                    st_.rejected += 1
                    log.warning("%s: dossier written but rejected by validation", pl.name)
                else:
                    st_.written += 1
                    st_.vetoes += int(d.veto)
                    st_.moved += int(d.week_multiplier != 1.0 or d.ros_multiplier != 1.0)
                    log.info("%-24s %s", pl.name, d.summary())
            else:
                st_.failed += 1
                st_.errors.append(f"{pl.name}: {res.error}")
                log.warning("%-24s FAILED: %s", pl.name, res.error)
                if res.error and res.error.startswith("capacity:") and not halted:
                    halted = True
                    log.error("rate limited — stopping the pass. Re-run to resume.")
                    for f in futures:
                        f.cancel()

    line = summarise(st_)
    print("\n" + "=" * 64 + "\n" + line)
    notify("info" if not st_.failed else "warn",
           f"Week {wk} research: {line}", "\n".join(st_.errors[:5]) or None)
    return 0 if (st_.written or st_.attempted == 0) else 1


if __name__ == "__main__":
    sys.exit(main())
