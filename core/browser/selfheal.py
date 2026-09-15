"""Probe, discover, heal, verify — the loop that re-points a stale selector.

🔴 The failure this exists for. 2026-09-14: the Buccaneers D/ST claim cleared
every gate, then `add_drop` could not find an Add/Claim button. The lineup write
on the same session verified fine, so it was not the cookie — ESPN had moved a
class name. The system's entire response was to post "can you run
scripts/discover_selectors.py before Sunday" to Slack and stop. Sunday came and
went. Roughly 4 points at D/ST, lost to a class rename nobody was awake for.

The loop, in order, and every step is a gate on the next:

    probe     — does the group resolve on a live page? (read-only, always safe)
    discover  — if not, scan the DOM for the element it SHOULD be
    heal      — write the winner to overrides.json
    verify    — re-probe. An unverified heal is discarded, not kept.

Verification is not a formality. `discover` is a heuristic over a DOM, and a
heuristic that is allowed to write without proving itself is just a way to
corrupt the selector file automatically instead of manually. A candidate that
does not resolve on re-probe is rolled back on the spot.

## What this deliberately does not do

It does not click anything. Discovery reads the DOM and scores elements; it
never actuates one to see what happens. On a page where a wrong click drops a
player or sends a trade, "try it and find out" is not a diagnostic.

It does not heal groups outside `groups.HEALABLE` — the draft pick button and
the trade send/accept buttons. Those are the writes with no reverse (§10.6).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from core.browser import groups as G
from core.browser import healing_log, overrides

log = logging.getLogger(__name__)

#: A button/input group identifies ONE control, so a candidate that resolves to
#: more than this many elements is ambiguous and is rejected. Row and text
#: groups are exempt — PLAYER_TABLE_ROW matching 52 rows is the whole point.
MAX_MATCHES = {"button": 1, "input": 2}

#: A discovered candidate must beat this to be written. Tuned so that a single
#: incidental word match ("add" appearing in a class) is not enough: the winner
#: needs either two signals or one strong one (an exact title/aria-label).
MIN_SCORE = 2.0

#: Scanning every element on an ESPN page is slow and pointless. Each `kind`
#: maps to the roots a real candidate could possibly be.
ROOTS = {
    "button": "button, a[role=button], [role=button], input[type=submit]",
    "input": "input, textarea",
    "row": "tr, li, [class*=row i], [class*=card i]",
    "text": "div, span, section, header",
}


@dataclass
class Probe:
    """One group's result on a live page."""

    group: str
    resolved: str | None          # the candidate that matched
    count: int
    optional: bool
    note: str = ""
    healed: bool = False          # matched via an override rather than a built-in

    @property
    def ok(self) -> bool:
        return self.resolved is not None

    @property
    def broken(self) -> bool:
        """Resolved nothing AND that is not normal for this group.

        🔴 The distinction the old prober lacked. LINEUP_EDIT_BUTTON is absent
        in-season by design, so counting it as a failure returned exit 1 on
        every healthy run and made the exit code meaningless.
        """
        return not self.ok and not self.optional

    def __str__(self) -> str:
        mark = "OK  " if self.ok else ("----" if self.optional else "DEAD")
        via = " (healed)" if self.healed else ""
        return f"{mark} {self.group:24} {self.count:>4}  {self.resolved or '*** none matched ***'}{via}"


@dataclass
class Report:
    target: str
    probes: list[Probe] = field(default_factory=list)
    dom: str | None = None
    shot: str | None = None

    @property
    def broken(self) -> list[Probe]:
        return [p for p in self.probes if p.broken]

    @property
    def healthy(self) -> bool:
        return not self.broken

    def text(self) -> str:
        lines = [f"{'':4} {'GROUP':24} {'N':>4}  MATCHED"]
        lines += [str(p) for p in self.probes]
        n_ok = sum(1 for p in self.probes if p.ok)
        lines.append(f"\n{n_ok}/{len(self.probes)} resolved · {len(self.broken)} broken")
        if self.dom:
            lines.append(f"DOM  -> {self.dom}")
        if self.shot:
            lines.append(f"shot -> {self.shot}")
        return "\n".join(lines)


