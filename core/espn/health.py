"""§8.5 — cookie expiry is the #1 silent failure.

espn_s2 dies without warning, and every read then returns something that looks
plausible: a login page, an empty roster, a 401 dressed as HTML. A system that
keeps going on that will set a lineup of nobody and call it done.

So every run starts here. Fetch our own roster, prove it is ours, prove the week
is sane. Anything unexpected flips the kill switch (§8.4) and stops the run.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from core.espn.client import EspnAuthError, EspnClient, EspnReadError, client
from core.gates import kill_switch

log = logging.getLogger(__name__)


@dataclass
class HealthResult:
    ok: bool
    checks: list[tuple[str, bool, str]] = field(default_factory=list)

    @property
    def failures(self) -> list[str]:
        return [f"{n}: {d}" for n, p, d in self.checks if not p]

    def summary(self) -> str:
        return " · ".join(f"{'✓' if p else '✗'} {n}" for n, p, _ in self.checks)


def check(c: EspnClient | None = None, *, kill_on_fail: bool = True) -> HealthResult:
    """Run the pre-flight. Cheap enough to call before every write."""
    checks: list[tuple[str, bool, str]] = []

    try:
        c = c or client()
    except Exception as e:
        r = HealthResult(False, [("connect", False, str(e)[:200])])
        if kill_on_fail:
            kill_switch.turn_off(f"cannot construct ESPN client: {e}")
        return r

    # 1 — auth works at all
    try:
        teams = c.league.teams
        checks.append(("auth", bool(teams), f"{len(teams)} teams visible"))
    except EspnAuthError as e:
        checks.append(("auth", False, str(e)[:200]))
        return _finish(HealthResult(False, checks), kill_on_fail)
    except (EspnReadError, Exception) as e:
        checks.append(("auth", False, f"read failed: {str(e)[:180]}"))
        return _finish(HealthResult(False, checks), kill_on_fail)

    # 2 — the league is the one we think it is
    try:
        n = len(teams)
        checks.append(("league", n >= 2, f"league {c.cfg.league_id}, {n} teams"))
    except Exception as e:
        checks.append(("league", False, str(e)[:200]))

    # 3 — our team resolves, by name, to exactly one team
    try:
        tid = c.my_team_id
        name = c.my_team.team_name
        checks.append(("our_team", True, f"id {tid} = {name!r}"))
    except Exception as e:
        checks.append(("our_team", False, str(e)[:200]))
        return _finish(HealthResult(False, checks), kill_on_fail)

    # 4 — the week is sane. ESPN reports 0 pre-season, which is fine; 25 is not.
    try:
        wk = int(c.league.current_week or 0)
        ok = 0 <= wk <= 18
        checks.append(("week", ok, f"current_week={wk}"))
    except Exception as e:
        checks.append(("week", False, str(e)[:200]))

    # 5 — the player universe answers. An empty pool with a valid cookie means
    #     ESPN changed something, and every downstream valuation would be empty.
    try:
        data = c.get_view(
            "kona_player_info",
            filters={"players": {"limit": 1, "offset": 0,
                                 "sortPercOwned": {"sortAsc": False, "sortPriority": 1}}},
        )
        n = len(data.get("players") or [])
        checks.append(("players", n > 0, f"{n} returned for a limit-1 probe"))
    except Exception as e:
        checks.append(("players", False, str(e)[:200]))

    ok = all(p for _, p, _ in checks)
    return _finish(HealthResult(ok, checks), kill_on_fail)


def _finish(r: HealthResult, kill_on_fail: bool) -> HealthResult:
    if not r.ok:
        log.error("HEALTH FAILED: %s", "; ".join(r.failures))
        if kill_on_fail:
            kill_switch.turn_off("health check failed: " + "; ".join(r.failures)[:300])
    return r
