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


class PartialWrite(ActionFailed):
    """A write failed AFTER something already committed on ESPN.

    🔴 This exists because of 2026-09-12. `add_drop` clicked Add, ESPN saw an
    open bench spot and committed the add on the spot ("Move saved - Jaguars
    D/ST added"), the drop leg then failed, and the gate recorded the whole
    action as `executed: false`. Two things followed from that single lie: the
    roster carried two defences into game week, and `record_add` never fired,
    so the week's add counter still read 0 of 3 spent.

    A failure that left the league changed must carry the change. The gate
    reads `.receipt` off this and records `executed=True` with the reason.
    """

    def __init__(self, message: str, *, receipt: Receipt | None = None) -> None:
        super().__init__(message)
        self.receipt = receipt


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

    # Verified 2026-09-04 in a live practice room:
    #   - for the first ~3.5 s of our turn every DRAFT button shows a ":03"
    #     countdown and is inert; then it reads DRAFT. So: wait for it.
    #   - a queued player's QUEUE row grows its own DRAFT button on our turn.
    #     If the target is already queued (he usually is — the queue is synced
    #     before the click), that button is the shortest path: no search.
    #   - otherwise the table is virtualised: search (ENTER applies), find the
    #     row whose NAME cell is his, click DRAFT there, clear the search.
    row = None
    queued = page.locator(f"{S.QUEUE_CONTAINER.split(',')[0].strip()} "
                          f"tr[{S.QUEUE_ROW_ID_ATTR}='{espn_id}']")
    try:
        if queued.count():
            row = queued.first
    except Exception:
        row = None

    searched = False
    try:
        if row is None:
            if not S.search_player(page, name):
                raise ActionFailed("could not find the draft-room player search box")
            searched = True
            row = S.player_row(page, name)
            if row is None:
                raise ActionFailed(f"no player-table row for {name!r} after searching — "
                                   "already drafted, or the search did not apply")

        btn = _wait_for_draft_button(page, row, timeout_ms=8_000)
        if btn is None:
            labels = [t.strip() for t in row.locator("button").all_inner_texts()]
            raise ActionFailed(f"row for {name!r} never showed a DRAFT button "
                               f"(buttons: {labels}) — are we on the clock?")
        btn.first.click()

        # ESPN may ask to confirm. Absence of a dialog is fine.
        confirm = S.first_present(page, S.CONFIRM_BUTTON)
        if confirm is not None:
            try:
                confirm.first.click(timeout=3000)
            except Exception:
                pass
        page.wait_for_timeout(1200)
    finally:
        if searched:
            S.clear_search(page)

    return _receipt(s, "draft pick", f"{name} ({espn_id})", verified=False)


def _wait_for_draft_button(page, row, *, timeout_ms: int):
    """The row's DRAFT button once ESPN's start-of-turn countdown has cleared."""
    import time

    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        try:
            btn = row.locator(S.DRAFT_BUTTON)
            if btn.count() and btn.first.is_enabled():
                return btn
        except Exception:
            pass
        page.wait_for_timeout(300)
    return None


# ── lineup ───────────────────────────────────────────────────────────────────


def set_lineup(s: EspnSession, league_id: int, team_id: int, season: int,
               moves: list[tuple[int, str]],
               names: dict[int, str] | None = None) -> Receipt:
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

    # The in-season team page is ALREADY in edit mode — every row renders MOVE
    # and no Edit Lineup button exists. Requiring it (what this did until
    # 2026-09-12) aborted the write before the first move. Click it if ESPN is
    # serving the preseason layout; otherwise carry on.
    edit = S.first_present(page, S.LINEUP_EDIT_BUTTON)
    if edit is not None:
        edit.first.click()
        page.wait_for_timeout(1200)
    else:
        log.info("no Edit Lineup button — in-season page, already editable")

    applied = 0
    names = names or {}
    for espn_id, slot in moves:
        try:
            # The name matters for a DEFENCE: a D/ST has no headshot and a
            # negative id, so the id scan cannot find its row. Without a name
            # to fall back on, the 2026-09-12 write skipped the Jaguars
            # silently and reported "0/2 applied".
            row = _row_for_player(page, espn_id, names.get(espn_id))
            if row is None:
                log.warning("no lineup row for player %s (%s)", espn_id,
                            names.get(espn_id) or "name unknown")
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

    # 🔴 Do NOT _need() a save. In-season each MOVE + HERE commits on its own
    # and ESPN paints "Move saved"; there is no lineup save button. The old
    # selector matched one element on the live page — the OneTrust cookie
    # dialog's hidden Submit — so a "successful save" was a consent click.
    save = _visible_save(page)
    if save is not None:
        save.click()
        page.wait_for_timeout(1500)
    else:
        log.info("no lineup save control — MOVE/HERE commits each move directly")

    # The banner is ESPN's own confirmation. Absent it, the caller re-reads the
    # roster off the API before claiming anything (§10.6).
    saved = _move_saved(page)
    return _receipt(
        s, "set lineup", f"{applied}/{len(moves)} moves applied",
        verified=applied == len(moves) and saved,
    )


