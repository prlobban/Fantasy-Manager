"""Shared fixtures. Synthetic leagues so the model is testable with no cookies."""

from __future__ import annotations

import pytest

from core.model.schema import LeagueSettings, Player, Pos, RosterSlot


def _slots(flex: int = 1, superflex: bool = False) -> list[RosterSlot]:
    s = [
        RosterSlot(name="QB", count=1, eligible=(Pos.QB,)),
        RosterSlot(name="RB", count=2, eligible=(Pos.RB,)),
        RosterSlot(name="WR", count=2, eligible=(Pos.WR,)),
        RosterSlot(name="TE", count=1, eligible=(Pos.TE,)),
        RosterSlot(name="K", count=1, eligible=(Pos.K,)),
        RosterSlot(name="D/ST", count=1, eligible=(Pos.DST,)),
    ]
    if flex:
        s.append(RosterSlot(name="RB/WR/TE", count=flex, eligible=(Pos.RB, Pos.WR, Pos.TE)))
    if superflex:
        s.append(RosterSlot(name="OP", count=1, eligible=(Pos.QB, Pos.RB, Pos.WR, Pos.TE)))
    return s


def make_settings(
    *,
    teams: int = 12,
    ppr: float = 1.0,
    flex: int = 1,
    superflex: bool = False,
) -> LeagueSettings:
    return LeagueSettings(
        league_id=1526991210,
        season=2026,
        name="test",
        team_count=teams,
        draft_type="SNAKE",
        starting_slots=_slots(flex=flex, superflex=superflex),
        bench_count=7,
        ir_count=1,
        scoring={53: ppr, 24: 0.1, 42: 0.1},  # receptions, rush yds, rec yds
        waiver_type="FAAB",
        faab_budget=100,
        trade_deadline=None,
        playoff_team_count=6,
        playoff_weeks=[15, 16, 17],
        regular_season_weeks=14,
        keeper_count=0,
    )


@pytest.fixture
def settings() -> LeagueSettings:
    return make_settings()


@pytest.fixture
def superflex_settings() -> LeagueSettings:
    return make_settings(superflex=True)


def make_player(
    espn_id: int,
    pos: Pos,
    proj: float,
    *,
    name: str | None = None,
    **kw,
) -> Player:
    return Player(
        espn_id=espn_id,
        name=name or f"{pos.value}{espn_id}",
        pos=pos,
        pro_team=kw.pop("pro_team", "FA"),
        proj_season=proj,
        **kw,
    )


def linear_pool(pos: Pos, n: int, top: float, step: float, start_id: int) -> list[Player]:
    """n players at one position, projections descending by a fixed step."""
    return [
        make_player(start_id + i, pos, top - i * step)
        for i in range(n)
    ]


@pytest.fixture
def pool() -> list[Player]:
    """A pool with a deliberately different shape per position, so replacement
    level and tiers have something real to bite on."""
    return (
        linear_pool(Pos.QB, 30, 380, 6, 1000)   # flat: QB12 is close to QB1
        + linear_pool(Pos.RB, 60, 320, 4, 2000)  # steep early
        + linear_pool(Pos.WR, 70, 300, 3, 3000)
        + linear_pool(Pos.TE, 25, 240, 9, 4000)  # one cliff at the top
        + linear_pool(Pos.K, 20, 140, 1.5, 5000)
        + linear_pool(Pos.DST, 20, 130, 2, 6000)
    )
