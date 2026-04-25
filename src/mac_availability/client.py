"""Polite HTTP client for Apple's public shop endpoints.

Per-region rate limiting prevents bursts to any one regional shop; a global
limiter layers on top because Apple appears to throttle on total IP volume.
Spoofing a browser User-Agent has not proven necessary.

When a rate-limit response does come back, the client:
 - honors ``Retry-After`` if present (this is what HTTP 429 and some 541s use);
 - otherwise pauses all coroutines for an exponentially growing cool-off;
 - permanently widens the global gap so the rest of the session — and the next
   session, via a persisted rate file — runs with more headroom.

The goal is to never see 541 in routine operation. The first 541 of a session
is treated as a signal that the configured rate was too aggressive, not as a
transient error to be simply retried.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

log = logging.getLogger(__name__)


APPLE_SOFT_RATE_LIMIT = 541
"""Apple's non-standard status code for soft rate limiting."""
STANDARD_RATE_LIMIT = 429
"""HTTP's standard "too many requests" code. Always carries Retry-After if used correctly."""


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Parse an HTTP ``Retry-After`` header value into seconds-from-now.

    RFC 7231 allows two forms: a delta in seconds, or an HTTP-date. Returns
    ``None`` when the header is absent or unparseable.
    """
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())


@dataclass
class RegionLimiter:
    """Serialize requests within a region and enforce a minimum gap between them."""

    min_gap_seconds: float
    jitter_fraction: float = 0.2
    _last_started: float = 0.0
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def acquire(self) -> None:
        async with self._lock:
            now = asyncio.get_event_loop().time()
            jitter = random.uniform(-self.jitter_fraction, self.jitter_fraction)
            target_gap = self.min_gap_seconds * (1.0 + jitter)
            wait = max(0.0, self._last_started + target_gap - now)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_started = asyncio.get_event_loop().time()


class AppleShopClient:
    """Thin wrapper around httpx with global + per-region rate limiting and retry on 5xx/541.

    Two limiters are stacked: a single ``global_limiter`` enforcing the overall
    request cadence regardless of region, and a per-region limiter that
    serializes within a region. Apple appears to throttle on total volume from
    a single source IP, so the global limiter is what actually keeps us under
    the radar; the per-region limiter is what makes within-region order
    deterministic.

    A circuit-breaker on top of the limiters reacts to ``541`` (Apple's soft
    rate-limit code): when one is observed, every concurrent caller pauses
    until the configured cool-off has elapsed. Without this, in-flight
    coroutines keep pumping requests at Apple while one is already being
    backed off, which prolongs the block.
    """

    def __init__(
        self,
        *,
        global_min_gap_seconds: float = 2.0,
        region_min_gap_seconds: float = 1.0,
        jitter_fraction: float = 0.2,
        max_retries: int = 5,
        timeout_seconds: float = 30.0,
        initial_cooloff_seconds: float = 60.0,
        max_cooloff_seconds: float = 900.0,
        gap_growth_factor: float = 1.5,
        max_global_gap_seconds: float = 30.0,
        rate_file: Optional[Path] = None,
    ) -> None:
        self._region_min_gap_seconds = region_min_gap_seconds
        self._jitter_fraction = jitter_fraction
        self._max_retries = max_retries
        self._initial_cooloff_seconds = initial_cooloff_seconds
        self._max_cooloff_seconds = max_cooloff_seconds
        self._gap_growth_factor = gap_growth_factor
        self._max_global_gap_seconds = max_global_gap_seconds
        self._rate_file = rate_file
        self._cooloff_seconds_next = initial_cooloff_seconds
        if rate_file is not None and rate_file.exists():
            try:
                persisted = json.loads(rate_file.read_text())
                saved_gap = float(persisted.get("global_min_gap_seconds") or 0)
                if saved_gap > global_min_gap_seconds:
                    log.info(
                        "Loaded persisted global gap %.1fs from %s (was %.1fs in args)",
                        saved_gap,
                        rate_file,
                        global_min_gap_seconds,
                    )
                    global_min_gap_seconds = saved_gap
            except Exception as exc:
                log.warning("Failed to read rate file %s: %s", rate_file, exc)
        self._global_limiter = RegionLimiter(
            min_gap_seconds=global_min_gap_seconds,
            jitter_fraction=jitter_fraction,
        )
        self._limiters: dict[str, RegionLimiter] = {}
        self._limiters_lock = asyncio.Lock()
        self._cooloff_until: float = 0.0
        self._cooloff_lock = asyncio.Lock()
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            follow_redirects=True,
            http2=False,
        )

    async def __aenter__(self) -> "AppleShopClient":
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self._client.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _wait_for_cooloff(self) -> None:
        while True:
            async with self._cooloff_lock:
                now = asyncio.get_event_loop().time()
                wait = self._cooloff_until - now
            if wait <= 0:
                return
            log.info("Cool-off active; sleeping %.0fs before next attempt", wait)
            await asyncio.sleep(wait)

    async def _trigger_cooloff(self, hint_seconds: Optional[float]) -> None:
        """Extend the global cool-off and permanently widen the global gap.

        If the server supplied a ``Retry-After`` hint, we honor it directly;
        otherwise we grow our own cool-off exponentially each time it's
        triggered this session. Either way we also bump the baseline gap so
        subsequent requests — and the next session, via the persisted rate
        file — run more gently.
        """
        async with self._cooloff_lock:
            now = asyncio.get_event_loop().time()
            already_cooling = self._cooloff_until > now

            if hint_seconds is not None and hint_seconds > 0:
                cooloff = min(max(hint_seconds, 5.0), self._max_cooloff_seconds)
                reason = f"Retry-After={hint_seconds:.0f}s"
            else:
                cooloff = self._cooloff_seconds_next
                reason = "no hint; exponential"

            target = now + cooloff
            if target > self._cooloff_until:
                self._cooloff_until = target
                log.warning("Cool-off set +%.0fs (%s)", cooloff, reason)

            if already_cooling:
                return

            if hint_seconds is None or hint_seconds <= 0:
                self._cooloff_seconds_next = min(
                    self._cooloff_seconds_next * 2, self._max_cooloff_seconds
                )

            old_gap = self._global_limiter.min_gap_seconds
            new_gap = min(old_gap * self._gap_growth_factor, self._max_global_gap_seconds)
            if new_gap > old_gap:
                self._global_limiter.min_gap_seconds = new_gap
                log.warning(
                    "Bumping global gap %.2fs -> %.2fs (learned from rate-limit)",
                    old_gap,
                    new_gap,
                )
                if self._rate_file is not None:
                    try:
                        self._rate_file.parent.mkdir(parents=True, exist_ok=True)
                        self._rate_file.write_text(
                            json.dumps(
                                {
                                    "global_min_gap_seconds": new_gap,
                                    "updated_at": datetime.now(timezone.utc).isoformat(),
                                },
                                indent=2,
                            )
                        )
                    except Exception as exc:
                        log.warning("Failed to persist rate file %s: %s", self._rate_file, exc)

    async def _limiter_for(self, region: str) -> RegionLimiter:
        async with self._limiters_lock:
            limiter = self._limiters.get(region)
            if limiter is None:
                limiter = RegionLimiter(
                    min_gap_seconds=self._region_min_gap_seconds,
                    jitter_fraction=self._jitter_fraction,
                )
                self._limiters[region] = limiter
            return limiter

    async def get(
        self,
        url: str,
        *,
        region: str = "global",
        accept: str = "text/html",
        referer: Optional[str] = None,
    ) -> httpx.Response:
        """Fetch ``url`` with politeness controls, retrying transient failures.

        ``region`` is an opaque key used to namespace the rate limiter — typically
        a country code or "global". ``accept`` is sent as the Accept header (use
        ``application/json`` for JSON endpoints). ``referer`` is sent as Referer
        when supplied; it can reduce friction on the JSON endpoints.
        """
        limiter = await self._limiter_for(region)
        headers: dict[str, str] = {"Accept": accept}
        if referer is not None:
            headers["Referer"] = referer

        attempt = 0
        backoff_seconds = 2.0
        while True:
            await self._wait_for_cooloff()
            await limiter.acquire()
            await self._global_limiter.acquire()
            try:
                response = await self._client.get(url, headers=headers)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt >= self._max_retries:
                    raise
                log.warning(
                    "Transport error for %s (attempt %d/%d): %s",
                    url,
                    attempt + 1,
                    self._max_retries,
                    exc,
                )
                await asyncio.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, 60.0)
                attempt += 1
                continue

            is_rate_limit = response.status_code in (
                APPLE_SOFT_RATE_LIMIT,
                STANDARD_RATE_LIMIT,
            )
            if (
                response.status_code >= 500 or is_rate_limit
            ) and attempt < self._max_retries:
                retry_after_hint: Optional[float] = None
                if is_rate_limit:
                    retry_after_hint = _parse_retry_after(response.headers.get("Retry-After"))
                    await self._trigger_cooloff(retry_after_hint)
                log.warning(
                    "HTTP %d for %s (attempt %d/%d), backing off %.1fs",
                    response.status_code,
                    url,
                    attempt + 1,
                    self._max_retries,
                    backoff_seconds,
                )
                await asyncio.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, 120.0)
                attempt += 1
                continue

            response.raise_for_status()
            return response
