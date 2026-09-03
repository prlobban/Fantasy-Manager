"""Reading the live draft board.

Two readers behind one interface:

  ApiReader — polls mDraftDetail. Verified 2026-09-03: pre-draft the endpoint
    already returns every pick slot for the whole draft, with the correct teamId
    and `playerId: -1`. Picks land by that -1 turning into a real id, so a pick
    is simply "a slot with a player in it." No websocket, no scraping.

  DomReader — parses the draft-room page. The fallback for a practice draft
    (which does not write to the league's draft record) and for the case where
    the API lags the room.

ApiReader is primary because it needs no browser and cannot be broken by a
redesign. DomReader exists so a single point of failure isn't the whole draft.
"""

from __future__ import annotations

import logging
from typing import Protocol

from core.draft.room import Pick
from core.espn.client import EspnClient, client
from core.espn.players import _POS_BY_ID
from core.model.schema import Player, Pos

log = logging.getLogger(__name__)


class DraftReader(Protocol):
    def read(self) -> list[Pick]: ...
    def is_complete(self) -> bool: ...


class ApiReader:
    """Poll mDraftDetail. Cheap, robust, no browser."""

    def __init__(self, c: EspnClient | None = None, by_id: dict[int, Player] | None = None):
        self.c = c or client()
        self.by_id = by_id or {}
        self._complete = False
        self._total_slots: int | None = None

    def read(self) -> list[Pick]:
        data = self.c.get_view("mDraftDetail")
        dd = data.get("draftDetail") or {}
        raw = dd.get("picks") or []
        self._total_slots = len(raw) or self._total_slots
        self._complete = bool(dd.get("drafted")) and not dd.get("inProgress")

        picks: list[Pick] = []
        for p in raw:
            pid = int(p.get("playerId", -1))
            if pid <= 0:  # -1 means the slot is still empty
                continue
            picks.append(
                Pick(
                    overall=int(p.get("overallPickNumber", 0)),
                    team_id=int(p.get("teamId", 0)),
                    espn_id=pid,
                    pos=self._pos_for(pid, p),
                    name=self._name_for(pid),
                )
            )
        picks.sort(key=lambda x: x.overall)
        return picks

    def _pos_for(self, pid: int, raw: dict) -> Pos | None:
        if pl := self.by_id.get(pid):
            return pl.pos
        # A player outside our 450-deep board still has to be tracked, or the
        # room model's roster counts silently drift.
        return _POS_BY_ID.get(int(raw.get("defaultPositionId", -1)))

    def _name_for(self, pid: int) -> str:
        pl = self.by_id.get(pid)
        return pl.name if pl else f"player {pid}"

    def is_complete(self) -> bool:
        return self._complete

    def pick_slots(self) -> int | None:
        """Total slots the draft has, straight from ESPN. Beats computing it."""
        return self._total_slots


class DomReader:
    """Parse picks out of the draft-room page.

    Selector-driven, so everything it depends on lives in browser/selectors.py
    and can be re-pointed after an ESPN redesign without touching this logic.
    """

    def __init__(self, session, by_name: dict[str, Player] | None = None):
        self.s = session
        self.by_name = by_name or {}
        self._complete = False

    def read(self) -> list[Pick]:
        from core.browser import selectors as S

        page = self.s.page
        rows = page.locator(S.DRAFT_PICK_ROW)
        n = rows.count()
        picks: list[Pick] = []
        for i in range(n):
            try:
                text = (rows.nth(i).inner_text() or "").strip()
            except Exception:
                continue
            parsed = S.parse_pick_row(text)
            if not parsed:
                continue
            overall, name = parsed
            pl = self.by_name.get(S.norm(name))
            picks.append(
                Pick(
                    overall=overall,
                    team_id=0,  # the DOM doesn't reliably expose it; room infers
                    espn_id=pl.espn_id if pl else -1,
                    pos=pl.pos if pl else None,
                    name=name,
                )
            )
        picks.sort(key=lambda x: x.overall)
        return picks

    def is_complete(self) -> bool:
        from core.browser import selectors as S

        try:
            return self.s.page.locator(S.DRAFT_COMPLETE).count() > 0
        except Exception:
            return self._complete


class FallbackReader:
    """Try the API; fall back to the DOM when it returns nothing.

    A practice draft does not write to mDraftDetail, so on practice day the API
    is silently empty and the DOM is the only truth. Rather than configuring
    which to use, this notices.
    """

    def __init__(self, api: ApiReader, dom: DomReader | None = None):
        self.api = api
        self.dom = dom
        self.using = "api"

    def read(self) -> list[Pick]:
        picks = self.api.read()
        if picks or self.dom is None:
            self.using = "api"
            return picks
        dom_picks = self.dom.read()
        if dom_picks:
            if self.using != "dom":
                log.warning("API reports no picks but the DOM has %d — switching to DOM",
                            len(dom_picks))
            self.using = "dom"
        return dom_picks

    def is_complete(self) -> bool:
        return self.api.is_complete() or (self.dom.is_complete() if self.dom else False)
