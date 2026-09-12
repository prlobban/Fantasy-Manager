"""The 2026-09-12 Week 1 write-path failures, each pinned to a test.

Three defects took down the first in-season sweep that tried to write:

  1. ESPN's in-season team page has no "Edit Lineup" button, and `set_lineup`
     required one — so every lineup write aborted before its first move.
  2. `LINEUP_SAVE_BUTTON` matched the OneTrust cookie-consent dialog's hidden
     Submit, so the one element it found was the wrong one entirely.
  3. `add_drop` assumed a drop modal. With an open bench spot ESPN commits the
     add immediately, the drop leg then failed, and the gate recorded the whole
     action as `executed: false` — leaving two defences on the roster and the
     week's add counter reading nothing spent.
"""

from __future__ import annotations

import pytest

from core.browser import actions as A
from core.browser import selectors as S
from core.gates import kill_switch, write_gate
from core.model.schema import Action, ActionKind

# ── fakes ────────────────────────────────────────────────────────────────────


class FakeLocator:
    def __init__(self, items=None):
        self._items = items if items is not None else []

    def count(self):
        return len(self._items)

    def nth(self, i):
        return self._items[i]

    @property
    def first(self):
        return self._items[0]


class FakeButton:
    def __init__(self, text="", cls="", visible=True, enabled=True):
        self.text, self.cls = text, cls
        self._visible, self._enabled = visible, enabled
        self.clicks = 0

    def inner_text(self):
        return self.text

    def get_attribute(self, name):
        return self.cls if name == "class" else None

    def is_visible(self):
        return self._visible

    def is_enabled(self):
        return self._enabled

    def is_disabled(self):
        return not self._enabled

    def click(self, **_):
        self.clicks += 1


class FakePage:
    """Just enough page for the helpers under test."""

    def __init__(self, *, body="", matches=None):
        self.body = body
        self.matches = matches or {}

    def inner_text(self, _sel):
        return self.body

    def locator(self, sel):
        return FakeLocator(self.matches.get(sel, []))


# ── 1 / 2. the lineup page ───────────────────────────────────────────────────


def test_edit_lineup_button_is_optional_not_required():
    """In-season there is no Edit Lineup button. Its absence must not abort."""
    page = FakePage()
    assert S.first_present(page, S.LINEUP_EDIT_BUTTON) is None


def test_consent_dialog_submit_is_never_taken_as_a_lineup_save(monkeypatch):
    """The live page's only LINEUP_SAVE_BUTTON match was the cookie dialog."""
    onetrust = FakeButton(
        text="Submit",
        cls="save-preference-btn-handler onetrust-close-btn-handler",
        visible=False,
    )
    monkeypatch.setattr(S, "first_present", lambda page, *c, **k: FakeLocator([onetrust]))

    assert A._visible_save(FakePage()) is None
    assert onetrust.clicks == 0


def test_a_real_visible_save_is_taken(monkeypatch):
    real = FakeButton(text="Save Lineup", cls="Button lineup-save")
    monkeypatch.setattr(S, "first_present", lambda page, *c, **k: FakeLocator([real]))

    assert A._visible_save(FakePage()) is real


# ── the banner ───────────────────────────────────────────────────────────────


def test_move_saved_banner_is_read_off_the_page():
    page = FakePage(body="Move saved - Jaguars D/ST added")
    assert A._move_saved(page) is True
    assert A._move_saved(page, "Jaguars D/ST") is True


def test_a_banner_for_someone_else_does_not_vouch_for_this_write():
    """A stale banner must not confirm a different transaction."""
    page = FakePage(body="Move saved - Jaguars D/ST added")
    assert A._move_saved(page, "Chris Boswell") is False


def test_no_banner_is_not_a_confirmation():
    assert A._move_saved(FakePage(body="My Team big P 0-0-0")) is False


# ── 3. the partial write ─────────────────────────────────────────────────────


def test_partial_write_carries_what_landed():
    receipt = A.Receipt(action="add drop", detail="add Jaguars D/ST", at=None,
                        verified=True)
    e = A.PartialWrite("drop failed", receipt=receipt)

    assert isinstance(e, A.ActionFailed)
    assert e.receipt is receipt


@pytest.fixture
def enabled(tmp_path, monkeypatch):
    p = tmp_path / "ENABLED"
    p.write_text("on\n", encoding="utf-8")
    monkeypatch.setattr(kill_switch, "path", lambda: p)
    return p


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    from core.state import store

    monkeypatch.setattr(store, "_path", lambda: tmp_path / "state.json")
    monkeypatch.setattr("core.state.decisions._path",
                        lambda: tmp_path / "decisions.jsonl")
    return tmp_path


def _add_action():
    return Action(
        kind=ActionKind.ADD_DROP,
        args={"add": -16030, "drop": -16005, "roster_has_room": True},
        cites=["D6.3"], reason="stream the D/ST",
    )


def test_gate_records_a_partial_write_as_executed(enabled, isolated_state):
    """🔴 The 2026-09-12 lie: the add was live, the log said executed=false."""
    receipt = A.Receipt(action="add drop", detail="add Jaguars D/ST", at=None,
                        verified=True)

    def perform():
        raise A.PartialWrite("the drop of 'Browns D/ST' failed", receipt=receipt)

    with pytest.raises(A.PartialWrite):
        write_gate.execute(_add_action(), perform, skip_health=True)

    import json

    rec = json.loads((isolated_state / "decisions.jsonl").read_text().strip())
    assert rec["executed"] is True, "an add that landed must not log as not-executed"
    assert "add Jaguars D/ST" in rec["receipt"]
    assert "drop" in rec["gate"]["reason"]


def test_gate_still_records_a_clean_failure_as_not_executed(enabled, isolated_state):
    """Nothing landed, so nothing is claimed. The other half of the rule."""

    def perform():
        raise A.ActionFailed("no Add button on the page")

    with pytest.raises(A.ActionFailed):
        write_gate.execute(_add_action(), perform, skip_health=True)

    import json

    rec = json.loads((isolated_state / "decisions.jsonl").read_text().strip())
    assert rec["executed"] is False
    assert rec["receipt"] is None
