"""The self-healing selector layer.

The cases here are the ones that would let a bad selector reach a live write,
so most of them are about REFUSING to heal rather than healing.
"""
from __future__ import annotations

import re

import pytest

from core.browser import groups as G
from core.browser import selectors as S
from core.browser import selfheal as SH

# ── the registry is the single enumeration ───────────────────────────────────

def test_registry_covers_every_selector_constant():
    """A selector group that exists must be one the prober checks.

    This is the guard on the 2026-09-14 failure mode: ADD_PLAYER_BUTTON was in
    selectors.py and in nobody's target list, so the diagnostic reported health
    while the claim could not find its button.
    """
    skip = {  # not groups: regexes, label maps, CSS text, banner strings
        "QUEUE_ROW_ID_ATTR", "SLOT_PAGE_LABELS", "ROOM_CSS", "MOVE_SAVED_BANNER",
        "TRADE_SENT_BANNER", "PICK_ANIMATION_OVERLAY", "ANY_ROW",
        "DRAFT_BOARD_CELL_DONE", "DRAFT_PLAYER_NAME", "DRAFT_SEARCH_CLEAR",
        "DRAFT_COMPLETE", "QUEUE_AUTOPICK_TOGGLE",
    }
    declared = {
        n for n in dir(S)
        if n.isupper() and isinstance(getattr(S, n), str) and n not in skip
    }
    missing = declared - set(G.BY_NAME)
    assert not missing, (
        f"selector groups missing from groups.py: {sorted(missing)} — "
        "an unregistered group is never probed and never healed"
    )


def test_healable_groups_all_exist():
    assert not (G.HEALABLE - set(G.BY_NAME))


def test_irreversible_writes_are_not_healable():
    """A write with no reverse is never re-pointed by a guess (§10.6)."""
    for name in ("DRAFT_BUTTON", "TRADE_SEND_BUTTON", "TRADE_ACCEPT_BUTTON"):
        assert name not in G.HEALABLE, f"{name} must stay human"


# ── scoring ──────────────────────────────────────────────────────────────────

def _el(**kw):
    base = {"tag": "button", "text": "", "title": "", "aria": "", "cls": "",
            "id": "", "ph": "", "type": "", "disabled": False, "visible": True}
    return {**base, **kw}


def test_renamed_class_is_found():
    """The real 2026-09-15 case: add-action-btn became claim-action-btn."""
    g = G.get("ADD_PLAYER_BUTTON")
    el = _el(title="Claim", cls="Button Button--sm claim-action-btn mh2")
    assert SH._score(el, g) >= SH.MIN_SCORE


def test_unrelated_button_scores_zero():
    """The Watch button sits in the same row and must never win."""
    g = G.get("ADD_PLAYER_BUTTON")
    el = _el(cls="Button Button--alt watch-action-btn mh2 inactive")
    assert SH._score(el, g) == 0.0


def test_consent_dialog_never_wins():
    """2026-09-12: the only 'Submit' on the page was OneTrust's cookie dialog."""
    g = G.get("LINEUP_SAVE_BUTTON")
    el = _el(text="Submit", cls="save-preference-btn-handler onetrust-close-btn-handler")
    assert SH._score(el, g) == 0.0


def test_disabled_elements_score_nothing():
    g = G.get("ADD_PLAYER_BUTTON")
    el = _el(title="Claim", cls="claim-action-btn", disabled=True)
    assert SH._score(el, g) == 0.0


# ── the selector that gets built ─────────────────────────────────────────────

def test_selector_prefers_the_specific_class():
    g = G.get("ADD_PLAYER_BUTTON")
    el = _el(cls="Button Button--sm Button--custom claim-action-btn mh2")
    sel = SH._selector_for(el, g.words)
    assert "claim-action-btn" in sel
    assert sel != "button.Button"


def test_selector_combines_class_and_text_when_both_exist():
    """Add/Drop/Move share `action-buttons`; only the text separates them."""
    g = G.get("TEAM_DROP_TOOLBAR")
    el = _el(text="Drop", cls="Button Button--alt ml4 action-buttons")
    sel = SH._selector_for(el, g.words)
    assert "action-buttons" in sel and "Drop" in sel


def test_hashed_classes_are_rejected():
    assert not SH._meaningful_class("css-1x7fma3")
    assert not SH._meaningful_class("dn")
    assert SH._meaningful_class("claim-action-btn")


def test_bare_framework_class_is_unusable():
    assert SH._class_rank("Button", ("add", "claim")) == 0.0
    assert SH._class_rank("claim-action-btn", ("add", "claim")) > 0


