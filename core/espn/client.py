"""The one ESPN read client. READ-ONLY — ESPN publishes no write endpoint.

Everything that reaches out to ESPN for data goes through here, so retries,
timeouts and the auth-failure sniff live in exactly one place. Writes are
Playwright, in core/browser, and are a completely separate path (§8.2).
"""

from __future__ import annotations

import functools
import json
import logging
import time
from typing import Any

from espn_api.football import League

from core.config import Settings, settings

log = logging.getLogger(__name__)


class EspnAuthError(RuntimeError):
    """Cookies are dead or wrong. §8.5 — the #1 silent failure.

    Raised rather than returned so no caller can accidentally proceed on an
    empty result set that looks like a legitimately empty league.
    """


class EspnReadError(RuntimeError):
    pass


#: Substrings that mean "you are not logged in" rather than "no data".
_AUTH_MARKERS = ("<!doctype html", "<html", "log in", "sign in", "unauthorized", "not authorized")


def _looks_like_login(payload: Any) -> bool:
    if isinstance(payload, str):
        low = payload[:2000].lower()
        return any(m in low for m in _AUTH_MARKERS)
    return False


class EspnClient:
    """Thin wrapper over espn_api.football.League plus raw view access."""

    def __init__(self, cfg: Settings | None = None) -> None:
        self.cfg = cfg or settings()
        self._league: League | None = None

    # ── connection ───────────────────────────────────────────────────────────

    @property
    def league(self) -> League:
        if self._league is None:
            self._league = self._connect()
        return self._league

    def _connect(self) -> League:
        try:
            return League(
                league_id=self.cfg.league_id,
                year=self.cfg.season,
                espn_s2=self.cfg.espn_s2,
                swid=self.cfg.swid,
            )
        except Exception as e:  # espn_api raises bare Exceptions on 401
            msg = str(e).lower()
            if "401" in msg or "unauthorized" in msg or "private" in msg:
                raise EspnAuthError(
                    "ESPN rejected the cookies. Re-mint SWID and espn_s2 "
                    "(§8.5) — every read is unreliable until then."
                ) from e
            raise EspnReadError(f"could not reach ESPN: {e}") from e

    def refresh(self) -> None:
        """Re-pull league state. Cheaper than reconnecting."""
        self.league.refresh()

    # ── raw views ────────────────────────────────────────────────────────────

    def get_view(
        self,
        views: str | list[str],
        *,
        filters: dict | None = None,
        params: dict | None = None,
        retries: int = 3,
    ) -> dict:
        """Fetch one or more mSettings-style views.

        `filters` becomes the x-fantasy-filter header, which is how ESPN does
        server-side filtering and sorting on the player universe.
        """
        p: dict[str, Any] = dict(params or {})
        p["view"] = views if isinstance(views, list) else [views]
        headers = {"x-fantasy-filter": json.dumps(filters)} if filters else None

        last: Exception | None = None
        for attempt in range(retries):
            try:
                data = self.league.espn_request.league_get(params=p, headers=headers)
                if _looks_like_login(data):
                    raise EspnAuthError(
                        "ESPN returned a login page instead of JSON — cookies are dead (§8.5)."
                    )
                return data
            except EspnAuthError:
                raise
            except Exception as e:
                last = e
                if attempt < retries - 1:
                    time.sleep(0.6 * (2**attempt))
        raise EspnReadError(f"view {views} failed after {retries} tries: {last}")

    # ── convenience ──────────────────────────────────────────────────────────

    @functools.cached_property
    def my_team_id(self) -> int:
        """Resolve our team id from the configured team name.

        Matched case-insensitively on a stripped name so a stray space in .env
        doesn't silently point the whole system at nobody.
        """
        want = self.cfg.team_name.strip().casefold()
        for t in self.league.teams:
            if t.team_name.strip().casefold() == want:
                return t.team_id
        names = ", ".join(repr(t.team_name) for t in self.league.teams)
        raise EspnReadError(
            f"no team named {self.cfg.team_name!r} in league {self.cfg.league_id}. Teams: {names}"
        )

    @property
    def my_team(self):
        return next(t for t in self.league.teams if t.team_id == self.my_team_id)

    @property
    def current_week(self) -> int:
        # Pre-season ESPN reports 0; week 1 is the useful floor for projections.
        return max(1, int(self.league.current_week or 0))


@functools.lru_cache(maxsize=1)
def client() -> EspnClient:
    return EspnClient()
