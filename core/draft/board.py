"""§3.2 — build the board BEFORE the draft.

The 90-second clock means the live loop re-sorts a table it already has; it does
not think (§3.2, §8.7). Everything expensive — ESPN pool, nflverse history,
durability, VOR, tiers — happens here, once, and is written to data/board.json.

The agent's news pass (agent/prompts/predraft.md) writes data/overrides.json,
which this module applies on a second run. Overrides are bounded by
priors.model.override_cap so a model can nudge the board, never rewrite it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from core.config import settings
from core.data import nflverse
from core.espn import players as espn_players
from core.espn import settings as espn_settings
from core.espn.client import client
from core.espn.settings import LeagueFacts
from core.model.priors import priors
from core.model.schema import Player, Valuation
from core.model.value import PlayerContext, value_pool

log = logging.getLogger(__name__)

BOARD_VERSION = 1


@dataclass
class Board:
    built_at: datetime
    facts: LeagueFacts
    players: list[Player]
    valuations: dict[int, Valuation]
    #: Diagnostics — how much of the board is standing on real data (§8.8).
    coverage: dict[str, float]

    @property
    def rows(self) -> list[tuple[Player, Valuation]]:
        """Sorted by VOR, which is the order everything downstream assumes."""
        pairs = [(p, self.valuations[p.espn_id]) for p in self.players
                 if p.espn_id in self.valuations]
        pairs.sort(key=lambda pv: pv[1].vor, reverse=True)
        return pairs

    @property
    def by_id(self) -> dict[int, Player]:
        return {p.espn_id: p for p in self.players}

    def age_hours(self) -> float:
        return (datetime.now(UTC) - self.built_at).total_seconds() / 3600.0

    def is_stale(self) -> bool:
        return self.age_hours() > float(priors().get("draft.board_max_age_hours"))


def build(
    *,
    size: int = 450,
    apply_overrides: bool = True,
    week: int | None = None,
) -> Board:
    """Build the board from live sources. Slow (seconds), run pre-draft."""
    cfg = settings()
    c = client()
    facts = espn_settings.load(c)

    pool = espn_players.load_pool(c, size=size)
    byes = espn_players.attach_byes(pool, c)

    # ── injury history, joined on ESPN id with a name fallback ───────────────
    contexts: dict[int, PlayerContext] = {}
    matched = 0
    for p in pool:
        events, ok = nflverse.history_for(p.espn_id, p.name)
        matched += int(ok)
        contexts[p.espn_id] = PlayerContext(injury_history=events)

    # ── the agent's bounded news pass (§2.8, §3.2) ───────────────────────────
    n_overrides = 0
    if apply_overrides:
        n_overrides = _apply_overrides(cfg.overrides_path, contexts, pool)

    cap = float(priors().get("model.override_cap"))
    vals = value_pool(
        pool,
        facts.settings,
        window="week" if week else "ros",
        week=week,
        weeks_remaining=facts.settings.regular_season_weeks,
        contexts=contexts,
        override_cap=cap,
    )

    coverage = {
        "players": float(len(pool)),
        "with_projection": float(sum(1 for p in pool if p.proj_season > 0)),
        "with_adp": float(sum(1 for p in pool if p.espn_adp)),
        "with_bye": float(byes),
        "injury_history_matched": float(matched),
        "news_overrides": float(n_overrides),
    }
    log.info("board built: %s", coverage)

    return Board(
        built_at=datetime.now(UTC),
        facts=facts,
        players=pool,
        valuations=vals,
        coverage=coverage,
    )


def _apply_overrides(
    path: Path,
    contexts: dict[int, PlayerContext],
    pool: list[Player],
) -> int:
    """Read data/overrides.json into the contexts. Never trusts its shape."""
    if not path.exists():
        return 0
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("overrides.json unreadable (%s) — ignoring", e)
        return 0

    items = raw.get("overrides", raw) if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        log.warning("overrides.json has no list of overrides — ignoring")
        return 0

    valid_ids = {p.espn_id for p in pool}
    n = 0
    for item in items:
        try:
            pid = int(item["espn_id"])
            mult = float(item["multiplier"])
        except (KeyError, TypeError, ValueError):
            continue
        if pid not in valid_ids:
            continue
        ctx = contexts.setdefault(pid, PlayerContext())
        ctx.news_override = mult
        ctx.news_reason = str(item.get("reason", ""))[:400]
        n += 1
    log.info("applied %d news overrides", n)
    return n


# ── persistence ──────────────────────────────────────────────────────────────


def save(board: Board, path: Path | None = None) -> Path:
    path = path or settings().board_path
    payload = {
        "version": BOARD_VERSION,
        "built_at": board.built_at.isoformat(),
        "league_id": board.facts.settings.league_id,
        "season": board.facts.settings.season,
        "coverage": board.coverage,
        "players": [p.model_dump(mode="json") for p in board.players],
        "valuations": {str(k): v.model_dump(mode="json") for k, v in board.valuations.items()},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    log.info("board saved to %s (%d players)", path, len(board.players))
    return path


def load(path: Path | None = None, facts: LeagueFacts | None = None) -> Board:
    path = path or settings().board_path
    if not path.exists():
        raise FileNotFoundError(
            f"no board at {path}. Run scripts/build_board.py before the draft (§3.2)."
        )
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw.get("version") != BOARD_VERSION:
        raise ValueError(f"board version {raw.get('version')} != {BOARD_VERSION}; rebuild it")

    return Board(
        built_at=datetime.fromisoformat(raw["built_at"]),
        facts=facts or espn_settings.load(),
        players=[Player.model_validate(p) for p in raw["players"]],
        valuations={int(k): Valuation.model_validate(v) for k, v in raw["valuations"].items()},
        coverage=raw.get("coverage", {}),
    )
