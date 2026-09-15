"""The selector GROUP registry — every group, where it lives, how to find it again.

`selectors.py` holds the candidates. This file holds the *metadata* the
self-healing loop needs and the candidates cannot carry: which page a group
renders on, what evidence identifies the right element when every candidate has
gone stale, and whether an empty result is a failure or just this time of year.

🔴 Why this exists as a separate file: on 2026-09-14 the Buccaneers D/ST claim
passed every gate and then could not find an Add/Claim button, and the standing
advice — "run scripts/discover_selectors.py" — would not have caught it. That
script probed four lineup selectors and nothing on the add modal. A registry
that is the SINGLE enumeration of every group makes that class of blind spot
impossible: the probe iterates this file, so a group that exists is a group that
gets checked.

Adding a selector group to selectors.py without adding it here is the one
mistake this design cannot detect on its own — so
`tests/test_selfheal.py::test_registry_covers_selectors` fails the build for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from core.browser import selectors as S

#: Where a group renders. The prober navigates once per target and probes every
#: group that lives there, rather than one page load per selector.
TEAM = "team"
ADD = "add"
DRAFT = "draft"
TRADE = "trade"


@dataclass(frozen=True)
class Group:
    """One selector group and everything needed to re-find it from scratch."""

    name: str
    #: The current candidate string from selectors.py (comma-separated).
    builtin: str
    #: Which page it renders on.
    target: str
    #: The element's role, used to constrain a discovery scan. One of
    #: "button", "input", "row", "text".
    kind: str = "button"
    #: Words that should appear in the element's text, title, aria-label or
    #: class when it IS the right element. Lowercase. Discovery scores a
    #: candidate by how many of these it carries.
    words: tuple[str, ...] = ()
    #: Words that disqualify a candidate outright. This is where the
    #: 2026-09-12 OneTrust bug lives as a permanent rule: a "Submit" inside the
    #: cookie-consent dialog is never a lineup save.
    never: tuple[str, ...] = ("onetrust", "save-preference", "consent", "cookie")
    #: True when resolving to zero elements is NORMAL and must not be reported
    #: as a failure. LINEUP_EDIT_BUTTON is the case that forced this: ESPN's
    #: in-season team page is permanently in edit mode, so the button is simply
    #: absent, and a prober that called that a failure returned exit 1 on every
    #: single run — a health signal that is always red is not a health signal.
    optional: bool = False
    #: Free text shown in the probe report.
    note: str = ""
    #: Groups that must ALSO resolve for this one to be meaningful; used to
    #: tell "the page did not load" apart from "this selector is stale".
    witness: tuple[str, ...] = field(default_factory=tuple)


#: 🔴 THE enumeration. Every group in selectors.py appears here exactly once.
GROUPS: tuple[Group, ...] = (
    # ── team page ────────────────────────────────────────────────────────────
    Group("LINEUP_SLOT_ROW", S.LINEUP_SLOT_ROW, TEAM, kind="row",
          words=("slot", "player", "row"),
          note="roster rows on My Team; the witness that the page rendered"),
    Group("LINEUP_MOVE_BUTTON", S.LINEUP_MOVE_BUTTON, TEAM,
          words=("move",), witness=("LINEUP_SLOT_ROW",),
          note="opens slot-selection mode; in-season this is always present"),
    Group("LINEUP_HERE_BUTTON", S.LINEUP_HERE_BUTTON, TEAM,
          words=("here",), optional=True, witness=("LINEUP_MOVE_BUTTON",),
          note="only rendered AFTER a MOVE click — absent on a cold page"),
    Group("LINEUP_EDIT_BUTTON", S.LINEUP_EDIT_BUTTON, TEAM,
          words=("edit", "lineup"), optional=True,
          note="OBSOLETE in-season: the team page is permanently in edit mode. "
               "Absence is normal and is never a failure (2026-09-12)"),
    Group("LINEUP_SAVE_BUTTON", S.LINEUP_SAVE_BUTTON, TEAM,
          words=("save", "submit", "lineup"), optional=True,
          note="OBSOLETE in-season: MOVE+HERE commits immediately. Any match "
               "must be visible AND outside the consent dialog"),
    Group("TEAM_DROP_TOOLBAR", S.TEAM_DROP_TOOLBAR, TEAM,
          words=("drop",), witness=("LINEUP_SLOT_ROW",),
          note="toolbar Drop, not the ten per-row drop buttons"),

    # ── add / free-agent page (the 2026-09-14 blind spot) ────────────────────
    Group("PLAYER_SEARCH", S.PLAYER_SEARCH, ADD, kind="input",
          words=("search", "player", "name"),
          note="filters on ENTER; typing alone leaves the table unfiltered"),
    Group("PLAYER_TABLE_ROW", S.PLAYER_TABLE_ROW, ADD, kind="row",
          words=("row", "table"), witness=("PLAYER_SEARCH",),
          note="the witness that the FA table rendered"),
    Group("ADD_PLAYER_BUTTON", S.ADD_PLAYER_BUTTON, ADD,
          words=("add", "claim"), witness=("PLAYER_TABLE_ROW",),
          note="🔴 an ICON button with NO text (title='Add', .add-action-btn). "
               "This is the group that broke the Buccaneers claim on 2026-09-14"),
    Group("DROP_PLAYER_BUTTON", S.DROP_PLAYER_BUTTON, ADD,
          words=("drop",), optional=True,
          note="inside the add modal only. Never take the disabled variant"),
    Group("CONFIRM_BUTTON", S.CONFIRM_BUTTON, ADD,
          words=("continue", "confirm", "yes"), optional=True,
          note="commits the add modal; DISABLED until a drop is selected"),

    # ── trades ───────────────────────────────────────────────────────────────
    Group("TRADE_PROPOSE_BUTTON", S.TRADE_PROPOSE_BUTTON, TRADE,
          words=("propose", "trade"),
          note="entry point to the trade builder"),
    Group("TRADE_PLAYER_CHECKBOX", S.TRADE_PLAYER_CHECKBOX, TRADE, kind="input",
          words=("checkbox",), optional=True, witness=("TRADE_PROPOSE_BUTTON",)),
    Group("TRADE_REVIEW_BUTTON", S.TRADE_REVIEW_BUTTON, TRADE,
          words=("continue", "review", "trade"), optional=True,
          note="reads 'Continue', not 'Review Trade' (verified 2026-09-08)"),
    Group("TRADE_SEND_BUTTON", S.TRADE_SEND_BUTTON, TRADE,
          words=("send", "propose", "offer"), optional=True,
          note="on the page AFTER review; never yet observed live"),
    Group("TRADE_ACCEPT_BUTTON", S.TRADE_ACCEPT_BUTTON, TRADE,
          words=("accept",), optional=True),
    Group("TRADE_REJECT_BUTTON", S.TRADE_REJECT_BUTTON, TRADE,
          words=("reject", "decline"), optional=True),

    # ── draft room (only resolvable while a room is open) ─────────────────────
    Group("DRAFT_BOARD_CELL_ANY", S.DRAFT_BOARD_CELL_ANY, DRAFT, kind="row",
          words=("pick", "cell", "board"), optional=True),
    Group("DRAFT_PICK_TRAIN", S.DRAFT_PICK_TRAIN, DRAFT, kind="text",
          words=("pick", "train", "clock"), optional=True),
    Group("DRAFT_PICK_ROW", S.DRAFT_PICK_ROW, DRAFT, kind="row",
          words=("pick", "history", "row"), optional=True),
    Group("DRAFT_ON_CLOCK", S.DRAFT_ON_CLOCK, DRAFT, kind="text",
          words=("clock", "own", "pick"), optional=True),
    Group("DRAFT_TIMER", S.DRAFT_TIMER, DRAFT, kind="text",
          words=("clock", "timer", "countdown"), optional=True),
    Group("DRAFT_PLAYER_ROW", S.DRAFT_PLAYER_ROW, DRAFT, kind="row",
          words=("player", "row"), optional=True),
    Group("DRAFT_BUTTON", S.DRAFT_BUTTON, DRAFT,
          words=("draft",), optional=True),
    Group("DRAFT_SEARCH", S.DRAFT_SEARCH, DRAFT, kind="input",
          words=("search", "player", "name"), optional=True),
    Group("QUEUE_CONTAINER", S.QUEUE_CONTAINER, DRAFT, kind="row",
          words=("queue", "list"), optional=True),
    Group("QUEUE_ROW", S.QUEUE_ROW, DRAFT, kind="row",
          words=("queue", "row"), optional=True),
    Group("QUEUE_ADD_BUTTON", S.QUEUE_ADD_BUTTON, DRAFT,
          words=("queue",), optional=True),
    Group("QUEUE_REMOVE_BUTTON", S.QUEUE_REMOVE_BUTTON, DRAFT,
          words=("remove", "dequeue"), optional=True),
)

BY_NAME: dict[str, Group] = {g.name: g for g in GROUPS}


def for_target(target: str) -> tuple[Group, ...]:
    """Every group that renders on `target`."""
    return tuple(g for g in GROUPS if g.target == target)


def get(name: str) -> Group | None:
    return BY_NAME.get(name)


#: Groups a healing pass may rewrite. A group NOT in here is one where a wrong
#: guess is unrecoverable, so it stays human: DRAFT_BUTTON clicks a pick that
#: cannot be taken back, and the trade send/accept buttons move players between
#: rosters. Healing those is the one thing full autonomy still does not buy —
#: not because the loop cannot do it, but because the failure has no reverse.
HEALABLE: frozenset[str] = frozenset({
    "LINEUP_SLOT_ROW", "LINEUP_MOVE_BUTTON", "LINEUP_HERE_BUTTON",
    "TEAM_DROP_TOOLBAR",
    "PLAYER_SEARCH", "PLAYER_TABLE_ROW", "ADD_PLAYER_BUTTON",
    "DROP_PLAYER_BUTTON", "CONFIRM_BUTTON",
    "TRADE_PROPOSE_BUTTON", "TRADE_PLAYER_CHECKBOX", "TRADE_REVIEW_BUTTON",
    "DRAFT_SEARCH", "DRAFT_PLAYER_ROW", "QUEUE_CONTAINER", "QUEUE_ROW",
    "QUEUE_ADD_BUTTON", "QUEUE_REMOVE_BUTTON",
    "DRAFT_BOARD_CELL_ANY", "DRAFT_PICK_ROW", "DRAFT_PICK_TRAIN",
    "DRAFT_ON_CLOCK", "DRAFT_TIMER",
})
