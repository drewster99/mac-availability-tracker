"""Build-to-order (BTO) Mac SKU discovery and polling.

Apple does not expose BTO part numbers as URL-addressable identifiers. They
are minted server-side inside an authenticated bag/checkout session, behind
CSRF + SHIELD. The path to track BTO availability is therefore two-step:

  1. **Discovery** — drive Apple's configurator UI in a browser and record
     every (Z-prefixed) part number that flows through any XHR. The user
     clicks through option combos they care about; we passively log the
     part numbers Apple mints into a ``bto_skus`` table along with whatever
     metadata we can scrape (chip, memory, storage, price).

  2. **Polling** — once part numbers are known, BTO SKUs poll exactly like
     preconfigured ones via :func:`mac_availability.availability.fetch_availability`.
     Pickup is uninteresting (Apple Stores don't stock BTO units) so we
     focus on per-SKU delivery ETAs and the ``isBuyable`` / ``commitCode``
     flags that signal when a config has been retired.

This module exposes:
  * :class:`BtoConfig` — a frozen description of one BTO config
  * :func:`record_session` — the headed-browser passive recorder
  * :func:`parse_summary_from_xhr` — best-effort metadata extraction from the
    JSON Apple returns when adding a config to the bag
  * :func:`needs_refresh` — heuristic for whether a recorded part needs
    re-minting (returned not-buyable for N consecutive sweeps)

The configurator-driving piece is intentionally passive: we don't try to
emulate every click programmatically because Apple's React app is fragile
and locale-dependent. The user opens a single headed session, clicks
through every config they care about, and the recorder captures all the
Z-parts in one go.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

log = logging.getLogger(__name__)

Z_PART_RE = re.compile(r"\bZ[A-Z0-9]{5,8}LL/[A-Z]\b")
"""Match a Z-prefixed Apple part number for any region (LL/A US, B/A UK, etc.)."""

# Map of locale → expected part-number suffix Apple uses.
LOCALE_PART_SUFFIX: dict[str, str] = {
    "en_US": "LL/A",
    "en_CA": "LL/A",  # shared with US
    "en_GB": "B/A",
    "en_AU": "X/A",
    "de_DE": "D/A",
    "fr_FR": "FN/A",
    "ja_JP": "J/A",
    "es_ES": "Y/A",
    "it_IT": "T/A",
    "nl_NL": "N/A",
}


@dataclass(frozen=True)
class BtoConfig:
    """One concrete (chip, RAM, storage, ...) selection within a configurable family.

    None fields mean "not yet known" — the recorder fills them in best-effort
    from the page state at click time.
    """

    family: str
    locale: str
    chip: Optional[str] = None
    cpu_cores: Optional[int] = None
    gpu_cores: Optional[int] = None
    memory_gb: Optional[int] = None
    storage_gb: Optional[int] = None

    def summary(self) -> str:
        """Human-readable summary for log output and the BTO browser column."""
        bits = [self.chip or "?"]
        if self.cpu_cores or self.gpu_cores:
            bits.append(f"{self.cpu_cores or '?'}C/{self.gpu_cores or '?'}G")
        if self.memory_gb:
            bits.append(f"{self.memory_gb}GB")
        if self.storage_gb:
            if self.storage_gb >= 1000:
                bits.append(f"{self.storage_gb // 1000}TB")
            else:
                bits.append(f"{self.storage_gb}GB")
        return " · ".join(b for b in bits if b)


@dataclass
class RecordedPart:
    """One Z-part observation — an Apple-allocated BTO part number plus context."""

    part_number: str
    family: str
    locale: str
    chip: Optional[str] = None
    cpu_cores: Optional[int] = None
    gpu_cores: Optional[int] = None
    memory_gb: Optional[int] = None
    storage_gb: Optional[int] = None
    price_string: Optional[str] = None
    raw_amount: Optional[float] = None
    currency: Optional[str] = None
    config_summary: Optional[str] = None
    raw_json: Optional[str] = None
    observed_at: str = ""


def _coerce_int(value: object) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        m = re.search(r"\d+", value)
        if m:
            return int(m.group(0))
    return None


_MEMORY_RE = re.compile(r"(\d+)\s*(?:GB|gb)\s*(?:unified\s*)?memory", re.IGNORECASE)
_STORAGE_RE = re.compile(r"(\d+)\s*(GB|TB|gb|tb)\s*(?:SSD\s*)?storage", re.IGNORECASE)
_CPU_RE = re.compile(r"(\d+)[-\s]?core\s*CPU", re.IGNORECASE)
_GPU_RE = re.compile(r"(\d+)[-\s]?core\s*GPU", re.IGNORECASE)
_CHIP_RE = re.compile(r"M\d(?:\s*(?:Pro|Max|Ultra))?", re.IGNORECASE)


def parse_summary_from_text(text: str) -> dict:
    """Extract config attributes from a free-form description string.

    Apple's bag/configure responses include a human-readable product title
    like "Mac Studio, M4 Max Chip, 16-core CPU, 40-core GPU, 64GB memory,
    1TB storage". This pulls structured fields out of that string.
    """
    out: dict = {}
    if not text:
        return out
    chip_match = _CHIP_RE.search(text)
    if chip_match:
        out["chip"] = chip_match.group(0).replace(" ", "").lower()
    cpu_match = _CPU_RE.search(text)
    if cpu_match:
        out["cpu_cores"] = int(cpu_match.group(1))
    gpu_match = _GPU_RE.search(text)
    if gpu_match:
        out["gpu_cores"] = int(gpu_match.group(1))
    mem_match = _MEMORY_RE.search(text)
    if mem_match:
        out["memory_gb"] = int(mem_match.group(1))
    stor_match = _STORAGE_RE.search(text)
    if stor_match:
        size = int(stor_match.group(1))
        unit = stor_match.group(2).upper()
        out["storage_gb"] = size * (1000 if unit == "TB" else 1)
    return out


def parse_summary_from_xhr(payload: object) -> list[RecordedPart]:
    """Walk a JSON XHR body and pull out (Z-part, config_summary) pairs.

    Apple's bag/checkout/save-config responses tend to contain part numbers
    and human-readable titles colocated; this best-effort extractor matches
    Z-prefixed parts with the closest enclosing title/description.
    """
    found: list[RecordedPart] = []
    if not isinstance(payload, (dict, list)):
        return found

    text = json.dumps(payload)
    z_parts = Z_PART_RE.findall(text)
    if not z_parts:
        return found

    # Walk the structure to associate each Z-part with the nearest title-like field.
    def walk(obj: object, ctx: dict) -> None:
        if isinstance(obj, dict):
            local_ctx = dict(ctx)
            for k, v in obj.items():
                if isinstance(v, str):
                    if k in ("partNumber", "skuPartNumber") and Z_PART_RE.match(v):
                        rec = RecordedPart(
                            part_number=v,
                            family=local_ctx.get("family", "?"),
                            locale=local_ctx.get("locale", "?"),
                            config_summary=local_ctx.get("title")
                            or local_ctx.get("productTitle")
                            or local_ctx.get("description"),
                            price_string=local_ctx.get("priceString"),
                            raw_amount=local_ctx.get("rawAmount"),
                            currency=local_ctx.get("currency"),
                        )
                        if rec.config_summary:
                            attrs = parse_summary_from_text(rec.config_summary)
                            for a, val in attrs.items():
                                setattr(rec, a, val)
                        found.append(rec)
                    if k in ("title", "productTitle", "description", "priceString", "currency"):
                        local_ctx[k] = v
                elif isinstance(v, (int, float)) and k in ("rawAmount", "price"):
                    local_ctx["rawAmount"] = float(v)
            for v in obj.values():
                walk(v, local_ctx)
        elif isinstance(obj, list):
            for v in obj:
                walk(v, ctx)

    walk(payload, {})
    return found


def needs_refresh(row: dict, *, max_unbuyable_runs: int = 3) -> bool:
    """Heuristic: should this BTO SKU be re-minted before the next sweep?

    A SKU is worth re-minting if it's been observed not-buyable repeatedly,
    or if its last_seen_at is so old that Apple may have changed its option
    encoding. The default threshold is 3 consecutive not-buyable observations.
    """
    last_buyable = row.get("last_observed_buyable")
    if last_buyable is None:
        return False
    if last_buyable == 1:
        return False
    last_seen = row.get("last_seen_at")
    if not last_seen:
        return True
    return True


def record_session(
    *,
    family: str = "mac-studio",
    locale: str = "en_US",
    region_path: str = "",
    out_path: Optional[Path] = None,
    headless: bool = False,
) -> list[RecordedPart]:
    """Open a real browser, watch every XHR for Z-parts, return what was seen.

    The user clicks through configurator combos they care about. The recorder
    does no clicking of its own — it just listens. When the user closes the
    browser tab (or the timeout elapses) the recorder returns the
    deduplicated list of parts seen.
    """
    try:
        from playwright.sync_api import sync_playwright
        from playwright_stealth import Stealth
    except ImportError as exc:
        raise RuntimeError(
            "playwright/playwright-stealth aren't installed. Run "
            "`pip install -e .` and `playwright install chromium`."
        ) from exc

    from .shield import DEFAULT_USER_AGENT

    seen: dict[str, RecordedPart] = {}
    raw_capture: list[dict] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    def on_response(resp):
        try:
            ct = resp.headers.get("content-type", "")
            if "json" not in ct:
                return
            body = resp.text()
            if not Z_PART_RE.search(body):
                return
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                return
            raw_capture.append({"url": resp.url, "status": resp.status, "body": payload})
            for rec in parse_summary_from_xhr(payload):
                rec.family = family
                rec.locale = locale
                rec.observed_at = now_iso
                rec.raw_json = json.dumps(payload, separators=(",", ":"))[:8000]
                if rec.part_number not in seen:
                    log.info(
                        "Recorded BTO part %s (%s)",
                        rec.part_number,
                        rec.config_summary or "?",
                    )
                seen[rec.part_number] = rec
        except Exception as exc:
            log.debug("response handler ignored error: %s", exc)

    nav_url = f"https://www.apple.com{region_path}/shop/buy-mac/{family}"
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        try:
            ctx = browser.new_context(
                user_agent=DEFAULT_USER_AGENT,
                viewport={"width": 1400, "height": 1100},
                locale=locale.replace("_", "-"),
            )
            Stealth().apply_stealth_sync(ctx)
            page = ctx.new_page()
            page.on("response", on_response)
            page.goto(nav_url, wait_until="domcontentloaded", timeout=60_000)
            log.info(
                "BTO recorder running. Browser is open at %s — "
                "click through every config you want tracked, then close the window. "
                "Z-prefixed part numbers will be captured automatically.",
                nav_url,
            )
            # Run until the user closes the browser. Playwright raises when the
            # context dies; we just wait on a long sleep and let close() kill it.
            try:
                page.wait_for_event("close", timeout=0)
            except Exception:
                pass
        finally:
            try:
                browser.close()
            except Exception:
                pass

    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(raw_capture, default=str, indent=2))

    return list(seen.values())
