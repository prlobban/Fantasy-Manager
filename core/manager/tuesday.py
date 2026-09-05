"""§1.4 / §7 — assemble last week for the Tuesday review.

Pulls the finished week's box score (who we started, what each scored, what
the opponent scored), the projections we acted on, and every decision the
log recorded that week — and hands review.py the numbers. The agent then
grades the DECISIONS (§7.3) and writes the lessons (D7.3).

ESPN's `mBoxscore` view with `scoringPeriodId=<week>` carries each team's
roster AS IT WAS STARTED that week (`rosterForCurrentScoringPeriod`), which is
the only place last week's lineup survives once the new week's slots are set.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from core.espn import players as players_mod
from core.espn.client import EspnClient
from core.espn.league_state import BENCH_SLOT, IR_SLOT, LeagueState
from core.manager import review as review_mod
from core.model.schema import Player
from core.state import decisions, lessons

log = logging.getLogger(__name__)


@dataclass
class WeekBox:
    week: int
    started: list[tuple[Player, float]]
    bench: list[tuple[Player, float]]
    opponent_points: float | None
    opponent_name: str | None
    #: espn_id -> the projection ESPN carried for that week.
    projections: dict[int, float] = field(default_factory=dict)
    source: str = "boxscore"


def _points_for(entry: dict, week: int) -> float:
    ppe = entry.get("playerPoolEntry") or {}
    v = ppe.get("appliedStatTotal")
    if v is not None:
        return float(v)
    for s in (ppe.get("player") or {}).get("stats") or []:
        if int(s.get("scoringPeriodId") or 0) == week and int(s.get("statSourceId", -1)) == 0:
            return float(s.get("appliedTotal") or 0.0)
    return 0.0


def last_week_box(c: EspnClient, st: LeagueState, week: int) -> WeekBox | None:
    """Our started/benched lines and the opponent's total for `week`."""
    try:
        raw = c.get_view(["mBoxscore", "mMatchupScore"], params={"scoringPeriodId": week})
    except Exception as e:
        log.warning("box score for week %d unavailable: %s", week, e)
        return None

    me = st.my_team_id
    for m in raw.get("schedule") or []:
        if int(m.get("matchupPeriodId", -1)) != week:
            continue
        home, away = m.get("home") or {}, m.get("away") or {}
        if home.get("teamId") == me:
            ours, theirs = home, away
        elif away.get("teamId") == me:
            ours, theirs = away, home
        else:
            continue

        started, bench, proj = [], [], {}
        entries = ((ours.get("rosterForCurrentScoringPeriod") or {}).get("entries")
                   or (ours.get("rosterForMatchupPeriod") or {}).get("entries") or [])
        for e in entries:
            pl = players_mod._to_player(
                {"player": (e.get("playerPoolEntry") or {}).get("player") or {}, "onTeamId": me},
                c.cfg.season,
            )
            if pl is None:
                continue
            pts = _points_for(e, week)
            if pl.proj_week.get(week) is not None:
                proj[pl.espn_id] = pl.proj_week[week]
            slot = int(e.get("lineupSlotId", BENCH_SLOT))
            if slot in (BENCH_SLOT, IR_SLOT):
                bench.append((pl, pts))
            else:
                started.append((pl, pts))

        opp_pts = theirs.get("totalPoints")
        opp_team = st.teams.get(theirs.get("teamId"))
        return WeekBox(
            week=week, started=started, bench=bench,
            opponent_points=float(opp_pts) if opp_pts is not None else None,
            opponent_name=opp_team.name if opp_team else None,
            projections=proj,
        )
    return None


def _decision_rows(since: datetime) -> list[dict]:
    rows = []
    for d in decisions.since(since):
        if d.get("kind") in ("queue_sync", "draft_pick"):
            continue
        rows.append({
            "at": d.get("at"), "kind": d.get("kind"), "executed": d.get("executed"),
            "reason": (d.get("reason") or "")[:400],
            "predicted": d.get("predicted") or {},
            "alternative": d.get("alternative"),
            "refused": (d.get("gate") or {}).get("reason") if not d.get("executed") else None,
        })
    return rows