# ── retry safety ─────────────────────────────────────────────────────────────

def test_precommit_and_postcommit_are_disjoint_and_complete():
    from core.gates import recover

    assert not (recover.PRE_COMMIT & recover.POST_COMMIT)
    unclassified = G.HEALABLE - recover.PRE_COMMIT - recover.POST_COMMIT
    assert not unclassified, (
        f"unclassified healable groups: {sorted(unclassified)} — recover.assess "
        "refuses to guess, so these can never be retried"
    )


def test_add_button_failure_is_precommit():
    """Failing to FIND the Add button means it was never clicked."""
    from core.gates import recover

    assert "ADD_PLAYER_BUTTON" in recover.PRE_COMMIT


def test_confirm_button_failure_is_postcommit():
    """CONFIRM is only reached after Add was clicked — the add may have landed."""
    from core.gates import recover

    assert "CONFIRM_BUTTON" in recover.POST_COMMIT


def test_a_partial_write_is_never_retried():
    from core.browser.actions import PartialWrite
    from core.gates import recover
    from core.model.schema import Action, ActionKind

    a = Action(kind=ActionKind.ADD_DROP, reason="x", cites=["§5.7"],
               args={"add_id": 1, "drop_id": 2})
    exc = PartialWrite("add landed, drop failed", group="CONFIRM_BUTTON")
    exc.receipt = object()
    ok, why = recover.assess(a, exc)
    assert not ok and "partial" in why.lower()


def test_a_non_selector_failure_is_not_healed():
    from core.gates import recover
    from core.model.schema import Action, ActionKind

    a = Action(kind=ActionKind.SET_LINEUP, reason="x", cites=["§4.1"], args={})
    ok, why = recover.assess(a, RuntimeError("network died"))
    assert not ok and "nothing to heal" in why


def test_only_one_retry():
    from core.browser.actions import ActionFailed
    from core.gates import recover
    from core.model.schema import Action, ActionKind

    a = Action(kind=ActionKind.SET_LINEUP, reason="x", cites=["§4.1"], args={})
    exc = ActionFailed("no move button", group="LINEUP_MOVE_BUTTON")
    assert recover.assess(a, exc, attempts=0)[0] is True
    assert recover.assess(a, exc, attempts=1)[0] is False


def test_unhealable_group_is_not_retried():
    from core.browser.actions import ActionFailed
    from core.gates import recover
    from core.model.schema import Action, ActionKind

    a = Action(kind=ActionKind.PROPOSE_TRADE, reason="x", cites=["§6.1"], args={})
    exc = ActionFailed("no send button", group="TRADE_SEND_BUTTON")
    ok, why = recover.assess(a, exc)
    assert not ok and "no reverse" in why


# ── overrides ────────────────────────────────────────────────────────────────

def test_builtins_are_never_lost(tmp_path, monkeypatch):
    """A heal ADDS a way to find the element; it never removes one."""
    from core.browser import overrides

    monkeypatch.setattr(overrides, "PATH", tmp_path / "o.json")
    overrides.reload()
    overrides.record("ADD_PLAYER_BUTTON", "button.new-thing", why="t", verified=True)
    chain = overrides.candidates("ADD_PLAYER_BUTTON")
    assert chain[0] == "button.new-thing"
    for builtin in G.get("ADD_PLAYER_BUTTON").builtin.split(","):
        assert builtin.strip() in chain
    overrides.reload()


def test_unverified_override_is_ignored(tmp_path, monkeypatch):
    from core.browser import overrides

    monkeypatch.setattr(overrides, "PATH", tmp_path / "o.json")
    overrides.reload()
    overrides.record("ADD_PLAYER_BUTTON", "button.unproven", why="t", verified=False)
    assert overrides.get("ADD_PLAYER_BUTTON") is None
    overrides.reload()


def test_corrupt_overrides_fall_back_to_builtins(tmp_path, monkeypatch):
    """§10.6 — fail closed to declared behaviour, never to no behaviour."""
    from core.browser import overrides

    bad = tmp_path / "o.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(overrides, "PATH", bad)
    overrides.reload()
    chain = overrides.candidates("ADD_PLAYER_BUTTON")
    assert "button.add-action-btn" in chain
    overrides.reload()


def test_unknown_group_is_rejected(tmp_path, monkeypatch):
    from core.browser import overrides

    monkeypatch.setattr(overrides, "PATH", tmp_path / "o.json")
    overrides.reload()
    with pytest.raises(ValueError):
        overrides.record("NOT_A_GROUP", "button.x", why="t", verified=True)
    overrides.reload()


