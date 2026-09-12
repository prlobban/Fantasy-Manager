"""§4.8 at the write tool — the moves ESPN cannot apply never reach the browser.

The optimiser pins locked players, so the only way an impossible move reaches
`set_lineup` is the agent building one by hand. On 2026-09-12 it did exactly
that, twice: first the pair (bench the locked kicker, start his replacement),
then — after only the locked PLAYER was stripped — the surviving half on its
own, into a slot the locked player still held. The browser applied 0 of 1.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from core.mcp_server import strip_locked_moves
from core.model.schema import Player, Pos, ProGame, RosterSlot

NOW = datetime(2026, 9, 12, 19, 0, tzinfo=UTC)

SLOTS = [
    RosterSlot(name="QB", count=1, eligible=(Pos.QB,)),
    RosterSlot(name="RB", count=2, eligible=(Pos.RB,)),
    RosterSlot(name="K", count=1, eligible=(Pos.K,)),
]


def pl(pid, pos, name, *, locked: bool) -> Player:
    p = Player(espn_id=pid, name=name, pos=pos, pro_team="XX")
    p.games = {
        1: ProGame(
            week=1,
            kickoff=NOW - timedelta(hours=20) if locked else NOW + timedelta(hours=18),
            stats_official=locked,
        )
    }
    return p


MEVIS = pl(1, Pos.K, "Mevis", locked=True)
BOSWELL = pl(2, Pos.K, "Boswell", locked=False)
RB_LOCKED = pl(3, Pos.RB, "Locked RB", locked=True)
RB_FREE = pl(4, Pos.RB, "Free RB", locked=False)
RB_BENCH = pl(5, Pos.RB, "Bench RB", locked=False)

BY_ID = {p.espn_id: p for p in (MEVIS, BOSWELL, RB_LOCKED, RB_FREE, RB_BENCH)}


def test_a_locked_player_is_never_moved():
    kept, skipped, why = strip_locked_moves(
        [{"espn_id": 1, "slot": "BE"}], BY_ID, {1: "K"}, SLOTS, 1
    )
    assert kept == []
    assert skipped == [1]
    assert "Mevis" in why[0]


def test_the_slot_a_locked_player_holds_cannot_receive_anyone():
    """🔴 The futile half: 'Boswell -> K' while Mevis is locked into K."""
    kept, skipped, why = strip_locked_moves(
        [{"espn_id": 2, "slot": "K"}], BY_ID, {1: "K"}, SLOTS, 1
    )
    assert kept == []
    assert skipped == [2]
    assert any("K" in w for w in why)


def test_the_whole_pair_is_stripped_together():
    kept, skipped, _ = strip_locked_moves(
        [{"espn_id": 1, "slot": "BE"}, {"espn_id": 2, "slot": "K"}],
        BY_ID, {1: "K"}, SLOTS, 1,
    )
    assert kept == []
    assert skipped == [1, 2]


def test_a_slot_with_a_spare_instance_is_still_a_legal_destination():
    """Two RB slots, one locked — the other is open, so the move stands."""
    kept, skipped, _ = strip_locked_moves(
        [{"espn_id": 5, "slot": "RB"}],
        BY_ID, {3: "RB", 4: "RB"}, SLOTS, 1,
    )
    assert kept == [{"espn_id": 5, "slot": "RB"}]
    assert skipped == []


def test_a_legal_move_passes_through_untouched():
    kept, skipped, why = strip_locked_moves(
        [{"espn_id": 5, "slot": "RB"}], BY_ID, {}, SLOTS, 1
    )
    assert kept == [{"espn_id": 5, "slot": "RB"}]
    assert skipped == [] and why == []


def test_legal_moves_survive_alongside_stripped_ones():
    kept, skipped, _ = strip_locked_moves(
        [{"espn_id": 1, "slot": "BE"}, {"espn_id": 5, "slot": "RB"}],
        BY_ID, {1: "K"}, SLOTS, 1,
    )
    assert kept == [{"espn_id": 5, "slot": "RB"}]
    assert skipped == [1]


def test_nothing_is_stripped_before_kickoff():
    """Sunday 11:00 runs before the 12:00 games: everything is still movable."""
    kept, skipped, _ = strip_locked_moves(
        [{"espn_id": 2, "slot": "K"}], BY_ID, {2: "K"}, SLOTS, 1
    )
    assert kept == [{"espn_id": 2, "slot": "K"}]
    assert skipped == []