# ── probe ────────────────────────────────────────────────────────────────────


def probe_group(page, g: G.Group) -> Probe:
    """Try every candidate for `g` — healed first — and report the winner."""
    healed = overrides.get(g.name)
    chain = list(overrides.candidates(g.name, g.builtin))

    # Try whatever resolved LAST time first. Two reasons, and the second is the
    # one that matters: it is faster, and it stops a long-dead candidate that
    # has started matching some unrelated element after a redesign from winning
    # just because it is declared earlier in the file.
    winner = healing_log.last_winner(g.name)
    if winner and winner in chain:
        chain.remove(winner)
        chain.insert(0, winner)

    for cand in chain:
        try:
            n = page.locator(cand).count()
        except Exception:
            continue  # an invalid selector is a dead candidate, not a crash
        if n:
            healing_log.note_winner(g.name, cand)
            return Probe(g.name, cand, n, g.optional, g.note, healed=(cand == healed))
    return Probe(g.name, None, 0, g.optional, g.note)


def probe(page, target: str) -> Report:
    """Probe every group that renders on `target`. Read-only."""
    rep = Report(target=target)
    for g in G.for_target(target):
        rep.probes.append(probe_group(page, g))
    return rep


# ── discover ─────────────────────────────────────────────────────────────────


def _attrs(page, el) -> dict:
    """Pull the identifying attributes of one element in a single round trip.

    One `evaluate` per element rather than six `get_attribute` calls: on a page
    with 400 buttons the chatty version takes minutes, and a discovery pass that
    takes minutes will be run by nobody and will time out inside a sweep.
    """
    try:
        return el.evaluate(
            """e => ({
                tag: e.tagName.toLowerCase(),
                text: (e.innerText || e.textContent || '').trim().slice(0, 80),
                title: e.getAttribute('title') || '',
                aria: e.getAttribute('aria-label') || '',
                cls: e.getAttribute('class') || '',
                id: e.getAttribute('id') || '',
                ph: e.getAttribute('placeholder') || '',
                type: e.getAttribute('type') || '',
                disabled: e.hasAttribute('disabled') || e.getAttribute('aria-disabled') === 'true',
                visible: !!(e.offsetWidth || e.offsetHeight || e.getClientRects().length)
            })"""
        )
    except Exception:
        return {}


def _score(a: dict, g: G.Group) -> float:
    """How much this element looks like `g`.

    Weighting reflects which signals have actually been trustworthy on ESPN:
    an exact `title` or `aria-label` match is near-proof (that is how
    ADD_PLAYER_BUTTON was finally identified on 2026-09-08 — an icon button
    with no text at all), visible text is strong, a class-name substring is
    weak because ESPN's class names are full of incidental words.
    """
    if not a or not a.get("visible") or a.get("disabled"):
        return 0.0
    hay_strong = f"{a.get('title', '')} {a.get('aria', '')} {a.get('ph', '')}".lower()
    hay_text = str(a.get("text", "")).lower()
    hay_weak = f"{a.get('cls', '')} {a.get('id', '')}".lower()

    if any(bad in f"{hay_strong} {hay_weak}" for bad in g.never):
        return 0.0

    # Class/id split into hyphen- and underscore-delimited tokens. The whole
    # point: "claim" IS a token of `claim-action-btn` but "add" is only a
    # substring of "padding", and those two deserve very different weights.
    tokens = set(re.split(r"[^a-z0-9]+", hay_weak))

    score = 0.0
    for w in g.words:
        if re.search(rf"\b{re.escape(w)}\b", hay_strong):
            score += 2.0
        elif re.search(rf"\b{re.escape(w)}\b", hay_text):
            score += 1.5
        elif w in tokens:
            score += 1.25
        elif w in hay_weak:
            score += 0.5
    if score == 0.0:
        return 0.0
    # An action button with a short, exact label is the common ESPN shape
    # ("MOVE", "HERE", "Continue"). Reward it; it disambiguates a bare
    # <button>Drop</button> from a paragraph mentioning dropping a player.
    if g.kind == "button" and hay_text and len(hay_text) <= 12:
        score += 0.5
    # ESPN's icon buttons carry no text at all, so the only thing separating
    # them from a decorative div is that they are built like controls.
    if g.kind == "button" and ("btn" in tokens or "button" in tokens
                               or "action" in tokens):
        score += 0.75
    return score


