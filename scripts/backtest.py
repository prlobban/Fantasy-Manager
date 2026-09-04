#!/usr/bin/env python
"""Replay a completed season through the engine and score the result.

    python scripts/backtest.py --season 2025
    python scripts/backtest.py --season 2025 --slot all
    python scripts/backtest.py --season 2024 2025 --slot all --mode normalised
    python scripts/backtest.py --season 2025 --slot all --json out.json

Modes:
  native      — score under that season's own settings and scoring.
  normalised  — score under 2026 settings, so the answer is "how would this
                engine do in MY league" rather than "in the league that was".
                Gated on the rescorer reproducing ESPN (core/backtest/rescore).

The number that matters is not our points total — it is our RANK among the ten
replayed rosters, because every team in a replay is scored by the same code.
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.backtest import history, replay, rescore, score  # noqa: E402
from core.backtest.score import POLICIES  # noqa: E402
from core.config import settings  # noqa: E402
from core.espn.client import EspnClient  # noqa: E402
from core.espn.settings import load as load_settings  # noqa: E402

log = logging.getLogger("backtest")

NORMALISE_TO = 2026


def target_settings(year: int):
    return load_settings(EspnClient(replace(settings(), season=year)))


def real_rosters(season: history.Season) -> dict[int, list]:
    """What each team ACTUALLY drafted — no replay, no engine."""
    by_id = season.by_id
    out: dict[int, list] = {}
    for pk in sorted(season.picks, key=lambda x: x.overall):
        if pk.espn_id in by_id:
            out.setdefault(pk.team_id, []).append(by_id[pk.espn_id])
    return out


def score_real(season: history.Season, facts, actuals: dict,
               weeks: list[int]) -> dict[str, dict[int, dict]]:
    """The human benchmark: every real 2025 roster, scored the same way.

    This is the comparison that means something. "We beat the field average" is
    weak — the field includes the replay's own fallback artefacts. "The engine
    in seat 4 scored more than the person who really had seat 4, under the same
    lineup policy" is a claim about the draft and nothing else.
    """
    rosters = real_rosters(season)
    out: dict[str, dict[int, dict]] = {}
    for policy in POLICIES:
        res = score.score_league(rosters, facts.settings, season=season.year,
                                 policy=policy, weeks=weeks, actuals=actuals)
        out[policy] = {
            tid: {"points": round(t.total, 1), "rank": res.rank_of(tid)}
            for tid, t in res.teams.items()
        }
    return out


def run_one(season: history.Season, team_id: int, *, mode: str,
            actuals: dict, weeks: list[int], facts, real: dict) -> dict:
    board = replay.build_board(season, facts=facts)
    rp = replay.replay(season, board, my_team_id=team_id)

    by_id = season.by_id
    rosters = {t: [by_id[i] for i in ids if i in by_id]
               for t, ids in rp.rosters.items()}

    out: dict = {
        "season": season.year, "mode": mode, "team_id": team_id,
        "slot": rp.slot, "fallback_rate": round(rp.fallback_rate, 4),
        "picks": [
            {"overall": p.overall, "round": p.round_num, "name": p.name,
             "pos": p.pos.value, "vor": round(p.vor, 1),
             "real": p.real_name}
            for p in rp.our_picks
        ],
        "policies": {},
    }

    for policy in POLICIES:
        res = score.score_league(rosters, facts.settings, season=season.year,
                                 policy=policy, weeks=weeks, actuals=actuals)
        me = res.teams[team_id]
        wins, losses = res.all_play(team_id)
        field_totals = [t.total for t in res.teams.values()]
        human = real.get(policy, {}).get(team_id, {})
        out["policies"][policy] = {
            "points": round(me.total, 1),
            "rank": res.rank_of(team_id),
            "of": len(res.teams),
            "field_mean": round(statistics.fmean(field_totals), 1),
            "vs_field": round(me.total - statistics.fmean(field_totals), 1),
            "weekly_mean": round(me.mean, 1),
            "weekly_stdev": round(me.stdev, 1),
            "all_play": f"{wins}-{losses}",
            # The human who really held this seat, scored identically.
            "human_points": human.get("points"),
            "human_rank": human.get("rank"),
            "vs_human": (round(me.total - human["points"], 1)
                         if human.get("points") is not None else None),
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--season", type=int, nargs="+", default=[2025])
    ap.add_argument("--slot", default="all",
                    help="draft slot (1-based), or 'all' to sweep every seat")
    ap.add_argument("--mode", choices=("native", "normalised"), default="native")
    ap.add_argument("--json", type=Path, default=None)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(message)s")

    results: list[dict] = []
    for year in args.season:
        season = history.load(year)

        if args.mode == "normalised":
            # ONLY the scoring rules are borrowed from 2026. The league
            # STRUCTURE stays the season's own — you cannot replay a 6-team
            # 2024 draft through a 10-team pick order, and an earlier version
            # that did exactly that made 2024 look 100 points worse than it is.
            scoring = target_settings(NORMALISE_TO).settings.scoring
            facts = season.facts.settings.model_copy(update={"scoring": scoring})
            facts = replace(season.facts, settings=facts)

            rep = rescore.verify(season)
            log.info("reproduction %d: %s", year, rep.describe())
            rs = rescore.rescored_weeks(season, scoring)
            # Rescore what the engine DRAFTS on as well as what it is graded
            # on, or it is marked down for maximising the objective it was
            # given instead of the one it is judged by.
            rescore.apply_projections(season, rescore.rescored_projections(season, scoring))
            print(f"[{year}] rescored to {NORMALISE_TO} scoring: {rs.describe()}")
            actuals = score.actuals_from_season(season, rs.weeks)
        else:
            facts = season.facts
            actuals = score.actuals_from_season(season)

        weeks = list(range(1, facts.settings.regular_season_weeks + 1))
        order = season.facts.pick_order
        slots = range(1, len(order) + 1) if args.slot == "all" else [int(args.slot)]
        real = score_real(season, facts, actuals, weeks)

        for slot in slots:
            if slot > len(order):
                print(f"[{year}] slot {slot} does not exist in a "
                      f"{len(order)}-team league — skipped")
                continue
            r = run_one(season, order[slot - 1], mode=args.mode,
                        actuals=actuals, weeks=weeks, facts=facts, real=real)
            results.append(r)
            _print_one(r)

    _print_summary(results)
    if args.json:
        args.json.write_text(json.dumps(results, indent=1), encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0


def _print_one(r: dict) -> None:
    e = r["policies"]["engine"]
    h = r["policies"]["hindsight"]
    vs = e.get("vs_human")
    vs_s = f"{vs:+7.1f} vs human" if vs is not None else "  (no human)"
    print(f"[{r['season']} slot {r['slot']:>2}] "
          f"engine {e['points']:>7.1f} rank {e['rank']}/{e['of']}  ·  "
          f"human {e['human_points']:>7.1f} rank {e['human_rank']}  ·  "
          f"{vs_s}  ·  ceiling {h['points']:>7.1f}  ·  "
          f"fallbacks {r['fallback_rate']:.0%}")


def _print_summary(results: list[dict]) -> None:
    if not results:
        return
    print("\n" + "=" * 78)
    by_season: dict[int, list[dict]] = {}
    for r in results:
        by_season.setdefault(r["season"], []).append(r)

    for year, rs in sorted(by_season.items()):
        print(f"\n{year} — {len(rs)} slot(s), mode {rs[0]['mode']}")
        for policy in POLICIES:
            ranks = [r["policies"][policy]["rank"] for r in rs]
            of = rs[0]["policies"][policy]["of"]
            top3 = sum(1 for x in ranks if x <= 3)
            vsh = [r["policies"][policy]["vs_human"] for r in rs
                   if r["policies"][policy]["vs_human"] is not None]
            beat = sum(1 for x in vsh if x > 0)
            print(f"  {policy:<10} mean rank {statistics.fmean(ranks):.2f}/{of}"
                  f"   top-3 {top3}/{len(ranks)}"
                  f"   beat the human in {beat}/{len(vsh)} seats"
                  f"   ({statistics.fmean(vsh):+.1f} pts avg)")


if __name__ == "__main__":
    sys.exit(main())
