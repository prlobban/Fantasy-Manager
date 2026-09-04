"""THE LIVE DRAFT LOOP.

Deterministic. No LLM anywhere in this file (§3.2, §8.7, §10.2). Everything
expensive happened pre-draft in board.py; on the clock this re-sorts a table it
already has.

The loop, every ~2 seconds:

    picks = reader.read()                  # API primary, DOM fallback
    if new picks:
        room.apply(picks)
        plan = picker.rank(board, room)    # pure, ~10ms on 450 players
        queue.sync(plan.top(N))            # diff only, usually 1-3 UI ops
    if it is our turn:
        actions.draft_player(plan.best)    # the fast path
        # the queue is already correct, so if this fails ESPN autopicks
        # plan.best anyway when the timer expires (§3.3)

Time budget: this league gives 90 seconds per pick. A full cycle measures in
low hundreds of milliseconds, so the clock is never close — but the ORDER
matters. The queue write happens before the click attempt, always, because the
queue is what makes a failure survivable.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from core.browser.session import EspnSession
from core.config import settings
from core.draft import board as board_mod
from core.draft import picker
from core.draft.queue import QueueSync
from core.draft.reader import ApiReader, DomReader, FallbackReader
from core.draft.room import RoomModel
from core.espn import health
from core.espn.client import client
from core.gates import kill_switch
from core.model.priors import priors
from core.model.schema import Action, ActionKind
from core.notify import notify
from core.state import decisions
from core.state.draftlog import DraftLog

log = logging.getLogger(__name__)


@dataclass
class DraftConfig:
    poll_seconds: float = 2.0
    queue_depth: int | None = None
    click: bool = True
    dry_run: bool = False
    use_browser: bool = True
    max_minutes: int = 240
    draft_url: str | None = None
    #: A League-Specific Practice Draft: skip the 10:00 order lock (the room
    #: seats us at the slot we chose) and read picks from the DOM only.
    practice: bool = False


@dataclass
class DraftStats:
    cycles: int = 0
    picks_seen: int = 0
    queue_ops: int = 0
    our_picks: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def preflight(cfg: DraftConfig) -> tuple[board_mod.Board, RoomModel]:
    """Everything that must be true before 11:00."""
    s = settings()
    log.info("--- preflight ---")

    h = health.check(kill_on_fail=False)
    log.info("health: %s", h.summary())
    if not h.ok:
        raise RuntimeError("health check failed: " + "; ".join(h.failures))

    if not cfg.dry_run and not kill_switch.is_on():
        raise RuntimeError(
            f"kill switch is off ({kill_switch.state()[:80]}). "
            "Turn it on deliberately before the draft."
        )

    bd = board_mod.load()
    age = bd.age_hours()
    log.info("board: %d players, built %.1fh ago", len(bd.players), age)
    if bd.is_stale():
        raise RuntimeError(
            f"board is {age:.1f}h old (limit "
            f"{priors().get('draft.board_max_age_hours')}h). Rebuild it: "
            "python scripts/build_board.py"
        )

    # 🔴 THE DRAFT ORDER IS RANDOMISED ONE HOUR BEFORE THE DRAFT.
    # board.load() re-reads mSettings live rather than trusting anything cached,
    # so pick_order here is whatever ESPN says right now — but "right now" is
    # only meaningful once the randomisation has happened. Starting the loop
    # before then would plan the whole draft around a slot we are about to lose.
    if cfg.practice:
        log.warning("PRACTICE room: skipping the order lock; our slot is whatever "
                    "was chosen on the practice panel and must match the league's "
                    "current slot for pick numbers to line up")
    else:
        _assert_pick_order_final(bd)

    c = client()
    room = RoomModel(facts=bd.facts, my_team_id=c.my_team_id)
    _ = room.my_picks  # raises a readable error if we are not in the pick order
    slot = bd.facts.pick_order.index(c.my_team_id) + 1
    log.info(
        "we are team %s (%s), slot %d of %d, picks %s",
        c.my_team_id, s.team_name, slot, len(bd.facts.pick_order), room.my_picks,
    )
    notify(
        "info",
        f"Draft slot {slot} of {len(bd.facts.pick_order)}",
        f"picks: {room.my_picks}\ngaps between our turns: "
        f"{[b - a for a, b in zip(room.my_picks, room.my_picks[1:], strict=False)]}",
    )
    return bd, room


#: ESPN randomises the order this many minutes before the draft (league setting,
#: confirmed by Pearce 2026-09-03). Anything read earlier is provisional.
ORDER_LOCK_MINUTES = 60


def _assert_pick_order_final(bd) -> None:
    """Refuse to plan a draft around a pick order that is still going to change."""
    draft_at = bd.facts.draft_at
    if draft_at is None:
        log.warning("no draft time in settings — cannot verify the order is final")
        return

    from datetime import timedelta

    lock_at = draft_at - timedelta(minutes=ORDER_LOCK_MINUTES)
    # draft_at is timezone-aware (settings._ms_to_dt); compare like with like.
    now = datetime.now().astimezone()
    if draft_at.tzinfo is None:
        now = now.replace(tzinfo=None)
    if now < lock_at:
        raise RuntimeError(
            f"the draft order is randomised at {lock_at:%H:%M} "
            f"({ORDER_LOCK_MINUTES} min before the {draft_at:%H:%M} draft) and it is "
            f"only {now:%H:%M}. The current order is provisional — starting now "
            "would plan every pick around a slot we are about to lose. "
            "Start the loop after the order locks."
        )
    log.info("pick order read at %s, after the %s lock — treating as final",
             f"{now:%H:%M}", f"{lock_at:%H:%M}")


def run(cfg: DraftConfig | None = None) -> DraftStats:
    cfg = cfg or DraftConfig()
    p = priors()
    depth = cfg.queue_depth or int(p.get("draft.queue_depth"))

    dlog = DraftLog("practice" if cfg.practice else "live")
    dlog.attach()
    log.info("draft log -> %s", dlog.dir)

    try:
        bd, room = preflight(cfg)
    except Exception as e:
        dlog.problem("preflight failed", str(e)[:300])
        dlog.close()
        raise

    dlog.header(
        league=bd.facts.settings.name, team=settings().team_name,
        slot=bd.facts.pick_order.index(room.my_team_id) + 1,
        teams=len(bd.facts.pick_order), my_picks=room.my_picks,
        board_players=len(bd.players), board_age_h=bd.age_hours(),
        dry_run=cfg.dry_run, click=cfg.click,
    )
    rows = bd.rows
    by_id = bd.by_id
    stats = DraftStats()

    session = None
    qsync = None
    dom_reader = None

    try:
        if cfg.use_browser:
            session = EspnSession(headless=True).start()
            s_ = bd.facts.settings
            # Prove the session on the TEAM page first: it is the only page
            # with owner-only markers. The draft room is then entered under
            # the weaker "no login block appeared" check, which is all it can
            # offer. Going straight to the room used to raise NotLoggedIn on a
            # perfectly good session (2026-09-04 review).
            session.goto(
                f"/football/team?leagueId={s_.league_id}&seasonId={s_.season}"
                f"&teamId={room.my_team_id}"
            )
            # Verified 2026-09-04: the room URL carries teamId and memberId
            # (our SWID). Without them the draft path bounces to the fantasy
            # home page. The room only exists once the draft is open.
            if cfg.practice and not cfg.draft_url:
                slot = bd.facts.pick_order.index(room.my_team_id) + 1
                _open_practice_room(session, slot)
            else:
                url = cfg.draft_url or (
                    f"/football/draft?leagueId={s_.league_id}&seasonId={s_.season}"
                    f"&teamId={room.my_team_id}&memberId={settings().swid}"
                )
                _join_room_when_open(session, url, bd.facts.draft_at)
            _wait_for_room(session)
            qsync = QueueSync(session, by_id=by_id)
            dom_reader = DomReader(session, by_name=_by_name(bd), n_teams=room.n_teams)

        api_reader = ApiReader(by_id=by_id)
        if cfg.practice and dom_reader is not None:
            # The league's mDraftDetail knows nothing about a practice room.
            reader = FallbackReader(_NoPicks(), dom_reader)
        else:
            reader = FallbackReader(api_reader, dom_reader)

        global _READER
        _READER = reader

        notify("info", "Draft loop started" + (" (DRY RUN)" if cfg.dry_run else ""),
               f"{bd.facts.settings.name} · our picks {room.my_picks}")

        deadline = time.monotonic() + cfg.max_minutes * 60
        last_plan = None
        me = settings().team_name.strip().casefold()

        def dom_says_our_turn() -> bool:
            # The room model lags the DOM by a cycle; the pick train knows
            # first. Editing the queue on our own turn just burns the add
            # budget on ":03" countdown buttons — and a sync that runs through
            # our clock blocks the click.
            if dom_reader is None:
                return False
            _, who = dom_reader.on_the_clock()
            return bool(who) and who.strip().casefold() == me

        #: overall pick number -> (attempts, monotonic time of the last one).
        #: ESPN's API can lag the click by seconds; without this the loop
        #: re-clicked the same player every 2s until the pick showed up.
        attempted: dict[int, tuple[int, float]] = {}

        while time.monotonic() < deadline:
            stats.cycles += 1
            try:
                cycle_start = time.monotonic()
                picks = reader.read()
                new = room.apply(picks)

                if new or last_plan is None:
                    stats.picks_seen = room.picks_made
                    last_plan = picker.rank(rows, room)
                    if qsync and new:
                        qsync.reset_attempts()

                    for i, pk in enumerate(new):
                        # Only the LAST of a batch changes what the board leads
                        # with; the earlier ones are recorded as they happened.
                        lead = (last_plan.best.player.name
                                if last_plan.best and i == len(new) - 1 else None)
                        log.info("pick %d: %s (%s) by team %s%s",
                                 pk.overall, pk.name,
                                 pk.pos.value if pk.pos else "?", pk.team_id,
                                 f" → best now {lead}" if lead else "")
                        dlog.room_pick(pk, ours=pk.team_id == room.my_team_id,
                                       best_now=lead)

                # ── the queue write comes FIRST, always (§3.3) ───────────────
                # Every cycle, not only after a new pick: when nothing changed
                # this is one DOM read and zero ops, and when an op failed to
                # land last time this is what retries it.
                #
                # Except on our own turn. Verified 2026-09-04: while we are on
                # the clock the queue's Remove buttons become DRAFT buttons, so
                # edits cannot land. The queue was synced on the cycles before;
                # the click below goes for the true #1 regardless.
                our_turn = room.on_the_clock_is_us or dom_says_our_turn()
                if qsync and not our_turn and not cfg.dry_run and qsync.ensure_autopick_off():
                    notify("warn", "ESPN had switched us to Autopick — turned it off",
                           "This happens after a missed pick. The queue covered it.")
                if qsync and last_plan and last_plan.candidates and not our_turn:
                    target = [c.player.espn_id for c in last_plan.top(depth)]
                    ops, ok = qsync.sync(target, dry_run=cfg.dry_run,
                                         budget_s=QUEUE_SYNC_BUDGET_S,
                                         abort=dom_says_our_turn)
                    aborted = room.on_the_clock_is_us or dom_says_our_turn()
                    our_turn = aborted
                    stats.queue_ops += ok
                    dlog.queue(
                        target=[by_id[i].name for i in target if i in by_id],
                        current=qsync.last_current_size, ops=ops,
                        landed=ok, aborted=aborted,
                    )
                    if ops and ok < len(ops):
                        log.warning("queue sync: only %d/%d ops landed", ok, len(ops))

                # ── our turn ─────────────────────────────────────────────────
                if our_turn and last_plan and last_plan.best:
                    # A sync can take 25 s; the plan from the top of the cycle
                    # may predate the pick right before ours. Rehearsal #4
                    # clicked on a player the previous team had just taken.
                    # Re-read and re-rank so the click is on a live target.
                    if room.apply(reader.read()):
                        last_plan = picker.rank(rows, room)
                        if qsync:
                            qsync.reset_attempts()
                    overall = room.next_overall
                    n_tries, last_at = attempted.get(overall, (0, 0.0))
                    if last_plan.best and (n_tries == 0 or (
                        n_tries < PICK_MAX_ATTEMPTS
                        and time.monotonic() - last_at >= PICK_RETRY_SECONDS
                    )):
                        attempted[overall] = (n_tries + 1, time.monotonic())
                        if n_tries == 0:
                            # The whole board state and every adjustment term,
                            # written BEFORE the click so it survives a failure.
                            dlog.our_turn(last_plan, room, overall=overall)
                        counted = _make_pick(session, cfg, room, last_plan, stats,
                                             dlog, retry=n_tries > 0)
                        if not counted:
                            # The target was gone anyway: not an attempt. Go
                            # again next cycle rather than after the retry wait.
                            attempted[overall] = (n_tries, 0.0)
                        # Re-read immediately so we don't double-pick.
                        if room.apply(reader.read()):
                            last_plan = picker.rank(rows, room)

                if reader.is_complete() or room.is_complete:
                    log.info("draft complete")
                    break

                elapsed = time.monotonic() - cycle_start
                time.sleep(max(0.0, cfg.poll_seconds - elapsed))

            except KeyboardInterrupt:
                raise
            except Exception as e:
                stats.errors.append(str(e))
                dlog.problem("cycle error", str(e)[:300])
                log.exception("cycle error (continuing — the queue still stands)")
                # The queue is the net: a broken cycle does not blow a pick,
                # it just means ESPN autopicks our current #1.
                time.sleep(cfg.poll_seconds)

    finally:
        if session:
            session.close()
        try:
            _postflight(bd, room, stats, dlog)
        except Exception:
            log.exception("postflight failed")
        log.info("draft log written to %s", dlog.dir)
        dlog.close()

    return stats


#: One click per pick, one retry if the API still shows the slot empty after
#: this many seconds. Never more: the queue has our #1 on top and ESPN will
#: autopick him at the horn, so a third click only risks hitting the wrong row.
PICK_MAX_ATTEMPTS = 2
PICK_RETRY_SECONDS = 20.0

#: Longest a single queue sync may run before yielding to the next cycle.
#: Well under the shortest gap between two picks in the real room (~30 s
#: when a manager is quick), so the loop never goes dark across a pick.
QUEUE_SYNC_BUDGET_S = 25.0


_READER = None  # set by run(); lets _make_pick re-read after a DRAFTED refusal


def _reread(room: RoomModel) -> list:
    try:
        return _READER.read() if _READER is not None else []
    except Exception:
        return []


def _make_pick(session, cfg: DraftConfig, room: RoomModel, plan, stats: DraftStats,
               dlog: DraftLog, *, retry: bool = False) -> bool:
    """Try to draft plan.best. Returns False only when the target turned out
    to be already drafted — a stale plan, not a failed attempt."""
    best = plan.best
    runner_up = plan.candidates[1] if len(plan.candidates) > 1 else None

    predicted = {
        "vor": best.valuation.vor,
        "score": best.score,
        "proj_points": best.valuation.points,
        "tier": float(best.valuation.tier),
    }
    alternative = (
        {"name": runner_up.player.name, "vor": runner_up.valuation.vor}
        if runner_up else None
    )
    reason = (
        f"round {plan.round_num} pick: {best.player.name} ({best.player.pos.value}) "
        f"vor={best.valuation.vor:.1f} tier={best.valuation.tier}"
        + (f" · {best.note}" if best.note else "")
    )

    if not cfg.click:
        # Queue-only mode: ESPN autopicks our #1 when the clock runs out.
        if not retry:
            decisions.record(ActionKind.QUEUE_SYNC, cites=["§3.3", "§3.9"],
                             reason=f"queue-only mode; expecting autopick of {best.player.name}",
                             predicted=predicted, alternative=alternative, executed=False)
        log.info("queue-only: leaving %s at the top for autopick", best.player.name)
        dlog.pick_made(
            overall=room.next_overall, name=best.player.name,
            pos=best.player.pos.value, vor=best.valuation.vor, score=best.score,
            tier=best.valuation.tier,
            runner_up=runner_up.player.name if runner_up else None,
            runner_up_vor=runner_up.valuation.vor if runner_up else None,
            how="queue-only (expecting autopick)",
        )
        return True

    from core.gates import write_gate

    action = Action(
        kind=ActionKind.DRAFT_PICK,
        args={"espn_id": best.player.espn_id, "name": best.player.name},
        cites=["§3.4", "§3.5", "§3.7"],
        reason=reason,
    )

    def performer():
        from core.browser import actions as A

        return A.draft_player(session, best.player.espn_id, best.player.name)

    try:
        gate, receipt = write_gate.execute(
            action, performer, predicted=predicted, alternative=alternative,
            skip_health=True,  # preflight already ran; 90s clock, one check is enough
            dry_run=cfg.dry_run,
        )
        if gate.allowed and receipt is not None:
            stats.our_picks.append(best.player.name)
            dlog.pick_made(
                overall=room.next_overall, name=best.player.name,
                pos=best.player.pos.value, vor=best.valuation.vor, score=best.score,
                tier=best.valuation.tier,
                runner_up=runner_up.player.name if runner_up else None,
                runner_up_vor=runner_up.valuation.vor if runner_up else None,
                how="clicked" + (" (retry)" if retry else ""), receipt=str(receipt),
            )
            notify("action", f"Pick {room.next_overall}: {best.player.name}"
                   + (" (retry)" if retry else ""),
                   f"{best.player.pos.value} · VOR {best.valuation.vor:.1f} · "
                   f"tier {best.valuation.tier}\npassed on: "
                   f"{runner_up.player.name if runner_up else '—'}")
        elif gate.allowed:
            # dry-run: gate passed, nothing was clicked
            log.info("DRY RUN pick %d would be %s", room.next_overall, best.player.name)
            dlog.pick_made(
                overall=room.next_overall, name=best.player.name,
                pos=best.player.pos.value, vor=best.valuation.vor, score=best.score,
                tier=best.valuation.tier,
                runner_up=runner_up.player.name if runner_up else None,
                runner_up_vor=runner_up.valuation.vor if runner_up else None,
                how="DRY RUN (not clicked)",
            )
        else:
            dlog.problem("write refused", f"{gate.refused_by}: {gate.reason}")
    except Exception as e:
        if "DRAFTED" in str(e):
            # Either the pick before ours took him (stale plan: re-rank and
            # go again), or ESPN's Autopick already drafted him FOR US from
            # the top of our queue the instant our turn began.
            room.apply(_reread(room))
            ours = {p.espn_id for p in room.picks if p.team_id == room.my_team_id}
            if best.player.espn_id in ours:
                stats.our_picks.append(best.player.name)
                log.info("%s was autopicked for us from the queue — counting it", best.player.name)
                dlog.pick_made(
                    overall=room.next_overall - 1, name=best.player.name,
                    pos=best.player.pos.value, vor=best.valuation.vor, score=best.score,
                    tier=best.valuation.tier,
                    runner_up=runner_up.player.name if runner_up else None,
                    runner_up_vor=runner_up.valuation.vor if runner_up else None,
                    how="ESPN autopick from the top of our queue",
                )
                notify("action", f"Pick {room.next_overall - 1}: {best.player.name} (via queue)",
                       f"{best.player.pos.value} · VOR {best.valuation.vor:.1f} · tier "
                       f"{best.valuation.tier} · ESPN autopicked him from the top of our queue.")
                return True
            log.warning("target %s was already drafted — re-ranking", best.player.name)
            dlog.problem("target already drafted",
                         f"{best.player.name} went to someone else before our click; re-ranking")
            return False
        stats.errors.append(f"pick failed: {e}")
        dlog.problem("click leg failed",
                     f"{best.player.name}: {str(e)[:200]} — leaving him top of the queue")
        notify("warn", "Click leg failed — falling back to the queue",
               f"{best.player.name} is top of the queue; ESPN will autopick him "
               f"when the timer expires.\n{e}")
    return True


def _by_name(bd) -> dict:
    from core.browser import selectors as S

    return {S.norm(p.name): p for p in bd.players}


class _NoPicks:
    """An API reader that never sees a pick — for practice rooms, which the
    league's draft record does not track."""

    def read(self) -> list:
        return []

    def is_complete(self) -> bool:
        return False