# ── probe semantics ──────────────────────────────────────────────────────────

def test_optional_group_absent_is_not_broken():
    """The always-red exit code, as a test. LINEUP_EDIT_BUTTON is obsolete
    in-season and its absence must never fail a run."""
    p = SH.Probe("LINEUP_EDIT_BUTTON", None, 0, optional=True)
    assert not p.broken
    assert not SH.Report("team", [p]).broken


def test_required_group_absent_is_broken():
    p = SH.Probe("ADD_PLAYER_BUTTON", None, 0, optional=False)
    assert p.broken


def test_edit_and_save_buttons_are_marked_optional():
    for name in ("LINEUP_EDIT_BUTTON", "LINEUP_SAVE_BUTTON"):
        assert G.get(name).optional, f"{name} is obsolete in-season"


# ── learning ─────────────────────────────────────────────────────────────────

def test_winner_is_tried_first(tmp_path, monkeypatch):
    from core.browser import healing_log

    monkeypatch.setattr(healing_log, "WINNERS", tmp_path / "w.json")
    healing_log.note_winner("PLAYER_SEARCH", "input[type=search]")
    assert healing_log.last_winner("PLAYER_SEARCH") == "input[type=search]"


def test_volatility_needs_repeats(tmp_path, monkeypatch):
    from core.browser import healing_log

    monkeypatch.setattr(healing_log, "PATH", tmp_path / "h.jsonl")
    for _ in range(healing_log.VOLATILE_AFTER - 1):
        healing_log.record_break("ADD_PLAYER_BUTTON", "add")
    assert "ADD_PLAYER_BUTTON" not in healing_log.volatile()
    healing_log.record_break("ADD_PLAYER_BUTTON", "add")
    assert "ADD_PLAYER_BUTTON" in healing_log.volatile()


def test_history_survives_a_corrupt_line(tmp_path, monkeypatch):
    from core.browser import healing_log

    h = tmp_path / "h.jsonl"
    monkeypatch.setattr(healing_log, "PATH", h)
    healing_log.record_break("ADD_PLAYER_BUTTON", "add")
    with h.open("a", encoding="utf-8") as fh:
        fh.write("{ garbage\n")
    healing_log.record_break("ADD_PLAYER_BUTTON", "add")
    assert len(healing_log.read()) == 2


def test_selector_group_names_are_valid_identifiers():
    for name in G.BY_NAME:
        assert re.fullmatch(r"[A-Z][A-Z0-9_]*", name), name


# ── a match is not a correct match ───────────────────────────────────────────

class _FakeLoc:
    """Minimal stand-in for a Playwright locator's .first.evaluate()."""

    def __init__(self, **attrs):
        self._a = {"id": "", "cls": "", "aria": "", "name": "", "visible": True, **attrs}

    @property
    def first(self):
        return self

    def evaluate(self, _script):
        return self._a


def test_cookie_dialog_match_is_rejected():
    """The 2026-09-15 live failure: PLAYER_SEARCH resolved to OneTrust's hidden
    vendor search, so a waiver claim clicked an invisible element for 20s."""
    loc = _FakeLoc(id="vendor-search-handler", aria="Cookie list search", visible=False)
    why = SH.disqualified(loc)
    assert why and "consent" in why.lower()


def test_invisible_element_is_rejected():
    assert SH.disqualified(_FakeLoc(id="real-thing", visible=False))


def test_a_normal_element_is_accepted():
    assert SH.disqualified(_FakeLoc(id="player-search", cls="form__control")) is None


def test_disqualifier_fails_open_on_error():
    """A DOM read that throws must not discard a working selector."""

    class Boom:
        @property
        def first(self):
            return self

        def evaluate(self, _s):
            raise RuntimeError("detached")

    assert SH.disqualified(Boom()) is None


def test_espn_player_search_is_the_first_candidate():
    """Ordering is load-bearing: the generic Search candidate matched the
    consent widget first and the chain never reached the real box."""
    chain = [c.strip() for c in G.get("PLAYER_SEARCH").builtin.split(",")]
    assert chain[0] == "input[placeholder='Player Name']"


def test_generic_search_candidates_exclude_consent_widgets():
    for cand in G.get("PLAYER_SEARCH").builtin.split(","):
        if "placeholder='Player Name'" in cand:
            continue
        assert "vendor" in cand and "cookie" in cand.lower(), cand
