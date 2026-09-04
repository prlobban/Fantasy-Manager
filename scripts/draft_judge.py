#!/usr/bin/env python
"""§3.10 — the judge: thinks between our picks, never during one.

Runs as a SEPARATE process alongside scripts/draft.py. It owns no browser,
never touches ESPN, and cannot make a pick. It reads two files and writes one:

    reads   data/drafts/<run>/clock.json          (the loop, every cycle)
            data/dossiers/*.json                  (the research pass)
    writes  data/drafts/<run>/verdicts/<pick>.json

The loop reads that verdict on our turn if it exists, and drafts on the maths
if it does not. There is no path by which this process can delay a pick — it is
killed the moment we are on the clock, and a killed run writes nothing.

    python scripts/draft_judge.py                 # newest draft dir
    python scripts/draft_judge.py --draft-dir data/drafts/2026...-live
    python scripts/draft_judge.py --shadow        # decide, log, never write

Shadow is the default for the first live draft: it produces the whole verdict
and posts it, but writes to `verdicts-shadow/` so the loop never sees it.
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent import run as agent_run  # noqa: E402
from agent.packet import judge_packet  # noqa: E402
from core.config import settings  # noqa: E402
from core.draft import board as board_mod  # noqa: E402
from core.draft import clock as clock_mod  # noqa: E402
from core.draft import dossiers as dmod  # noqa: E402
from core.draft import picker  # noqa: E402
from core.draft import verdict as vmod  # noqa: E402
from core.draft.room import RoomModel  # noqa: E402
from core.espn.client import client  # noqa: E402
from core.notify import notify  # noqa: E402

log = logging.getLogger("judge")

#: How often the watcher re-reads the clock to decide whether to kill the run.
WATCH_INTERVAL_S = 2.0

#: A clock file older than this means the loop has died or stalled. Judging
#: against a stale board is worse than not judging.
CLOCK_MAX_STALE_S = 90.0


def newest_draft_dir() -> Path | None:
    base = settings().data_dir / "drafts"
    dirs = [d for d in base.glob("*") if d.is_dir()] if base.exists() else []
    return max(dirs, key=lambda d: d.stat().st_mtime) if dirs else None


def run_judge(packet: dict, budget_s: float, draft_dir: Path) -> tuple[dict | None, str]:
    """Invoke the judge under a kill-timer tied to the room's clock.

    Returns (payload, note). The kill is the whole safety story: the loop is
    not waiting on us, but a run still burning tokens while we are on the clock
    is a run whose answer is already worthless.
    """
    killed = threading.Event()
    proc_box: dict = {}

    def watcher():
        deadline = time.monotonic() + budget_s
        while not killed.is_set() and time.monotonic() < deadline:
            time.sleep(WATCH_INTERVAL_S)
            tick = clock_mod.read(draft_dir)
            if tick is None:
                continue
            if tick.our_turn or tick.picks_until_our_turn <= 1 or tick.complete:
                proc = proc_box.get("proc")
                if proc is not None and proc.poll() is None:
                    log.warning("our turn arrived — killing the judge mid-run")
                    killed.set()
                    try:
                        proc.kill()
                    except Exception:
                        pass
                return
        if not killed.is_set():
            proc = proc_box.get("proc")
            if proc is not None and proc.poll() is None:
                log.warning("budget of %.0fs spent — killing the judge", budget_s)
                killed.set()
                try:
                    proc.kill()
                except Exception:
                    pass

    t = threading.Thread(target=watcher, daemon=True)
    t.start()
    try:
        res = agent_run.run("judge", packet, timeout=int(budget_s) + 10,
                            proc_box=proc_box)
    finally:
        killed.set()

    if killed.is_set() and not (res.ok if "res" in dir() else False):
        pass
    if not res.ok:
        return None, res.error or "judge failed"
    return res.output, ""


def main() -> int:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s",
                        datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--draft-dir", type=Path, default=None)
    ap.add_argument("--shadow", action="store_true",
                    help="produce and post verdicts but never let the loop see them")
    ap.add_argument("--once", action="store_true", help="one pick, then exit")
    args = ap.parse_args()

    draft_dir = args.draft_dir or newest_draft_dir()
    if draft_dir is None:
        log.error("no draft directory — start scripts/draft.py first")
        return 1

    out_dir = draft_dir / ("verdicts-shadow" if args.shadow else "verdicts")
    out_dir.mkdir(parents=True, exist_ok=True)

    bd = board_mod.load()
    me = client().my_team_id
    ds = dmod.load_all()
    log.info("judge up: %s · %s · %d dossiers · %s",
             draft_dir.name, "SHADOW" if args.shadow else "LIVE",
             len(ds), settings().claude_model)
    notify("info", f"Judge online ({'shadow' if args.shadow else 'live'})",
           f"{len(ds)} dossiers loaded · watching {draft_dir.name}")

    done: set[int] = set()
    idle_logged = False

    while True:
        tick = clock_mod.read(draft_dir)
        if tick is None:
            time.sleep(WATCH_INTERVAL_S)
            continue
        if tick.complete:
            log.info("draft complete — judge exiting")
            break
        if tick.stale_s > CLOCK_MAX_STALE_S:
            if not idle_logged:
                log.warning("clock is %.0fs stale — the loop may be down", tick.stale_s)
                idle_logged = True
            time.sleep(WATCH_INTERVAL_S)
            continue
        idle_logged = False

        overall = tick.next_overall
        if overall in done:
            time.sleep(WATCH_INTERVAL_S)
            continue

        budget = clock_mod.budget_for(tick)
        if budget <= 0.0:
            time.sleep(WATCH_INTERVAL_S)
            continue

        # Rebuild the room from the loop's own record so the two agree about
        # what has been taken. The room is cheap; the board is already loaded.
        room = RoomModel(facts=bd.facts, my_team_id=me)
        room.apply(_picks_from_log(draft_dir))
        if room.next_overall != overall:
            log.debug("room disagrees with the clock (%d vs %d) — waiting",
                      room.next_overall, overall)
            time.sleep(WATCH_INTERVAL_S)
            continue

        plan = picker.rank(bd.rows, room)
        if plan.best is None:
            time.sleep(WATCH_INTERVAL_S)
            continue

        log.info("pick #%d (R%d): %.0fs budget, %d picks out, pace %.0fs",
                 overall, plan.round_num, budget, tick.picks_until_our_turn,
                 tick.pace_s)

        # §3.10 — the one exception to "all research before 11:00". A position
        # run can push the board deeper than the pool we researched, and a
        # top-3 candidate we know nothing about is exactly where a dossier is
        # worth most. One player, one lookup, hard timeout, and only with slack
        # to spare: everything else still comes off disk.
        spent = _fill_missing_dossier(plan, ds, budget)
        budget -= spent

        packet = judge_packet(plan, room, for_overall=overall, budget_s=budget,
                              dossiers=ds, board_by_id=bd.by_id,
                              recent_picks=room.picks)
        t0 = time.monotonic()
        payload, err = run_judge(packet, budget, draft_dir)
        took = time.monotonic() - t0

        if payload is None:
            log.warning("no verdict for #%d after %.0fs: %s", overall, took, err)
            notify("warn", f"Judge produced nothing for pick {overall}",
                   f"{err} — the maths drafts this one.")
            done.add(overall)
            continue

        v = vmod.parse(payload, plan=plan, for_overall=overall)
        vmod.write(out_dir, overall, payload)
        done.add(overall)

        log.info("verdict #%d in %.0fs: %s", overall, took, v.describe())
        body = v.summary
        if v.rejected:
            body += "\n_refused: " + "; ".join(v.rejected[:3]) + "_"
        notify("action" if v.acts else "info",
               f"🧠 Judge · pick {overall} · {v.describe()}"
               + (" (shadow)" if args.shadow else ""),
               body)

        if args.once:
            break

    return 0


#: Only reach for a live lookup with this much budget in hand: the search is
#: capped at LIVE_LOOKUP_TIMEOUT_S and the judge still has to think afterwards.
LIVE_LOOKUP_MIN_BUDGET_S = 150.0
LIVE_LOOKUP_TIMEOUT_S = 60


def _fill_missing_dossier(plan, ds: dict, budget_s: float) -> float:
    """Research ONE undossiered top-3 candidate. Returns seconds spent.

    Deliberately narrow. This is the only web access inside a draft, so it is
    gated three ways — top three only, one player, and only when the budget can
    absorb a full timeout and still leave the judge room to answer. It never
    raises: failing to research is the normal case, not an error.
    """
    if budget_s < LIVE_LOOKUP_MIN_BUDGET_S:
        return 0.0
    missing = [c for c in plan.top(3) if c.player.espn_id not in ds]
    if not missing:
        return 0.0

    c = missing[0]
    log.info("no dossier for %s (top 3) — one live lookup, %ds cap",
             c.player.name, LIVE_LOOKUP_TIMEOUT_S)
    t0 = time.monotonic()
    try:
        from agent.packet import dossier_packet

        res = agent_run.run("dossier", dossier_packet(c.player, c.valuation),
                            timeout=LIVE_LOOKUP_TIMEOUT_S)
        if res.ok and res.output:
            out = dict(res.output)
            out.setdefault("espn_id", c.player.espn_id)
            out.setdefault("name", c.player.name)
            dmod.write(c.player.espn_id, out)
            d = dmod.load_one(c.player.espn_id)
            if d is not None:
                ds[c.player.espn_id] = d
                log.info("live dossier for %s: %s", c.player.name, d.summary())
        else:
            log.info("live lookup for %s produced nothing: %s",
                     c.player.name, res.error)
    except Exception as e:
        log.warning("live lookup for %s failed: %s", c.player.name, e)
    return time.monotonic() - t0


def _picks_from_log(draft_dir: Path) -> list:
    """Every pick the loop has recorded, from its own events.jsonl.

    Reading the loop's log rather than ESPN keeps the two processes agreeing
    about the room without a second API session, and means the judge cannot
    see a pick the loop has not yet applied.
    """
    import json

    from core.draft.room import Pick
    from core.model.schema import Pos

    out = []
    p = draft_dir / "events.jsonl"
    if not p.exists():
        return out
    try:
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("event") != "room_pick":
                continue
            pos = rec.get("pos")
            out.append(Pick(
                overall=int(rec["overall"]),
                team_id=int(rec["team_id"]),
                espn_id=int(rec.get("espn_id") or 0),
                pos=Pos(pos) if pos else None,
                name=rec.get("name") or "",
            ))
    except OSError:
        return []
    return out


if __name__ == "__main__":
    sys.exit(main())
