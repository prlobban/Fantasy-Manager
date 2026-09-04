"""§3.10 — the limits on what the judge can do to a pick.

Every rule here is enforced in code rather than asked for in the prompt, and
these tests are what makes that claim true. The one that matters most is
`test_cross_tier_promotion_is_refused`: promotion is the lever deliberately not
granted, and a prompt saying so is not a control.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from core.draft import verdict as V

# ── a minimal stand-in for the PickPlan the judge is shown ───────────────────

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


def plan(*specs) -> _Plan:
    """specs: (id, name, tier, score)"""
    return _Plan([_Cand(_Player(i, n), _Val(t), s) for i, n, t, s in specs])


P = plan(
    (1, "Gibbs", 1, 100.0),
    (2, "Bijan", 1, 95.0),
    (3, "Nacua", 1, 90.0),
    (4, "Pollard", 3, 40.0),
    (5, "Warren", 3, 38.0),
)


def lever(espn_id, name, **over):
    base = {"espn_id": espn_id, "name": name, "reason": "because",
            "cites": ["§3.7"], "dossier_fact": "the dossier says so"}
    base.update(over)
    return base


def raw(**over):
    base = {"for_overall": 17, "agree": True, "summary": "s"}
    base.update(over)
    return base


# ── the lever that does not exist ────────────────────────────────────────────

def test_cross_tier_promotion_is_refused():
    """The whole point: the judge may reorder INSIDE a tier and nothing more."""
    v = V.parse(raw(reorder=[lever(4, "Pollard", above_espn_id=1)]), plan=P,
                for_overall=17)
    assert v.reorders == []
    assert any("cross-tier" in r for r in v.rejected)


def test_within_tier_reorder_is_allowed():
    v = V.parse(raw(reorder=[lever(3, "Nacua", above_espn_id=1)]), plan=P,
                for_overall=17)
    assert len(v.reorders) == 1
    assert v.rejected == []
    assert v.agree is False, "a verdict that acts is not an agreement"


def test_reorder_actually_moves_the_candidate():
    v = V.parse(raw(reorder=[lever(3, "Nacua", above_espn_id=1)]), plan=P,
                for_overall=17)
    out = V.apply(P, v)
    assert [c.player.name for c in out.candidates][:2] == ["Nacua", "Gibbs"]
    # The size of the override is recorded so §7 can grade it.
    assert out.candidates[0].reasons["agent_reorder"] == pytest.approx(10.0)


def test_apply_never_mutates_the_original_plan():
    """Shadow mode compares judged against un-judged; the original must survive."""
    before = [c.player.name for c in P.candidates]
    v = V.parse(raw(reorder=[lever(3, "Nacua", above_espn_id=1)]), plan=P,
                for_overall=17)
    V.apply(P, v)
    assert [c.player.name for c in P.candidates] == before


# ── scope ────────────────────────────────────────────────────────────────────

def test_a_lever_on_someone_not_shown_is_refused():
    v = V.parse(raw(veto=[lever(999, "Nobody")]), plan=P, for_overall=17)
    assert v.vetoes == []
    assert any("not in the top" in r for r in v.rejected)


def test_reorder_target_must_also_be_in_the_list():
    v = V.parse(raw(reorder=[lever(1, "Gibbs", above_espn_id=999)]), plan=P,
                for_overall=17)
    assert v.reorders == []
    assert any("not in the candidate list" in r for r in v.rejected)


# ── freshness ────────────────────────────────────────────────────────────────

def test_a_verdict_for_another_pick_is_discarded_whole():
    """A stale verdict is an answer about a board that no longer exists."""
    v = V.parse(raw(for_overall=17, veto=[lever(1, "Gibbs")]), plan=P,
                for_overall=24)
    assert v.agree is True and not v.acts
    assert any("written for pick 17" in r for r in v.rejected)


# ── citations ────────────────────────────────────────────────────────────────

def test_an_uncited_lever_is_refused():
    v = V.parse(raw(veto=[lever(1, "Gibbs", cites=[])]), plan=P, for_overall=17)
    assert v.vetoes == []
    assert any("no § citation" in r for r in v.rejected)


def test_a_lever_without_a_dossier_fact_is_refused():
    """§3.10 — the judge may not act on its own recall, only on the record."""
    v = V.parse(raw(veto=[lever(1, "Gibbs", dossier_fact="")]), plan=P,
                for_overall=17)
    assert v.vetoes == []
    assert any("dossier_fact" in r for r in v.rejected)


# ── caps ─────────────────────────────────────────────────────────────────────

def test_vetoes_are_capped():
    v = V.parse(raw(veto=[lever(1, "Gibbs"), lever(2, "Bijan"), lever(3, "Nacua")]),
                plan=P, for_overall=17)
    assert len(v.vetoes) == 2
    assert any("over the cap" in r for r in v.rejected)


def test_veto_removes_the_candidate():
    v = V.parse(raw(veto=[lever(1, "Gibbs")]), plan=P, for_overall=17)
    out = V.apply(P, v)
    assert "Gibbs" not in [c.player.name for c in out.candidates]
    assert out.candidates[0].player.name == "Bijan"


# ── the quiet path ───────────────────────────────────────────────────────────

def test_agreement_changes_nothing():
    v = V.parse(raw(agree=True), plan=P, for_overall=17)
    assert not v.acts
    assert V.apply(P, v) is P


def test_none_verdict_changes_nothing():
    assert V.apply(P, None) is P


def test_a_verdict_claiming_agreement_while_acting_is_corrected():
    v = V.parse(raw(agree=True, veto=[lever(1, "Gibbs")]), plan=P, for_overall=17)
    assert v.agree is False and len(v.vetoes) == 1


def test_one_bad_lever_does_not_void_the_good_one():
    v = V.parse(raw(veto=[lever(999, "Nobody"), lever(1, "Gibbs")]), plan=P,
                for_overall=17)
    assert [lv.name for lv in v.vetoes] == ["Gibbs"]
    assert len(v.rejected) == 1


# ── on disk ──────────────────────────────────────────────────────────────────

def test_write_then_read_round_trips(tmp_path):
    V.write(tmp_path, 17, raw(veto=[lever(1, "Gibbs")]))
    got = V.read(tmp_path, 17, plan=P)
    assert got is not None and len(got.vetoes) == 1


def test_reading_a_pick_with_no_verdict_is_none(tmp_path):
    assert V.read(tmp_path, 99, plan=P) is None


def test_a_half_written_file_is_never_read(tmp_path):
    """write() goes through a .tmp and renames, so the loop cannot catch it
    mid-flight — the judge writes while the loop is polling."""
    V.write(tmp_path, 17, raw())
    assert not list(V.dir_for(tmp_path).glob("*.tmp"))


# ── the writer and the reader must agree about the path ─────────────────────

def test_the_judge_writes_where_the_loop_reads(tmp_path):
    """The bug that made live mode silently useless.

    draft_judge.py passed its own `verdicts-shadow/` directory to vmod.write,
    which appends its own "verdicts" level — so the judge wrote
    verdicts-shadow/verdicts/N.json while the loop read verdicts/N.json. In
    live mode it would have written verdicts/verdicts/N.json and the loop
    would never have found a single verdict. Nothing failed loudly; the judge
    just appeared to have no opinions.

    Found by scripts/rehearse_judge.py, 2026-09-04.
    """
    from core.draft.run import consult_judge

    V.write(tmp_path, 17, raw(veto=[lever(1, "Gibbs")]))

    # Exactly one JSON file, exactly where path_for says it is.
    files = list(tmp_path.rglob("*.json"))
    assert files == [V.path_for(tmp_path, 17)], f"wrote to {files}"
    assert not list(tmp_path.rglob("verdicts/verdicts")), "nested verdicts dir"

    # And the loop's own read path finds it.
    out, verdict, _ = consult_judge(P, mode="live", draft_dir=tmp_path,
                                    overall=17)
    assert verdict is not None and len(verdict.vetoes) == 1
    assert out.best.player.name == "Bijan", "the lever never reached the pick"
