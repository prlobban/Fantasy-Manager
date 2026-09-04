"""Full-draft simulation against ADP bots.

This exercises the SAME picker code the live loop uses — there is no test-only
path. It is the closest thing to a dress rehearsal that doesn't need a draft
room, and it is what catches "the engine produces a legal but stupid roster."

The bots draft near ADP with noise, which is roughly how a real casual league
behaves and is exactly the population our survival model assumes.
"""

from __future__ import annotations

import random

import pytest

from core.draft import picker
from core.draft.room import Pick, RoomModel
from core.espn.settings import LeagueFacts
from core.model.schema import LeagueSettings, Player, Pos, RosterSlot
from core.model.value import value_pool

# ── a synthetic league that matches Pearce's real one ────────────────────────


def make_facts(teams: int = 10) -> LeagueFacts:
    slots = [
        RosterSlot(name="QB", count=1, eligible=(Pos.QB,)),
        RosterSlot(name="RB", count=2, eligible=(Pos.RB,)),
        RosterSlot(name="WR", count=2, eligible=(Pos.WR,)),
        RosterSlot(name="TE", count=1, eligible=(Pos.TE,)),
        RosterSlot(name="D/ST", count=1, eligible=(Pos.DST,)),
        RosterSlot(name="K", count=1, eligible=(Pos.K,)),
        RosterSlot(name="RB/WR/TE", count=1, eligible=(Pos.RB, Pos.WR, Pos.TE)),
    ]
    s = LeagueSettings(
        league_id=1, season=2026, name="sim", team_count=teams, draft_type="SNAKE",
        starting_slots=slots, bench_count=4, ir_count=1,
        scoring={53: 0.5, 24: 0.1, 42: 0.1},
        waiver_type="WAIVERS_TRADITIONAL", faab_budget=None, trade_deadline=None,
        playoff_team_count=6, playoff_weeks=[15, 16, 17],
        regular_season_weeks=14, keeper_count=0,
    )
    order = list(range(1, teams + 1))
    random.Random(0).shuffle(order)
    return LeagueFacts(
        settings=s,
        position_limits={Pos.QB: 2, Pos.RB: 4, Pos.WR: 6, Pos.TE: 3, Pos.K: 2, Pos.DST: 2},
        seconds_per_pick=90, pick_order=order, draft_at=None,
        acquisition_type="WAIVERS_TRADITIONAL", using_acquisition_budget=False,
        waiver_process_days=[], trade_revision_hours=24, veto_votes_required=5,
        playoff_seeding_rule="TOTAL_POINTS_SCORED",
    )


def make_pool(rng: random.Random) -> list[Player]:
    """A pool shaped like a real one: steep RB, deep WR, flat QB, cliffed TE."""
    shape = [
        (Pos.QB, 30, 380, 5.0, 1000),
        (Pos.RB, 70, 320, 3.2, 2000),
        (Pos.WR, 85, 300, 2.4, 3000),
        (Pos.TE, 30, 250, 7.0, 4000),
        (Pos.K, 20, 145, 1.2, 5000),
        (Pos.DST, 20, 140, 1.8, 6000),
    ]
    players: list[Player] = []
    for pos, n, top, step, base in shape:
        for i in range(n):
            players.append(
                Player(
                    espn_id=base + i,
                    name=f"{pos.value}{i + 1}",
                    pos=pos,
                    pro_team=f"T{i % 32}",
                    proj_season=max(0.0, top - i * step + rng.gauss(0, 3)),
                    bye_week=(i % 14) + 4,
                )
            )
    # ADP follows a VOR-ish consensus: rank the pool the way a room would.
    ranked = sorted(players, key=lambda p: -p.proj_season)
    qb_seen = te_seen = 0
    board = []
    for p in ranked:
        # crude consensus: push QBs and TEs down the way real ADP does
        penalty = 0.0
        if p.pos is Pos.QB:
            qb_seen += 1
            penalty = 60 if qb_seen <= 12 else 20
        elif p.pos is Pos.TE:
            te_seen += 1
            penalty = 25 if te_seen > 3 else 0
        elif p.pos in (Pos.K, Pos.DST):
            penalty = 500
        board.append((p, p.proj_season - penalty))
    board.sort(key=lambda pv: -pv[1])
    for i, (p, _) in enumerate(board, start=1):
        p.espn_adp = float(i)
        p.adp_stdev = max(3.0, 0.18 * i)
    return players