def _class_rank(cls: str, words: tuple[str, ...]) -> float:
    """How specific is this class? Higher is better; 0 means unusable.

    The ranking exists because ESPN puts four or five classes on every control
    and only one of them identifies it. `Button` is on everything;
    `claim-action-btn` is on exactly the thing we want.
    """
    if not _meaningful_class(cls):
        return 0.0
    low = cls.lower()
    tokens = set(re.split(r"[^a-z0-9]+", low))
    # A bare framework/utility class identifies nothing.
    if low in {"button", "btn", "link", "row", "cell", "item", "input"}:
        return 0.0
    rank = 1.0
    if any(w in tokens for w in words):
        rank += 3.0            # carries the group's own vocabulary
    if "-" in cls or "_" in cls:
        rank += 1.0            # compound names are ESPN's semantic ones
    if any(t in tokens for t in ("action", "btn")):
        rank += 0.5
    return rank


def _selector_for(a: dict, words: tuple[str, ...] = ()) -> str | None:
    """The most durable CSS selector that identifies this element.

    Preference order is about surviving the NEXT redesign, not about being
    shortest: a semantic attribute (title/aria-label) is stable across visual
    rewrites, a hashed utility class is not. A class is taken only when it
    looks meaningful — `add-action-btn` yes, `css-1x7fma3` no.
    """
    tag = a.get("tag", "*")
    if a.get("title"):
        return f"{tag}[title='{a['title']}' i]"
    if a.get("aria"):
        return f"{tag}[aria-label='{a['aria']}' i]"
    if a.get("ph"):
        return f"{tag}[placeholder='{a['ph']}' i]"
    text = str(a.get("text", "")).strip()
    usable_text = text and len(text) <= 20 and "'" not in text and "\n" not in text

    ranked = sorted(
        ((_class_rank(c, words), c) for c in str(a.get("cls", "")).split()),
        key=lambda t: (-t[0], len(t[1])),
    )
    if ranked and ranked[0][0] > 0:
        cls = ranked[0][1]
        # Class says what KIND of control this is; text says WHICH one. ESPN
        # puts the same class on Add, Drop and Move, so the pair is what
        # actually identifies the button (2026-09-15).
        if usable_text:
            return f"{tag}.{cls}:text-is('{text}')"
        return f"{tag}.{cls}"
    if usable_text:
        return f"{tag}:text-is('{text}')"
    return None


#: A hashed/utility class carries no meaning and will change again next week.
_HASHED = re.compile(r"(^|[-_])(css|sc|jsx|hash)[-_]?[0-9a-z]{4,}$", re.I)


def _meaningful_class(cls: str) -> bool:
    if len(cls) < 4 or _HASHED.search(cls):
        return False
    if re.fullmatch(r"[a-z]{1,3}[0-9]*", cls, re.I):   # tailwind-ish: dn, pa2
        return False
    return bool(re.search(r"[a-z]{3,}-[a-z]{2,}|btn|button|action|search|queue", cls, re.I))


def find_disabled(page, g: G.Group, *, limit: int = 400) -> str | None:
    """Is the element PRESENT but disabled? Returns a description, or None.

    🔴 This check runs before discovery and is allowed to veto it. A disabled
    control and a renamed class look identical to a scan — both resolve nothing
    — but a disabled Add button means the roster cannot take the player (full
    bench), and the fix is a drop, not a selector. Healing that would point the
    group at whatever unrelated element scored next.

    Found the hard way: 2026-09-14's failed Buccaneers D/ST claim ran with a
    full bench and an empty RB starting slot.
    """
    root = ROOTS.get(g.kind, ROOTS["button"])
    try:
        loc = page.locator(root)
        n = min(loc.count(), limit)
    except Exception:
        return None
    for i in range(n):
        a = _attrs(page, loc.nth(i))
        if not a or not a.get("disabled"):
            continue
        # Score it as if it were enabled — we want to know whether THIS is the
        # element, not whether it is clickable.
        if _score({**a, "disabled": False, "visible": True}, g) >= MIN_SCORE:
            label = (a.get("title") or a.get("aria") or a.get("text")
                     or a.get("cls") or "?")
            return str(label)[:80]
    return None


