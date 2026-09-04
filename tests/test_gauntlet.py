"""§6.8 — the anti-fleece gauntlet.

Each fleece below is a real way managers get robbed in fantasy leagues, and each
must fail on the SPECIFIC gate designed to catch it. Failing for the wrong
reason would mean the gate that matters isn't working — the trade just happens
to be bad in another way too.

The fair trade must sweep all thirteen, or the gauntlet is a wall rather than a
filter and the trade feature is dead weight.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from core.manager.gauntlet import NewsItem, Offer, run
from core.model.schema import InjuryStatus, LeagueSettings, Player, Pos, RosterSlot
from core.model.value import value_pool

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
LONG_AGO = NOW - timedelta(hours=6)


def settings_10() -> LeagueSettings:
    return LeagueSettings(
        league_id=1, season=2026, name="t", team_count=10, draft_type="SNAKE",
        starting_slots=[
            RosterSlot(name="QB", count=1, eligible=(Pos.QB,)),
            RosterSlot(name="RB", count=2, eligible=(Pos.RB,)),
            RosterSlot(name="WR", count=2, eligible=(Pos.WR,)),
            RosterSlot(name="TE", count=1, eligible=(Pos.TE,)),
            RosterSlot(name="RB/WR/TE", count=1, eligible=(Pos.RB, Pos.WR, Pos.TE)),
        ],
        bench_count=4, ir_count=1, scoring={53: 0.5},
        waiver_type="WAIVERS_TRADITIONAL", faab_budget=None, trade_deadline=None,
        playoff_team_count=6, playoff_weeks=[15, 16, 17],
        regular_season_weeks=14, keeper_count=0,
    )


def pl(pid: int, pos: Pos, pts: float, name: str | None = None, **kw) -> Player:
    return Player(
        espn_id=pid, name=name or f"{pos.value}{pid}", pos=pos, pro_team="XX",
        proj_season=pts, **kw,
    )


def value_all(players: list[Player], s: LeagueSettings):
    return value_pool(players, s, window="ros")


def make_case(our: list[Player], theirs: list[Player],
              incoming: list[Player], outgoing: list[Player], **kw):
    s = settings_10()
    universe = list({p.espn_id: p for p in our + theirs}.values())
    vals = value_all(universe, s)
    offer = Offer(
        offer_id="o1", from_team=3, incoming=incoming, outgoing=outgoing,
        proposed_at=LONG_AGO, their_roster=theirs,
    )
    return run(offer, our, vals, s, now=NOW, first_seen=LONG_AGO, **kw)


# ── a baseline roster pair ───────────────────────────────────────────────────


def rosters():
    """Us: strong at RB, thin at WR. Them: the mirror image."""
    our = (
        [pl(101, Pos.QB, 300)]
        + [pl(110 + i, Pos.RB, 260 - i * 10) for i in range(5)]   # RB surplus
        + [pl(120 + i, Pos.WR, 180 - i * 30) for i in range(2)]   # WR thin
        + [pl(130, Pos.TE, 150)]
    )
    theirs = (
        [pl(201, Pos.QB, 295)]
        + [pl(210 + i, Pos.RB, 175 - i * 25) for i in range(2)]   # RB thin
        + [pl(220 + i, Pos.WR, 265 - i * 8) for i in range(5)]    # WR surplus
        + [pl(230, Pos.TE, 145)]
    )
    return our, theirs


# ── THE FAIR TRADE — must pass all thirteen ──────────────────────────────────


def test_fair_surplus_swap_passes_every_gate():
    """Our spare RB for their spare WR. Both sides genuinely improve, and this
    is the trade the whole feature exists to make."""
    our, theirs = rosters()
    incoming = [next(p for p in theirs if p.espn_id == 220)]   # their WR1, 265
    outgoing = [next(p for p in our if p.espn_id == 111)]      # our RB2, 250

    r = make_case(our, theirs, incoming, outgoing)
    assert r.accepted, f"a fair trade was rejected on {r.failed_on}"
    assert len(r.checks) == 13, f"expected 13 gates, ran {len(r.checks)}"


# ── THE FLEECES — each must fail on ITS OWN gate ─────────────────────────────


def test_fleece_marginal_gain_fails_the_margin_gate():
    """§6.8.1 — a trade that grades +2 is inside our own error bars, and inside
    those bars the sender's read beats ours."""
    our, theirs = rosters()
    # Our WR2 projects 150. This one projects 156 — a genuine but tiny upgrade,
    # the shape of trade that looks free and usually isn't.
    incoming = [pl(299, Pos.WR, 156)]
    outgoing = [next(p for p in our if p.espn_id == 121)]
    theirs = theirs + incoming

    r = make_case(our, theirs, incoming, outgoing)
    assert not r.accepted
    margin = next(c for c in r.checks if c.section == "§6.8.1")
    assert not margin.passed, f"a +6/season upgrade cleared the bar: {margin.detail}"


