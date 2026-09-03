"""Playwright session — the ONLY write path that exists.

ESPN's fantasy API is read-only, so every pick, lineup change, claim and trade
goes through a logged-in browser. That makes this module the most fragile thing
in the system and the one most worth defending:

  - cookies injected, never a login form (we don't have the password, and
    shouldn't)
  - a real user agent and viewport; a default headless fingerprint is the
    fastest way to get served an interstitial
  - a screenshot on every exception, into data/screenshots/, because a failure
    at 11:00 on draft day has to be diagnosable after the fact
  - fails closed (§10.6): a page that doesn't look right raises, it does not
    return a best guess
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.config import Settings, settings

log = logging.getLogger(__name__)

ESPN_BASE = "https://fantasy.espn.com"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)


class BrowserError(RuntimeError):
    pass


class NotLoggedIn(BrowserError):
    """The page rendered, but as a logged-out visitor. §8.5's browser twin."""


class EspnSession:
    """A logged-in ESPN browser context.

    Use as a context manager:

        with EspnSession() as s:
            page = s.goto("/football/team?leagueId=123")
    """

    def __init__(self, cfg: Settings | None = None, *, headless: bool = True,
                 slow_mo: int = 0, use_saved_session: bool = True) -> None:
        self.cfg = cfg or settings()
        self.headless = headless
        self.slow_mo = slow_mo
        self.use_saved_session = use_saved_session
        self._pw = None
        self._browser = None
        self._ctx = None
        self.page = None

    @property
    def storage_state_path(self) -> Path:
        """Saved MyDisney web session.

        🔴 The API cookies are NOT enough for the web UI. Verified 2026-09-03:
        with valid SWID/espn_s2, fantasy.espn.com renders "Log in Required" in
        the page body. The web app wants a full MyDisney session, which lives
        across a set of Disney cookies plus localStorage that we cannot mint
        from the two API cookies.

        So a human logs in ONCE via scripts/login.py, and this file carries that
        session forward. It is gitignored and must never be committed.
        """
        return self.cfg.data_dir / "espn-session.json"

    def has_saved_session(self) -> bool:
        return self.storage_state_path.exists()

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> EspnSession:
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self.headless,
            slow_mo=self.slow_mo,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",  # the box has limited /dev/shm
            ],
        )
        ctx_args: dict[str, Any] = {
            "user_agent": UA,
            "viewport": {"width": 1600, "height": 1000},
            "locale": "en-US",
            "timezone_id": "America/Chicago",
        }
        if self.use_saved_session and self.has_saved_session():
            ctx_args["storage_state"] = str(self.storage_state_path)
            log.info("using saved ESPN web session from %s", self.storage_state_path.name)
        elif self.use_saved_session:
            log.warning(
                "no saved ESPN web session at %s — the web UI will likely show "
                "'Log in Required'. Run scripts/login.py once.",
                self.storage_state_path,
            )

        self._ctx = self._browser.new_context(**ctx_args)
        # API cookies go on regardless: they are what the XHRs the page makes
        # need, and they are harmless alongside a saved Disney session.
        self._ctx.add_cookies(self._cookies())
        # navigator.webdriver is the single most-checked automation tell.
        self._ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        self._ctx.set_default_timeout(20_000)
        self.page = self._ctx.new_page()
        return self

    def _cookies(self) -> list[dict[str, Any]]:
        common = {"path": "/", "httpOnly": False, "secure": True, "sameSite": "Lax"}
        out = []
        for domain in (".espn.com", ".fantasy.espn.com"):
            out.append({"name": "SWID", "value": self.cfg.swid, "domain": domain, **common})
            out.append({"name": "espn_s2", "value": self.cfg.espn_s2, "domain": domain, **common})
        return out

    def close(self) -> None:
        for closer in (self._ctx, self._browser):
            try:
                if closer:
                    closer.close()
            except Exception:
                pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._ctx = self._browser = self._pw = self.page = None

    def __enter__(self) -> EspnSession:
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is not None:
            self.screenshot(f"exception-{exc_type.__name__}")
        self.close()

    # ── navigation ───────────────────────────────────────────────────────────

    def goto(
        self,
        path_or_url: str,
        *,
        wait: str = "domcontentloaded",
        hydrate_for: str | None = None,
        hydrate_timeout: int = 25_000,
    ):
        """Navigate, wait for the SPA to hydrate, then prove we're logged in.

        ESPN Fantasy is a React app: `domcontentloaded` fires against an empty
        shell whose nav still says "Sign Up". Verified 2026-09-03 — a screenshot
        taken at that moment shows blank panels even though the session is
        authenticated. So every navigation waits for real content before any
        assertion or read is allowed to run.
        """
        url = path_or_url if path_or_url.startswith("http") else ESPN_BASE + path_or_url
        log.info("goto %s", url)
        self.page.goto(url, wait_until=wait)
        self.dismiss_overlays()
        self.wait_hydrated(selector=hydrate_for, timeout=hydrate_timeout)
        self.assert_logged_in()
        return self.page

    def dismiss_overlays(self, *, tries: int = 3) -> bool:
        """Clear the MyDisney login modal and any consent/ad interstitial.

        🔴 Verified 2026-09-03, and it is the single biggest headless gotcha in
        this system: ESPN renders a "MyDisney — enter your email to continue"
        modal over the fantasy app EVEN WHEN the SWID/espn_s2 cookies are valid
        and the app behind it is fully authenticated. It loads in an iframe from
        cdn.registerdisney.go.com and brings a reCAPTCHA frame with it.

        Escape closes it. Without this, every selector below the fold is
        unclickable and the draft loop would look mysteriously broken.
        """
        dismissed = False
        for _ in range(tries):
            if not self._overlay_present():
                break
            self.page.keyboard.press("Escape")
            self.page.wait_for_timeout(600)
            dismissed = True
        if dismissed:
            log.info("dismissed a MyDisney/consent overlay")
        return dismissed

    def _overlay_present(self) -> bool:
        try:
            if self.page.locator("#did-ui-view, [id*=did-ui], [class*=disneyid]").count():
                return True
            for f in self.page.frames:
                if "registerdisney" in (f.url or ""):
                    return True
        except Exception:
            pass
        return False

    def wait_hydrated(self, *, selector: str | None = None, timeout: int = 25_000) -> None:
        """Block until the app has actually rendered something."""
        if selector:
            self.page.wait_for_selector(selector, timeout=timeout, state="visible")
            return
        try:
            # networkidle is the most reliable generic signal for this SPA;
            # a body-length check backs it up when a poller keeps the socket warm.
            self.page.wait_for_load_state("networkidle", timeout=timeout)
        except Exception:
            pass
        try:
            self.page.wait_for_function(
                "() => document.body && document.body.innerText.length > 400",
                timeout=timeout,
            )
        except Exception:
            log.warning("page never reported hydrated content within %dms", timeout)

    #: Page text that proves we are logged OUT. Content, never nav.
    HARD_BLOCK = (
        "log in required",
        "login required",
        "you must be logged in",
        "enter your email to continue",
    )
    #: Page text that only ever renders for a signed-in manager.
    #: ⚠️ NOTHING from the nav belongs here. "Transaction Counter", "Waiver
    #: Order" and friends are League-menu links that render to logged-out
    #: visitors too — one of them was in this list on 2026-09-03 and made the
    #: check pass on a "Log in Required" page.
    SIGNED_IN = (
        "edit lineup",
        "move players",
        "opponent's roster",
        "proj pts",
    )

    #: Minimum time to keep looking before ACCEPTING a positive signal.
    #: "Log in Required" lands ~3s after load, well behind the nav. Concluding
    #: early reads the shell and calls a logged-out page healthy.
    MIN_DWELL_MS = 4_500

    def assert_logged_in(self, *, timeout_ms: int = 12_000) -> None:
        """Prove we're a logged-in user, not an anonymous visitor.

        🔴 Two traps, both verified 2026-09-03 against the live site:

        1. **The nav lies.** ESPN renders "My Team / League / Players" to a
           logged-OUT visitor and puts "Log in Required" in the body where the
           roster belongs. Checking the nav passes on a logged-out page.

        2. **The verdict arrives late.** "Log in Required" only appears ~3s in.
           Sampling the body once, right after load, reads an empty shell and
           concludes everything is fine.

        So this POLLS until the page says something decisive, and treats running
        out of time as failure rather than success (§10.6, fail closed).
        """
        url = (self.page.url or "").lower()
        if any(m in url for m in ("/login", "cdn.registerdisney", "signin")):
            self.screenshot("not-logged-in-url")
            raise NotLoggedIn(f"redirected to a login page: {self.page.url}")

        start = time.monotonic()
        deadline = start + timeout_ms / 1000.0
        min_dwell = self.MIN_DWELL_MS / 1000.0
        positive_seen = False

        while time.monotonic() < deadline:
            try:
                body = (self.page.inner_text("body", timeout=4000) or "").lower()
            except Exception:
                body = ""

            # A hard block always wins, whenever it shows up.
            for marker in self.HARD_BLOCK:
                if marker in body:
                    self.screenshot("login-required")
                    raise NotLoggedIn(
                        f"ESPN says {marker!r}. SWID/espn_s2 authenticate the API but "
                        "NOT the web app — the browser needs a saved MyDisney session. "
                        "Run scripts/login.py once (§8.5)."
                    )

            if any(m in body for m in self.SIGNED_IN):
                positive_seen = True
                # Only trust it once the page has had time to render a block.
                if time.monotonic() - start >= min_dwell:
                    return

            self.page.wait_for_timeout(400)

        if positive_seen:
            return

        # Neither signal inside the window. Do not assume success.
        self.screenshot("login-undetermined")
        raise NotLoggedIn(
            f"could not confirm a logged-in session on {self.page.url} within "
            f"{timeout_ms}ms — neither a login block nor signed-in content appeared. "
            "Refusing to proceed on an unverified session (§10.6)."
        )

    # ── diagnostics ──────────────────────────────────────────────────────────

    def screenshot(self, label: str) -> Path | None:
        if not self.page:
            return None
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        p = self.cfg.screenshot_dir / f"{stamp}-{label}.png"
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            self.page.screenshot(path=str(p), full_page=False)
            log.info("screenshot -> %s", p)
            return p
        except Exception as e:
            log.warning("screenshot failed: %s", e)
            return None

    def save_session(self) -> Path:
        """Persist the current web session so headless runs can reuse it."""
        p = self.storage_state_path
        p.parent.mkdir(parents=True, exist_ok=True)
        self._ctx.storage_state(path=str(p))
        log.info("saved ESPN web session -> %s", p)
        return p

    def dump_dom(self, label: str) -> Path | None:
        """Save the DOM for selector work. Gitignored — never commit ESPN HTML."""
        if not self.page:
            return None
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        p = self.cfg.screenshot_dir / f"dom-snapshot-{stamp}-{label}.html"
        try:
            p.write_text(self.page.content(), encoding="utf-8")
            return p
        except Exception as e:
            log.warning("dom dump failed: %s", e)
            return None


@contextmanager
def session(*, headless: bool = True, slow_mo: int = 0):
    s = EspnSession(headless=headless, slow_mo=slow_mo)
    try:
        yield s.start()
    finally:
        s.close()