def discover(page, g: G.Group, *, limit: int = 400) -> tuple[str | None, float, str]:
    """Scan the live DOM for the element `g` should be pointing at.

    Returns (candidate, score, why). Reads only — nothing is clicked.
    """
    root = ROOTS.get(g.kind, ROOTS["button"])
    try:
        loc = page.locator(root)
        n = min(loc.count(), limit)
    except Exception as e:
        return None, 0.0, f"could not scan the page: {e}"
    if not n:
        return None, 0.0, f"no {g.kind} elements on the page at all"

    best: tuple[float, str, dict] | None = None
    for i in range(n):
        a = _attrs(page, loc.nth(i))
        s = _score(a, g)
        if s <= 0:
            continue
        cand = _selector_for(a, g.words)
        if not cand:
            continue
        try:
            hits = page.locator(cand).count()
        except Exception:
            continue
        if hits == 0:
            continue  # the selector we just built must actually resolve
        cap = MAX_MATCHES.get(g.kind)
        if cap is not None and hits > cap:
            # Ambiguous: it found the right sort of thing but cannot say which.
            # Clicking one of several is the failure the row-scoping in
            # actions.py exists to prevent, so it is not written.
            log.debug("rejecting %s for %s: matches %d elements (cap %d)",
                      cand, g.name, hits, cap)
            continue
        if best is None or s > best[0]:
            best = (s, cand, a)

    if best is None:
        return None, 0.0, f"nothing on the page scored as {g.name}"
    s, cand, a = best
    if s < MIN_SCORE:
        return None, s, (f"best guess {cand!r} scored {s:.1f}, under the {MIN_SCORE} "
                         "bar — too weak to write")
    why = (f"score {s:.1f}: "
           + ", ".join(p for p in (
               f"title={a['title']!r}" if a.get("title") else "",
               f"aria={a['aria']!r}" if a.get("aria") else "",
               f"text={a['text']!r}" if a.get("text") else "",
               f"class={a['cls']!r}" if a.get("cls") else "",
           ) if p))
    return cand, s, why


# ── heal ─────────────────────────────────────────────────────────────────────


@dataclass
class Heal:
    group: str
    healed: bool
    candidate: str | None = None
    score: float = 0.0
    why: str = ""

    def __str__(self) -> str:
        if self.healed:
            return f"HEALED {self.group} -> {self.candidate}  ({self.why})"
        return f"could not heal {self.group}: {self.why}"


def heal_group(page, group: str) -> Heal:
    """Discover a replacement for `group`, write it, and VERIFY it resolves.

    An unverified candidate is rolled back rather than left on disk — a wrong
    override that persists is strictly worse than no override, because it sits
    in front of the built-ins on every future run.
    """
    g = G.get(group)
    if g is None:
        return Heal(group, False, why=f"{group!r} is not a registered selector group")
    if group not in G.HEALABLE:
        return Heal(group, False,
                    why=("not healable by policy — this write has no reverse, "
                         "so a wrong selector cannot be taken back (§10.6)"))

    blocked = find_disabled(page, g)
    if blocked:
        return Heal(group, False,
                    why=(f"{group} IS on the page but DISABLED ({blocked!r}). That is a "
                         "roster/state problem, not a stale selector — most often a full "
                         "bench with no drop selected. Refusing to heal: a new selector "
                         "cannot fix a control ESPN is deliberately greying out."))

    cand, score, why = discover(page, g)
    if not cand:
        healing_log.record_heal_failed(group, why)
        return Heal(group, False, score=score, why=why)

    # Verify BEFORE writing. A candidate that cannot prove itself never reaches
    # the file, so a bad heal is not something we have to undo.
    try:
        n = page.locator(cand).count()
    except Exception as e:
        return Heal(group, False, candidate=cand, score=score,
                    why=f"candidate {cand!r} is not a usable selector: {e}")
    if n == 0:
        return Heal(group, False, candidate=cand, score=score,
                    why=f"candidate {cand!r} resolved nothing on re-check — discarded")

    overrides.record(group, cand, why=why, verified=True)
    # Now prove the whole chain resolves, which is what the action layer will
    # actually call. If it somehow does not, roll back rather than leave an
    # override sitting in front of working built-ins.
    check = probe_group(page, g)
    if not check.ok:
        overrides.forget(group)
        return Heal(group, False, candidate=cand, score=score,
                    why=f"{cand!r} resolved alone but not through the candidate "
                        "chain — rolled back")
    overrides.commit(group, cand, why)
    healing_log.record_heal(group, cand, score, why)
    log.warning("SELF-HEAL %s -> %s (%s)", group, cand, why)
    return Heal(group, True, candidate=cand, score=score, why=why)