def _open_practice_room(session, slot: int) -> None:
    """Start a League-Specific Practice Draft from the team page at `slot`.

    Verified 2026-09-04: the Practice Draft button opens an inline panel with
    a 1-10 position picker and a Start button; Start opens the room in a new
    page. That page becomes the session's page for the rest of the run.
    """
    page = session.page
    page.locator("button:has-text('Practice Draft')").first.click()
    page.wait_for_timeout(1500)
    page.locator(f"label:has-text('{slot}')").first.click()
    with session._ctx.expect_page(timeout=30_000) as ev:
        page.locator("button:has-text('Start Practice Draft')").first.click()
    room = ev.value
    try:
        room.wait_for_load_state("networkidle", timeout=30_000)
    except Exception:
        pass
    session.page = room
    log.info("practice room opened at slot %d: %s", slot, room.url)
    notify("info", "Practice room opened", f"slot {slot}\n{room.url}")


#: How long past the scheduled draft time to keep trying to join the room.
ROOM_JOIN_GRACE_MINUTES = 15


def _join_room_when_open(session, url: str, draft_at) -> None:
    """Navigate to the real draft room, retrying until ESPN opens it.

    Before the room opens the draft URL bounces to the fantasy home page
    (verified 2026-09-04). Starting the loop early must not be a crash: keep
    trying every 30 s until the room renders, giving up only well past the
    scheduled start.
    """
    from datetime import timedelta

    attempt = 0
    while True:
        attempt += 1
        session.goto(url, require_owner_markers=False)
        try:
            _wait_for_room(session, timeout_ms=20_000)
            return
        except RuntimeError as e:
            now = datetime.now().astimezone()
            late = draft_at is not None and now > draft_at + timedelta(
                minutes=ROOM_JOIN_GRACE_MINUTES)
            if late:
                raise
            log.info("room not open yet (attempt %d): %s — retrying in 30s",
                     attempt, str(e)[:80])
            if attempt == 1:
                notify("info", "Waiting for the draft room to open",
                       "The loop is up and will join the room the moment ESPN opens it.")
            time.sleep(30)


