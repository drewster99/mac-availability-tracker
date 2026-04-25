"""Fetch latitude/longitude for every Apple Store and update the on-disk cache.

Coordinates come from each store's individual retail page
(``apple.com[/<cc>]/retail/<slug>/``). Only stores not already in the cache
are fetched; the cache is committed to the repo so the common case skips the
network entirely. After the fetch, the live SQLite db is updated to match.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from mac_availability import db, store_coords
from mac_availability.client import AppleShopClient


async def _amain(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/mac.sqlite")
    parser.add_argument("--cache", default=str(store_coords.DEFAULT_CACHE_PATH))
    parser.add_argument(
        "--global-gap-seconds",
        type=float,
        default=5.0,
        help="Minimum seconds between any two requests",
    )
    parser.add_argument(
        "--rate-file",
        default="data/rate.json",
        help="Persisted learned global gap (shared with poll_anchors.py)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("fetch_store_coords")

    cache_path = Path(args.cache)

    with db.connect(args.db) as conn:
        store_rows = list(conn.execute("SELECT * FROM stores ORDER BY locale, id"))

    if not store_rows:
        log.warning("No stores in db; run refresh_catalog.py first")
        return

    async with AppleShopClient(
        global_min_gap_seconds=args.global_gap_seconds,
        rate_file=Path(args.rate_file) if args.rate_file else None,
    ) as client:
        cache = await store_coords.ensure_coords(client, store_rows, cache_path=cache_path)

    coord_map: dict[str, tuple[float, float]] = {}
    for store_id, entry in cache.items():
        lat = entry.get("latitude")
        lon = entry.get("longitude")
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            coord_map[store_id] = (float(lat), float(lon))

    with db.connect(args.db) as conn:
        n = db.update_store_coords(conn, coord_map)
        log.info("Updated %d store rows in %s with cached coordinates", n, args.db)

    have = sum(1 for r in store_rows if r["id"] in coord_map)
    total = len(store_rows)
    log.info("Coverage: %d / %d stores now have coordinates (%.1f%%)", have, total, 100.0 * have / total)


def main() -> None:
    import sys

    asyncio.run(_amain(sys.argv[1:]))


if __name__ == "__main__":
    main()
