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
    _assert_pick_order_final(bd)

    c = client()
    room = RoomModel(facts=bd.facts, my_team_id=c.my_team_id)
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
    now = datetime.now()
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

    bd, room = preflight(cfg)
    rows = bd.rows
    by_id = bd.by_id
    stats = DraftStats()

    session = None
    qsync = None
    dom_reader = None

    try:
        if cfg.use_browser:
            session = EspnSession(headless=True).start()
            url = cfg.draft_url or (
                f"/football/draft?leagueId={bd.facts.settings.league_id}"
                f"&seasonId={bd.facts.settings.season}"
            )
            session.goto(url)
            qsync = QueueSync(session, by_id=by_id)
            dom_reader = DomReader(session, by_name=_by_name(bd))

        reader = FallbackReader(ApiReader(by_id=by_id), dom_reader)

        notify("info", "Draft loop started",
               f"{bd.facts.settings.name} · our picks {room.my_picks}")

        deadline = time.monotonic() + cfg.max_minutes * 60
        last_plan = None

        while time.monotonic() < deadline:
            stats.cycles += 1
            try:
                cycle_start = time.monotonic()
                picks = reader.read()
                new = room.apply(picks)

                if new or last_plan is None:
                    stats.picks_seen = room.picks_made
                    last_plan = picker.rank(rows, room)

                    if new:
                        log.info(
                            "pick %d: %s → best now %s",
                            new[-1].overall,
                            new[-1].name,
                            last_plan.best.player.name if last_plan.best else "?",
                        )

                    # ── the queue write comes FIRST, always (§3.3) ───────────
                    if qsync and last_plan.candidates:
                        target = [c.player.espn_id for c in last_plan.top(depth)]
                        ops, ok = qsync.sync(target, dry_run=cfg.dry_run)
                        stats.queue_ops += ok
                        if ops and ok < len(ops):
                            log.warning("queue sync: only %d/%d ops landed", ok, len(ops))

                # ── our turn ─────────────────────────────────────────────────
                if room.on_the_clock_is_us and last_plan and last_plan.best:
                    _make_pick(session, cfg, room, last_plan, stats)
                    # Re-read immediately so we don't double-pick.
                    room.apply(reader.read())

                if reader.is_complete() or room.is_complete:
                    log.info("draft complete")
                    break

                elapsed = time.monotonic() - cycle_start
                time.sleep(max(0.0, cfg.poll_seconds - elapsed))

            except KeyboardInterrupt:
                raise
            except Exception as e:
                stats.errors.append(str(e))
                log.exception("cycle error (continuing — the queue still stands)")
                # The queue is the net: a broken cycle does not blow a pick,
                # it just means ESPN autopicks our current #1.
                time.sleep(cfg.poll_seconds)

    finally:
        if session:
            session.close()

    _postflight(bd, room, stats)
    return stats


def _make_pick(session, cfg: DraftConfig, room: RoomModel, plan, stats: DraftStats) -> None:
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
        decisions.record(ActionKind.QUEUE_SYNC, cites=["§3.3", "§3.9"],
                         reason=f"queue-only mode; expecting autopick of {best.player.name}",
                         predicted=predicted, alternative=alternative, executed=False)
        log.info("queue-only: leaving %s at the top for autopick", best.player.name)
        return

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
        if gate.allowed:
            stats.our_picks.append(best.player.name)
            notify("action", f"Pick {room.next_overall}: {best.player.name}",
                   f"{best.player.pos.value} · VOR {best.valuation.vor:.1f} · "
                   f"tier {best.valuation.tier}\npassed on: "
                   f"{runner_up.player.name if runner_up else '—'}")
    except Exception as e:
        stats.errors.append(f"pick failed: {e}")
        notify("warn", "Click leg failed — falling back to the queue",
               f"{best.player.name} is top of the queue; ESPN will autopick him "
               f"when the timer expires.\n{e}")


def _by_name(bd) -> dict:
    from core.browser import selectors as S

    return {S.norm(p.name): p for p in bd.players}


def _postflight(bd, room: RoomModel, stats: DraftStats) -> None:
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
    decisions.record(
        ActionKind.NOTIFY, cites=["§3.8"], reason="draft complete",
        predicted={"picks": float(len(roster)), "cycles": float(stats.cycles)},
        executed=True,
        extra={"roster": [p.name for p in roster],
               "errors": stats.errors,
               "at": datetime.now(UTC).isoformat()},
    )
