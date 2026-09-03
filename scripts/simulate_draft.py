#!/usr/bin/env python
"""Dress rehearsal: simulate our real draft against the real board.

Uses the actual ESPN pool, actual ADP, actual league settings and our actual
pick slot. The other nine teams are ADP bots with noise, which is roughly how a
casual league drafts and is exactly the population survival.py assumes.

This is the closest thing to a draft-day rehearsal that doesn't need a room,
and unlike tests/test_draft_sim.py it needs cookies and runs against live data.

    python scripts/simulate_draft.py            # one draft, verbose
    python scripts/simulate_draft.py -n 20      # 20 drafts, summary only
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
from collections import Counter

from core.draft import board as board_mod
from core.draft import picker
from core.draft.room import Pick, RoomModel
from core.espn.client import client
from core.model.schema import Player, Pos


def bot_pick(available: list[Player], roster: Counter, facts, rnd: int,
             rounds: int, rng: random.Random) -> Player:
    """A competent ADP drafter: caps, mandatory slots, no early K/DST."""
    starters = {
        pos: facts.settings.starters_at(pos)
        for pos in (Pos.QB, Pos.RB, Pos.WR, Pos.TE, Pos.K, Pos.DST)
    }
    rounds_left = rounds - rnd + 1
    legal = [p for p in available
             if roster.get(p.pos, 0) < facts.position_limits.get(p.pos, 99)]
    if not legal:
        legal = list(available)

    missing = {p: n - roster.get(p, 0) for p, n in starters.items()
               if n and roster.get(p, 0) < n}
    if missing and rounds_left <= sum(missing.values()):
        legal = [p for p in legal if p.pos in missing] or legal
    elif rnd <= rounds - 2:
        legal = [p for p in legal if p.pos not in (Pos.K, Pos.DST)] or legal

    window = sorted(legal, key=lambda p: p.espn_adp or 9999)[:6]
    weights = [max(0.05, 1.0 / (i + 1)) for i in range(len(window))]
    return rng.choices(window, weights=weights, k=1)[0]


def best_lineup_points(roster: list[Player], settings) -> float:
    by_pos: dict[Pos, list[Player]] = {}
    for p in roster:
        by_pos.setdefault(p.pos, []).append(p)
    for v in by_pos.values():
        v.sort(key=lambda p: -p.proj_season)
    used: set[int] = set()
    total = 0.0
    for slot in sorted(settings.starting_slots, key=lambda s: len(s.eligible)):
        for _ in range(slot.count):
            best = None
            for pos in slot.eligible:
                for p in by_pos.get(pos, []):
                    if p.espn_id in used:
                        continue
                    if best is None or p.proj_season > best.proj_season:
                        best = p
                    break
            if best:
                used.add(best.espn_id)
                total += best.proj_season
    return total


def run_one(bd, my_team_id: int, seed: int, verbose: bool):
    rng = random.Random(seed)
    facts = bd.facts
    rows = bd.rows
    room = RoomModel(facts=facts, my_team_id=my_team_id)
    available = {p.espn_id: p for p in bd.players}
    rosters: dict[int, list[Player]] = {t: [] for t in facts.pick_order}
    counts: dict[int, Counter] = {t: Counter() for t in facts.pick_order}
    rounds = facts.draftable_spots
    n = len(facts.pick_order)

    for overall in range(1, rounds * n + 1):
        tid = room.team_on_clock(overall)
        if tid == my_team_id:
            plan = picker.rank(rows, room)
            if plan.best is None:
                break
            chosen = plan.best.player
            if verbose:
                rnd = (overall - 1) // n + 1
                ru = plan.candidates[1] if len(plan.candidates) > 1 else None
                print(f"  R{rnd:>2} #{overall:>3}  {chosen.name:24} {chosen.pos.value:5} "
                      f"vor={plan.best.valuation.vor:6.1f} tier={plan.best.valuation.tier} "
                      f"adp={chosen.espn_adp or 0:5.0f}"
                      + (f"   (passed: {ru.player.name})" if ru else ""))
                if plan.best.note:
                    print(f"        note: {plan.best.note}")
        else:
            chosen = bot_pick(list(available.values()), counts[tid], facts,
                              (overall - 1) // n + 1, rounds, rng)
        available.pop(chosen.espn_id, None)
        rosters[tid].append(chosen)
        counts[tid][chosen.pos] += 1
        room.apply([Pick(overall=overall, team_id=tid, espn_id=chosen.espn_id,
                         pos=chosen.pos, name=chosen.name)])

    ours = best_lineup_points(rosters[my_team_id], facts.settings)
    theirs = [best_lineup_points(rosters[t], facts.settings)
              for t in facts.pick_order if t != my_team_id]
    return ours, rosters[my_team_id], theirs


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", type=int, default=1, help="number of simulated drafts")
    ap.add_argument("--rebuild", action="store_true", help="rebuild the board first")
    args = ap.parse_args()

    bd = board_mod.build() if args.rebuild else board_mod.load()
    me = client().my_team_id
    slot = bd.facts.pick_order.index(me) + 1
    print(f"{bd.facts.settings.name}: {bd.facts.settings.team_count} teams, "
          f"{bd.facts.draftable_spots} rounds, we pick {slot} "
          f"(team {me}) — board built {bd.age_hours():.1f}h ago\n")

    wins, ranks, margins = 0, [], []
    for seed in range(args.n):
        verbose = args.n == 1
        if verbose:
            print("OUR PICKS")
        ours, roster, theirs = run_one(bd, me, seed, verbose)
        avg = sum(theirs) / len(theirs)
        rank = 1 + sum(1 for t in theirs if t > ours)
        wins += int(ours > avg)
        ranks.append(rank)
        margins.append(ours - avg)
        if verbose:
            print(f"\nROSTER BY POSITION: {dict(Counter(p.pos.value for p in roster))}")
            print(f"projected starting points: {ours:.1f}  "
                  f"(field avg {avg:.1f}, best rival {max(theirs):.1f})")
            print(f"finish: {rank} of {bd.facts.settings.team_count}")

    if args.n > 1:
        print(f"\n{args.n} drafts")
        print(f"  beat field average : {wins}/{args.n} ({wins/args.n:.0%})")
        print(f"  mean margin        : {sum(margins)/len(margins):+.1f} pts")
        print(f"  mean finish        : {sum(ranks)/len(ranks):.1f} of "
              f"{bd.facts.settings.team_count}")
        print(f"  1st place          : {ranks.count(1)}")
        print(f"  top 3              : {sum(1 for r in ranks if r <= 3)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
