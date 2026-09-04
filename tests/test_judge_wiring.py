"""§3.10 — what the DRAFT LOOP does with a verdict.

The practice room cannot rehearse this: ESPN's auto-teams pick instantly, the
observed pace floors at 3s, and the budget correctly computes to zero on every
turn, so the judge never runs and the loop never reads one. That is the budget
logic working, but it leaves the read side unexercised in the one place that
matters. These tests cover it.

The property that must hold in every branch: `consult_judge` always returns a
plan to draft from. Missing file, stale verdict, refused levers, a judge that
crashed — every path ends with a pick, because the alternative is a loop that
waits, and a loop that waits blows the pick (§8.7, §10.2).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.draft import verdict as V
from core.draft.run import consult_judge


@dataclass
class _Val:
    tier: int | None
    vor: float = 10.0


@dataclass
class _Player:
    espn_id: int
    name: str


@dataclass
class _Cand:
    player: _Player
    valuation: _Val
    score: float
    reasons: dict = field(default_factory=dict)
    note: str = ""


@dataclass
class _Plan:
    candidates: list

    def top(self, n):
        return self.candidates[:n]

    @property
    def best(self):
        return self.candidates[0] if self.candidates else None


def a_plan():
    return _Plan([
        _Cand(_Player(1, "Gibbs"), _Val(1), 100.0),
        _Cand(_Player(2, "Bijan"), _Val(1), 95.0),
        _Cand(_Player(3, "Pollard"), _Val(3), 40.0),
    ])


def a_verdict(**over):
    base = {
        "for_overall": 17, "agree": False, "summary": "dossier says Gibbs is out",
        "veto": [{"espn_id": 1, "name": "Gibbs", "reason": "out for the season",
                  "cites": ["§2.5"], "dossier_fact": "torn Achilles, confirmed"}],
    }
    base.update(over)
    return base


# ── the three modes ──────────────────────────────────────────────────────────

def test_off_does_not_even_read_the_file(tmp_path):
    V.write(tmp_path, 17, a_verdict())
    plan = a_plan()
    out, verdict, diff = consult_judge(plan, mode="off", draft_dir=tmp_path,
                                       overall=17)
    assert out is plan and verdict is None and diff is None


def test_shadow_reads_and_reports_but_does_not_change_the_pick(tmp_path):
    """The Saturday setting: the judge is fully exercised and fully inert."""
    V.write(tmp_path, 17, a_verdict())
    plan = a_plan()
    out, verdict, diff = consult_judge(plan, mode="shadow", draft_dir=tmp_path,
                                       overall=17)
    assert out.best.player.name == "Gibbs", "shadow must not move the pick"
    assert verdict is not None and len(verdict.vetoes) == 1
    assert diff and "would have taken Bijan over Gibbs" in diff


def test_live_applies_the_lever(tmp_path):
    V.write(tmp_path, 17, a_verdict())
    out, verdict, diff = consult_judge(a_plan(), mode="live", draft_dir=tmp_path,
                                       overall=17)
    assert out.best.player.name == "Bijan"
    assert diff is None, "a live change is not a shadow diff"


def test_shadow_reports_nothing_when_the_judge_agrees(tmp_path):
    V.write(tmp_path, 17, {"for_overall": 17, "agree": True, "summary": "fine"})
    out, verdict, diff = consult_judge(a_plan(), mode="shadow",
                                       draft_dir=tmp_path, overall=17)
    assert out.best.player.name == "Gibbs"
    assert verdict is not None and verdict.agree
    assert diff is None


# ── every failure still yields a pick ────────────────────────────────────────

def test_no_verdict_file_still_yields_a_pick(tmp_path):
    """The common case: the judge had no budget, or is still thinking."""
    plan = a_plan()
    out, verdict, diff = consult_judge(plan, mode="live", draft_dir=tmp_path,
                                       overall=17)
    assert out is plan and verdict is None


def test_a_stale_verdict_is_ignored_and_the_maths_drafts(tmp_path):
    """A verdict for pick 17 must not decide pick 24."""
    V.write(tmp_path, 24, a_verdict(for_overall=17))
    out, verdict, _ = consult_judge(a_plan(), mode="live", draft_dir=tmp_path,
                                    overall=24)
    assert out.best.player.name == "Gibbs"
    assert verdict is not None and not verdict.acts


def test_a_corrupt_verdict_file_still_yields_a_pick(tmp_path):
    V.dir_for(tmp_path).mkdir(parents=True, exist_ok=True)
    V.path_for(tmp_path, 17).write_text("{not json", encoding="utf-8")
    plan = a_plan()
    out, verdict, _ = consult_judge(plan, mode="live", draft_dir=tmp_path,
                                    overall=17)
    assert out is plan and verdict is None


def test_a_verdict_of_refused_levers_still_yields_a_pick(tmp_path):
    """Every lever thrown out — the loop drafts the maths, and says so."""
    V.write(tmp_path, 17, a_verdict(veto=[{
        "espn_id": 999, "name": "Nobody", "reason": "x",
        "cites": ["§2.5"], "dossier_fact": "y"}]))
    out, verdict, _ = consult_judge(a_plan(), mode="live", draft_dir=tmp_path,
                                    overall=17)
    assert out.best.player.name == "Gibbs"
    assert verdict.rejected


def test_a_verdict_that_empties_the_board_is_survivable(tmp_path):
    """Two vetoes against a three-deep plan still leave someone to draft."""
    V.write(tmp_path, 17, a_verdict(veto=[
        {"espn_id": 1, "name": "Gibbs", "reason": "out", "cites": ["§2.5"],
         "dossier_fact": "a"},
        {"espn_id": 2, "name": "Bijan", "reason": "out", "cites": ["§2.5"],
         "dossier_fact": "b"},
    ]))
    out, _, _ = consult_judge(a_plan(), mode="live", draft_dir=tmp_path,
                              overall=17)
    assert out.best is not None and out.best.player.name == "Pollard"


def test_no_plan_is_not_a_crash(tmp_path):
    out, verdict, diff = consult_judge(None, mode="live", draft_dir=tmp_path,
                                       overall=17)
    assert out is None and verdict is None and diff is None


# ── a judged draft must still be a legal draft ───────────────────────────────

def test_a_vetoing_judge_never_produces_an_illegal_roster():
    """§3.7 — the judge removes candidates; roster legality is core's and has
    to survive that. A veto that emptied a position, or walked us past a cap,
    would be a lever reaching further than it was granted.

    The judge here is far more aggressive than the caps allow in practice: it
    vetoes the top candidate on every single turn.
    """
    import random

    from core.draft import picker
    from core.draft import verdict as V
    from core.draft.room import Pick, RoomModel
    from core.model.value import value_pool
    from tests.test_draft_sim import make_facts, make_pool

    facts = make_facts(teams=10)
    pool = make_pool(random.Random(3))
    vals = value_pool(pool, facts.settings, window="ros")
    rows = [(p, vals[p.espn_id]) for p in pool if p.espn_id in vals]
    rows.sort(key=lambda pv: pv[1].vor, reverse=True)

    me = facts.pick_order[0]
    room = RoomModel(facts=facts, my_team_id=me)
    caps = facts.position_limits

    taken: set[int] = set()
    for overall in range(1, 40):
        plan = picker.rank(rows, room)
        if plan.best is None:
            break
        top = plan.top(1)
        raw = {"for_overall": overall, "agree": False, "summary": "s",
               "veto": [{"espn_id": top[0].player.espn_id,
                         "name": top[0].player.name, "reason": "r",
                         "cites": ["§2.5"], "dossier_fact": "f"}]}
        v = V.parse(raw, plan=plan, for_overall=overall)
        judged = V.apply(plan, v)
        assert judged.best is not None, "a veto emptied the board"

        chosen = judged.best.player
        assert chosen.espn_id not in taken, "drafted the same player twice"
        taken.add(chosen.espn_id)
        room.apply([Pick(overall=overall, team_id=room.team_on_clock(overall),
                         espn_id=chosen.espn_id, pos=chosen.pos,
                         name=chosen.name)])

    # §3.7 — our roster obeys ESPN's position caps despite the vetoing.
    for pos, n in room.my_positions.items():
        assert n <= caps.get(pos, 99), f"{pos.value}: {n} over cap {caps.get(pos)}"


def test_the_test_suite_cannot_post_to_the_live_channel(monkeypatch, tmp_path):
    """Running this suite on the box put two junk 'Shadow · pick 17' messages
    into #fantasy on 2026-09-04, because consult_judge posts a shadow diff for
    real and the box is where the Slack token actually resolves.

    The guard keys off PYTEST_CURRENT_TEST so it needs no cooperation from the
    tests themselves — including the ones nobody thought to check.
    """
    import os

    from core import notify as N

    assert os.environ.get("PYTEST_CURRENT_TEST"), "pytest should set this"

    posted = []
    monkeypatch.setattr(N.urllib.request, "urlopen",
                        lambda *a, **k: posted.append(a) or (_ for _ in ()).throw(
                            AssertionError("a test reached the network")))
    assert N.notify("info", "should not post", "body") is None

    # And through the real path the judge tests use.
    V.write(tmp_path, 17, a_verdict())
    consult_judge(a_plan(), mode="shadow", draft_dir=tmp_path, overall=17)
    assert posted == []
