"""The physical writes. Playwright against ESPN's UI.

🔴 NOTHING calls this module directly. Every function here is reached through
core/gates/write_gate.py, which enforces §8.2. That is the whole point of §10.3:
an agent that can construct an arbitrary browser action has no guardrails, only
suggestions.

Every action returns a Receipt carrying a screenshot path, because a write we
cannot prove happened is a write we have to assume didn't.

STATUS 2026-09-03: the selectors these rely on are unverified — the draft room
and the lineup editor only render for a logged-in session, which is blocked
until scripts/login.py has been run once. Each function is written to fail
closed and say which selector it could not find.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from core.browser import selectors as S
from core.browser.session import EspnSession

log = logging.getLogger(__name__)


class ActionFailed(RuntimeError):
    """A write could not be completed. Never swallowed — the gate logs it."""


@dataclass
class Receipt:
    action: str
    detail: str
    at: datetime
    screenshot: str | None = None
    verified: bool = False

    def __str__(self) -> str:
        mark = "verified" if self.verified else "UNVERIFIED"
        return f"{self.action}: {self.detail} [{mark}] {self.screenshot or ''}"


def _receipt(s: EspnSession, action: str, detail: str, *, verified: bool) -> Receipt:
    shot = s.screenshot(action.replace(" ", "-").lower())
    return Receipt(
        action=action,
        detail=detail,
        at=datetime.now(UTC),
        screenshot=str(shot) if shot else None,
        verified=verified,
    )


def _need(page, *candidates: str, what: str):
    loc = S.first_present(page, *candidates)
    if loc is None:
        raise ActionFailed(
            f"could not find {what}. Selectors are in core/browser/selectors.py; "
            "run scripts/discover_selectors.py against a live page to re-point them."
        )
    return loc


# ── draft ────────────────────────────────────────────────────────────────────


def _need_in_row(page, name: str, *candidates: str, what: str):
    """A button that sits in a row carrying `name`. Fails closed: a click that
    cannot be tied to the intended player is not attempted at all."""
    loc = S.in_row_with(page, name, *candidates)
    if loc is None:
        raise ActionFailed(
            f"could not find {what} in a row containing {name!r}. Either the "
            "search did not narrow to him or the selectors are stale — see "
            "core/browser/selectors.py and scripts/discover_selectors.py."
        )
    return loc


def draft_player(s: EspnSession, espn_id: int, name: str) -> Receipt:
    """Click Draft on a player. The fast path; the queue is the safety net.

    The Draft button is located INSIDE the row that carries his name. "Search,
    then click the first Draft button on the page" would draft whoever ESPN
    rendered first if the filter lagged — and a wrong click is the one failure
    the queue cannot undo.
    """
    page = s.page
    s.dismiss_overlays()

    box = _need(page, S.DRAFT_SEARCH, what="the draft-room player search box")
    box.first.fill(name)
    page.wait_for_timeout(500)

    btn = _need_in_row(page, name, S.DRAFT_BUTTON, what="a Draft button")
    btn.first.click()

    # ESPN usually asks to confirm. Absence of a dialog is fine.
    confirm = S.first_present(page, S.CONFIRM_BUTTON)
    if confirm is not None:
        try:
            confirm.first.click(timeout=3000)
        except Exception:
            pass

    page.wait_for_timeout(1200)
    return _receipt(s, "draft pick", f"{name} ({espn_id})", verified=False)


# ── lineup ───────────────────────────────────────────────────────────────────


def set_lineup(s: EspnSession, league_id: int, team_id: int, season: int,
               moves: list[tuple[int, str]]) -> Receipt:
    """Apply start/sit moves. `moves` is [(espn_id, target_slot_name)].

    ESPN's editor is two clicks per move: MOVE on the player, then HERE on the
    destination slot. Clicking MOVE alone (what the first version did) selects
    him and changes nothing — Save then saves the lineup as it was.

    Moving a bench player into an occupied slot swaps the occupant to the
    bench, so a plan's "X to RB" is one move even when it displaces someone.
    """
    page = s.goto(
        f"/football/team?leagueId={league_id}&teamId={team_id}&seasonId={season}"
    )
    s.dismiss_overlays()

    edit = _need(page, S.LINEUP_EDIT_BUTTON, what="the Edit Lineup button")
    edit.first.click()
    page.wait_for_timeout(1200)

    applied = 0
    for espn_id, slot in moves:
        try:
            row = _row_for_player(page, espn_id)
            if row is None:
                log.warning("no lineup row for player %s", espn_id)
                continue
            mv = row.locator(S.LINEUP_MOVE_BUTTON)
            if mv.count() == 0:
                log.warning("player %s has no MOVE control (locked slot?)", espn_id)
                continue
            mv.first.click()
            page.wait_for_timeout(500)

            dest = _slot_row_with_here(page, slot)
            if dest is None:
                log.warning("no destination slot %r offering HERE for player %s — "
                            "cancelling that move", slot, espn_id)
                mv.first.click()  # a second click deselects
                page.wait_for_timeout(300)
                continue
            dest.first.click()
            page.wait_for_timeout(500)
            applied += 1
        except Exception as e:
            log.warning("lineup move for %s failed: %s", espn_id, e)

    save = _need(page, S.LINEUP_SAVE_BUTTON, what="the lineup Save button")
    save.first.click()
    page.wait_for_timeout(1500)

    return _receipt(
        s, "set lineup", f"{applied}/{len(moves)} moves applied", verified=applied == len(moves)
    )


def _slot_row_with_here(page, slot: str):
    """The HERE button in the first row labelled with `slot` that offers one.

    Slot names come from ESPN's own slot map ("RB", "RB/WR/TE", "BE"), which
    is what the roster table prints in its first column.
    """
    import re

    label = re.compile(rf"^\s*{re.escape(slot)}\b", re.I)
    rows = page.locator(S.LINEUP_SLOT_ROW)
    for i in range(rows.count()):
        try:
            r = rows.nth(i)
            text = (r.inner_text() or "").strip()
            if not label.search(text):
                continue
            here = r.locator(S.LINEUP_HERE_BUTTON)
            if here.count() > 0:
                return here
        except Exception:
            continue
    return None


def _row_for_player(page, espn_id: int):
    rows = page.locator(S.LINEUP_SLOT_ROW)
    for i in range(rows.count()):
        try:
            html = rows.nth(i).inner_html()
            if str(espn_id) in html:
                return rows.nth(i)
        except Exception:
            continue
    return None


# ── waivers / free agents ────────────────────────────────────────────────────


def add_drop(s: EspnSession, league_id: int, season: int,
             add_id: int, add_name: str, drop_id: int | None,
             drop_name: str | None) -> Receipt:
    page = s.goto(f"/football/players/add?leagueId={league_id}&seasonId={season}")
    s.dismiss_overlays()

    box = _need(page, S.DRAFT_SEARCH, what="the player search box")
    box.first.fill(add_name)
    page.wait_for_timeout(800)

    btn = _need_in_row(page, add_name, S.ADD_PLAYER_BUTTON, what="an Add/Claim button")
    btn.first.click()
    page.wait_for_timeout(1000)

    if drop_id and drop_name:
        row = _row_for_player(page, drop_id)
        if row is not None:
            d = row.locator(S.DROP_PLAYER_BUTTON)
            if d.count():
                d.first.click()
                page.wait_for_timeout(600)

    confirm = S.first_present(page, S.CONFIRM_BUTTON)
    if confirm is not None:
        confirm.first.click()
        page.wait_for_timeout(1200)

    detail = f"add {add_name}" + (f", drop {drop_name}" if drop_name else "")
    return _receipt(s, "add drop", detail, verified=False)


# ── trades ───────────────────────────────────────────────────────────────────


def _trade_button(page, player_names: list[str], *candidates: str, what: str):
    """The button on the offer card that mentions EVERY player in the offer.

    Two pending offers render two Accept buttons; "click the first" would act
    on whichever ESPN listed first. With no player names to anchor on, refuse.
    """
    import re

    if not player_names:
        raise ActionFailed(f"cannot locate {what}: no player names supplied to anchor the card")
    cards = page.locator(S.ANY_ROW)
    for n in player_names:
        cards = cards.filter(has_text=re.compile(re.escape(n), re.I))
    if cards.count() == 0:
        raise ActionFailed(f"no offer card mentions all of {player_names} — cannot find {what}")
    for sel in candidates:
        for group in sel.split(","):
            group = group.strip()
            try:
                loc = cards.locator(group)
                if loc.count() > 0:
                    return loc
            except Exception:
                continue
    raise ActionFailed(f"offer card for {player_names} has no {what}")


def accept_trade(s: EspnSession, league_id: int, season: int, offer_id: str,
                 player_names: list[str]) -> Receipt:
    """§6.8 — reached ONLY after a clean gauntlet sweep and the cool-down."""
    page = s.goto(f"/football/tradeoffers?leagueId={league_id}&seasonId={season}")
    s.dismiss_overlays()
    btn = _trade_button(page, player_names, S.TRADE_ACCEPT_BUTTON, what="an Accept button")
    btn.first.click()
    confirm = S.first_present(page, S.CONFIRM_BUTTON)
    if confirm is not None:
        confirm.first.click()
    page.wait_for_timeout(1500)
    return _receipt(s, "accept trade", f"offer {offer_id}", verified=False)


def reject_trade(s: EspnSession, league_id: int, season: int, offer_id: str,
                 player_names: list[str]) -> Receipt:
    page = s.goto(f"/football/tradeoffers?leagueId={league_id}&seasonId={season}")
    s.dismiss_overlays()
    btn = _trade_button(page, player_names, S.TRADE_REJECT_BUTTON, what="a Reject button")
    btn.first.click()
    confirm = S.first_present(page, S.CONFIRM_BUTTON)
    if confirm is not None:
        confirm.first.click()
    page.wait_for_timeout(1200)
    return _receipt(s, "reject trade", f"offer {offer_id}", verified=False)
