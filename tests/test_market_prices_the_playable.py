"""§6.3 / D9 — the trade market does not pay for a player who cannot play.

2026-11-05: the Week 9 sweep escalated "McKenfrey holds Kenneth Walker III, and
at his market value of 98 no package clears the fairness floor without sending
Kelce plus Burden plus Warren". Behind that number: `value_pool` keeps a vetoed
player (IR with no designated return, OUT, suspended) out of the pool VOR is
computed on, so his `Valuation.vor` falls back to the 0.0 default — exactly
replacement level. By week 9 only ~70 of ~320 valued players are ABOVE
replacement, so that default seated every unplayable body in the top quarter of
`trades_out.market_values`' rest-of-season rank: an IR'd back priced at 22.3 of
market value while the healthy below-replacement receiver we would actually put
in a package priced at 2.6, behind all 31 of them.

`core/model/vor.py` already refuses this on a draft board ("in the dead rounds,
where every real player is negative, [0.0] would float him to the TOP") — but
that guard lives in `compute_vor`, which never sees a vetoed player.
"""

from __future__ import annotations

from core.manager import trades_out
from core.model.schema import InjuryStatus, Player, Pos
from core.model.value import value_pool
from tests.test_lineup import pl, settings_10

WEEK = 9
#: 14-week regular season, valued in week 9. At `trades.market_adp_decay_weeks`
#: 8 the ADP half of `market.market_rank` is fully decayed out by now, so the
#: rest-of-season rank is the whole price.
WEEKS_LEFT = 6


def _p(pid: int, pos: Pos, wk: float, *, team: int | None = None,
       status: InjuryStatus = InjuryStatus.ACTIVE) -> Player:
    p = pl(pid, pos, wk, injury_status=status)
    # ESPN's season row, in-season, is on the games-left scale (see ros_base).
    p.proj_season = wk * 10.0
    p.on_team_id = team
    p.espn_adp = float(pid - 1000)
    return p


def _league() -> tuple[list[Player], Player, Player]:
    """Ten rosters plus a 200-deep wire, the shape of the live week-9 pool.

    Returns the pool, an IR'd back with no designated return sitting on a
    bench, and a healthy receiver who is below replacement — the kind of body
    a 2-for-1 package is actually built out of.
    """
    pool: list[Player] = []
    pid = 1000

    def add(pos, wk, *, team=None, status=InjuryStatus.ACTIVE):
        nonlocal pid
        pid += 1
        pool.append(_p(pid, pos, wk, team=team, status=status))
        return pool[-1]

    for t in range(1, 11):
        add(Pos.QB, 22.0 - 0.8 * t, team=t)
        for i in range(3):
            add(Pos.RB, 19.0 - 0.55 * (3 * (t - 1) + i), team=t)
        for i in range(4):
            add(Pos.WR, 18.0 - 0.33 * (4 * (t - 1) + i), team=t)
        for i in range(2):
            add(Pos.TE, 13.0 - 0.33 * (2 * (t - 1) + i), team=t)
        add(Pos.K, 10.0 - 0.2 * t, team=t)
        add(Pos.DST, 9.0 - 0.2 * t, team=t)

    ir_back = add(Pos.RB, 18.0, team=5, status=InjuryStatus.IR)
    healthy_wr = [p for p in pool if p.pos is Pos.WR][-6]

    # The wire, thirty of whom are OUT or on IR — an ordinary week-9 ESPN pool.
    for i in range(200):
        pos = [Pos.RB, Pos.WR, Pos.TE, Pos.QB, Pos.K, Pos.DST][i % 6]
        if i < 30:
            add(pos, max(0.5, 14.0 - 0.1 * i),
                status=InjuryStatus.OUT if i % 2 else InjuryStatus.IR)
        else:
            add(pos, max(0.5, 9.0 - 0.04 * i))

    return pool, ir_back, healthy_wr


def _priced():
    pool, ir_back, healthy_wr = _league()
    vals = value_pool(pool, settings_10(), window="ros",
                      weeks_remaining=WEEKS_LEFT, current_week=WEEK)
    mv = trades_out.market_values(pool, vals, week=WEEK)
    return pool, vals, mv, ir_back, healthy_wr


def test_an_unplayable_player_is_not_worth_more_than_a_healthy_one():
    pool, vals, mv, ir_back, healthy_wr = _priced()

    assert vals[ir_back.espn_id].vetoed, "the IR'd back should be vetoed (§2.5)"
    assert not vals[healthy_wr.espn_id].vetoed
    assert vals[healthy_wr.espn_id].vor < 0, (
        "this receiver is meant to be below replacement — the body a package "
        "is built from, not a starter")

    assert mv[ir_back.espn_id] < mv[healthy_wr.espn_id], (
        f"a back on IR with no designated return is priced at "
        f"{mv[ir_back.espn_id]} of market value against {mv[healthy_wr.espn_id]} "
        f"for a healthy receiver who can actually fill the slot")


def test_the_market_rank_seats_every_playable_body_above_the_unplayable():
    pool, vals, mv, _ir, _wr = _priced()

    vetoed = [p for p in pool if vals[p.espn_id].vetoed]
    playable = [p for p in pool if not vals[p.espn_id].vetoed]
    assert vetoed and playable

    worst_playable = min(mv[p.espn_id] for p in playable)
    over = sorted((p for p in vetoed if mv[p.espn_id] > worst_playable),
                  key=lambda p: -mv[p.espn_id])
    assert not over, (
        f"{len(over)} players who cannot play are priced above the cheapest "
        f"playable body ({worst_playable}): "
        + ", ".join(f"{p.name} {mv[p.espn_id]}" for p in over[:5]))
