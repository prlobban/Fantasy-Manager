"""Typed settings from .env. Fails loud rather than defaulting.

§10.6 — core fails closed. A missing credential must stop the run, not produce a
half-configured client that returns plausible-looking nonsense.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]


def _require(key: str) -> str:
    v = os.environ.get(key, "").strip()
    if not v:
        raise RuntimeError(
            f"{key} is not set. Copy .env.example to .env and fill it in. "
            "core refuses to run half-configured (§10.6)."
        )
    return v


@dataclass(frozen=True)
class Settings:
    swid: str
    espn_s2: str
    league_id: int
    season: int
    team_name: str

    data_dir: Path
    enabled_file: Path

    slack_token_file: str | None
    slack_channel_id: str | None

    draft_at: datetime | None
    claude_bin: str
    claude_model: str
    claude_research_model: str
    agent_max_turns: int

    log_level: str

    @property
    def board_path(self) -> Path:
        return self.data_dir / "board.json"

    @property
    def overrides_path(self) -> Path:
        return self.data_dir / "overrides.json"

    @property
    def state_path(self) -> Path:
        return self.data_dir / "state.json"

    @property
    def decisions_path(self) -> Path:
        return self.data_dir / "decisions.jsonl"

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def screenshot_dir(self) -> Path:
        return self.data_dir / "screenshots"

    @property
    def agent_runs_dir(self) -> Path:
        return self.data_dir / "agent-runs"

    @property
    def dossiers_dir(self) -> Path:
        """§3.2 — one research record per player, plus the usage ledger."""
        return self.data_dir / "dossiers"

    def ensure_dirs(self) -> None:
        for d in (
            self.data_dir,
            self.cache_dir,
            self.screenshot_dir,
            self.agent_runs_dir,
            self.dossiers_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)


_cached: Settings | None = None


def settings(reload: bool = False) -> Settings:
    """Process-wide settings. Reads .env once."""
    global _cached
    if _cached is not None and not reload:
        return _cached

    load_dotenv(REPO_ROOT / ".env", override=False)

    data_dir = Path(os.environ.get("DATA_DIR", REPO_ROOT / "data"))
    if not data_dir.is_absolute():
        data_dir = REPO_ROOT / data_dir

    enabled = Path(os.environ.get("ENABLED_FILE", REPO_ROOT / "ENABLED"))
    if not enabled.is_absolute():
        enabled = REPO_ROOT / enabled

    draft_at = None
    if raw := os.environ.get("DRAFT_AT", "").strip():
        try:
            draft_at = datetime.fromisoformat(raw)
        except ValueError:
            draft_at = None

    _cached = Settings(
        swid=_require("ESPN_SWID"),
        espn_s2=_require("ESPN_S2"),
        league_id=int(_require("ESPN_LEAGUE_ID")),
        season=int(os.environ.get("ESPN_SEASON", "2026")),
        team_name=_require("ESPN_TEAM_NAME"),
        data_dir=data_dir,
        enabled_file=enabled,
        slack_token_file=os.environ.get("SLACK_TOKEN_FILE") or None,
        slack_channel_id=os.environ.get("SLACK_CHANNEL_ID") or None,
        draft_at=draft_at,
        claude_bin=os.environ.get("CLAUDE_BIN", "claude"),
        claude_model=os.environ.get("CLAUDE_MODEL", "claude-opus-5"),
        # §10 — the research pass is bulk retrieval, not judgment, and it is the
        # one workload big enough to threaten a rate-limit window. Sonnet by
        # default; the judge and the manager stay on claude_model.
        claude_research_model=os.environ.get("CLAUDE_RESEARCH_MODEL", "claude-sonnet-5"),
        agent_max_turns=int(os.environ.get("AGENT_MAX_TURNS", "12")),
        log_level=os.environ.get("LOG_LEVEL", "INFO"),
    )
    _cached.ensure_dirs()
    return _cached
