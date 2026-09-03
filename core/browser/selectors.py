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

#: Rows in the pick history / "recent picks" list.
DRAFT_PICK_ROW = (
    ".draft-columns--pick-history .pick-history-row, "
    "[class*=pickHistory] [class*=row], "
    "[class*=draft-pick-row]"
)

#: The "on the clock" banner.
DRAFT_ON_CLOCK = "[class*=onTheClock], [class*=on-the-clock], [class*=OnTheClock]"

#: Countdown timer text.
DRAFT_TIMER = "[class*=draft-timer], [class*=DraftTimer], [class*=countdown]"

#: Rendered when the draft is over.
DRAFT_COMPLETE = "[class*=draft-complete], [class*=DraftComplete]"

#: The player table in the draft room.
DRAFT_PLAYER_ROW = "table tbody tr, [class*=player-row], [class*=PlayerRow]"

#: The "Draft" action button inside a player row.
DRAFT_BUTTON = (
    "button:has-text('Draft'), "
    "[class*=draft-button], "
    "button[title*='Draft']"
)

#: The player-search box in the draft room.
DRAFT_SEARCH = (
    "input[placeholder*='Search'], "
    "input[type=search], "
    "[class*=playerSearch] input"
)

# ── the queue (§3.3 — the load-bearing mechanism) ────────────────────────────

#: Container holding the queued players, in order.
QUEUE_CONTAINER = (
    "[class*=queue] [class*=list], "
    "[class*=Queue] [class*=List], "
    "[class*=player-queue]"
)

#: One row in the queue.
QUEUE_ROW = f"{QUEUE_CONTAINER} [class*=row], {QUEUE_CONTAINER} li"

#: Add-to-queue control inside a player row.
QUEUE_ADD_BUTTON = (
    "button[title*='queue' i], "
    "button[aria-label*='queue' i], "
    "[class*=addToQueue]"
)

#: Remove-from-queue control inside a queue row.
QUEUE_REMOVE_BUTTON = (
    "button[title*='remove' i], "
    "button[aria-label*='remove' i], "
    "[class*=removeFromQueue]"
)

# ── team / roster pages ──────────────────────────────────────────────────────

LINEUP_EDIT_BUTTON = "button:has-text('Edit Lineup'), a:has-text('Edit Lineup')"
LINEUP_SAVE_BUTTON = "button:has-text('Save'), button:has-text('Submit')"
LINEUP_SLOT_ROW = "table tbody tr, [class*=player-row]"
LINEUP_MOVE_BUTTON = "button:has-text('MOVE'), button:has-text('Move')"

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


def norm(name: str) -> str:
    return (
        name.lower()
        .replace(".", "")
        .replace("'", "")
        .replace("-", " ")
        .strip()
    )


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
