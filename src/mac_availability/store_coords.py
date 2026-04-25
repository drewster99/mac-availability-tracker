"""Per-store latitude/longitude cache.

Apple's storelist payload doesn't include geolocation — it carries postal-level
addresses only. Each individual retail page (``apple.com[/<cc>]/retail/<slug>/``)
embeds the store's coordinates in a JSON-LD block. This module fetches and
caches those coordinates in a small JSON file checked into the repo, so the
common case is "load cache, skip the network entirely".

Use ``ensure_coords`` to fill in any cached gaps. Newly-added Apple Stores
won't be in the cache yet; ``ensure_coords`` fetches just those and writes
back to the cache file.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from pathlib import Path
from typing import Iterable, Optional

from .catalog import REGION_TO_PATH, LOCALE_TO_REGION
from .client import AppleShopClient

log = logging.getLogger(__name__)

DEFAULT_CACHE_PATH = Path("data/store_coords.json")
MANUAL_OVERRIDES_PATH = Path("data/store_coords_manual.json")
"""Hand-curated coordinates for stores whose individual retail pages 404
(currently Belgium + Switzerland — their per-country retail microsites have
been retired by Apple even though the stores remain operational)."""

_LAT_RE = re.compile(r'"latitude":\s*(-?\d+(?:\.\d+)?)')
_LON_RE = re.compile(r'"longitude":\s*(-?\d+(?:\.\d+)?)')


def parse_coords_from_page(html: str) -> Optional[tuple[float, float]]:
    """Pull the store's own latitude/longitude out of its retail page HTML.

    The store's geo-coordinates appear before the locale's
    ``defaultGeolocation`` in the embedded ``__NEXT_DATA__`` JSON, so we just
    take the first ``"latitude"``/``"longitude"`` pair.
    """
    lat_match = _LAT_RE.search(html)
    lon_match = _LON_RE.search(html)
    if not lat_match or not lon_match:
        return None
    try:
        return float(lat_match.group(1)), float(lon_match.group(1))
    except ValueError:
        return None


def _load_json_stores_block(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Failed to read coords file %s: %s", path, exc)
        return {}
    return data.get("stores", {}) if isinstance(data, dict) else {}


def load_cache(
    cache_path: Path = DEFAULT_CACHE_PATH,
    manual_path: Path = MANUAL_OVERRIDES_PATH,
) -> dict[str, dict]:
    """Load coords from the auto-fetched cache, layered with manual overrides.

    Manual entries take precedence so curated values aren't clobbered when the
    fetcher next runs — useful for stores Apple no longer publishes retail
    pages for.
    """
    merged = _load_json_stores_block(cache_path)
    for store_id, entry in _load_json_stores_block(manual_path).items():
        merged[store_id] = entry
    return merged


def save_cache(coords: dict[str, dict], cache_path: Path = DEFAULT_CACHE_PATH) -> None:
    """Write the coordinate cache to disk, sorted by store id for stable diffs."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "stores": {key: coords[key] for key in sorted(coords)},
    }
    cache_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def store_retail_url(locale: str, slug: str) -> Optional[str]:
    """Build the public retail URL for a store, or ``None`` if unsupported."""
    region = LOCALE_TO_REGION.get(locale)
    if region is None:
        return None
    region_path = REGION_TO_PATH.get(region, "")
    return f"https://www.apple.com{region_path}/retail/{slug}/"


async def fetch_one(
    client: AppleShopClient,
    *,
    store_id: str,
    locale: str,
    slug: str,
) -> Optional[tuple[float, float]]:
    """Fetch a single store's coordinates from its retail page."""
    url = store_retail_url(locale, slug)
    if url is None:
        log.warning("No region mapping for locale %s; cannot fetch coords for %s", locale, store_id)
        return None
    try:
        response = await client.get(url, region=LOCALE_TO_REGION[locale], accept="text/html")
    except Exception as exc:
        log.warning("Failed to fetch %s: %s", url, exc)
        return None
    return parse_coords_from_page(response.text)


def stores_missing_coords(
    rows: Iterable[sqlite3.Row], cached: dict[str, dict]
) -> list[sqlite3.Row]:
    """Return store rows that are eligible for fetch but absent from the cache."""
    out: list[sqlite3.Row] = []
    for row in rows:
        if row["id"] in cached:
            continue
        if not row["slug"]:
            continue
        if LOCALE_TO_REGION.get(row["locale"]) is None:
            continue
        out.append(row)
    return out


async def ensure_coords(
    client: AppleShopClient,
    store_rows: Iterable[sqlite3.Row],
    *,
    cache_path: Path = DEFAULT_CACHE_PATH,
    manual_path: Path = MANUAL_OVERRIDES_PATH,
) -> dict[str, dict]:
    """Populate the auto-fetch cache for every fetchable store missing from it.

    Manual overrides are honored as already-covered, so they're never re-fetched
    or written to the auto cache. Returns the merged view (auto + manual) for
    callers that want a single dict to apply to the SQLite ``stores`` table via
    :func:`db.update_store_coords`.
    """
    rows = list(store_rows)
    auto_cache = _load_json_stores_block(cache_path)
    manual = _load_json_stores_block(manual_path)
    merged = {**auto_cache, **manual}
    missing = stores_missing_coords(rows, merged)
    if not missing:
        log.info("Coordinate cache covers all %d eligible stores", len(rows))
        return merged
    log.info("Fetching coords for %d stores not yet cached", len(missing))
    for store in missing:
        coords = await fetch_one(
            client,
            store_id=store["id"],
            locale=store["locale"],
            slug=store["slug"],
        )
        if coords is None:
            log.warning("No coords parsed for %s (%s, slug=%s)", store["id"], store["locale"], store["slug"])
            continue
        auto_cache[store["id"]] = {
            "latitude": coords[0],
            "longitude": coords[1],
            "locale": store["locale"],
            "slug": store["slug"],
            "name": store["name"],
        }
    save_cache(auto_cache, cache_path)
    merged = {**auto_cache, **manual}
    log.info(
        "Coordinate cache now covers %d stores (%d auto + %d manual)",
        len(merged),
        len(auto_cache),
        len(manual),
    )
    return merged