def league_scan(st: LeagueState, ros_vals: dict) -> list[str]:
    """D7.4 — who is stacked where we are short, who is selling."""
    from core.manager import roster as roster_mod

    settings = st.facts.settings
    ours = roster_mod.analyse(st.me.roster, ros_vals, settings)
    lines = []
    for tid, t in st.teams.items():
        if tid == st.my_team_id:
            continue
        shape = roster_mod.analyse(t.roster, ros_vals, settings)
        bits = []
        for pos, n in shape.surplus.items():
            if pos in ours.short:
                bits.append(f"surplus {n} {pos.value} where we are short")
        if t.wins + t.losses >= 3 and t.wins == 0:
            bits.append("winless — likely selling")
        if bits:
            lines.append(f"{t.name} ({t.wins}-{t.losses}): " + "; ".join(bits))
    return lines


def build_section(c: EspnClient, st: LeagueState, ros_vals: dict) -> dict:
    """Everything the Tuesday packet carries beyond the daily fields."""
    week = max(1, st.week - 1)
    box = last_week_box(c, st, week)
    since = datetime.now(UTC) - timedelta(days=7)
    rows = _decision_rows(since)

    out: dict = {
        "week_reviewed": week,
        "decisions_last_week": rows,
        "lessons_so_far": lessons.read(),
        "league_scan": league_scan(st, ros_vals),
    }
    if box is None:
        out["box"] = None
        out["note"] = "box score unavailable — grade decisions on their predictions only"
        return out

    rv = review_mod.build(
        week, box.started, box.bench, st.facts.settings,
        opponent_points=box.opponent_points, projections=box.projections,
        league_notes=out["league_scan"],
    )
    out["box"] = {
        "opponent": box.opponent_name,
        "started": [{"name": p.name, "pos": p.pos.value, "points": round(pts, 1),
                     "projected": box.projections.get(p.espn_id)} for p, pts in box.started],
        "bench": [{"name": p.name, "pos": p.pos.value, "points": round(pts, 1),
                   "projected": box.projections.get(p.espn_id)} for p, pts in box.bench],
    }
    out["result"] = (
        {"won": rv.result.won, "our_points": rv.result.our_points,
         "their_points": rv.result.their_points, "margin": rv.result.margin}
        if rv.result else None
    )
    out["efficiency"] = (
        {"actual": rv.efficiency.actual, "best_possible": rv.efficiency.best_possible,
         "pct": rv.efficiency.pct, "left_on_bench": rv.efficiency.points_left_on_bench,
         "worst_call": rv.efficiency.worst_call}
        if rv.efficiency else None
    )
    out["calibration"] = {
        "overall_bias": rv.calibration.overall_bias,
        "by_position": rv.calibration.by_position,
        "sample": rv.calibration.sample,
        "notes": rv.calibration.notes,
    }
    return out


def write_history(week: int, section: dict, output: dict) -> str:
    """§7.5 — the dated history file. Accretes; never edited."""
    from core.config import REPO_ROOT

    d = REPO_ROOT / "docs" / "2026-season"
    d.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).date().isoformat()
    p = d / f"{stamp}-week-{week}-review.md"
    res = section.get("result") or {}
    eff = section.get("efficiency") or {}
    lines = [
        f"# Week {week} review — {stamp}", "",
        f"**Result:** {output.get('result', '')}", "",
        f"**Efficiency:** {eff.get('pct', '?')} (actual {eff.get('actual', '?')} / best "
        f"{eff.get('best_possible', '?')}, {eff.get('left_on_bench', '?')} left on bench)"
        + (f" — {eff['worst_call']}" if eff.get("worst_call") else ""), "",
        f"**Read:** {output.get('efficiency_read', '')}", "",
        "## Decisions graded", "",
    ]
    for g in output.get("decision_grades") or []:
        lines.append(f"- **{g.get('grade')}** — {g.get('decision')}: {g.get('why')}")
    lines += ["", "## Calibration", ""] + [f"- {x}" for x in output.get("calibration") or []]
    lines += ["", "## Lessons", ""] + [f"- {x}" for x in output.get("lessons") or []]
    lines += ["", "## League", ""] + [f"- {x}" for x in output.get("league_scan") or []]
    props = output.get("prior_proposals") or []
    lines += ["", "## Prior proposals (Pearce applies)", ""]
    lines += [f"- `{x.get('key')}`: {x.get('from')} → {x.get('to')} — {x.get('evidence')}"
              for x in props] or ["- none"]
    lines += ["", "## Watch", ""] + [f"- {x}" for x in output.get("watch_items") or []]
    if res:
        lines += ["", f"_raw: {res}_"]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(p)
