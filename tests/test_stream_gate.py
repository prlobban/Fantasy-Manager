"""§5.10 / D6.4 — never stream into a slot that is already settled.

A K or D/ST is a one-week rental valued on this week's matchup (D6.1), and its
drop is always the incumbent (D6.3). Once the incumbent's game has kicked off,
ESPN will neither bench nor drop him: the add cannot score, and the drop lands
on an innocent third player.

2026-09-12 did exactly that — Boswell added over a locked Mevis, the drop fell
through to the Browns D/ST, and the roster carried two kickers into a week
whose K slot was frozen at 0.85 points. One add and one bench spot, for nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from core.manager.waivers import stream_slot_settled
from core.model.schema import Player, Pos, ProGame

NOW = datetime.now(UTC)


def pl(pid, pos, name, *, played: bool) -> Player:
    p = Player(espn_id=pid, name=name, pos=pos, pro_team="XX")
    p.games = {
        1: ProGame(
            week=1,
            kickoff=NOW - timedelta(hours=20) if played else NOW + timedelta(days=2),
            stats_official=played,
        )
    }
    return p


def test_a_locked_incumbent_settles_the_slot():
    """🔴 The Mevis case."""
    roster = [pl(1, Pos.K, "Mevis", played=True)]
    stuck = stream_slot_settled(Pos.K, roster, 1)
    assert stuck is not None and stuck.name == "Mevis"


def test_a_live_incumbent_leaves_the_slot_streamable():
    roster = [pl(1, Pos.K, "Mevis", played=False)]
    assert stream_slot_settled(Pos.K, roster, 1) is None


def test_one_live_incumbent_is_enough_to_keep_it_open():
    """Two kickers, one still to play — the stream can still land."""
    roster = [pl(1, Pos.K, "Locked", played=True),
              pl(2, Pos.K, "Playing", played=False)]
    assert stream_slot_settled(Pos.K, roster, 1) is None


def test_an_empty_slot_is_a_free_fill():
    """No incumbent to drop, so nothing is settled and nothing is wasted."""
    assert stream_slot_settled(Pos.K, [pl(1, Pos.RB, "a back", played=True)], 1) is None


def test_defences_are_streamed_too():
    roster = [pl(1, Pos.DST, "Browns D/ST", played=True)]
    assert stream_slot_settled(Pos.DST, roster, 1) is not None


def test_kept_positions_are_untouched_by_this_rule():
    """§5.10 is about rentals. A locked RB does not block an RB add — that is a
    rest-of-season decision and §4.8 already zeroes its weekly gain."""
    roster = [pl(1, Pos.RB, "a locked back", played=True)]
    assert stream_slot_settled(Pos.RB, roster, 1) is None
