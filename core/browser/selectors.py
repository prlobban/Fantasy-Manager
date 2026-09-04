"""EVERY selector in one file.

When ESPN ships a redesign, this is the only file that changes. Nothing else in
the codebase may contain a CSS selector or an XPath.

STATUS, 2026-09-03: these are CANDIDATES, not verified selectors. The draft room
only exists while a draft is running, so they cannot be confirmed until the
league's Practice Draft is open. `scripts/discover_selectors.py` runs against a
live room, tries each candidate, and reports which ones actually resolve.

Each entry lists several candidates in priority order and is resolved at run time
by `first_present`, so a single ESPN class-name change degrades to the next
candidate instead of breaking the draft.
"""

from __future__ import annotations

import re

# ── draft room ───────────────────────────────────────────────────────────────

#
# ✅ VERIFIED 2026-09-04 against a live League-Specific Practice Draft room
# (three rooms, headless). The first candidate in each group is the one that
# resolved; the rest are fallbacks for a redesign. Where a group is a single
# selector it was verified and has no plausible fallback.
#
# Facts about the room worth knowing before touching any of this:
#   - It opens in a POPUP from the team page:
#       /football/draft?leagueId=..&seasonId=..&teamId=..&memberId={SWID}
#   - The Board and Pick History tabs are in the DOM even when hidden
#     (`tab__item dn`), so picks can be read WITHOUT switching tabs — but a
#     hidden element's inner_text() is "", so read them with textContent.
#   - The player table is a virtualised FixedDataTable: only ~20-30 rows exist
#     at a time. To reach a player, search for him (fill + ENTER; typing alone
#     does not filter).
#   - Queue rows carry the ESPN player id in data-drag-id, and show
#     ABBREVIATED names ("J. Taylor"). Never match the queue by name.
#   - Drag-and-drop reorder in the queue works with Playwright's drag_to.

#: Completed picks on the (hidden) Board grid. Each has .roundPick "R.P",
#: .playerFirstName, .playerLastName, .playerProTeam, .positionPill.
DRAFT_BOARD_CELL_DONE = ".draft-board-grid-pick-cell.completedPick"
DRAFT_BOARD_CELL_ANY = ".draft-board-grid-pick-cell"

#: Rows in the (hidden) Pick History tab — the fallback pick reader.
#: Row text: "1 Puka Nacua Q LAR WR Amon Drugz 310.5 292 4"
DRAFT_PICK_ROW = ".pick-history .public_fixedDataTable_bodyRow"

#: The pick train across the top. Its text carries
#: "RND 1 OF 13 00:30 ON THE CLOCK: PICK 4 big P PICK 5 AUTO ..."
DRAFT_PICK_TRAIN = ".pickTrain, .pick-train__content"
DRAFT_ON_CLOCK = ".pick-component.own-pick, [class*=onTheClock], [class*=on-the-clock]"

#: Countdown timer.
DRAFT_TIMER = ".clock__container, [class*=draft-timer], [class*=countdown]"

#: Rendered when the draft is over (unverified — the practice room was
#: abandoned before the end; the loop also stops on the pick count).
DRAFT_COMPLETE = "[class*=draft-complete], [class*=DraftComplete], .draft-over"

#: A row of the player table (virtualised).
DRAFT_PLAYER_ROW = ".draft-players .public_fixedDataTable_bodyRow"
#: The full player name inside a player-table row.
DRAFT_PLAYER_NAME = ".playerinfo__playername"

#: The action button in a player row reads QUEUE normally and DRAFT when we
#: are on the clock. Same element, different text.
DRAFT_BUTTON = "button:has-text('DRAFT'), button:has-text('Draft'), [class*=draft-button]"

#: The player-search box. Filters on ENTER, not on typing.
DRAFT_SEARCH = (
    "input[placeholder='Player Name'], "
    "input[placeholder*='Search'], "
    "input[type=search]"
)
#: The X that clears the search (leaving a search active hides the table).
DRAFT_SEARCH_CLEAR = ".player--search--clear"

# ── the queue (§3.3 — the load-bearing mechanism) ────────────────────────────

#: Container holding the queued players, in order.
QUEUE_CONTAINER = ".pick-queue, [class*=queue] [class*=list], [class*=player-queue]"

#: One row in the queue. data-drag-id == ESPN player id.
QUEUE_ROW = ".pick-queue tbody tr[data-drag-id]"
QUEUE_ROW_ID_ATTR = "data-drag-id"

