#!/usr/bin/env python
"""The season manager (§1.3, §1.4). Runs on the box from scripts/cron_manage.sh.

    python scripts/manage.py --dry-run --no-agent   # core's plans only
    python scripts/manage.py --dry-run              # + the agent, no writes
    python scripts/manage.py                        # the daily sweep
    python scripts/manage.py --tuesday              # the weekly review (§7)
    python scripts/manage.py --task lineup          # one slice only

The sweep decides on THIS MORNING's research (scripts/research_week.py). If
no fresh dossier exists for our roster it still runs — with a loud note in
the packet and in Slack — because a lineup set on projections beats no lineup.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys

from agent import run as agent_run
from agent.packet import build as build_packet
from core.espn import health, league_state
from core.gates import kill_switch
from core.notify import notify

log = logging.getLogger("manage")


def _print_core_view(packet: dict) -> None:
    if rs := packet.get("roster_shape"):
        print(f"\nSHAPE   {rs['summary']}")
        for n in rs.get("notes", []):
            print(f"  {n}")
    if r := packet.get("research"):
        print(f"RESEARCH {r['coverage']}")

    if lp := packet.get("lineup_plan"):
        print(f"\nLINEUP  projected {lp['projected']} · playing for "
              f"{lp['variance_mode']}" + (f" · margin {lp['margin']:+}"
                                          if lp.get("margin") is not None else ""))
        for a in lp["assignments"]:
            print(f"  {a['slot']:9} {(a['player'] or '— EMPTY —'):24} {a['points']:6.1f}")
        for c in lp["changes"]:
            print(f"  CHANGE {c['player']}: {c['from']} -> {c['to']}  ({c['why']})")
        if not lp["changes"]:
            print("  (already optimal — no moves)")

    if wp := packet.get("waiver_plan"):
        print(f"\nWAIVERS priority {wp['priority']} · adds left {wp.get('adds_left_this_week')}")
        for c in wp["free_adds"]:
            print(f"  FREE ADD {c['name']:24} +{c['gain_per_week']}/wk  drop {c['drop'] or '—'}"
                  + ("  [drop is TRADEABLE]" if c.get("drop_tradeable") else ""))
        for c in wp["claims"]:
            print(f"  CLAIM    {c['name']:24} +{c['gain_per_week']}/wk  drop {c['drop'] or '—'}"
                  f"  ({c['archetype']})" + ("  [drop is TRADEABLE]" if c.get("drop_tradeable") else ""))
        if not wp["free_adds"] and not wp["claims"]:
            print("  (nothing clears the bar)")
        for s in wp["skipped"][:5]:
            print(f"  skip     {s['name']:24} {s['why']}")

    if ti := packet.get("trade_ideas"):
        print(f"\nTRADES  left today {ti['proposals_left_today']} "
              f"· this week {ti['proposals_left_this_week']}")
        for i in ti["ideas"][:5]:
            print(f"  to {i['to_team_name']:22} give {', '.join(x['name'] for x in i['give'])} / "
                  f"get {', '.join(x['name'] for x in i['get'])}  "
                  f"us {i['our_gain']:+} them {i['their_gain']:+} · {i['shape_effect']}")
        if not ti["ideas"]:
            print("  (no mutual-gain idea today)")

    if rv := packet.get("review"):
        print(f"\nREVIEW  week {rv.get('week_reviewed')}")
        if rv.get("result"):
            r = rv["result"]
            print(f"  {'WON' if r['won'] else 'LOST'} {r['our_points']} – {r['their_points']}")
        if rv.get("efficiency"):
            e = rv["efficiency"]
            print(f"  efficiency {e['pct']} · left on bench {e['left_on_bench']}"
                  + (f" · {e['worst_call']}" if e.get("worst_call") else ""))
        print(f"  {len(rv.get('decisions_last_week') or [])} decisions to grade")
        if rv.get("note"):
            print(f"  note: {rv['note']}")


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--tuesday", action="store_true")
    ap.add_argument("--task", choices=["lineup", "waivers", "trades", "all"], default="all")
    ap.add_argument("--no-agent", action="store_true",
                    help="print core's plans without invoking the model")
    args = ap.parse_args()

    h = health.check(kill_on_fail=not args.dry_run)
    print("health:", h.summary())
    if not h.ok:
        print("FAILED:", "; ".join(h.failures))
        return 1

    if not kill_switch.is_on() and not args.dry_run:
        print(f"kill switch is off ({kill_switch.state()[:80]}) — read-only")
        args.dry_run = True

    task = "tuesday" if args.tuesday else "daily"
    st = league_state.snapshot()
    packet = build_packet(task, st, scope=args.task)
    if args.task != "all":
        print(f"scope: {args.task} only")

    # core's view first, always. If this looks wrong, the model cannot fix it.
    _print_core_view(packet)

    cov = (packet.get("research") or {}).get("coverage", "")
    if cov.startswith("0/"):
        print("\n⚠ no research this morning — deciding on projections alone (D3.1)")

    if args.no_agent:
        return 0

    res = agent_run.run(task, packet, dry_run=args.dry_run)
    if not res.ok:
        print(f"\nAGENT FAILED: {res.error}")
        notify("error", "Fantasy manager: agent run failed", str(res.error)[:400])
        return 1
    if res.output is None:          # dry-run of the invocation itself
        return 0

    out = res.output
    print("\nAGENT")
    print(json.dumps(out, indent=1))

    if task == "tuesday":
        from core.manager import tuesday as tue
        from core.state import lessons, store

        week = (packet.get("review") or {}).get("week_reviewed", max(1, st.week - 1))
        path = tue.write_history(week, packet.get("review") or {}, out)
        n = lessons.append(week, out.get("lessons") or [])
        store.update(last_review_week=week)
        notify("good", f"Week {week} review written",
               f"{out.get('result', '')}\n{out.get('efficiency_read', '')}\n"
               f"{n} lesson(s) recorded · {len(out.get('prior_proposals') or [])} "
               f"prior proposal(s)\n{path}")
    else:
        body = out.get("roster_assessment") or ""
        if out.get("actions"):
            body += "\n\n" + "\n".join(
                f"• {a.get('tool')}: {a.get('reason')}" for a in out["actions"])
        elif out.get("no_action_reason"):
            body += f"\n\nno action: {out['no_action_reason']}"
        notify("info", f"Week {st.week} sweep" + (" (DRY RUN)" if args.dry_run else ""),
               body[:2800])

    if esc := (out.get("escalate") or "").strip():
        notify("warn", "Fantasy manager needs Pearce", esc[:1500])
    return 0


if __name__ == "__main__":
    sys.exit(main())
