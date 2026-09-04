"""§P.3 — put the fitted projection onto the board.

Mirrors §2.2b's consensus blend: our model produces an ORDER, that order is
mapped onto the board's own value ladder, and the result is mixed into
`proj_season` before valuation. Working in ladder space rather than raw points
keeps one scale in the system — VOR, tiers and every downstream consumer still
see a single number.

**One deliberate difference from `market.blend`.** ESPN ranks the whole pool, so
its rank *r* can be read against the whole ladder. Our model ranks only the
players it can see — four positions, and only those with prior NFL seasons — so
reading its rank against the whole ladder would quietly promote every covered
player over every uncovered one (a rookie would sink regardless of merit). The
ladder here is therefore built from the covered players' OWN current values: the
blend reorders players within the covered set and leaves the set's overall level,
and everyone outside it, untouched.
"""

from __future__ import annotations

import logging

import polars as pl

from core.model.schema import Player
from core.proj import features, model, nflstats

log = logging.getLogger(__name__)


def project_pool(players: list[Player], m: model.Model, scoring: dict[int, float],
                 season: int) -> dict[int, model.Projection]:
    """espn_id -> projection, for every pool player the model can see.

    Features come from the three seasons before `season`. A player absent from
    the result is one the model has no opinion about; the caller must leave him
    alone rather than scoring him zero.
    """
    from core.data.nflverse import espn_to_gsis

    wide = nflstats.seasons(list(range(season - features.LOOKBACK, season)), scoring)
    rows = features.build(wide.filter(pl.col("season") < season), season)
    projs = model.project_all(rows, m)

    bridge = espn_to_gsis()
    out: dict[int, model.Projection] = {}
    for p in players:
        g = bridge.get(p.espn_id)
        if g and g in projs:
            out[p.espn_id] = projs[g]
    log.info("projection model covers %d/%d pool players", len(out), len(players))
    return out


def blend(players: list[Player], projections: dict[int, model.Projection],
          weight: float) -> int:
    """Mix the model's ordering into `proj_season`. Returns players touched."""
    if weight <= 0.0:
        return 0
    if not 0.0 <= weight <= 1.0:
        raise ValueError(f"projection blend weight must be in [0, 1], got {weight}")

    covered = [p for p in players if p.espn_id in projections]
    if not covered:
        log.warning("projection blend requested but no pool player is covered")
        return 0

    # The ladder is the covered players' own values, so the blend reorders
    # within the covered set without moving it relative to everyone else.
    ladder = sorted((p.proj_season for p in covered), reverse=True)
    order = sorted(covered, key=lambda p: -projections[p.espn_id].points)

    for i, p in enumerate(order):
        p.proj_season = (1.0 - weight) * p.proj_season + weight * ladder[i]

    log.info("projection blend w=%.2f applied to %d/%d players",
             weight, len(covered), len(players))
    return len(covered)
