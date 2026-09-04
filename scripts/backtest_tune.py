#!/usr/bin/env python
"""Sweep the picker's coefficients against two real seasons.

    python scripts/backtest_tune.py                    # sweep everything
    python scripts/backtest_tune.py --prior scarcity_weight
    python scripts/backtest_tune.py --json data/backtest/sweep.json

**What is being maximised.** Hindsight-optimal points minus the human who
really held that seat, averaged over every slot in both seasons. Hindsight,
because it isolates the DRAFT: the engine lineup policy would mix start/sit
skill into a number meant to grade picking.

**Why one coefficient at a time.** 2024 and 2025 give 16 drafted seats. That is
enough to expose a coefficient that is badly wrong and nowhere near enough to
fit six of them jointly — a joint search on 16 samples finds the noise. Any
result inside the noise band is reported as "no signal", and no-signal means
the value does not move.

**The overfitting guard.** Every candidate is scored on 2024 and 2025
SEPARATELY as well as together. A value that only helps the season it was
picked on is rejected on sight, however good the combined number looks.

The board is built once per season and reused across every coefficient value,
because these weights only touch `picker.rank` — valuation, tiers and VOR are
upstream of them and cannot change. That is what makes a sweep affordable.
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

from core.backtest import history, replay, rescore, score  # noqa: E402
from core.config import settings  # noqa: E402
from core.espn.client import EspnClient  # noqa: E402
from core.espn.settings import load as load_settings  # noqa: E402
from core.model.priors import overridden, priors  # noqa: E402

log = logging.getLogger("tune")

SEASONS = (2024, 2025)
NORMALISE_TO = 2026


def _target_scoring() -> dict[int, float]:
    return load_settings(EspnClient(
        dataclasses.replace(settings(), season=NORMALISE_TO))).settings.scoring

#: The sweep grid. Each entry is (prior name under `draft.`, values to try).
#: Ranges bracket the committed value generously in both directions, including
#: 0.0 — because "does this term do anything at all" is the first question, and
#: a term that never helps should be deleted rather than tuned.
GRID: dict[str, list[float]] = {
    "scarcity_weight": [0.0, 0.15, 0.25, 0.35, 0.5, 0.7, 1.0],
    "tier_break_weight": [0.0, 0.06, 0.12, 0.2, 0.35],
    "room_demand_per_team": [0.0, 0.01, 0.02, 0.04, 0.08],
    "run_join_weight": [0.0, 0.03, 0.06, 0.12],
    "run_wait_weight": [0.0, 0.02, 0.04, 0.08],
    "hole_weight": [0.0, 0.08, 0.15, 0.3, 0.5],
    "stack_penalty": [0.0, 10.0, 25.0, 50.0, 90.0],
    "bench_opportunity_cost": [0.0, 10.0, 25.0, 50.0, 90.0],
}

#: A swing smaller than this, in season points averaged over 16 seats, is not a
#: finding. One roster's single good week is ~120 points; a 15-point average
#: difference is well inside what one lucky replay divergence produces.
NOISE_BAND = 15.0


class Harness:
    """Everything expensive, loaded once."""

    def __init__(self, years=SEASONS, *, mode: str = "normalised") -> None:
        self.seasons: dict[int, history.Season] = {}
        self.boards: dict[int, object] = {}
        self.actuals: dict[int, dict] = {}
        self.weeks: dict[int, list[int]] = {}
        self.human: dict[int, dict[int, float]] = {}
        for y in years:
            s = history.load(y)
            if mode == "normalised":
                # Tune under the rules he actually plays, not the rules those
                # seasons used. Structure stays the season's own — see
                # scripts/backtest.py for why that distinction is load-bearing.
                scoring = _target_scoring()
                rs = rescore.rescored_weeks(s, scoring)
                rescore.apply_projections(s, rescore.rescored_projections(s, scoring))
                s.facts = dataclasses.replace(
                    s.facts, settings=s.facts.settings.model_copy(
                        update={"scoring": scoring}))
                self.actuals[y] = score.actuals_from_season(s, rs.weeks)
            else:
                self.actuals[y] = score.actuals_from_season(s)
            self.seasons[y] = s
            self.boards[y] = replay.build_board(s)
            self.weeks[y] = list(range(1, s.facts.settings.regular_season_weeks + 1))
            self.human[y] = self._human_totals(s)

    def _human_totals(self, s: history.Season) -> dict[int, float]:
        by_id = s.by_id
        rosters: dict[int, list] = {}
        for pk in sorted(s.picks, key=lambda x: x.overall):
            if pk.espn_id in by_id:
                rosters.setdefault(pk.team_id, []).append(by_id[pk.espn_id])
        res = score.score_league(rosters, s.facts.settings, season=s.year,
                                 policy="hindsight", weeks=self.weeks[s.year],
                                 actuals=self.actuals[s.year])
        return {t: v.total for t, v in
                ((tid, ts) for tid, ts in res.teams.items())}

    def evaluate(self) -> dict[int, float]:
        """Mean (engine - human) hindsight points per seat, by season."""
        out: dict[int, float] = {}
        for year, s in self.seasons.items():
            board = self.boards[year]
            by_id = s.by_id
            deltas: list[float] = []
            for team_id in s.facts.pick_order:
                rp = replay.replay(s, board, my_team_id=team_id)
                rosters = {t: [by_id[i] for i in ids if i in by_id]
                           for t, ids in rp.rosters.items()}
                res = score.score_league(rosters, s.facts.settings, season=year,
                                         policy="hindsight", weeks=self.weeks[year],
                                         actuals=self.actuals[year])
                deltas.append(res.teams[team_id].total - self.human[year][team_id])
            out[year] = statistics.fmean(deltas)
        return out


def sweep(h: Harness, prior: str, values: list[float]) -> list[dict]:
    rows = []
    committed = priors().get(f"draft.{prior}")
    for v in values:
        with overridden(**{f"draft__{prior}": v}):
            per_season = h.evaluate()
        combined = statistics.fmean(per_season.values())
        rows.append({"value": v, "combined": round(combined, 1),
                     "is_committed": v == committed,
                     **{str(y): round(x, 1) for y, x in per_season.items()}})
        log.info("  %s=%-6s combined %+7.1f  %s", prior, v, combined,
                 "  ".join(f"{y}:{x:+.0f}" for y, x in per_season.items()))
    return rows


def verdict(prior: str, rows: list[dict]) -> dict:
    """Does this coefficient earn a change? Deliberately hard to satisfy."""
    base = next((r for r in rows if r["is_committed"]), None)
    best = max(rows, key=lambda r: r["combined"])
    spread = best["combined"] - min(r["combined"] for r in rows)

    if base is None:
        return {"prior": prior, "call": "no committed value in grid"}

    gain = best["combined"] - base["combined"]
    years = [k for k in rows[0] if k.isdigit()]
    helps_all = all(best[y] >= base[y] for y in years)

    if spread < NOISE_BAND:
        call = "INERT — no value in the grid moves the result; consider deleting the term"
    elif gain < NOISE_BAND:
        call = "KEEP — committed value is already at or near the best"
    elif not helps_all:
        call = (f"REJECT — {best['value']} wins combined (+{gain:.0f}) but loses a "
                f"season; that is a fit to one year, not a finding")
    else:
        call = f"CHANGE to {best['value']} (+{gain:.0f} pts/seat, helps both seasons)"

    return {"prior": prior, "committed": base["value"], "best": best["value"],
            "gain": round(gain, 1), "spread": round(spread, 1),
            "helps_all_seasons": helps_all, "call": call, "rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prior", nargs="*", default=None)
    ap.add_argument("--json", type=Path, default=Path("data/backtest/sweep.json"))
    ap.add_argument("--mode", choices=("native", "normalised"), default="normalised")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    log.info("loading seasons and boards (once)...")
    h = Harness(mode=args.mode)
    log.info("baseline: %s", {y: round(v, 1) for y, v in h.evaluate().items()})

    todo = args.prior or list(GRID)
    out = []
    for prior in todo:
        if prior not in GRID:
            log.warning("no grid for %s — skipped", prior)
            continue
        log.info("\nsweeping draft.%s", prior)
        out.append(verdict(prior, sweep(h, prior, GRID[prior])))

    print("\n" + "=" * 78 + "\nVERDICTS\n")
    for v in out:
        print(f"  draft.{v['prior']:<24} {v['call']}")
    args.json.parent.mkdir(parents=True, exist_ok=True)
    args.json.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