def _visible_save(page):
    """A save control that is actually a lineup save.

    Fails to None rather than clicking a stranger: the consent dialog's Submit
    is invisible and carries `onetrust`/`save-preference` classes, and clicking
    it looks like a successful save while the lineup never moved.
    """
    loc = S.first_present(page, S.LINEUP_SAVE_BUTTON)
    if loc is None:
        return None
    for i in range(loc.count()):
        e = loc.nth(i)
        try:
            cls = (e.get_attribute("class") or "").lower()
            if "onetrust" in cls or "save-preference" in cls:
                continue
            if e.is_visible() and e.is_enabled():
                return e
        except Exception:
            continue
    return None


def _move_saved(page, name: str | None = None) -> bool:
    """Whether ESPN is showing its green "Move saved" bar.

    The positive proof that a roster or lineup write committed. With `name`,
    the banner must also mention that player, so one stale banner cannot
    vouch for a different transaction.
    """
    try:
        body = " ".join((page.inner_text("body") or "").split()).lower()
    except Exception:
        return False
    if S.MOVE_SAVED_BANNER not in body:
        return False
    if name is None:
        return True
    surname = name.replace(" D/ST", "").split()[-1].lower()
    return surname in body


def _slot_row_with_here(page, slot: str):
    """The HERE button in the first row the page labels as `slot`.

    🔴 The slot name handed in comes from ESPN's READ API ("RB/WR/TE", "BE").
    The page's own SLOT column spells those "FLEX" and "Bench", so matching the
    API spelling against the page found nothing and the 2026-09-12 flex move
    was cancelled as "no destination slot offering HERE". `S.slot_labels`
    carries the aliases; each is tried in turn.
    """
    import re

    rows = page.locator(S.LINEUP_SLOT_ROW)
    n = rows.count()
    for candidate in S.slot_labels(slot):
        label = re.compile(rf"^\s*{re.escape(candidate)}\b", re.I)
        for i in range(n):
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


def _row_for_player(page, espn_id: int, name: str | None = None):
    """The table row for a player: by ESPN id in the markup, else by name.

    The id lives in the headshot URL, which is why the id path works at all —
    and why it CANNOT work for a defence. A D/ST has no headshot and no
    positive id (the Browns are -16005), so the id scan returns None, the
    caller skips the drop, and Continue is clicked while disabled: a write
    that reports success while the roster never changed.

    The name fallback reads each row's text in PYTHON rather than through
    Playwright's `has_text=`. That filter takes a regex but hands it to a JS
    engine, and anything spanning two cells ("Browns" … "D/ST", separated by
    newlines and tabs) silently matches nothing — verified 2026-09-08.
    """
    rows = page.locator(S.PLAYER_TABLE_ROW)
    n = rows.count()
    for i in range(n):
        try:
            if str(espn_id) in rows.nth(i).inner_html():
                return rows.nth(i)
        except Exception:
            continue
    if not name:
        return None
    # "Browns D/ST" to us; "D/ST Browns CLE D/ST DROP …" on the page.
    wanted = [w for w in name.replace("/", " ").split() if w.lower() != "dst"]
    is_dst = name.upper().endswith("D/ST")
    for i in range(n):
        try:
            text = " ".join(rows.nth(i).inner_text().split())
        except Exception:
            continue
        low = text.lower()
        if name.lower() in low:
            return rows.nth(i)
        if wanted and all(w.lower() in low for w in wanted):
            if not is_dst or "d/st" in low:
                return rows.nth(i)
    return None