#: Dedicated starting slots a competent manager fills before taking depth.
_STARTERS = {Pos.QB: 1, Pos.RB: 2, Pos.WR: 2, Pos.TE: 1, Pos.K: 1, Pos.DST: 1}


def bot_pick(available: list[Player], roster: dict[Pos, int], facts: LeagueFacts,
             rnd: int, rounds: int, rng: random.Random) -> Player:
    """A competent ADP drafter, not a naive one.

    Deliberately not a pushover: it follows ADP with noise, respects position
    caps, fills mandatory slots before the draft runs out, avoids stacking a
    position it can't start, and doesn't take K/DST early. Beating a bot that
    drafts three kickers would prove nothing about the engine.
    """
    limits = facts.position_limits
    rounds_left = rounds - rnd + 1

    legal = [p for p in available if roster.get(p.pos, 0) < limits.get(p.pos, 99)]
    if not legal:
        legal = list(available)

    # Endgame: fill mandatory starting slots before the draft ends.
    missing = {pos: n - roster.get(pos, 0) for pos, n in _STARTERS.items()
               if roster.get(pos, 0) < n}
    if rounds_left <= sum(missing.values()) and missing:
        forced = [p for p in legal if p.pos in missing]
        if forced:
            legal = forced
    elif rnd <= rounds - 2:
        legal = [p for p in legal if p.pos not in (Pos.K, Pos.DST)] or legal

    # Don't stack a position that's already covered, most of the time.
    if rng.random() < 0.8:
        unstacked = [
            p for p in legal
            if roster.get(p.pos, 0) < _STARTERS.get(p.pos, 1) + (1 if p.pos in
               (Pos.RB, Pos.WR) else 0)
        ]
        if unstacked:
            legal = unstacked

    window = sorted(legal, key=lambda p: p.espn_adp or 9999)[:6]
    weights = [max(0.05, 1.0 / (i + 1)) for i in range(len(window))]
    return rng.choices(window, weights=weights, k=1)[0]


def starting_points(roster: list[Player], settings: LeagueSettings) -> float:
    """Best legal starting lineup's projected points — the only score that counts."""
    by_pos: dict[Pos, list[Player]] = {}
    for p in roster:
        by_pos.setdefault(p.pos, []).append(p)
    for v in by_pos.values():
        v.sort(key=lambda p: -p.proj_season)

    used: set[int] = set()
    total = 0.0
    # Dedicated slots first, then flex from whatever is left.
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


def simulate(seed: int, our_slot: int = 4, teams: int = 10) -> tuple[float, list[Player], list[float]]:
    rng = random.Random(seed)
    facts = make_facts(teams)
    pool = make_pool(rng)
    vals = value_pool(pool, facts.settings, window="ros")
    rows = sorted(
        [(p, vals[p.espn_id]) for p in pool], key=lambda pv: -pv[1].vor
    )

    order = facts.pick_order
    me = order[our_slot - 1]
    room = RoomModel(facts=facts, my_team_id=me)

    available = {p.espn_id: p for p in pool}
    rosters: dict[int, list[Player]] = {t: [] for t in order}
    pos_counts: dict[int, dict[Pos, int]] = {t: {} for t in order}
    rounds = facts.draftable_spots

    for overall in range(1, rounds * teams + 1):
        tid = room.team_on_clock(overall)
        if tid == me:
            plan = picker.rank(rows, room)
            assert plan.best is not None, f"picker returned nothing at pick {overall}"
            chosen = plan.best.player
        else:
            chosen = bot_pick(
                list(available.values()), pos_counts[tid], facts,
                (overall - 1) // teams + 1, rounds, rng,
            )
        available.pop(chosen.espn_id, None)
        rosters[tid].append(chosen)
        pos_counts[tid][chosen.pos] = pos_counts[tid].get(chosen.pos, 0) + 1
        room.apply([Pick(overall=overall, team_id=tid, espn_id=chosen.espn_id,
                         pos=chosen.pos, name=chosen.name)])

    ours = starting_points(rosters[me], facts.settings)
    theirs = [starting_points(rosters[t], facts.settings) for t in order if t != me]
    return ours, rosters[me], theirs


