"""§3.2 — the pre-draft research pass: one agent per player, in parallel.

**Why one agent per player rather than one agent for the pool.** A single agent
working through 80 players re-sends its growing context on every turn, which is
quadratic and hits `--max-turns` long before the end. Per-player agents are
linear, independently retryable, and a failure costs one player instead of the
run. The cost of that shape is a fixed per-invocation overhead, which is why
the dossier task carries no playbook (`agent/run.py`).

**Why it resumes by default.** The pass is the one workload here big enough to
hit a rate limit, and it runs the night before a draft that happens once. A
limit at player 60 must cost a wait, not a restart.

    python scripts/research.py                  # the pool, resuming
    python scripts/research.py --refresh-top 15 # post-order-lock refresh
    python scripts/research.py --limit 5        # a taste
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime

from agent import run as agent_run
from agent.packet import dossier_packet
from core.config import settings
from core.draft import board as board_mod
from core.draft import dossiers as dmod
from core.model.priors import priors
from core.notify import notify

log = logging.getLogger(__name__)

#: Pause and retry once when the plan says no. Long enough to clear a short
#: burst limit, short enough that a Friday-night pass still finishes.
RATE_LIMIT_PAUSE_S = 120.0

#: One dossier is 5-6 turns of search. Past this something is wrong and the
#: worker should free itself rather than hold a slot.
PER_PLAYER_TIMEOUT_S = 300


@dataclass
class PassStats:
    attempted: int = 0
    written: int = 0
    skipped_fresh: int = 0
    failed: int = 0
    vetoes: int = 0
    moved: int = 0                      # multiplier != 1.0 after validation
    rejected: int = 0                   # written but failed core validation
    cost_usd: float = 0.0
    errors: list[str] = field(default_factory=list)
    started: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def elapsed_min(self) -> float:
        return (datetime.now(UTC) - self.started).total_seconds() / 60.0


def candidates(bd, *, pool_size: int | None = None) -> list[tuple]:
    """The players worth researching, ADP-ordered.

    ADP order rather than VOR order because ADP is what decides whether a
    player is *reachable*; VOR decides whether he is *wanted*. We then top up
    with anyone the board rates highly that ADP has not noticed, so a value the
    room is sleeping on still gets researched.
    """
    n = pool_size if pool_size is not None else int(priors().get("research.pool_size"))
    rows = bd.rows                                     # already VOR-sorted
    by_adp = sorted(
        (r for r in rows if r[0].espn_adp),
        key=lambda r: r[0].espn_adp,
    )
    picked: dict[int, tuple] = {}
    for p, v in by_adp[:n]:
        picked[p.espn_id] = (p, v)
    # Top up from the top of the VOR board — the sleepers ADP has missed.
    for p, v in rows:
        if len(picked) >= n:
            break
        picked.setdefault(p.espn_id, (p, v))
    return list(picked.values())


def _needs_research(espn_id: int, *, max_age_hours: float) -> bool:
    d = dmod.load_one(espn_id, max_age_hours=max_age_hours)
    return d is None


def research_one(player, val, *, timeout: int = PER_PLAYER_TIMEOUT_S):
    """One player, one agent. Returns the AgentResult; never raises."""
    try:
        packet = dossier_packet(player, val)
        return agent_run.run("dossier", packet, timeout=timeout)
    except Exception as e:                       # a worker must not kill the pool
        log.exception("research_one crashed for %s", player.name)
        return agent_run.AgentResult(False, "dossier", None, "", error=str(e))


def _record_usage(espn_id: int, name: str, res, wall_s: float) -> float:
    """Append to the usage ledger. Returns this run's cost, 0.0 if unknown."""
    u = res.usage or {}
    cost = u.get("total_cost_usd")
    cost = float(cost) if isinstance(cost, int | float) else 0.0
    row = {
        "at": datetime.now(UTC).isoformat(timespec="seconds"),
        "espn_id": espn_id, "name": name, "ok": res.ok, "error": res.error,
        "input_tokens": u.get("input_tokens"),
        "output_tokens": u.get("output_tokens"),
        "cache_read": u.get("cache_read_input_tokens"),
        "cache_write": u.get("cache_creation_input_tokens"),
        "num_turns": u.get("num_turns"),
        "cost_usd": cost,
        "wall_s": round(wall_s, 1),
    }
    try:
        p = settings().dossiers_dir / "_usage.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except OSError as e:
        log.debug("usage ledger write failed: %s", e)
    return cost