def heal_report(page, rep: Report) -> list[Heal]:
    """Attempt a heal for every broken group in a probe report."""
    return [heal_group(page, p.group) for p in rep.broken]


# ── navigation ───────────────────────────────────────────────────────────────


def goto_target(s, target: str):
    """Put the session on the page where `target`'s groups render."""
    from core.config import settings
    from core.espn.client import client

    cfg = settings()
    if target == G.ADD:
        return s.goto(f"/football/players/add?leagueId={cfg.league_id}&seasonId={cfg.season}")
    if target == G.DRAFT:
        return s.goto(f"/football/draft?leagueId={cfg.league_id}&seasonId={cfg.season}"
                      f"&teamId={client().my_team_id}&memberId={cfg.swid}")
    if target == G.TRADE:
        # Trades are proposed from the OTHER manager's team page, so that is
        # the only page where TRADE_PROPOSE_BUTTON exists (actions.py:585).
        # Any opponent will do for a probe; take the first that is not us.
        me = client().my_team_id
        other = next((t for t in _team_ids() if t != me), me)
        return s.goto(f"/football/team?leagueId={cfg.league_id}"
                      f"&teamId={other}&seasonId={cfg.season}")
    return s.goto(f"/football/team?leagueId={cfg.league_id}"
                  f"&teamId={client().my_team_id}&seasonId={cfg.season}")


def _team_ids() -> list[int]:
    """Every team id in the league, for picking a trade counterparty to probe."""
    try:
        from core.espn import league_state as ls

        return [t.team_id for t in ls.snapshot().teams.values()]
    except Exception as e:
        log.warning("could not list teams (%s) — probing trades on our own page", e)
        return []


def run(target: str, *, heal: bool = False, headless: bool = True,
        settle_ms: int = 6000) -> tuple[Report, list[Heal]]:
    """Open a session, probe `target`, optionally heal, and dump the evidence.

    This is the entry point for the script, the MCP tool and the pre-flight
    alike — one implementation, so the agent and the human are never looking at
    two different diagnostics.
    """
    from core.browser.session import EspnSession

    s = EspnSession(headless=headless)
    s.start()
    try:
        goto_target(s, target)
        s.page.wait_for_timeout(settle_ms)
        s.dismiss_overlays()

        rep = probe(s.page, target)
        for pr in rep.probes:
            if pr.broken:
                healing_log.record_break(pr.group, target)
            elif pr.ok:
                healing_log.record_resolved(pr.group, pr.resolved, pr.healed)
        heals: list[Heal] = []
        if heal and rep.broken:
            heals = heal_report(s.page, rep)
            rep = probe(s.page, target)  # re-probe so the report reflects the heals
        # Evidence is captured whenever something is wrong, so a heal that goes
        # sideways can still be reconstructed after the fact.
        if rep.broken or heals:
            try:
                rep.dom = str(s.dump_dom(f"selfheal-{target}"))
                rep.shot = str(s.screenshot(f"selfheal-{target}"))
            except Exception as e:
                log.warning("could not capture evidence: %s", e)
        return rep, heals
    finally:
        s.close()