# ── the tests ────────────────────────────────────────────────────────────────


def test_roster_is_always_legal():
    facts = make_facts()
    for seed in range(8):
        _, roster, _ = simulate(seed)
        counts: dict[Pos, int] = {}
        for p in roster:
            counts[p.pos] = counts.get(p.pos, 0) + 1
        assert len(roster) == facts.draftable_spots
        for pos, cap in facts.position_limits.items():
            assert counts.get(pos, 0) <= cap, f"seed {seed}: {pos.value} over cap"


def test_we_always_fill_every_starting_slot():
    """A roster that can't field a legal lineup is a failed draft, whatever the
    projections say."""
    facts = make_facts()
    for seed in range(8):
        _, roster, _ = simulate(seed)
        counts: dict[Pos, int] = {}
        for p in roster:
            counts[p.pos] = counts.get(p.pos, 0) + 1
        for pos in (Pos.QB, Pos.TE, Pos.K, Pos.DST):
            assert counts.get(pos, 0) >= 1, f"seed {seed}: no {pos.value} drafted"
        assert counts.get(Pos.RB, 0) >= 2, f"seed {seed}: fewer than 2 RB"
        assert counts.get(Pos.WR, 0) >= 2, f"seed {seed}: fewer than 2 WR"


def test_no_kicker_or_dst_before_the_last_two_rounds():
    facts = make_facts()
    rounds = facts.draftable_spots
    for seed in range(6):
        rng_room = RoomModel(facts=facts, my_team_id=facts.pick_order[3])
        _, roster, _ = simulate(seed)
        # roster is in pick order, so index gives the round
        for i, p in enumerate(roster):
            rnd = i + 1
            if p.pos in (Pos.K, Pos.DST):
                assert rnd > rounds - 2, (
                    f"seed {seed}: took {p.pos.value} in round {rnd} of {rounds}"
                )


def test_we_beat_the_adp_bots_most_of_the_time():
    """The headline quality check. Not a truth — a floor. If value-based
    drafting with scarcity and survival modelling cannot beat naive ADP in a
    clear majority of drafts, something in the engine is wrong."""
    wins = 0
    trials = 25
    margins = []
    for seed in range(trials):
        ours, _, theirs = simulate(seed)
        avg = sum(theirs) / len(theirs)
        margins.append(ours - avg)
        if ours > avg:
            wins += 1
    rate = wins / trials
    mean_margin = sum(margins) / len(margins)
    assert rate >= 0.70, (
        f"only beat the ADP field in {rate:.0%} of drafts "
        f"(mean margin {mean_margin:+.1f} pts)"
    )


def test_we_are_usually_top_three_in_the_league():
    top3 = 0
    trials = 20
    for seed in range(trials):
        ours, _, theirs = simulate(seed)
        rank = 1 + sum(1 for t in theirs if t > ours)
        if rank <= 3:
            top3 += 1
    assert top3 / trials >= 0.6, f"top-3 in only {top3}/{trials} drafts"


@pytest.mark.parametrize("slot", [1, 4, 7, 10])
def test_engine_works_from_any_draft_slot(slot):
    ours, roster, theirs = simulate(seed=42, our_slot=slot)
    assert len(roster) == make_facts().draftable_spots
    assert ours > 0