def test_fleece_they_gain_more_fails_the_both_sides_gate():
    """§6.8.2 — the important one. Good for us, better for them, so we lose it."""
    our, theirs = rosters()
    # They are desperate at RB; our spare RB fixes their lineup far more than
    # their surplus WR fixes ours.
    incoming = [next(p for p in theirs if p.espn_id == 224)]   # their WR5, 233
    outgoing = [next(p for p in our if p.espn_id == 110)]      # our RB1, 260

    r = make_case(our, theirs, incoming, outgoing)
    assert not r.accepted
    assert any(c.section == "§6.8.2" and not c.passed for c in r.checks), r.failed_on


def test_fleece_inexplicable_offer_fails_the_why_gate():
    """§6.8.3 — if you cannot write down why they sent it, you have not found
    the logic yet, and the logic you cannot see is never in your favour."""
    our, theirs = rosters()
    # They send their best WR for our worst WR, from a roster with no hole our
    # player fills and no surplus they are clearing.
    lone = [pl(301, Pos.QB, 290), pl(302, Pos.RB, 240), pl(303, Pos.RB, 235),
            pl(304, Pos.WR, 270), pl(305, Pos.WR, 265), pl(306, Pos.TE, 160)]
    incoming = [lone[3]]
    outgoing = [next(p for p in our if p.espn_id == 121)]

    r = make_case(our, lone, incoming, outgoing)
    assert any(c.section == "§6.8.3" and not c.passed for c in r.checks), r.failed_on


def test_fleece_offered_right_after_news_fails_the_information_gate():
    """§6.8.4 — offering before the report gets around is how a sharp manager
    takes a player off a dead-money owner."""
    our, theirs = rosters()
    incoming = [next(p for p in theirs if p.espn_id == 220)]
    outgoing = [next(p for p in our if p.espn_id == 111)]

    news = [NewsItem(
        espn_id=220, published_at=NOW - timedelta(hours=3),
        headline="WR220 leaves practice with a hamstring injury", source="beat",
    )]
    r = make_case(our, theirs, incoming, outgoing, news=news)
    assert not r.accepted
    assert any(c.section == "§6.8.4" and not c.passed for c in r.checks)


def test_fleece_hurt_star_fails_the_health_gate():
    """§6.8.5 — the single most common fleece: a name-brand player who is
    quietly hurt. The name still reads as valuable."""
    our, theirs = rosters()
    star = pl(400, Pos.WR, 290, name="Injured Star",
              injury_status=InjuryStatus.OUT)
    theirs = theirs + [star]
    outgoing = [next(p for p in our if p.espn_id == 110)]

    r = make_case(our, theirs, [star], outgoing)
    assert not r.accepted
    assert any(c.section == "§6.8.5" and not c.passed for c in r.checks)


def test_fleece_two_for_one_fails_the_consolidation_gate():
    """§6.8.6 — three WR3s do not replace a WR1. You can only start so many."""
    our, theirs = rosters()
    incoming = [pl(410, Pos.WR, 185), pl(411, Pos.WR, 180), pl(412, Pos.WR, 175)]
    theirs = theirs + incoming
    outgoing = [next(p for p in our if p.espn_id == 110)]   # our RB1

    r = make_case(our, theirs, incoming, outgoing)
    assert not r.accepted
    assert any(c.section == "§6.8.6" and not c.passed for c in r.checks), r.failed_on


