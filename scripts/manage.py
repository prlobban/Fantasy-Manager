#!/usr/bin/env python
"""The season manager (§1.3, §1.4). Runs on the box from scripts/cron_manage.sh.

    python scripts/manage.py --no-agent             # core's plans only, no model
    python scripts/manage.py                        # the sweep; ENABLED=off = agent runs, writes refused
    python scripts/manage.py --dry-run              # + never flip the switch on a health failure
    python scripts/manage.py --tuesday              # the weekly review (§7)
    python scripts/manage.py --task lineup          # one slice only

The sweep decides on THIS MORNING's research (scripts/research_week.py). If
no fresh dossier exists for our roster it still runs — with a loud note in
the packet and in Slack — because a lineup set on projections beats no lineup.

What lands in Slack (Pearce, 2026-09-05: "just what it did"): one sentence,
then one line per move — lineup changes, adds, proposals, accepts/rejects —
with a ✅ / ⛔ / "would" marker. The reasoning is NOT posted. It is written
to data/reasoning/<date>-<task>.md and to decisions.jsonl, where it can be
read when wanted and graded on Tuesday.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from agent import run as agent_run
from agent.packet import build as build_packet
from core.config import settings
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
        print(f"\nWAIVERS priority {wp['priority']} · adds left {wp.get('adds_left_this_week')}"
              f" · core recommends {wp['core_recommends']}")
        for c in wp["candidates"]:
            flags = f"  [{'; '.join(c['flags'])}]" if c["flags"] else ""
            print(f"  {c['core_verdict']:5} {c['name']:24} {c['pos']:4} +{c['gain_per_week']}/wk"
                  f" ros {c['ros_vor']}  drop {c['drop'] or '—'}"
                  + ("  [drop TRADEABLE]" if c.get("drop_tradeable") else "") + flags)
        if not wp["candidates"]:
            print("  (no candidates)")

    if ti := packet.get("trade_ideas"):
        print(f"\nTRADES  left today {ti['proposals_left_today']} "
              f"· this week {ti['proposals_left_this_week']}")
        for i in ti["ideas"][:5]:
            print(f"  to {i['to_team_name']:22} give {', '.join(x['name'] for x in i['give'])} / "
                  f"get {', '.join(x['name'] for x in i['get'])}  "
                  f"us {i['our_gain']:+} them~{i['their_gain_advisory']:+} "
                  f"market {i['market_ratio']} · {i['shape_effect']}"
                  + (f"  [{'; '.join(i['flags'])}]" if i.get("flags") else ""))
        if not ti["ideas"]:
            print("  (no idea clears our-gain and the market floor today)")

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


# ── what happened, from the record ───────────────────────────────────────────

_WRITE_KINDS = {"set_lineup", "add_drop", "waiver_claim", "propose_trade",
                "accept_trade", "reject_trade"}


def _decisions_since(t0: datetime) -> list[dict]:
    p = settings().decisions_path
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            d = json.loads(line)
            at = datetime.fromisoformat(d["at"].replace("Z", "+00:00"))
        except Exception:
            continue
        if at >= t0 and d.get("kind") in _WRITE_KINDS:
            out.append(d)
    return out


def _what(d: dict, names: dict[int, str]) -> str:
    """One line of WHAT, from the action's args. No reasoning."""
    a = (d.get("extra") or {}).get("args") or {}
    k = d.get("kind")
    n = lambda i: names.get(int(i), f"#{i}") if i is not None else "—"  # noqa: E731
    if k == "set_lineup":
        moves = ", ".join(f"{n(m.get('espn_id'))} → {m.get('slot')}" for m in a.get("moves") or [])
        return f"lineup: {moves or 'no moves'}"
    if k in ("add_drop", "waiver_claim"):
        verb = "claim" if k == "waiver_claim" else "add"
        return f"{verb} {n(a.get('add'))}" + (f", drop {n(a['drop'])}" if a.get("drop") else "")
    if k == "propose_trade":
        return (f"offer to {a.get('to_team_name') or a.get('to_team')}: give "
                f"{', '.join(a.get('give_names') or [n(i) for i in a.get('give') or []])} / get "
                f"{', '.join(a.get('get_names') or [n(i) for i in a.get('get') or []])}")
    if k == "accept_trade":
        return (f"ACCEPT trade from {a.get('from_team_name') or a.get('from_team')}: get "
                f"{', '.join(a.get('get_names') or [])} / give {', '.join(a.get('give_names') or [])}")
    if k == "reject_trade":
        return f"reject trade from {a.get('from_team_name') or a.get('from_team')}"
    return k or "?"


def _digest(out: dict, decisions: list[dict], names: dict[int, str], *, read_only: bool,
            scope: str) -> tuple[str, list[str]]:
    """(one-sentence brief, one line per move)."""
    brief = (out.get("summary") or out.get("no_action_reason") or "").strip()
    brief = brief.split(". ")[0].rstrip(".") + "." if brief else "No summary."
    lines: list[str] = []
    for d in decisions:
        what = _what(d, names)
        gate = d.get("gate") or {}
        if d.get("executed"):
            lines.append(f"✅ {what}")
        elif read_only and gate.get("refused_by") == "§8.4":
            lines.append(f"would: {what}")
        else:
            why = (gate.get("reason") or "").split(":")[0][:80]
            lines.append(f"⛔ {what} — {gate.get('refused_by') or 'refused'} {why}".rstrip())
    if not lines:
        if out.get("actions"):
            # The model listed actions it never called as tools. Say so.
            for a in out["actions"]:
                if a.get("tool") == "notify":
                    continue
                lines.append(f"planned (not called): {a.get('tool')} {json.dumps(a.get('args'))[:120]}")
        else:
            lines.append("no moves" + (" (lineup pass)" if scope == "lineup" else ""))
    return brief, lines


