"""§6.3 / D9 — a deal's market price is the same number wherever core prices it.

2026-10-17: the Week 6 sweep offered Swift + Hubbard to Purdy Mouths for James
Cook III and printed the price twice in one run — the trade board said market
1.03, the gate inside `propose_trade` said 1.24 — and escalated it as "core
disagrees with itself on that number (§8.8)". `trades_out.market_values` took
the rest-of-season rank behind the ADP decay from whatever pool it was handed:
ten rosters from `build`, our two from `value_check`. §6.3's floor is enforced
on that ratio and the agent quotes it to another human, so the board and the
gate have to agree.
"""

from __future__ import annotations

from core.manager import trades_out
from core.model.schema import Pos
from core.model.value import value_pool
from tests.test_lineup import pl, settings_10

WEEK = 6


def _ours():
    """Four backs for two RB slots and a flex — the 10-17 shape."""
    return [
        pl(1, Pos.QB, 21, espn_adp=18.0),
        pl(3, Pos.RB, 14, espn_adp=30.0),
        pl(4, Pos.RB, 11, espn_adp=44.0),
        pl(5, Pos.RB, 9, espn_adp=61.0),
        pl(6, Pos.WR, 15, espn_adp=22.0),
        pl(7, Pos.WR, 12, espn_adp=40.0),
        pl(8, Pos.WR, 9, espn_adp=70.0),
        pl(9, Pos.TE, 10, espn_adp=50.0),
        pl(10, Pos.TE, 6, espn_adp=120.0),
    ]


def _them():
    """Two RB1s for two RB slots: the roster the consolidation is aimed at."""
    return [
        pl(21, Pos.QB, 19, espn_adp=25.0),
        pl(22, Pos.RB, 16, espn_adp=6.0),
        pl(23, Pos.RB, 15, espn_adp=11.0),
        pl(24, Pos.WR, 10, espn_adp=55.0),
        pl(25, Pos.WR, 8, espn_adp=80.0),
        pl(26, Pos.TE, 7, espn_adp=95.0),
    ]


def _filler(base, pts):
    """A third and fourth roster, so the league pool is bigger than two teams."""
    return [
        pl(base + 1, Pos.QB, pts, espn_adp=float(base)),
        pl(base + 2, Pos.RB, pts - 1, espn_adp=float(base + 5)),
        pl(base + 3, Pos.RB, pts - 3, espn_adp=float(base + 9)),
        pl(base + 4, Pos.WR, pts - 2, espn_adp=float(base + 14)),
        pl(base + 5, Pos.WR, pts - 4, espn_adp=float(base + 19)),
        pl(base + 6, Pos.TE, pts - 6, espn_adp=float(base + 24)),
    ]


def _league():
    ours, them = _ours(), _them()
    others = {2: ("Purdy Mouths", them), 3: ("filler 3", _filler(30, 13)),
              4: ("filler 4", _filler(40, 12))}
    pool = list(ours) + [p for _, r in others.values() for p in r]
    vals = value_pool(pool, settings_10(), window="ros",
                      weeks_remaining=9, current_week=WEEK)
    return ours, others, pool, vals


def test_a_players_market_value_does_not_depend_on_who_is_being_priced():
    ours, _others, pool, vals = _league()
    them = _them()

    league_wide = trades_out.market_values(pool, vals, week=WEEK)
    two_rosters = trades_out.market_values(list(ours) + them, vals, week=WEEK)

    for p in list(ours) + them:
        assert two_rosters[p.espn_id] == league_wide[p.espn_id], (
            f"{p.name} is worth {two_rosters[p.espn_id]} to the gate and "
            f"{league_wide[p.espn_id]} to the board")


def test_the_gate_prices_a_board_proposal_at_the_boards_market_ratio():
    ours, others, _pool, vals = _league()

    props = trades_out.build(ours, others, vals, settings_10(), week=WEEK)
    board = next(p for p in props if p.to_team == 2)

    ok, why, _ours_gain, _theirs = trades_out.value_check(
        ours, others[2][1], board.give, board.get, vals, settings_10(),
        week=WEEK, to_team=2)

    assert ok, why
    assert f"market ratio {board.market_ratio:.2f}" in why, (
        f"the board offered this at {board.market_ratio:.2f} and the gate "
        f"priced it differently: {why}")