def run_pool(players: list[tuple], *, workers: int | None = None,
             resume: bool = True, stats: PassStats | None = None) -> PassStats:
    """Research every player in `players`. Returns what happened."""
    p = priors()
    workers = workers if workers is not None else int(p.get("research.workers"))
    max_age = float(p.get("research.max_age_hours"))
    st = stats or PassStats()

    todo = players
    if resume:
        todo = [(pl, v) for pl, v in players
                if _needs_research(pl.espn_id, max_age_hours=max_age)]
        st.skipped_fresh = len(players) - len(todo)
        if st.skipped_fresh:
            log.info("resuming: %d already fresh, %d to do",
                     st.skipped_fresh, len(todo))

    if not todo:
        return st

    halted = False

    def work(pair):
        pl, v = pair
        t0 = time.monotonic()
        res = research_one(pl, v)
        return pl, v, res, time.monotonic() - t0

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(work, pair): pair for pair in todo}
        for fut in as_completed(futures):
            pl, v, res, wall = fut.result()
            st.attempted += 1
            st.cost_usd += _record_usage(pl.espn_id, pl.name, res, wall)

            if res.ok and res.output:
                out = dict(res.output)
                out.setdefault("espn_id", pl.espn_id)
                out.setdefault("name", pl.name)
                dmod.write(pl.espn_id, out)
                # Read it straight back through the real validator: a dossier
                # that cannot survive validation is a failure now, not a
                # surprise at board-build time on Saturday morning.
                d = dmod.load_one(pl.espn_id, max_age_hours=max_age)
                if d is None:
                    st.rejected += 1
                    log.warning("%s: dossier written but rejected by validation", pl.name)
                else:
                    st.written += 1
                    st.vetoes += int(d.veto)
                    st.moved += int(d.multiplier != 1.0)
                    log.info("%-24s %s", pl.name, d.summary())
            else:
                st.failed += 1
                st.errors.append(f"{pl.name}: {res.error}")
                log.warning("%-24s FAILED: %s", pl.name, res.error)
                if res.error and res.error.startswith("capacity:") and not halted:
                    # §10.5 — the plan's limit, not ours. Stop taking new work;
                    # the pass resumes from disk on the next run.
                    halted = True
                    log.error("rate limited — stopping the pass. Re-run to resume.")
                    for f in futures:
                        f.cancel()

    return st


def summarise(st: PassStats) -> str:
    return (f"{st.written}/{st.attempted} dossiers · {st.vetoes} vetoes · "
            f"{st.moved} moved · {st.rejected} rejected · {st.failed} failed · "
            f"${st.cost_usd:.2f} · {st.elapsed_min:.0f} min")


def main(argv: list[str] | None = None) -> int:
    import argparse

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--limit", type=int, default=None,
                    help="research only the first N (a taste, not the pool)")
    ap.add_argument("--refresh-top", type=int, default=None,
                    help="re-research the top N by VOR, ignoring freshness")
    ap.add_argument("--no-resume", action="store_true",
                    help="re-research everyone, even fresh dossiers")
    ap.add_argument("--no-overrides", action="store_true",
                    help="skip writing data/overrides.json at the end")
    args = ap.parse_args(argv)

    bd = board_mod.load()
    if args.refresh_top:
        players = bd.rows[:args.refresh_top]
        resume = False
        what = f"refresh of the top {args.refresh_top} by VOR"
    else:
        players = candidates(bd)
        if args.limit:
            players = players[:args.limit]
        resume = not args.no_resume
        what = f"pool of {len(players)}"

    log.info("research pass: %s · model %s · %d workers",
             what, settings().claude_research_model,
             args.workers or int(priors().get("research.workers")))

    st = run_pool(players, workers=args.workers, resume=resume)

    if not args.no_overrides:
        dmod.write_overrides()

    line = summarise(st)
    print("\n" + "=" * 64)
    print(line)
    if st.errors:
        print("\nfailures:")
        for e in st.errors[:10]:
            print("  " + e)
        kinds = Counter(e.split(":")[1].strip() if ":" in e else e for e in st.errors)
        print(f"  ({dict(kinds.most_common(4))})")
    notify("info" if not st.failed else "warn", f"Research: {line}",
           "\n".join(st.errors[:5]) or None)
    return 0 if st.written else 1
