"""Bootstrap Apple's SHIELD bot-detection cookies via Playwright + Stealth.

The ``/shop/fulfillment-messages?fae=true`` endpoint is gated by SHIELD:

  ``/shop/shld/v1/verify.js``  — obfuscated JS that fingerprints canvas / audio
                                  / fonts and probes for HeadlessChrome,
                                  PhantomJS, etc.
  ``/shop/shld/work/v1/q``     — proof-of-work challenge the browser solves.
  ``/shop/shld/work/v1/pat``   — fingerprint POST.

When SHIELD accepts the browser it sets cookies (``shld_bt_m``, ``sh_spksy``,
``shld_bt_ck``, plus the regular adobe analytics cookies). Only then does
fulfillment-messages return JSON. Plain headless Playwright fails the
verify.js check; ``playwright-stealth`` masks the headless markers.

Productive scrape pattern — bootstrap cookies once via Playwright (~15 s),
then poll cheaply with httpx. Refresh when a request 541s.

Cookies are cached on disk because they outlive a single session: as long
as Apple's SHIELD validation token is still valid, plain httpx can keep
calling fulfillment-messages with the cached cookies.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

DEFAULT_CACHE = Path("data/shield_cookies.json")
DEFAULT_TTL_SECONDS = 30 * 60
DEFAULT_NAV_URL = "https://www.apple.com/shop/buy-mac/macbook-pro"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
)


@dataclass
class ShieldSession:
    """Cookies + the User-Agent string they were minted under.

    The cookies must be replayed with the same User-Agent that was sent
    during bootstrap, otherwise SHIELD's edge-side checks reject them.
    """

    cookies: dict[str, str]
    user_agent: str
    minted_at: float

    def is_fresh(self, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> bool:
        return (time.time() - self.minted_at) < ttl_seconds

    def to_json(self) -> dict:
        return {
            "cookies": self.cookies,
            "user_agent": self.user_agent,
            "minted_at": self.minted_at,
        }

    @classmethod
    def from_json(cls, data: dict) -> "ShieldSession":
        return cls(
            cookies=dict(data.get("cookies") or {}),
            user_agent=data.get("user_agent") or DEFAULT_USER_AGENT,
            minted_at=float(data.get("minted_at") or 0.0),
        )


def _required_cookies_present(cookies: dict[str, str]) -> bool:
    """SHIELD's marker cookies — without all three we have no validation token."""
    return all(name in cookies for name in ("shld_bt_m", "sh_spksy", "shld_bt_ck"))


def load_cached(
    cache_path: Path = DEFAULT_CACHE,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
) -> Optional[ShieldSession]:
    """Return the cached SHIELD session if it exists and is still fresh.

    A "fresh" session is one whose mint timestamp is within the TTL. The TTL
    is a heuristic — Apple may invalidate sooner — so callers must still be
    prepared to re-bootstrap on a 541 even with a fresh-looking cache.
    """
    if not cache_path.exists():
        return None
    try:
        data = json.loads(cache_path.read_text())
    except Exception as exc:
        log.warning("Failed to read shield cookie cache %s: %s", cache_path, exc)
        return None
    sess = ShieldSession.from_json(data)
    if not _required_cookies_present(sess.cookies):
        return None
    if not sess.is_fresh(ttl_seconds):
        return None
    return sess


def save_cached(session: ShieldSession, cache_path: Path = DEFAULT_CACHE) -> None:
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(session.to_json(), indent=2))


def bootstrap(
    *,
    nav_url: str = DEFAULT_NAV_URL,
    user_agent: str = DEFAULT_USER_AGENT,
    settle_seconds: float = 6.0,
    cache_path: Optional[Path] = DEFAULT_CACHE,
) -> ShieldSession:
    """Run a real Chromium browser through SHIELD and harvest the cookies.

    Synchronous because Playwright's sync API is simpler and bootstrap is a
    one-shot operation per sweep. The async polling loop awaits this in a
    thread executor.

    Raises ``RuntimeError`` with actionable guidance if Playwright or its
    Chromium browser is not installed, so users know exactly what to fix.
    """
    try:
        from playwright.sync_api import sync_playwright
        from playwright_stealth import Stealth
    except ImportError as exc:
        raise RuntimeError(
            "playwright/playwright-stealth aren't installed. Run "
            "`pip install -e .` (these are now declared deps), "
            "then `playwright install chromium`."
        ) from exc

    log.info("Bootstrapping SHIELD cookies via %s", nav_url)
    stealth = Stealth()
    try:
        with sync_playwright() as pw:
            try:
                browser = pw.chromium.launch(headless=True)
            except Exception as exc:
                msg = str(exc)
                if "Executable doesn't exist" in msg or "playwright install" in msg:
                    raise RuntimeError(
                        "Chromium isn't installed for Playwright. Run: "
                        "`playwright install chromium`"
                    ) from exc
                raise RuntimeError(f"Failed to launch Chromium: {exc}") from exc
            try:
                ctx = browser.new_context(
                    user_agent=user_agent,
                    viewport={"width": 1280, "height": 1200},
                    locale="en-US",
                )
                stealth.apply_stealth_sync(ctx)
                page = ctx.new_page()
                page.goto(nav_url, wait_until="domcontentloaded", timeout=60_000)
                page.wait_for_timeout(int(settle_seconds * 1000))
                cookies = {c["name"]: c["value"] for c in ctx.cookies()}
            finally:
                browser.close()
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"SHIELD bootstrap aborted: {exc}") from exc

    if not _required_cookies_present(cookies):
        raise RuntimeError(
            f"SHIELD bootstrap completed but required cookies are missing — "
            f"Apple may have changed the gate. Cookies received: {sorted(cookies)}"
        )

    session = ShieldSession(
        cookies=cookies,
        user_agent=user_agent,
        minted_at=time.time(),
    )
    if cache_path is not None:
        save_cached(session, cache_path)
        log.info(
            "SHIELD bootstrap OK — %d cookies cached to %s",
            len(cookies),
            cache_path,
        )
    return session


def get_session(
    *,
    cache_path: Path = DEFAULT_CACHE,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    nav_url: str = DEFAULT_NAV_URL,
    user_agent: str = DEFAULT_USER_AGENT,
    force_refresh: bool = False,
) -> ShieldSession:
    """Return a usable SHIELD session, bootstrapping a fresh one if needed."""
    if not force_refresh:
        cached = load_cached(cache_path, ttl_seconds=ttl_seconds)
        if cached is not None:
            log.info(
                "Using cached SHIELD cookies (age=%.0fs, %d cookies)",
                time.time() - cached.minted_at,
                len(cached.cookies),
            )
            return cached
    return bootstrap(
        nav_url=nav_url,
        user_agent=user_agent,
        cache_path=cache_path,
    )


async def aget_session(
    *,
    cache_path: Path = DEFAULT_CACHE,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    nav_url: str = DEFAULT_NAV_URL,
    user_agent: str = DEFAULT_USER_AGENT,
    force_refresh: bool = False,
) -> ShieldSession:
    """Async wrapper. Playwright's sync API can't run inside an asyncio loop,
    so the bootstrap is offloaded to a worker thread."""
    import asyncio

    return await asyncio.to_thread(
        get_session,
        cache_path=cache_path,
        ttl_seconds=ttl_seconds,
        nav_url=nav_url,
        user_agent=user_agent,
        force_refresh=force_refresh,
    )
