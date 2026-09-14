"""§5.9 — which week a forward-looking decision is actually about.

ESPN's `week` stays on the current scoring period until it rolls over on
Tuesday. Between the last game of a week and that rollover, every game in
`week` is played: every weekly projection is settled, so every waiver gain
computes to exactly 0.0 and the sweep cannot recommend anything.

The gap is narrow but it lands on a scheduled run (Tuesday 07:30), and it is
the moment Sunday's breakouts are most worth claiming.

The counter-case matters as much: on 2026-09-14 the Monday sweep also produced
no adds, and that was CORRECT — Denver at Kansas City had not kicked off, so
Week 1 was still live and still the right week to decide on. A rule that
advanced on the calendar rather than on the schedule would have been wrong.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from core.espn.league_state import _decision_week
from core.model.schema import Player, Pos, ProGame

NOW = datetime.now(UTC)


def pl(pid, *, played: bool | None) -> Player:
    """`played=None` means we have no schedule row for this player at all."""
    p = Player(espn_id=pid, name=f"p{pid}", pos=Pos.RB, pro_team="XX")
    if played is not None:
        p.games = {
            1: ProGame(
                week=1,
                kickoff=NOW - timedelta(hours=20) if played else NOW + timedelta(days=3),
                stats_official=played,
            )
        }
    return p


def test_the_week_holds_while_any_game_is_still_to_come():
    """🔴 The live 2026-09-14 case: Sunday is done, MNF is not."""
    pool = [pl(1, played=True), pl(2, played=True), pl(3, played=False)]
    assert _decision_week(1, pool) == 1


def test_the_week_advances_once_the_slate_is_finished():
    pool = [pl(1, played=True), pl(2, played=True), pl(3, played=True)]
    assert _decision_week(1, pool) == 2


def test_one_unplayed_game_is_enough_to_hold_the_week():
    pool = [pl(i, played=True) for i in range(20)] + [pl(99, played=False)]
    assert _decision_week(1, pool) == 1


def test_no_schedule_data_at_all_holds_the_week():
    """Fails SAFE: without schedules this is the pre-§4.8 behaviour, not a
    silent jump to a week we know nothing about."""
    assert _decision_week(1, [pl(1, played=None), pl(2, played=None)]) == 1


def test_players_without_a_schedule_row_do_not_vote():
    """A missing row is unknown, not 'still to play' — otherwise a single
    unmapped defence would pin the week forever."""
    pool = [pl(1, played=True), pl(2, played=True), pl(3, played=None)]
    assert _decision_week(1, pool) == 2


def test_an_empty_pool_holds_the_week():
    assert _decision_week(5, []) == 5