def _write_reasoning(task: str, scope: str, packet: dict, out: dict,
                     decisions: list[dict], names: dict[int, str]) -> Path:
    """The full reasoning, on disk. Pearce reads this when he wants the why."""
    d = settings().data_dir / "reasoning"
    d.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y-%m-%d-%H%M")
    p = d / f"{stamp}-{task}{'' if scope == 'all' else '-' + scope}.md"
    L = [f"# {task} · week {packet.get('league', {}).get('week')} · {stamp}Z", ""]
    if out.get("summary"):
        L += [f"**Summary.** {out['summary']}", ""]
    if out.get("roster_assessment"):
        L += ["## Roster assessment", "", out["roster_assessment"], ""]
    if out.get("actions"):
        L += ["## Actions", ""]
        for a in out["actions"]:
            L.append(f"### {a.get('tool')} · {json.dumps(a.get('args'))[:200]}")
            L.append(f"cites: {', '.join(a.get('cites') or [])}")
            for f in ("reason", "short_term", "long_term", "alternative", "evidence",
                      "would_be_wrong_if", "why_they_accept"):
                if a.get(f):
                    L.append(f"- **{f}:** {a[f]}")
            L.append("")
    if out.get("no_action_reason"):
        L += ["## No action", "", out["no_action_reason"], ""]
    if out.get("uncertainties"):
        L += ["## Uncertainties", ""] + [f"- {u}" for u in out["uncertainties"]] + [""]
    if out.get("escalate"):
        L += ["## Escalated", "", out["escalate"], ""]
    if decisions:
        L += ["## Gate record", ""]
        for dd in decisions:
            g = dd.get("gate") or {}
            L.append(f"- {'✅' if dd.get('executed') else '⛔'} {_what(dd, names)}"
                     + ("" if dd.get("executed") else f" — {g.get('refused_by')}: {g.get('reason')}"))
        L.append("")
    if task == "tuesday":
        for k in ("result", "efficiency_read", "calibration_read", "league_scan"):
            if out.get(k):
                L += [f"## {k}", "", str(out[k]), ""]
        if out.get("decision_grades"):
            L += ["## Decision grades", ""]
            L += [f"- {json.dumps(g)}" for g in out["decision_grades"]] + [""]
        if out.get("lessons"):
            L += ["## Lessons", ""] + [f"- {x}" for x in out["lessons"]] + [""]
        if out.get("prior_proposals"):
            L += ["## Prior proposals", ""]
            L += [f"- {json.dumps(x)}" for x in out["prior_proposals"]] + [""]
    p.write_text("\n".join(L), encoding="utf-8")
    return p


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

    # Read-and-report: with the switch off the agent still runs and still
    # posts, and every write is refused at the gate (§8.4). That is the test
    # mode. `--dry-run` only stops a health failure flipping the switch.
    read_only = not kill_switch.is_on()
    if read_only:
        print(f"kill switch is off ({kill_switch.state()[:80]}) — every write will be refused")

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

    t0 = datetime.now(UTC)
    res = agent_run.run(task, packet, timeout=2400)
    if not res.ok:
        print(f"\nAGENT FAILED: {res.error}")
        notify("error", "Fantasy manager: agent run failed", str(res.error)[:300])
        return 1

    out = res.output
    print("\nAGENT")
    print(json.dumps(out, indent=1))

    names = {p.espn_id: p.name for p in st.all_players()}
    decisions = _decisions_since(t0)
    path = _write_reasoning(task, args.task, packet, out, decisions, names)
    print(f"\nreasoning written to {path}")

    if task == "tuesday":
        from core.manager import tuesday as tue
        from core.state import lessons, store

        week = (packet.get("review") or {}).get("week_reviewed", max(1, st.week - 1))
        hist = tue.write_history(week, packet.get("review") or {}, out)
        n = lessons.append(week, out.get("lessons") or [])
        store.update(last_review_week=week)
        r = (packet.get("review") or {}).get("result") or {}
        e = (packet.get("review") or {}).get("efficiency") or {}
        line = (f"{'WON' if r.get('won') else 'LOST'} {r.get('our_points')}–{r.get('their_points')}"
                if r else "no result")
        notify("good", f"Week {week} review",
               f"{line} · efficiency {e.get('pct', '?')} · {n} lesson(s) · "
               f"{len(out.get('prior_proposals') or [])} prior change(s) proposed\n{hist}")
    else:
        brief, lines = _digest(out, decisions, names, read_only=read_only, scope=args.task)
        quiet = args.task == "lineup" and not decisions and not out.get("actions")
        if not quiet:
            title = f"Week {st.week} " + ("lineup pass" if args.task == "lineup" else "sweep") \
                + (" · READ-ONLY" if read_only else "")
            notify("info", title, brief + "\n" + "\n".join(lines))

    if esc := (out.get("escalate") or "").strip():
        notify("warn", "Needs Pearce", esc[:600])
    return 0


if __name__ == "__main__":
    sys.exit(main())
