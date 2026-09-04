"""Recompute fantasy points from a raw stat line under an arbitrary scoring map.

Needed because scoring drifts. 2024 and 2025 share a scoring map; **2026 does
not** — passing TDs went 4 to 5, and the yardage items changed statId
(2026 carries `{24: 0.1, 42: 0.1}`, 2025 carried `{23: 0.5, 28: 1.0, 48: 1.0}`).
So ESPN's `appliedTotal` for 2025 answers "what would he have scored in the 2025
league", and the question worth asking is "what would he have scored in MY 2026
league".

**Nothing here is trusted until it reproduces ESPN's own arithmetic.** `verify`
recomputes every historical weekly line under that season's OWN scoring and
compares against `appliedTotal`. Measured on 2025 (6,030 lines):

    WR 2008/2008 · RB 1351/1351 · TE 1136/1136 · QB 548/548 · K 443/443
    D/ST 23/544

Every offensive line reproduces to the cent. D/ST does not, and cannot: ESPN
scores a defense with BRACKETS — points allowed 0, 1-6, 7-13 and so on — which
the flat statId-to-rate map cannot express, so those items come through at 0.0.

Rather than lower the bar to 91% and call it agreement, this module scores each
line by whichever method is provably correct for it:

- a line that **reproduces** is recomputed under the new map;
- a line that does not is **carried through at ESPN's own total**, but ONLY if
  it contains no stat whose rate differs between the two maps. Every D/ST stat
  is identical in 2025 and 2026, so a 2025 defense scores the same in either —
  carrying it forward is exact arithmetic, not an approximation.
- a line that neither reproduces nor is drift-free is **refused**. Nothing is
  guessed.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

#: Agreement tolerance, in fantasy points, for one weekly line.
TOLERANCE = 0.01


def points(stats: dict[int, float], scoring: dict[int, float]) -> float:
    """Fantasy points for one stat line under one scoring map.

    ESPN's offensive model is flat: every scoring item is points-per-unit of
    some statId, including ones that look like thresholds — "0.1 per rushing
    yard" is statId 24 at 0.1, not a bracket. Stats the league does not score
    are absent from the map and ignored.
    """
    total = 0.0
    for stat_id, value in stats.items():
        rate = scoring.get(int(stat_id))
        if rate:
            total += rate * float(value)
    return total


def drifted_stat_ids(a: dict[int, float], b: dict[int, float]) -> set[int]:
    """Every statId whose rate differs between two scoring maps.

    A stat absent from one map is treated as rate 0 there, which is what it
    means: the league did not score it.
    """
    return {sid for sid in set(a) | set(b) if a.get(sid, 0.0) != b.get(sid, 0.0)}


@dataclass
class Reproduction:
    """How well `points` reproduces ESPN's own arithmetic, by position."""

    lines: int = 0
    agreed: int = 0
    by_pos: dict[str, tuple[int, int]] = field(default_factory=dict)  # pos -> (agreed, total)
    worst_gap: float = 0.0
    worst_example: str = ""

    @property
    def rate(self) -> float:
        return self.agreed / self.lines if self.lines else 0.0

    def describe(self) -> str:
        parts = " · ".join(f"{p} {a}/{t}" for p, (a, t) in sorted(self.by_pos.items()))
        return f"{self.agreed}/{self.lines} ({self.rate:.1%}) — {parts}"


def verify(season, *, tolerance: float = TOLERANCE) -> Reproduction:
    """Recompute every weekly line under the season's OWN scoring.

    Takes a `history.Season`. Reports per position, because "91% of lines
    agree" and "one position never agrees and the rest are perfect" are
    completely different findings and only the second is actionable.
    """
    scoring = season.facts.settings.scoring
    by_id = season.by_id
    rep = Reproduction()
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])

    for espn_id, weeks in season.raw_weekly.items():
        player = by_id.get(espn_id)
        if player is None:
            continue
        for wk, stats in weeks.items():
            espn_total = player.actual_week.get(wk)
            if espn_total is None or not stats:
                continue
            rep.lines += 1
            counts[player.pos.value][1] += 1
            gap = abs(points(stats, scoring) - espn_total)
            if gap <= tolerance:
                rep.agreed += 1
                counts[player.pos.value][0] += 1
            elif gap > rep.worst_gap:
                rep.worst_gap = gap
                rep.worst_example = f"{player.name} wk{wk} off by {gap:.2f}"

    rep.by_pos = {p: (a, t) for p, (a, t) in counts.items()}
    return rep


