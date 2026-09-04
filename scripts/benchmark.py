#!/usr/bin/env python
"""Engine vs ESPN autopick, ten seats a season.

    python scripts/benchmark.py
    python scripts/benchmark.py --season 2025 --ranking PPR
    python scripts/benchmark.py --json data/backtest/benchmark.json

For each season and each of ESPN's two published rankings:

  1. one ALL-AUTOPICK draft. It is deterministic, so it yields the baseline
     roster for all ten seats at once;
  2. ten more drafts, the engine in seat 1..10 against nine bots;
  3. compare THE SAME SEAT, engine against autopick.

Holding the seat, pool, scoring and opponents fixed is what makes the delta the
engine's contribution and not a property of the draft position.

Everything is scored under 2026 rules — his league — with both seasons' players
rescored into them, so the two years are comparable.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.backtest import arena, autopick, history, replay, rescore, score  # noqa: E402
from core.config import settings  # noqa: E402
from core.espn.client import EspnClient  # noqa: E402
from core.espn.settings import load as load_settings  # noqa: E402

log = logging.getLogger("benchmark")

TEAMS = 10
TARGET_SEASON = 2026


def target_facts():
    return load_settings(EspnClient(dataclasses.replace(settings(), season=TARGET_SEASON)))


def prepare(year: int, tf):
    """A season's players and results, in 2026 rules and 2026 league shape."""
    s = history.load(year)
    scoring = tf.settings.scoring

    rs = rescore.rescored_weeks(s, scoring)
    rescore.apply_projections(s, rescore.rescored_projections(s, scoring))

    # 2026's league shape for both years — 10 teams, 13 rounds, his roster —
    # but that season's own regular-season length, because a week that was not
    # played cannot be scored.
    facts = dataclasses.replace(
        tf,
        settings=tf.settings.model_copy(update={
            "scoring": scoring,
            "regular_season_weeks": s.facts.settings.regular_season_weeks,
        }),
        position_limits=tf.position_limits,
        pick_order=list(range(1, TEAMS + 1)),
    )
    actuals = score.actuals_from_season(s, rs.weeks)
    weeks = list(range(1, facts.settings.regular_season_weeks + 1))
    return s, facts, actuals, weeks


def market_ranks(year: int) -> dict[int, int]:
    from core.model import market as mkt
    from core.model.priors import priors
    raw = json.loads((history.cache_dir(year) / "pool.json").read_text(encoding="utf-8"))
    return mkt.ranks_from_raw(raw, str(priors().get("model.market_rank_type")))


def score_league(rosters: dict[int, list], facts, actuals, weeks, policy: str):
    res = score.score_league(rosters, facts.settings, season=0, policy=policy,
                             weeks=weeks, actuals=actuals)
    return {seat: t.total for seat, t in res.teams.items()}


def run_block(year: int, ranking: str, tf, policy: str) -> list[dict]:
    s, facts, actuals, weeks = prepare(year, tf)
    ranks = autopick.ranks_from_pool(
        json.loads((history.cache_dir(year) / "pool.json").read_text(encoding="utf-8")),
        ranking)
    missing = [p.name for p in s.players if p.espn_id not in ranks]
    if missing:
        log.warning("%d players have no %s rank, e.g. %s", len(missing), ranking,
                    missing[:3])

    # §2.2b — the consensus blend, from the SAME source the live board uses.
    board = replay.build_board(s, facts=facts, market_ranks=market_ranks(year))

    control = arena.run(board, facts, ranks, engine_seat=None, teams=TEAMS)
    base = score_league(control.rosters, facts, actuals, weeks, policy)

    rows: list[dict] = []
    for seat in range(1, TEAMS + 1):
        res = arena.run(board, facts, ranks, engine_seat=seat, teams=TEAMS)
        pts = score_league(res.rosters, facts, actuals, weeks, policy)
        ours = pts[seat]
        field = sorted(pts.values(), reverse=True)
        rows.append({
            "season": year, "ranking": ranking, "policy": policy, "seat": seat,
            "engine": round(ours, 1),
            "autopick": round(base[seat], 1),
            "delta": round(ours - base[seat], 1),
            "rank": field.index(ours) + 1,
            "of": TEAMS,
            "picks": [{"round": p.round_num, "overall": p.overall, "name": p.name,
                       "pos": p.pos.value,
                       "vor": round(p.vor, 1) if p.vor is not None else None}
                      for p in res.our_picks()],
            "autopick_picks": [{"round": p.round_num, "name": p.name, "pos": p.pos.value}
                               for p in control.picks if p.seat == seat],
        })
        log.info("  %d %s seat %2d: engine %7.1f  autopick %7.1f  %+7.1f  rank %d/%d",
                 year, ranking, seat, ours, base[seat], ours - base[seat],
                 rows[-1]["rank"], TEAMS)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--season", type=int, nargs="+", default=[2024, 2025])
    ap.add_argument("--ranking", nargs="+", default=list(autopick.RANKINGS))
    ap.add_argument("--policy", default="hindsight",
                    choices=("hindsight", "engine", "naive"))
    ap.add_argument("--json", type=Path, default=Path("data/backtest/benchmark.json"))
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    tf = target_facts()
    log.info("league: %d teams, %d rounds, %s scoring\n", TEAMS, tf.draftable_spots,
             TARGET_SEASON)

    rows: list[dict] = []
    for year in args.season:
        for ranking in args.ranking:
            rows.extend(run_block(year, ranking, tf, args.policy))

    _summary(rows)
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    print(f"\nwrote {args.json}")
    return 0


def _summary(rows: list[dict]) -> None:
    print("\n" + "=" * 74)
    for key in sorted({(r["season"], r["ranking"]) for r in rows}):
        sub = [r for r in rows if (r["season"], r["ranking"]) == key]
        wins = sum(1 for r in sub if r["delta"] > 0)
        d = [r["delta"] for r in sub]
        print(f"{key[0]} · {key[1]:<8} engine beats autopick in {wins}/{len(sub)} seats"
              f"   mean {statistics.fmean(d):+7.1f}   median {statistics.median(d):+7.1f}"
              f"   worst {min(d):+7.1f}   best {max(d):+7.1f}")
    wins = sum(1 for r in rows if r["delta"] > 0)
    d = [r["delta"] for r in rows]
    print("-" * 74)
    print(f"OVERALL   engine beats ESPN autopick in {wins}/{len(rows)} paired seats"
          f"   mean {statistics.fmean(d):+.1f} pts   median {statistics.median(d):+.1f}")


if __name__ == "__main__":
    sys.exit(main())
