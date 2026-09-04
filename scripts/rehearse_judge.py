#!/usr/bin/env python
"""Rehearse the judge at REAL draft pace, without a draft room.

ESPN's practice room cannot exercise the judge: its auto-teams pick instantly,
observed pace floors at 3s, the budget correctly computes to zero on every turn
and the judge never runs. That is the budget logic working — but it means the
one component that will be making judgment calls on Saturday has never actually
been driven, and Saturday is not the place to find that out.

So this replays a completed draft's `events.jsonl` into a fresh directory,
writing `clock.json` at a realistic pace, and runs the REAL judge process
against it. Everything downstream of the room is genuine: the room model, the
ranking, the packet, `claude -p`, the verdict, the validation, the write.

    python scripts/rehearse_judge.py                       # newest draft, 3 turns
    python scripts/rehearse_judge.py --turns 5 --pace 45
    python scripts/rehearse_judge.py --from data/drafts/<dir>

It costs roughly $0.50 per turn and takes about 20s. It writes to a rehearsal
directory, never to a live draft's.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config import settings  # noqa: E402
from core.draft import clock as clock_mod  # noqa: E402
from core.draft import verdict as vmod  # noqa: E402


def newest_completed_draft() -> Path | None:
    base = settings().data_dir / "drafts"
    if not base.exists():
        return None
    dirs = [d for d in base.glob("*") if (d / "events.jsonl").exists()]
    return max(dirs, key=lambda d: d.stat().st_mtime) if dirs else None


def read_events(src: Path) -> list[dict]:
    out = []
    for line in (src / "events.jsonl").read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from", dest="src", type=Path, default=None)
    ap.add_argument("--turns", type=int, default=3,
                    help="how many of OUR picks to rehearse")
    ap.add_argument("--pace", type=float, default=45.0,
                    help="seconds per pick to simulate (real league is 90s;"
                         " managers typically use 30-60)")
    ap.add_argument("--keep", action="store_true", help="keep the rehearsal dir")
    args = ap.parse_args()

    src = args.src or newest_completed_draft()
    if src is None:
        print("no completed draft to replay — run a practice draft first")
        return 1

    events = read_events(src)
    picks = [e for e in events if e.get("event") == "room_pick"]
    ours = sorted({e["overall"] for e in picks if e.get("ours")})
    if not picks:
        print(f"{src.name} has no recorded picks")
        return 1

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    dest = settings().data_dir / "drafts" / f"{stamp}-judge-rehearsal"
    dest.mkdir(parents=True, exist_ok=True)

    print(f"replaying {src.name}: {len(picks)} picks, our picks at {ours}")
    print(f"simulated pace {args.pace:.0f}s/pick, rehearsing {args.turns} turns")
    print(f"-> {dest}\n")

    targets = ours[:args.turns]
    if not targets:
        print("no picks of ours in that draft")
        return 1

    # Start the real judge against the rehearsal directory.
    #
    # Its output goes to a FILE, not a pipe. A pipe nobody drains fills its
    # buffer and blocks the child forever, which is exactly what happened the
    # first time this ran: the judge sat wedged mid-log and produced nothing
    # for ten minutes while this loop waited for a verdict.
    judge_log = dest / "judge.log"
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("draft_judge.py")),
         "--draft-dir", str(dest), "--shadow"],
        stdout=judge_log.open("w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
        cwd=str(Path(__file__).resolve().parents[1]))

    results = []
    try:
        for target in targets:
            # Everything that happened BEFORE our pick, written as the loop would.
            before = [p for p in picks if p["overall"] < target]
            with (dest / "events.jsonl").open("w", encoding="utf-8") as f:
                for p in before:
                    f.write(json.dumps(p) + "\n")

            class _Room:
                next_overall = target
                picks_until_my_turn = 7
                current_round = (target - 1) // 10 + 1

            class _Pace:
                def observed(self):
                    return args.pace

            clock_mod.write(dest, room=_Room(), our_turn=False, pace=_Pace())
            tick = clock_mod.read(dest)
            budget = clock_mod.budget_for(tick)
            print(f"pick #{target}: budget {budget:.0f}s ... ", end="", flush=True)

            t0 = time.monotonic()
            deadline = t0 + max(30.0, budget + 30.0)
            got = None
            while time.monotonic() < deadline:
                time.sleep(1.0)
                p = vmod.path_for(dest, target)
                if p.exists():
                    got = json.loads(p.read_text(encoding="utf-8"))
                    break
            took = time.monotonic() - t0

            if got is None:
                print(f"NO VERDICT after {took:.0f}s")
                results.append((target, None, took))
            else:
                v = vmod.parse(got, for_overall=target)
                print(f"{took:.0f}s — {v.describe()}")
                print(f"    {v.summary[:220]}")
                if v.rejected:
                    print(f"    refused: {'; '.join(v.rejected[:2])}")
                results.append((target, v, took))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    ok = sum(1 for _, v, _ in results if v is not None)
    print(f"\n{ok}/{len(results)} turns produced a verdict")
    if results:
        print(f"median latency: "
              f"{sorted(t for _, _, t in results)[len(results) // 2]:.0f}s")
    acted = sum(1 for _, v, _ in results if v is not None and v.acts)
    print(f"{acted} verdict(s) would have changed a pick")

    if not args.keep:
        shutil.rmtree(dest, ignore_errors=True)
    else:
        print(f"kept: {dest}")
    print(f"judge log: {judge_log}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
