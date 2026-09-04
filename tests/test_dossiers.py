"""§3.2 — the rules that decide whether research may move the board.

The retired `predraft` validator asked for a `source` and accepted the string
"ESPN". These tests pin the replacement: a claim that cannot be checked does
not get to move a draft board, and the failure mode is losing the CLAIM, never
silently repairing it into something the model did not say.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from core.draft import dossiers as d


def make(**over) -> dict:
    """A valid dossier, overridden per test.

    `projection_check.direction` defaults to whatever is CONSISTENT with the
    multiplier, so a test about the sourcing rules is not silently also a test
    about the self-consistency rule. Pass `projection_check` explicitly to
    exercise that one.
    """
    base = {
        "espn_id": 4430807,
        "name": "Bijan Robinson",
        "durability": {"verdict": "clean", "detail": "no missed games"},
        "role": {"verdict": "locked", "detail": "bell cow"},
        "news_since": [],
        "multiplier": 1.0,
        "veto": False,
        "veto_reason": None,
        "confidence": "high",
        "sources": ["https://www.espn.com/nfl/story/_/id/1"],
        "researched_at": datetime.now(UTC).isoformat(),
    }
    base.update(over)
    if "projection_check" not in base:
        m = base["multiplier"]
        direction = "fair" if m == 1.0 else ("low" if m > 1.0 else "high")
        base["projection_check"] = {"direction": direction, "why": "test fixture"}
    return base


# ── sources must be checkable ────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["ESPN", "reports", "", "espn.com", "not a url",
                                 "ftp://espn.com/x", "https://", "http://localhost"])
def test_unsourced_dossier_is_rejected(bad):
    """The exact failure the old validator passed: a source that is a word."""
    got, problems = d.validate(make(sources=[bad]))
    assert got is None
    assert any("verifiable source" in p for p in problems)


def test_a_real_url_survives():
    got, problems = d.validate(make())
    assert got is not None
    assert problems == []
    assert got.hosts == {"espn.com"}


def test_junk_sources_are_dropped_but_good_ones_kept():
    got, problems = d.validate(make(
        sources=["ESPN", "https://www.nfl.com/news/x"]))
    assert got is not None
    assert got.sources == ["https://www.nfl.com/news/x"]
    assert any("not URLs" in p for p in problems)


def test_www_is_stripped_so_hosts_dedupe():
    got, _ = d.validate(make(sources=["https://www.espn.com/a",
                                      "https://espn.com/b"]))
    assert got.hosts == {"espn.com"}


# ── the veto bar ─────────────────────────────────────────────────────────────

def test_veto_needs_two_independent_hosts():
    got, problems = d.validate(make(
        veto=True, veto_reason="torn ACL", confidence="high",
        sources=["https://www.espn.com/a", "https://www.espn.com/b"]))
    assert got.veto is False, "one outlet must not be able to erase a player"
    assert got.veto_reason is None
    assert any("veto dropped" in p for p in problems)


def test_veto_needs_high_confidence():
    got, _ = d.validate(make(
        veto=True, veto_reason="maybe done", confidence="medium",
        sources=["https://www.espn.com/a", "https://www.nfl.com/b"]))
    assert got.veto is False


def test_a_properly_evidenced_veto_stands():
    got, problems = d.validate(make(
        veto=True, veto_reason="torn Achilles, out for the season",
        confidence="high",
        sources=["https://www.espn.com/a", "https://www.nfl.com/b"]))
    assert got.veto is True
    assert problems == []


def test_a_dropped_veto_keeps_the_prose():
    """Demotion costs the claim, not the record — the judge still reads it."""
    got, _ = d.validate(make(
        veto=True, veto_reason="x", confidence="low",
        durability={"verdict": "concern", "detail": "knee, twice"},
        sources=["https://www.espn.com/a"]))
    assert got.veto is False
    assert got.durability["detail"] == "knee, twice"
    assert got.demotions


# ── the multiplier bar ───────────────────────────────────────────────────────

def test_large_multiplier_on_one_host_is_reset():
    got, problems = d.validate(make(
        multiplier=1.15, sources=["https://www.espn.com/a"]))
    assert got.multiplier == 1.0
    assert any("multiplier" in p for p in problems)


def test_large_multiplier_with_two_hosts_stands():
    got, problems = d.validate(make(
        multiplier=1.15,
        sources=["https://www.espn.com/a", "https://www.nfl.com/b"]))
    assert got.multiplier == 1.15
    assert problems == []


def test_small_multiplier_needs_only_one_host():
    """A routine nudge should be cheap; only a real claim needs corroboration."""
    got, problems = d.validate(make(
        multiplier=1.03, sources=["https://www.espn.com/a"]))
    assert got.multiplier == 1.03
    assert problems == []


@pytest.mark.parametrize("mult", [0.95, 1.05])
def test_the_single_source_band_is_inclusive(mult):
    got, _ = d.validate(make(multiplier=mult, sources=["https://www.espn.com/a"]))
    assert got.multiplier == mult


# ── shelf life ───────────────────────────────────────────────────────────────

def test_a_stale_dossier_is_ignored():
    old = (datetime.now(UTC) - timedelta(hours=40)).isoformat()
    got, problems = d.validate(make(researched_at=old), max_age_hours=30)
    assert got is None
    assert any("stale" in p for p in problems)


def test_a_fresh_dossier_survives():
    recent = (datetime.now(UTC) - timedelta(hours=6)).isoformat()
    got, _ = d.validate(make(researched_at=recent), max_age_hours=30)
    assert got is not None


def test_an_undated_dossier_is_not_treated_as_fresh_or_stale():
    got, _ = d.validate(make(researched_at=None), max_age_hours=30)
    assert got is not None and got.age_hours() is None


def test_no_espn_id_is_not_a_dossier():
    got, problems = d.validate(make(espn_id="not a number"))
    assert got is None
    assert "no usable espn_id" in problems


# ── the bridge to the board ──────────────────────────────────────────────────

def test_overrides_payload_shape_matches_what_the_board_reads():
    """core.draft.board._apply_overrides is the consumer; this is its contract."""
    ds = {
        1: d.validate(make(espn_id=1, multiplier=1.12,
                           sources=["https://a.com/1", "https://b.com/2"]))[0],
        2: d.validate(make(espn_id=2, veto=True, veto_reason="out for year",
                           confidence="high",
                           sources=["https://a.com/1", "https://b.com/2"]))[0],
        3: d.validate(make(espn_id=3))[0],       # 1.0, no veto -> not emitted
    }
    items = []
    for x in ds.values():
        if x.veto:
            items.append({"espn_id": x.espn_id, "multiplier": 1.0, "veto": True,
                          "reason": x.veto_reason, "source": x.sources[0]})
        elif x.multiplier != 1.0:
            items.append({"espn_id": x.espn_id, "multiplier": x.multiplier,
                          "reason": "", "source": x.sources[0]})

    assert len(items) == 2
    for it in items:
        assert isinstance(it["espn_id"], int)
        assert isinstance(it["multiplier"], float)
        assert it["source"].startswith("https://")


def test_veto_reaches_the_valuation_as_a_veto(settings):
    """End to end through the model: a research veto makes a player ineligible
    the same way a §2.5 suspension does — one mechanism, one enforcement point."""
    from core.model.schema import Pos
    from core.model.value import PlayerContext, value_one
    from tests.conftest import make_player

    p = make_player(1, Pos.RB, 200.0)

    _, clean, _, _, _ = value_one(p, settings, window="ros",
                                  ctx=PlayerContext())
    assert not clean.vetoed

    _, dur, _, _, _ = value_one(
        p, settings, window="ros",
        ctx=PlayerContext(news_veto="torn Achilles, out for the season"))
    assert dur.vetoed
    assert any("research" in v for v in dur.vetoes)


def test_vetoed_player_is_not_draftable(settings, pool):
    """§3.7 — picker._eligible drops him, so he can never enter the queue."""
    from core.model.value import PlayerContext, value_pool

    target = pool[0]
    vals = value_pool(pool, settings, window="ros",
                      contexts={target.espn_id: PlayerContext(news_veto="retired")})
    assert vals[target.espn_id].vetoed


# ── the dossier must agree with itself ───────────────────────────────────────

def two_hosts():
    return ["https://www.espn.com/a", "https://www.nfl.com/b"]


def test_fair_direction_forces_multiplier_to_one():
    """Observed live 2026-09-04: a dossier said 'fair', argued in prose that no
    adjustment was warranted, and sent 0.95 anyway."""
    got, problems = d.validate(make(
        multiplier=0.95, sources=two_hosts(),
        projection_check={"direction": "fair", "why": "no adjustment warranted"}))
    assert got.multiplier == 1.0
    assert any("'fair'" in p for p in problems)


def test_high_direction_may_not_raise_the_projection():
    got, _ = d.validate(make(
        multiplier=1.12, sources=two_hosts(),
        projection_check={"direction": "high", "why": "too generous"}))
    assert got.multiplier == 1.0


def test_low_direction_may_not_cut_the_projection():
    got, _ = d.validate(make(
        multiplier=0.88, sources=two_hosts(),
        projection_check={"direction": "low", "why": "too stingy"}))
    assert got.multiplier == 1.0


def test_a_consistent_cut_stands():
    got, problems = d.validate(make(
        multiplier=0.88, sources=two_hosts(),
        projection_check={"direction": "high", "why": "lost the starting job"}))
    assert got.multiplier == 0.88
    assert problems == []


def test_a_consistent_raise_stands():
    got, problems = d.validate(make(
        multiplier=1.12, sources=two_hosts(),
        projection_check={"direction": "low", "why": "named the starter"}))
    assert got.multiplier == 1.12
    assert problems == []
