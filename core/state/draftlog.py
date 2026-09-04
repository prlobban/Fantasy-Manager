"""One directory per draft, holding EVERYTHING that happened in it.

Why this exists: the live loop used to log only the last new pick of each
cycle and never wrote its reasoning down at all. When a pick looked wrong
afterwards there was no way to answer "why did it do that?" — the ranking
that justified it had been computed, used and thrown away (§7.1 says log the
prediction, not just the action; this is that rule applied to the draft).

Each run writes:

    data/drafts/<stamp>-<live|practice>/
        run.log        every log line from every module, timestamped
        events.jsonl   one structured record per event, machine-readable
        decisions.md   readable: the full board state and ranking behind
                       each of OUR picks, plus every pick in the room

Nothing here is allowed to break a draft. Every method swallows its own
errors: a logging failure at 11:00 on draft day must not cost a pick.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.config import settings

log = logging.getLogger(__name__)


class DraftLog:
    """A per-draft record. Created once per run, closed in a finally block."""

    def __init__(self, kind: str = "live", *, root: Path | None = None) -> None:
        self.kind = kind
        self.started = datetime.now(UTC)
        stamp = self.started.strftime("%Y%m%dT%H%M%S")
        base = root or (settings().data_dir / "drafts")
        self.dir = base / f"{stamp}-{kind}"
        self._handler: logging.Handler | None = None
        self._ok = True
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:  # pragma: no cover - disk problems only
            log.warning("could not create draft log dir: %s", e)
            self._ok = False

    # ── plumbing ─────────────────────────────────────────────────────────────

    @property
    def run_log(self) -> Path:
        return self.dir / "run.log"

    @property
    def events_path(self) -> Path:
        return self.dir / "events.jsonl"

    @property
    def decisions_path(self) -> Path:
        return self.dir / "decisions.md"

    def attach(self) -> None:
        """Capture every log line from every module into run.log.

        This is what makes the file complete rather than a summary: browser
        warnings, queue op failures and Playwright errors all land here too.
        """
        if not self._ok:
            return
        try:
            h = logging.FileHandler(self.run_log, encoding="utf-8")
            h.setLevel(logging.INFO)
            h.setFormatter(logging.Formatter(
                "%(asctime)s %(levelname)-7s %(name)-22s %(message)s",
                datefmt="%H:%M:%S",
            ))
            root = logging.getLogger()
            root.addHandler(h)
            if root.level > logging.INFO:
                root.setLevel(logging.INFO)
            self._handler = h
        except Exception as e:
            log.warning("could not attach draft log handler: %s", e)

    def detach(self) -> None:
        if self._handler is not None:
            try:
                logging.getLogger().removeHandler(self._handler)
                self._handler.close()
            except Exception:
                pass
            self._handler = None

    # ── writing ──────────────────────────────────────────────────────────────

    def event(self, kind: str, **fields: Any) -> None:
        """One structured record. Never raises."""
        if not self._ok:
            return
        try:
            rec = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "event": kind}
            rec.update(fields)
            with self.events_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=str) + "\n")
        except Exception as e:
            log.debug("draft event write failed: %s", e)

    def md(self, text: str) -> None:
        """Append readable text to decisions.md. Never raises."""
        if not self._ok:
            return
        try:
            with self.decisions_path.open("a", encoding="utf-8") as f:
                f.write(text.rstrip() + "\n\n")
        except Exception as e:
            log.debug("draft md write failed: %s", e)

    # ── the things the loop records ──────────────────────────────────────────

    def header(self, *, league: str, team: str, slot: int, teams: int,
               my_picks: list[int], board_players: int, board_age_h: float,
               dry_run: bool, click: bool) -> None:
        self.md(
            f"# {league} — {'PRACTICE' if self.kind == 'practice' else 'LIVE'} draft\n\n"
            f"Started {self.started:%Y-%m-%d %H:%M:%S} UTC · team **{team}** · "
            f"slot **{slot} of {teams}**\n\n"
            f"- our picks: `{my_picks}`\n"
            f"- board: {board_players} players, built {board_age_h:.1f}h ago\n"
            f"- mode: {'DRY RUN' if dry_run else ('queue + click' if click else 'queue only')}\n"
        )
        self.event("start", league=league, team=team, slot=slot, teams=teams,
                   my_picks=my_picks, board_players=board_players,
                   board_age_hours=round(board_age_h, 2), dry_run=dry_run, click=click)

    def room_pick(self, pick, *, ours: bool, best_now: str | None) -> None:
        """EVERY pick in the room, one line each — not just the last of a cycle."""
        who = "US" if ours else f"team {pick.team_id}"
        self.md(f"`#{pick.overall:>3}` {who:>8} — **{pick.name}** "
                f"({pick.pos.value if pick.pos else '?'})"
                + (f" · our board now leads with {best_now}" if best_now else ""))
        self.event("room_pick", overall=pick.overall, team_id=pick.team_id, ours=ours,
                   espn_id=pick.espn_id, name=pick.name,
                   pos=pick.pos.value if pick.pos else None, best_now=best_now)

    def our_turn(self, plan, room, *, overall: int) -> None:
        """The FULL ranking behind one of our picks: outlooks, every candidate,
        every adjustment term. This is the answer to "why did it take him?"."""
        have = {p.value: n for p, n in room.my_positions.items()}
        self.md(
            f"---\n\n## Our pick #{overall} (round {plan.round_num})\n\n"
            f"Roster so far: `{have or '{}'}` · "
            f"{plan.picks_until_next} picks until our next turn"
            + (f" · **run on at {plan.run_on.value}**" if plan.run_on else "")
        )

        lines = ["| pos | cost of waiting | best now | expected next | top tier left |",
                 "|---|---|---|---|---|"]
        for pos, o in sorted(plan.outlooks.items(), key=lambda kv: -kv[1].cost):
            lines.append(f"| {pos.value} | {o.cost:.1f} | {o.best_now:.1f} | "
                         f"{o.expected_next:.1f} | tier {o.top_tier}: {o.top_tier_remaining} |")
        self.md("\n".join(lines))

        rows = ["| # | player | pos | VOR | score | adjustments | note |",
                "|---|---|---|---|---|---|---|"]
        for i, c in enumerate(plan.top(12), 1):
            adj = " ".join(f"{k}{v:+.1f}" for k, v in c.reasons.items() if k != "base")
            rows.append(f"| {i} | {c.player.name} | {c.player.pos.value} | "
                        f"{c.valuation.vor:.1f} | {c.score:.1f} | {adj or '—'} | "
                        f"{c.note or ''} |")
        self.md("\n".join(rows))

        self.event(
            "our_turn", overall=overall, round=plan.round_num,
            roster=have, picks_until_next=plan.picks_until_next,
            run_on=plan.run_on.value if plan.run_on else None,
            outlooks={
                pos.value: {"cost": round(o.cost, 2), "best_now": round(o.best_now, 2),
                            "expected_next": round(o.expected_next, 2),
                            "top_tier": o.top_tier, "top_tier_left": o.top_tier_remaining}
                for pos, o in plan.outlooks.items()
            },
            candidates=[
                {"rank": i, "name": c.player.name, "pos": c.player.pos.value,
                 "espn_id": c.player.espn_id, "vor": round(c.valuation.vor, 2),
                 "points": round(c.valuation.points, 2), "tier": c.valuation.tier,
                 "score": c.score, "reasons": c.reasons, "note": c.note}
                for i, c in enumerate(plan.top(20), 1)
            ],
        )

    def queue(self, *, target: list[str], current: int, ops: list, landed: int,
              aborted: bool = False) -> None:
        if not ops:
            return
        kinds = {}
        for o in ops:
            kinds[o.kind] = kinds.get(o.kind, 0) + 1
        self.event("queue_sync", target=target, current_size=current,
                   ops={k: v for k, v in kinds.items()}, planned=len(ops),
                   landed=landed, aborted=aborted)

    def pick_made(self, *, overall: int, name: str, pos: str, vor: float, score: float,
                  tier: int, runner_up: str | None, runner_up_vor: float | None,
                  how: str, receipt: str | None = None) -> None:
        self.md(f"**→ took {name} ({pos})** · VOR {vor:.1f} · score {score:.1f} · "
                f"tier {tier} · via {how}"
                + (f" · passed on {runner_up} (VOR {runner_up_vor:.1f})"
                   if runner_up and runner_up_vor is not None else ""))
        self.event("our_pick", overall=overall, name=name, pos=pos, vor=vor, score=score,
                   tier=tier, runner_up=runner_up, runner_up_vor=runner_up_vor,
                   how=how, receipt=receipt)

    def judge(self, verdict, *, overall: int, mode: str, changed: bool,
              before: str | None, after: str | None) -> None:
        """§3.10 — what the judge said, whether it was applied, and what it cost.

        Recorded whether the mode is shadow or live, because the shadow record
        is the evidence for granting it live: a judge that would have changed
        nothing for 13 picks has not earned the wheel.
        """
        head = f"**Judge ({mode})** · {verdict.describe()}"
        if changed:
            head += f" · would take **{after}** over {before}"
        body = head + "\n\n> " + verdict.summary
        if verdict.rejected:
            body += "\n>\n> ⚠️ refused: " + "; ".join(verdict.rejected)
        self.md(body)
        self.event(
            "judge", overall=overall, mode=mode, agree=verdict.agree,
            changed=changed, before=before, after=after,
            summary=verdict.summary,
            vetoes=[{"espn_id": lv.espn_id, "name": lv.name, "reason": lv.reason,
                     "cites": lv.cites, "dossier_fact": lv.dossier_fact}
                    for lv in verdict.vetoes],
            reorders=[{"espn_id": lv.espn_id, "name": lv.name,
                       "above": lv.above_espn_id, "reason": lv.reason,
                       "cites": lv.cites, "dossier_fact": lv.dossier_fact}
                      for lv in verdict.reorders],
            rejected=verdict.rejected, flags=verdict.flags,
        )

    def problem(self, what: str, detail: str) -> None:
        self.md(f"> ⚠️ **{what}** — {detail}")
        self.event("problem", what=what, detail=detail)

    def finish(self, *, roster: list[str], stats) -> None:
        self.md("---\n\n## Final roster\n\n"
                + "\n".join(f"{i}. {n}" for i, n in enumerate(roster, 1))
                + f"\n\ncycles {stats.cycles} · picks seen {stats.picks_seen} · "
                f"queue ops {stats.queue_ops} · errors {len(stats.errors)}")
        self.event("finish", roster=roster, cycles=stats.cycles,
                   picks_seen=stats.picks_seen, queue_ops=stats.queue_ops,
                   errors=stats.errors)

    def close(self) -> None:
        self.detach()