class RescoreRefused(RuntimeError):
    """Raised rather than returning numbers nobody checked."""


@dataclass
class RescoreResult:
    """Rescored points plus an honest account of how each line was produced."""

    weeks: dict[int, dict[int, float]]
    recomputed: int = 0
    carried: int = 0
    refused: list[str] = field(default_factory=list)

    def describe(self) -> str:
        s = f"{self.recomputed} lines recomputed, {self.carried} carried unchanged"
        if self.refused:
            s += f", {len(self.refused)} REFUSED"
        return s


def rescored_weeks(season, scoring: dict[int, float], *,
                   tolerance: float = TOLERANCE) -> RescoreResult:
    """espn_id -> week -> points, under `scoring`.

    Per-line, using whichever method is provably correct (see module docstring).
    Raises `RescoreRefused` if any line is neither reproducible nor drift-free,
    because a season with a hole in it is not a season we can score.
    """
    own = season.facts.settings.scoring
    drifted = drifted_stat_ids(own, scoring)
    by_id = season.by_id
    res = RescoreResult(weeks={})

    for espn_id, weeks in season.raw_weekly.items():
        player = by_id.get(espn_id)
        if player is None:
            continue
        out: dict[int, float] = {}
        for wk, stats in weeks.items():
            espn_total = player.actual_week.get(wk)
            if espn_total is None:
                continue
            if stats and abs(points(stats, own) - espn_total) <= tolerance:
                out[wk] = points(stats, scoring)
                res.recomputed += 1
            elif not (drifted & {int(k) for k in stats}):
                # Nothing this line scores changed between the two maps, so its
                # total is the same number in both. Exact, not approximate.
                out[wk] = espn_total
                res.carried += 1
            else:
                res.refused.append(f"{player.name} ({player.pos.value}) wk{wk}")
        res.weeks[espn_id] = out

    if res.refused:
        raise RescoreRefused(
            f"{len(res.refused)} line(s) can be neither reproduced nor carried "
            f"forward, e.g. {res.refused[:3]} — rescoring {season.year} refused"
        )
    log.info("rescored %d: %s", season.year, res.describe())
    return res


def rescored_projections(season, scoring: dict[int, float], *,
                         tolerance: float = TOLERANCE) -> dict[int, float]:
    """espn_id -> preseason season projection, under `scoring`.

    Normalised mode needs this, not just rescored actuals. Drafting on 2025
    scoring and then grading on 2026 scoring measures the engine against an
    objective it was never given — it would be marked down for correctly
    maximising the wrong thing. Rescoring the projection puts the engine and
    the scoreboard back on one set of rules.

    A projection whose stat line does not reproduce is left at ESPN's own
    number, which is right whenever nothing it scores drifted and is the least
    wrong option when something did — this is the input to a ranking, not a
    result, and dropping a player from the board entirely would be worse.
    """
    own = season.facts.settings.scoring
    out: dict[int, float] = {}
    for p in season.players:
        stats = season.raw_projection.get(p.espn_id)
        if stats and abs(points(stats, own) - p.proj_season) <= tolerance:
            out[p.espn_id] = points(stats, scoring)
        else:
            out[p.espn_id] = p.proj_season
    return out


def apply_projections(season, projections: dict[int, float]) -> None:
    """Rewrite each player's `proj_season` in place. Mutates the Season."""
    for p in season.players:
        if p.espn_id in projections:
            p.proj_season = projections[p.espn_id]


