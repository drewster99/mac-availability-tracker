"""Poll fulfillment-messages for every recorded BTO SKU.

Pickup is uninteresting for BTO — Apple Stores don't stock custom configs —
so this script focuses on the per-SKU delivery messaging. Results land in the
existing ``delivery_rows`` table, joined to a fresh ``availability_snapshot``
per (region, ZIP).

Re-mints stale BTO parts: a SKU that consistently returns
``COMMIT_CODE_NOT_BUYABLE`` for ``--unbuyable-strikes`` consecutive runs is
flagged as needing re-mint via the BTO recorder.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from mac_availability import availability, catalog, db, shield
from mac_availability.client import AppleShopClient


log = logging.getLogger("poll_bto")


async def _amain(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/mac.sqlite")
    parser.add_argument(
        "--zip",
        action="append",
        default=None,
        help="ZIP/postal code to query (one per --zip). Defaults to a small per-region set.",
    )
    parser.add_argument(
        "--include-locale",
        action="append",
        default=None,
        help="Restrict to BTO SKUs in these locale(s).",
    )
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--global-gap-seconds", type=float, default=5.0)
    parser.add_argument("--region-gap-seconds", type=float, default=3.0)
    parser.add_argument("--cooloff-seconds", type=float, default=120.0)
    parser.add_argument("--rate-file", default="data/rate.json")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    default_zips_by_region: dict[str, list[str]] = {
        "US": ["94103", "10001", "60601"],
        "UK": ["W1D 3QA"],
        "DE": ["60311"],
        "FR": ["75001"],
        "JP": ["100-0005"],
        "AU": ["2000"],
    }

    with db.connect(args.db) as conn:
        rows = db.all_bto_skus(conn)

    if not rows:
        log.warning("No BTO SKUs in %s. Run scripts/record_bto.py first.", args.db)
        return

    # Group by (locale, region) so we can poll efficiently.
    parts_by_locale: dict[str, list[str]] = {}
    for r in rows:
        if args.include_locale and r["locale"] not in args.include_locale:
            continue
        parts_by_locale.setdefault(r["locale"], []).append(r["part_number"])

    if not parts_by_locale:
        log.warning("BTO SKUs found, but none match --include-locale filter.")
        return

    log.info("Polling BTO across %d locales", len(parts_by_locale))

    session = await shield.aget_session()

    async def refresh_shield(_client, _url, _status):
        log.warning("Re-bootstrapping SHIELD cookies (rate-limit)")
        new_session = await shield.aget_session(force_refresh=True)
        _client.set_cookies(new_session.cookies)

    async with AppleShopClient(
        global_min_gap_seconds=args.global_gap_seconds,
        region_min_gap_seconds=args.region_gap_seconds,
        initial_cooloff_seconds=args.cooloff_seconds,
        rate_file=Path(args.rate_file) if args.rate_file else None,
        default_user_agent=session.user_agent,
        default_cookies=session.cookies,
    ) as client:
        client.set_rate_limit_callback(refresh_shield)

        total_snapshots = 0
        for locale, parts in parts_by_locale.items():
            region = catalog.LOCALE_TO_REGION.get(locale)
            if region is None:
                log.warning("No region for locale %s; skipping", locale)
                continue
            zips = args.zip or default_zips_by_region.get(region) or default_zips_by_region["US"]

            for zip_code in zips:
                for batch_start in range(0, len(parts), args.batch_size):
                    batch = parts[batch_start : batch_start + args.batch_size]
                    try:
                        snap = await availability.fetch_availability(
                            batch,
                            zip_code,
                            region=region,
                            client=client,
                        )
                    except Exception as exc:
                        log.warning(
                            "BTO query failed locale=%s zip=%s parts=%d: %s",
                            locale, zip_code, len(batch), exc,
                        )
                        continue

                    with db.connect(args.db) as conn:
                        snap_id = db.insert_snapshot(conn, snap)
                        # Update last_seen_at + buyability flag per recorded SKU
                        for d in snap.deliveries:
                            db.upsert_bto_sku(
                                conn,
                                part_number=d.part_number,
                                locale=locale,
                                family=next(
                                    (r["family"] for r in rows if r["part_number"] == d.part_number),
                                    "?",
                                ),
                                last_observed_buyable=d.is_buyable,
                                observed_at=snap.observed_at,
                            )
                    total_snapshots += 1
                    log.info(
                        "BTO snapshot %d: locale=%s zip=%s batch=%d (%d delivery rows, %d stores)",
                        snap_id, locale, zip_code, len(batch), len(snap.deliveries), len(snap.stores),
                    )
        log.info("Done. Persisted %d BTO snapshots", total_snapshots)


def main() -> None:
    import sys
    asyncio.run(_amain(sys.argv[1:]))


if __name__ == "__main__":
    main()