# ── waivers / free agents ────────────────────────────────────────────────────


def add_drop(s: EspnSession, league_id: int, season: int,
             add_id: int, add_name: str, drop_id: int | None,
             drop_name: str | None, team_id: int | None = None) -> Receipt:
    page = s.goto(f"/football/players/add?leagueId={league_id}&seasonId={season}")
    s.dismiss_overlays()

    box = _need(page, S.PLAYER_SEARCH, S.DRAFT_SEARCH, what="the player search box")
    box.first.fill(add_name)
    page.wait_for_timeout(600)
    # The table filters on ENTER. Typing alone leaves the full free-agent list
    # rendered under an autocomplete dropdown — which is exactly how the
    # 2026-09-08 sweep read "no Add button for Jordan Mason" off a page that
    # was showing 200 other players.
    box.first.press("Enter")
    page.wait_for_timeout(2500)

    btn = _need_in_row(page, add_name, S.ADD_PLAYER_BUTTON, what="an Add/Claim button")
    btn.first.click()
    page.wait_for_timeout(2000)

    # 🔴 The Add click has TWO outcomes and 2026-09-12 conflated them:
    #   roster full  -> a modal opens listing roster rows to drop.
    #   room on bench-> ESPN commits the add THERE AND THEN, no modal, and
    #                   paints "Move saved - <player> added".
    # In the second case the old code went hunting for a drop row in a modal
    # that never existed, raised, and reported the whole thing as failed while
    # the player was already on the roster. The drop is a separate transaction
    # from here, and the add must be recorded either way.
    if _move_saved(page, add_name):
        add_receipt = _receipt(
            s, "add drop", f"add {add_name} (committed immediately, bench had room)",
            verified=True,
        )
        if not drop_name:
            return add_receipt
        log.info("%s committed without a modal; dropping %s separately",
                 add_name, drop_name)
        try:
            drop_player(s, league_id, team_id, season, drop_id, drop_name)
        except Exception as e:
            raise PartialWrite(
                f"{add_name} IS ADDED and live on the roster, but the follow-up "
                f"drop of {drop_name!r} failed: {e}. The add counts against §5.7; "
                "do not re-add. The roster is carrying both players.",
                receipt=add_receipt,
            ) from e
        return _receipt(s, "add drop", f"add {add_name}, drop {drop_name}",
                        verified=True)

    if drop_name:
        row = _row_for_player(page, drop_id or 0, drop_name)
        if row is None:
            raise ActionFailed(
                f"the add modal has no row for {drop_name!r} — nothing was added. "
                "A drop we cannot select leaves Continue disabled, and a click "
                "on it would report success while the roster never changed."
            )
        d = row.locator(S.DROP_PLAYER_BUTTON)
        if d.count() == 0:
            raise ActionFailed(f"no enabled DROP button on the row for {drop_name!r}")
        d.first.click()
        page.wait_for_timeout(800)

    confirm = _need(page, S.CONFIRM_BUTTON, what="the Continue/Confirm button")
    if confirm.first.is_disabled():
        raise ActionFailed(
            "Continue is still disabled after selecting the drop — ESPN has not "
            "accepted the transaction, so nothing was committed."
        )
    confirm.first.click()
    page.wait_for_timeout(1500)
    # ESPN sometimes puts a second confirmation behind the first.
    again = S.first_present(page, S.CONFIRM_BUTTON)
    if again is not None and again.count() and not again.first.is_disabled():
        again.first.click()
        page.wait_for_timeout(1200)

    detail = f"add {add_name}" + (f", drop {drop_name}" if drop_name else "")
    return _receipt(s, "add drop", detail, verified=False)


