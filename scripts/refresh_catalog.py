"""Rebuild the local stores + SKUs catalog from apple.com."""
from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timezone

from mac_availability import db, stores, catalog
from mac_availability.client import AppleShopClient


async def _amain(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/mac.sqlite", help="SQLite path")
    parser.add_argument("--region", default="US", help="Region for catalog crawl when not using --all-locales")
    parser.add_argument("--locale", default="en_US")
    parser.add_argument("--currency", default="USD")
    parser.add_argument(
        "--all-locales",
        action="store_true",
        help="Crawl the catalog for every Apple Store locale derived from the stores table",
    )
    parser.add_argument(
        "--skip-stores",
        action="store_true",
        help="Skip refreshing the global store list",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("refresh_catalog")

    async with AppleShopClient() as client:
        if not args.skip_stores:
            store_records = await stores.fetch_all_stores(client)
            log.info("Fetched %d stores", len(store_records))
        else:
            store_records = []

        if args.all_locales:
            with db.connect(args.db) as conn:
                if store_records:
                    db.upsert_stores(conn, store_records)
                locales = sorted({row["locale"] for row in conn.execute("SELECT DISTINCT locale FROM stores")})
            log.info("Crawling catalogs across %d locales: %s", len(locales), locales)
            configs = await catalog.fetch_catalog_for_locales(locales, client=client)
        else:
            configs = await catalog.fetch_full_catalog(
                client,
                region=args.region,
                locale=args.locale,
                currency=args.currency,
            )
        log.info("Fetched %d preconfigured SKUs", len(configs))

    observed_at = datetime.now(timezone.utc).isoformat()
    with db.connect(args.db) as conn:
        if store_records:
            n = db.upsert_stores(conn, store_records)
            log.info("Upserted %d store rows", n)
        n = db.upsert_skus(conn, configs, observed_at=observed_at)
        log.info("Upserted %d SKU rows", n)


def main() -> None:
    import sys

    asyncio.run(_amain(sys.argv[1:]))


if __name__ == "__main__":
    main()
