"""§2.3 / §2.5 — a one-week OUT tag is not a rest-of-season veto.

2026-11-08, week 9: Garrett Wilson was in concussion protocol, OUT for Sunday
and projected back the following week, and his rest-of-season line read
`ros_proj 0.0 / ros_vor 0.0`. §2.5's hard veto is "ruled OUT **for the week in
question**", but `availability()` applied it in the rest-of-season window too,
which zeroed his whole remaining season — the exact mistake §2.3 names ("scaling
the whole projection assumes the slot scores zero in the weeks a player misses").
`value_pool` then left him out of the pool VOR is computed on, so his
`Valuation.vor` fell back to the 0.0 default: exactly replacement level, below
every healthy body on the bench with a real number.

The cost: `choose_drop` named an 88.5%-snap WR1 as the cut behind every single
waiver candidate while a healthy below-him receiver sat on the same bench, and
the sweep escalated "do you want me to spend Garrett Wilson" instead of making
the add.
"""

from __future__ import annotations

from core.manager import waivers
from core.model.durability import availability
from core.model.schema import InjuryStatus, Player, Pos
from core.model.value import value_pool
from tests.test_lineup import pl, settings_10

WEEK = 9
#: 14-week regular season valued in week 9 — this week plus five.
WEEKS_LEFT = 6


def _p(pid: int, pos: Pos, wk: float, *, name: str | None = None,
       status: InjuryStatus = InjuryStatus.ACTIVE,
       back_in: int | None = None) -> Player:
    p = pl(pid, pos, wk, name=name, injury_status=status)
    # ESPN's season row, in-season, is on the games-left scale (see ros_base).
    p.proj_season = wk * WEEKS_LEFT
    if status.cannot_start:
        # ESPN zeroes the week he will not play...
        p.proj_week[WEEK] = 0.0
        # ...and still projects the weeks it expects him back, or zeroes those
        # too when he is done for the year.
        if back_in is not None:
            p.proj_week[WEEK + back_in] = wk
    return p


def _wire() -> list[Player]:
    """A pool deep enough for replacement level to be the real thing: ten
    teams' worth of starters at every position (§2.3)."""
    out: list[Player] = []
    pid = 2000
    for pos, n, top, step in ((Pos.QB, 12, 20.0, 0.6), (Pos.RB, 30, 15.0, 0.4),
                              (Pos.WR, 25, 10.0, 0.4), (Pos.TE, 12, 9.0, 0.5)):
        for i in range(n):
            pid += 1
            out.append(_p(pid, pos, max(0.4, top - step * i)))
    return out


def _roster() -> dict[str, Player]:
    """Week 9's shape: a WR1 out for one week, and one healthy spare body."""
    r = [
        _p(1, Pos.QB, 20.0, name="Allen"),
        _p(2, Pos.RB, 17.0, name="Cook"),
        _p(3, Pos.RB, 10.0, name="White"),
        _p(4, Pos.RB, 7.0, name="Warren"),
        _p(5, Pos.RB, 6.0, name="Etienne"),
        _p(6, Pos.WR, 16.0, name="Jefferson"),
        _p(7, Pos.WR, 15.0, name="Golden"),
        _p(8, Pos.WR, 12.0, name="Burden"),
        _p(9, Pos.WR, 14.0, name="Wilson",
           status=InjuryStatus.OUT, back_in=1),
        # Healthy, above replacement, and the cheapest real body we own — the
        # cut §5.4 should be pricing.
        _p(10, Pos.WR, 4.0, name="Boston"),
        _p(11, Pos.TE, 9.0, name="Kelce"),
    ]
    return {p.name: p for p in r}


def _valued(roster: list[Player], extra: list[Player] | None = None):
    pool = [*roster, *_wire(), *(extra or [])]
    week = value_pool(pool, settings_10(), window="week", week=WEEK)
    ros = value_pool(pool, settings_10(), window="ros",
                     weeks_remaining=WEEKS_LEFT, current_week=WEEK)
    return week, ros


def test_a_one_week_out_tag_does_not_zero_the_rest_of_season():
    by_name = _roster()
    _week, ros = _valued(list(by_name.values()))

    wilson = ros[by_name["Wilson"].espn_id]
    boston = ros[by_name["Boston"].espn_id]

    assert not wilson.vetoed, (
        "§2.5 vetoes a player 'ruled OUT for the week in question' — one week "
        f"is not the rest of the season: {wilson.vetoes}")
    assert wilson.points > 0.0, "a receiver back next week is not worth 0.0 ROS"
    assert 0.0 < wilson.availability < 1.0, (
        "the week he misses is a discount on the surplus (§2.3), not a ban")
    assert boston.vor > 0.0, "fixture check: Boston is above replacement"
    assert wilson.vor > boston.vor, (
        f"a WR1 out for one week ({wilson.vor}) must out-value the cheapest "
        f"healthy body on our bench ({boston.vor})")


def test_a_player_out_with_no_projected_return_is_still_vetoed():
    """The other half of §2.5: no evidence of a return is still a hard veto."""
    by_name = _roster()
    done = _p(12, Pos.WR, 14.0, name="Done", status=InjuryStatus.OUT)
    _week, ros = _valued(list(by_name.values()), extra=[done])

    v = ros[done.espn_id]
    assert v.vetoed and "§2.5" in v.vetoes[0]
    assert v.points == 0.0


def test_out_with_a_projected_return_costs_only_the_missed_weeks():
    """The §2.5 unit, alongside `ir_return_week`: a known return is a discount."""
    healthy = availability(pos=Pos.WR, status=InjuryStatus.ACTIVE, history=[],
                           current_week=WEEK, weeks_remaining=WEEKS_LEFT)
    one_week = availability(pos=Pos.WR, status=InjuryStatus.OUT, history=[],
                            out_return_week=WEEK + 1,
                            current_week=WEEK, weeks_remaining=WEEKS_LEFT)
    assert not one_week.vetoed
    assert 0.0 < one_week.availability < healthy.availability
    assert one_week.components["out_weeks_missed"] == 1.0
    # With no return week the veto stands — §10.6, no fallback guess.
    assert availability(pos=Pos.WR, status=InjuryStatus.OUT, history=[]).vetoed


def test_the_weekly_window_still_refuses_to_start_him():
    """§4.3 is untouched: this week he is OUT for the week in question."""
    by_name = _roster()
    week, _ros = _valued(list(by_name.values()))

    wilson = week[by_name["Wilson"].espn_id]
    assert wilson.vetoed and wilson.points == 0.0


def test_choose_drop_does_not_cut_a_wr1_who_is_back_next_week():
    by_name = _roster()
    roster = list(by_name.values())
    week, ros = _valued(roster)

    drop, _cost, reason, _tradeable = waivers.choose_drop(
        roster, week, settings_10(), current_week=WEEK,
        bench_open=0, ros_valuations=ros,
    )

    assert drop is not None, reason
    assert drop.name != "Wilson", (
        f"core named the WR1 who is back next week as the cut: {reason}")
    assert drop.name == "Boston", reason