def drop_player(s: EspnSession, league_id: int, team_id: int, season: int,
                drop_id: int | None, drop_name: str) -> Receipt:
    """Drop one rostered player. The other half of a stream (D6.3).

    ✅ Flow verified 2026-09-12 on the live team page: the toolbar Drop button
    puts the roster into drop-selection mode, every eligible row renders an
    enabled DROP (locked rows read LOCKED), and Continue commits.

    This existed nowhere before 2026-09-12, which is why an add that committed
    on its own had no way to finish the job.
    """
    page = s.goto(
        f"/football/team?leagueId={league_id}&teamId={team_id}&seasonId={season}"
    )
    s.dismiss_overlays()

    toolbar = _need(page, S.TEAM_DROP_TOOLBAR, what="the team page's Drop button")
    toolbar.first.click()
    page.wait_for_timeout(2000)

    row = _row_for_player(page, drop_id or 0, drop_name)
    if row is None:
        raise ActionFailed(
            f"no roster row for {drop_name!r} in drop mode — nothing was dropped"
        )
    d = row.locator(S.DROP_PLAYER_BUTTON)
    if d.count() == 0 or not d.first.is_enabled():
        raise ActionFailed(
            f"no enabled DROP button on the row for {drop_name!r} "
            "(a locked row reads LOCKED once his game has started)"
        )
    d.first.click()
    page.wait_for_timeout(1000)

    confirm = _need(page, S.CONFIRM_BUTTON, what="the drop Continue/Confirm button")
    if confirm.first.is_disabled():
        raise ActionFailed(
            f"Continue is still disabled after selecting {drop_name!r} — ESPN has "
            "not accepted the drop, so nothing was committed."
        )
    confirm.first.click()
    page.wait_for_timeout(1500)
    again = S.first_present(page, S.CONFIRM_BUTTON)
    if again is not None and again.count() and not again.first.is_disabled():
        again.first.click()
        page.wait_for_timeout(1200)

    return _receipt(s, "drop player", f"drop {drop_name}",
                    verified=_move_saved(page, drop_name))


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


def propose_trade(s: EspnSession, league_id: int, season: int, to_team_id: int,
                  give: list[tuple[int, str]], get: list[tuple[int, str]]) -> Receipt:
    """§6.1–§6.7 — send an outgoing offer. Reached ONLY through write_gate,
    which has already checked the rate limits and the both-sides value test.

    STATUS 2026-09-05: written fail-closed and UNVERIFIED, by instruction
    ("do not test whether it can trade"). ESPN's flow, from the team page of
    the other manager: Propose Trade → tick players on both sides → Review →
    Send. Every selector below is a candidate; the first one that fails raises
    and nothing is sent.
    """
    import re

    page = s.goto(
        f"/football/team?leagueId={league_id}&teamId={to_team_id}&seasonId={season}"
    )
    s.dismiss_overlays()

    start = _need(page, S.TRADE_PROPOSE_BUTTON, what="the Propose Trade button")
    start.first.click()
    page.wait_for_timeout(1500)

    def _tick(espn_id: int, name: str) -> None:
        row = _row_for_player(page, espn_id)
        if row is None:
            row = page.locator(S.ANY_ROW).filter(has_text=re.compile(re.escape(name), re.I))
            if row.count() == 0:
                raise ActionFailed(f"no trade row for {name!r} ({espn_id}) — nothing sent")
            row = row.first
        box = row.locator(S.TRADE_PLAYER_CHECKBOX)
        if box.count() == 0:
            raise ActionFailed(f"no checkbox on the trade row for {name!r} — nothing sent")
        box.first.click()
        page.wait_for_timeout(300)

    for pid, name in give:
        _tick(pid, name)
    for pid, name in get:
        _tick(pid, name)

    review = _need(page, S.TRADE_REVIEW_BUTTON, what="the Review Trade button")
    review.first.click()
    page.wait_for_timeout(1200)
    send = _need(page, S.TRADE_SEND_BUTTON, what="the Send Trade button")
    send.first.click()
    page.wait_for_timeout(2500)

    # ESPN paints "Your trade offer has been confirmed and sent" on success.
    # Without it we do not know whether the offer landed, and a receipt that
    # says "verified" when it isn't is worse than no receipt at all.
    try:
        sent = S.TRADE_SENT_BANNER.lower() in page.inner_text("body").lower()
    except Exception:
        sent = False

    detail = (f"to team {to_team_id}: give {', '.join(n for _, n in give)} / "
              f"get {', '.join(n for _, n in get)}")
    return _receipt(s, "propose trade", detail, verified=sent)


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