#: Add-to-queue control inside a PLAYER-TABLE row (text "QUEUE").
QUEUE_ADD_BUTTON = (
    "button:has-text('QUEUE'), "
    "button[title*='queue' i], "
    "button[aria-label*='queue' i]"
)

#: Remove-from-queue control inside a QUEUE row (text "Remove").
QUEUE_REMOVE_BUTTON = (
    "button.Button--dequeue, "
    "button:has-text('Remove'), "
    "button[title*='remove' i]"
)

#: ESPN plays a full-screen Lottie animation on every pick. While it is up it
#: intercepts every pointer event — clicks and drags silently do nothing
#: (found in the first rehearsal: 0/12 queue ops landed in a room where the
#: auto-teams picked every two seconds). The room injects CSS that hides it.
PICK_ANIMATION_OVERLAY = ".LottieFullScreenWrapper, .LottieFullScreen"
ROOM_CSS = (
    ".LottieFullScreenWrapper, .LottieFullScreen "
    "{ display: none !important; pointer-events: none !important; }"
)

#: The Autopick toggle in the queue header. We leave it OFF: on, ESPN drafts
#: the queue top the instant our turn starts, which removes the chance to
#: re-rank after the pick before ours.
QUEUE_AUTOPICK_TOGGLE = ".autoPick-toggle input[type=checkbox]"

# ── team / roster pages ──────────────────────────────────────────────────────

LINEUP_EDIT_BUTTON = "button:has-text('Edit Lineup'), a:has-text('Edit Lineup')"
LINEUP_SAVE_BUTTON = "button:has-text('Save'), button:has-text('Submit')"
LINEUP_SLOT_ROW = "table tbody tr, [class*=player-row]"
LINEUP_MOVE_BUTTON = "button:has-text('MOVE'), button:has-text('Move')"
#: After MOVE is clicked on a player, every slot he can go to shows this.
LINEUP_HERE_BUTTON = "button:has-text('HERE'), button:has-text('Here')"

#: Any element that reads as "a row" — used to scope a button to the row that
#: carries a given player's name, so a click after a search can never land on
#: the first button of a different player.
ANY_ROW = "tr, li, [class*=row], [class*=Row], [class*=card], [class*=Card]"

ADD_PLAYER_BUTTON = "button:has-text('Add'), button:has-text('Claim')"
DROP_PLAYER_BUTTON = "button:has-text('Drop')"
CONFIRM_BUTTON = "button:has-text('Confirm'), button:has-text('Yes'), button:has-text('Continue')"

# ── trades ───────────────────────────────────────────────────────────────────

TRADE_PROPOSE_BUTTON = "button:has-text('Propose Trade'), a:has-text('Propose Trade')"
TRADE_ACCEPT_BUTTON = "button:has-text('Accept')"
TRADE_REJECT_BUTTON = "button:has-text('Reject'), button:has-text('Decline')"

# ── helpers ──────────────────────────────────────────────────────────────────

#: "R1 P4  Jahmyr Gibbs, Det RB" and friends.
_PICK_RE = re.compile(
    r"(?:R\s*(?P<rnd>\d+)\D{0,4}P\s*(?P<pick>\d+)|(?P<overall>\d+)\s*[.)])\s*(?P<name>[A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+)+)"
)


def parse_pick_row(text: str) -> tuple[int, str] | None:
    """Pull (overall, player name) out of a pick-history row.

    Returns None rather than guessing when the row doesn't match — a
    mis-parsed pick would corrupt the room model, which is worse than
    missing one.
    """
    m = _PICK_RE.search(text.replace("\n", " "))
    if not m:
        return None
    name = m.group("name").strip()
    if m.group("overall"):
        return int(m.group("overall")), name
    # Round/pick form needs the team count to convert; the caller has it, so
    # encode the round and pick and let room.py resolve it.
    rnd, pick = int(m.group("rnd")), int(m.group("pick"))
    return _encode_round_pick(rnd, pick), name


def _encode_round_pick(rnd: int, pick: int) -> int:
    """Placeholder overall for a round/pick pair. room.py normalises this once
    it knows the team count; encoded so it can never collide with a real
    overall pick number."""
    return -(rnd * 1000 + pick)