def test_fleece_playoff_bye_fails_the_schedule_gate():
    """§6.8.8 — value concentrated in weeks we may not be playing."""
    our, theirs = rosters()
    incoming = [pl(420, Pos.WR, 275, bye_week=16)]
    theirs = theirs + incoming
    outgoing = [next(p for p in our if p.espn_id == 111)]

    r = make_case(our, theirs, incoming, outgoing)
    assert any(c.section == "§6.8.8" and not c.passed for c in r.checks)


# ── the structural rules ─────────────────────────────────────────────────────


def test_missing_valuation_rejects_immediately():
    """§6.8.11 — never estimate a value in order to clear a gate."""
    our, theirs = rosters()
    ghost = pl(999, Pos.WR, 250, name="Unknown Player")
    s = settings_10()
    vals = value_all(our + theirs, s)   # ghost deliberately absent
    offer = Offer(offer_id="o2", from_team=3, incoming=[ghost],
                  outgoing=[our[1]], proposed_at=LONG_AGO, their_roster=theirs)
    r = run(offer, our, vals, s, now=NOW, first_seen=LONG_AGO)
    assert not r.accepted
    assert r.checks[0].section == "§6.8.11"
    assert len(r.checks) == 1, "must short-circuit before computing on invented numbers"


def test_cooldown_blocks_an_immediate_accept():
    """§6.8.9 — 'accepting this in the next hour only' is a red flag."""
    our, theirs = rosters()
    incoming = [next(p for p in theirs if p.espn_id == 220)]
    outgoing = [next(p for p in our if p.espn_id == 111)]
    s = settings_10()
    vals = value_all(our + theirs, s)
    offer = Offer(offer_id="o3", from_team=3, incoming=incoming, outgoing=outgoing,
                  proposed_at=NOW, their_roster=theirs)
    r = run(offer, our, vals, s, now=NOW, first_seen=NOW)
    assert not r.accepted
    assert any(c.section == "§6.8.9" and not c.passed for c in r.checks)


def test_weekly_accept_cap_blocks(monkeypatch):
    """§6.8.10."""
    our, theirs = rosters()
    incoming = [next(p for p in theirs if p.espn_id == 220)]
    outgoing = [next(p for p in our if p.espn_id == 111)]
    r = make_case(our, theirs, incoming, outgoing, accepts_this_week=1)
    assert not r.accepted
    assert any(c.section == "§6.8.10" and not c.passed for c in r.checks)


def test_same_manager_cooldown_blocks():
    our, theirs = rosters()
    incoming = [next(p for p in theirs if p.espn_id == 220)]
    outgoing = [next(p for p in our if p.espn_id == 111)]
    r = make_case(our, theirs, incoming, outgoing,
                  last_accept_from_them=NOW - timedelta(days=3))
    assert not r.accepted
    assert any(c.section == "§6.8.10" and not c.passed for c in r.checks)


def test_unknown_opposing_roster_is_a_rejection_not_a_pass():
    """§6.8.2 — we cannot rule out a fleece we cannot compute."""
    our, theirs = rosters()
    s = settings_10()
    vals = value_all(our + theirs, s)
    offer = Offer(offer_id="o4", from_team=3,
                  incoming=[next(p for p in theirs if p.espn_id == 220)],
                  outgoing=[next(p for p in our if p.espn_id == 111)],
                  proposed_at=LONG_AGO, their_roster=[])
    r = run(offer, our, vals, s, now=NOW, first_seen=LONG_AGO)
    assert not r.accepted
    assert any(c.section == "§6.8.2" and not c.passed for c in r.checks)


def test_one_failure_is_enough():
    """§6.8.0 — no averaging, no overall score."""
    our, theirs = rosters()
    incoming = [next(p for p in theirs if p.espn_id == 220)]
    outgoing = [next(p for p in our if p.espn_id == 111)]
    r = make_case(our, theirs, incoming, outgoing, accepts_this_week=99)
    failed = [c for c in r.checks if not c.passed]
    assert len(failed) == 1 and not r.accepted, (
        "a single failed gate must reject even when twelve others pass"
    )
