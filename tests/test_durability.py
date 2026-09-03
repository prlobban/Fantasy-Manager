"""§2.5 — durability is a discount, not a ban.

The behaviour these tests pin down is the one judgment call in the module:
soft-tissue injuries repeat, clean breaks do not, and one event of any kind is
not a pattern.
"""

from __future__ import annotations

from core.model.durability import InjuryEvent, availability
from core.model.schema import InjuryStatus, Pos


def ev(season: int, missed: int, desc: str) -> InjuryEvent:
    return InjuryEvent(season=season, games_missed=missed, description=desc)


def avail(history, **kw) -> float:
    return availability(
        pos=kw.pop("pos", Pos.RB),
        status=kw.pop("status", InjuryStatus.ACTIVE),
        history=history,
        **kw,
    ).availability


# ── The core distinction ─────────────────────────────────────────────────────


def test_two_soft_tissue_events_are_a_pattern():
    soft = [ev(2025, 3, "hamstring strain"), ev(2024, 2, "calf strain")]
    clean = [ev(2025, 3, "fractured collarbone"), ev(2024, 2, "broken thumb")]
    # Same games missed, very different meaning.
    assert avail(soft) < avail(clean)


def test_one_freak_injury_is_not_a_pattern():
    one = [ev(2025, 4, "broken collarbone"), ev(2024, 0, ""), ev(2023, 0, "")]
    healthy = [ev(2025, 0, ""), ev(2024, 0, ""), ev(2023, 0, "")]
    # It costs him something (he did miss games) but not a recurrence penalty.
    assert avail(one) < avail(healthy)
    assert avail(one) > 0.70


def test_single_soft_tissue_event_is_penalised_less_than_two():
    one = [ev(2025, 3, "hamstring")]
    two = [ev(2025, 3, "hamstring"), ev(2024, 3, "hamstring")]
    assert avail(two) < avail(one)


def test_chronic_injury_compounds():
    acl = [ev(2025, 8, "torn ACL")]
    none = [ev(2025, 8, "illness")]
    assert avail(acl) < avail(none)


# ── Recency ──────────────────────────────────────────────────────────────────


def test_recent_seasons_weigh_more():
    recent_bad = [ev(2025, 8, "illness"), ev(2024, 0, ""), ev(2023, 0, "")]
    old_bad = [ev(2025, 0, ""), ev(2024, 0, ""), ev(2023, 8, "illness")]
    assert avail(recent_bad) < avail(old_bad)


# ── Hard vetoes (§2.5) ───────────────────────────────────────────────────────


def test_ir_without_return_date_is_a_veto():
    r = availability(pos=Pos.WR, status=InjuryStatus.IR, history=[])
    assert r.vetoed and r.availability == 0.0


def test_ir_with_a_return_date_is_not_a_veto_but_costs_the_missed_weeks():
    r = availability(
        pos=Pos.WR,
        status=InjuryStatus.IR,
        history=[],
        ir_return_week=8,
        current_week=4,
        weeks_remaining=14,
    )
    assert not r.vetoed
    assert 0.0 < r.availability < 0.8


def test_ruled_out_is_a_veto():
    assert availability(pos=Pos.RB, status=InjuryStatus.OUT, history=[]).vetoed


def test_long_suspension_is_a_veto_short_one_is_not():
    long_ban = availability(
        pos=Pos.WR, status=InjuryStatus.SUSPENSION, history=[], suspension_through_week=10
    )
    short_ban = availability(
        pos=Pos.WR, status=InjuryStatus.SUSPENSION, history=[], suspension_through_week=3
    )
    assert long_ban.vetoed
    assert not short_ban.vetoed


def test_an_elite_but_injury_prone_player_is_not_vetoed():
    """The rule that keeps us from passing on the best players in football."""
    r = availability(
        pos=Pos.RB,
        status=InjuryStatus.ACTIVE,
        history=[ev(2025, 4, "hamstring"), ev(2024, 3, "hamstring")],
    )
    assert not r.vetoed
    assert r.availability > 0.5  # discounted, still very much draftable


# ── Age ──────────────────────────────────────────────────────────────────────


def test_rb_age_cliff_is_sharper_than_wr():
    old_rb = avail([], pos=Pos.RB, age=31)
    old_wr = avail([], pos=Pos.WR, age=31)
    assert old_rb < old_wr


def test_young_player_gets_no_age_penalty():
    assert avail([], pos=Pos.RB, age=23) == avail([], pos=Pos.RB, age=None)


# ── Honesty about gaps (§8.8) ────────────────────────────────────────────────


def test_no_history_is_flagged_not_assumed_healthy():
    r = availability(pos=Pos.WR, status=InjuryStatus.ACTIVE, history=[])
    assert any("no injury history" in m for m in r.missing)


def test_partial_history_is_flagged():
    r = availability(pos=Pos.WR, status=InjuryStatus.ACTIVE, history=[ev(2025, 0, "")])
    assert any("season(s) of injury history" in m for m in r.missing)


def test_components_explain_the_number():
    r = availability(
        pos=Pos.RB,
        status=InjuryStatus.QUESTIONABLE,
        history=[ev(2025, 3, "hamstring"), ev(2024, 2, "groin")],
        age=29,
    )
    assert "soft_tissue_pattern" in r.components
    assert "age" in r.components
    assert "questionable" in r.components