def decode_round_pick(encoded: int, n_teams: int) -> int:
    if encoded >= 0:
        return encoded
    v = -encoded
    rnd, pick = divmod(v, 1000)
    return (rnd - 1) * n_teams + pick


#: "RND 1 OF 13 00:30 ON THE CLOCK: PICK 4 big P PICK 5 AUTO rick ..."
_ON_CLOCK_RE = re.compile(
    r"ON THE CLOCK:\s*PICK\s*(?P<pick>\d+)\s*(?P<team>.*?)\s*(?:PICK\s*\d+|ROUND|$)",
    re.I | re.S,
)
_ROUND_RE = re.compile(r"RND\s*(?P<rnd>\d+)\s*OF\s*(?P<of>\d+)", re.I)


def parse_pick_train(text: str) -> tuple[int | None, str, int | None]:
    """(overall pick on the clock, team name on the clock, current round)."""
    t = " ".join(text.split())
    m = _ON_CLOCK_RE.search(t)
    r = _ROUND_RE.search(t)
    return (
        int(m.group("pick")) if m else None,
        (m.group("team").strip() if m else ""),
        int(r.group("rnd")) if r else None,
    )


def norm(name: str) -> str:
    return (
        name.lower()
        .replace(".", "")
        .replace("'", "")
        .replace("-", " ")
        .strip()
    )


def search_player(page, name: str, *, settle_ms: int = 1200) -> bool:
    """Type a name into the room's search box and apply it (ENTER).

    Verified: typing alone does not filter the virtualised table; ENTER does.
    Returns whether a box was found.
    """
    box = first_present(page, DRAFT_SEARCH)
    if box is None:
        return False
    box.first.fill(name)
    box.first.press("Enter")
    page.wait_for_timeout(settle_ms)
    return True


def clear_search(page) -> None:
    """Clear the search filter. Cheap and bounded: the X is only rendered
    while the box has text, and a 20 s default click timeout on a hidden X
    cost the first rehearsal ~20 s per queue op (2026-09-04)."""
    try:
        box = first_present(page, DRAFT_SEARCH)
        if box is None:
            return
        if box.first.input_value():
            x = page.locator(DRAFT_SEARCH_CLEAR)
            try:
                if x.count() and x.first.is_visible():
                    x.first.click(timeout=1_500)
                    page.wait_for_timeout(300)
                    return
            except Exception:
                pass
            box.first.fill("")
            box.first.press("Enter")
            page.wait_for_timeout(300)
    except Exception:
        pass


def player_row(page, name: str, *, row_selector: str | None = None):
    """The player-table row whose NAME cell is `name`.

    Resolved by content, not by index: the table is virtualised and re-renders
    between a count() and a click(), so an nth(i) locator can land on a
    different row than the one that was inspected. Returns a locator or None.
    """
    rows = page.locator(row_selector or DRAFT_PLAYER_ROW)
    exact = rows.filter(has=page.locator(DRAFT_PLAYER_NAME, has_text=re.compile(
        r"^\s*" + re.escape(name) + r"\s*$", re.I)))
    try:
        if exact.count():
            return exact.first
        loose = rows.filter(has=page.locator(DRAFT_PLAYER_NAME, has_text=re.compile(
            re.escape(name), re.I)))
        if loose.count():
            return loose.first
    except Exception:
        return None
    return None


def in_row_with(page, text: str, *button_candidates: str):
    """The first button candidate that sits inside a row containing `text`.

    Returns a locator or None. This is the guard against the class of bug
    where "search for X, click the first Draft button" drafts whoever ESPN
    happened to render first — a wrong click is the one failure the queue
    safety net cannot undo.
    """
    if not text:
        return None
    try:
        rows = page.locator(ANY_ROW).filter(has_text=re.compile(re.escape(text), re.I))
        if rows.count() == 0:
            return None
        for sel in button_candidates:
            for group in sel.split(","):
                group = group.strip()
                if not group:
                    continue
                try:
                    loc = rows.locator(group)
                    if loc.count() > 0:
                        return loc
                except Exception:
                    continue
    except Exception:
        return None
    return None


def first_present(page, *candidates: str, timeout: int = 2000):
    """Return the first candidate selector that actually resolves on the page.

    This is what makes a redesign survivable: the code asks for "the queue
    container" and the file offers three ways ESPN might be spelling it today.
    """
    for sel in candidates:
        try:
            loc = page.locator(sel)
            if loc.count() > 0:
                return loc
        except Exception:
            continue
    return None