def _wait_for_room(session, timeout_ms: int = 60_000) -> None:
    """Block until the room has rendered its player table and pick train.

    The room is a popup-style SPA that takes several seconds to hydrate;
    reading it before then finds zero rows and an empty queue container.
    """
    from core.browser import selectors as S

    page = session.page
    try:
        page.wait_for_selector(S.DRAFT_PLAYER_ROW.split(",")[0].strip(), timeout=timeout_ms)
    except Exception as e:
        session.screenshot("room-not-rendered")
        raise RuntimeError(
            f"the draft room did not render a player table within {timeout_ms}ms at "
            f"{page.url} — wrong URL, or the room is not open yet"
        ) from e
    # Kill the per-pick animation overlay, which swallows clicks and drags.
    try:
        page.add_style_tag(content=S.ROOM_CSS)
    except Exception as e:
        log.warning("could not inject room CSS: %s", e)
    train = S.first_present(page, S.DRAFT_PICK_TRAIN)
    if train is not None:
        log.info("room: %s", " ".join((train.first.inner_text() or "").split())[:160])


def _postflight(bd, room: RoomModel, stats: DraftStats, dlog: DraftLog) -> None:
    roster = [p for p in room.picks if p.team_id == room.my_team_id]
    lines = [f"  R{(p.overall - 1)//room.n_teams + 1:>2} #{p.overall:>3}  "
             f"{p.name} ({p.pos.value if p.pos else '?'})" for p in roster]
    body = "\n".join(lines) if lines else "(no picks recorded)"
    log.info("final roster:\n%s", body)
    notify(
        "good" if not stats.errors else "warn",
        f"Draft finished — {len(roster)} picks",
        body + (f"\n\n{len(stats.errors)} error(s): {stats.errors[:3]}" if stats.errors else ""),
    )
    dlog.finish(roster=[f"R{(p.overall - 1)//room.n_teams + 1} #{p.overall} {p.name}"
                        for p in roster], stats=stats)
    decisions.record(
        ActionKind.NOTIFY, cites=["§3.8"], reason="draft complete",
        predicted={"picks": float(len(roster)), "cycles": float(stats.cycles)},
        executed=True,
        extra={"roster": [p.name for p in roster],
               "errors": stats.errors,
               "at": datetime.now(UTC).isoformat()},
    )
