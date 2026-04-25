"""Rebuild the local stores + SKUs catalog from apple.com.

Three passes: (1) refresh the global store list and apply cached coordinates,
(2) crawl every supported locale's family pages for preconfigured SKUs and
archive each family's bootstrap JSON for forensic re-parsing, (3) enrich the
canonical (en_US) catalog with memory/storage fields by fetching each per-config
slug, then propagate those specs to other locales by matching ``(family, price_key)``.
"""
from __future__ import annotations

import argparse
import asyncio
import json as _json
import logging
from datetime import datetime, timezone

from mac_availability import db, stores, catalog, store_coords
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
    parser.add_argument(
        "--skip-enrichment",
        action="store_true",
        help="Skip the per-slug enrichment pass (memory_gb / storage_gb)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log = logging.getLogger("refresh_catalog")

    canonical_locale = "en_US"
    canonical_region = "US"
    canonical_html: dict[str, str] = {}
    by_locale_html: dict[str, dict[str, str]] = {}

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
            configs = []
            for locale in locales:
                if locale in catalog.UNSUPPORTED_LOCALES:
                    continue
                region = catalog.LOCALE_TO_REGION.get(locale)
                if region is None:
                    continue
                currency = catalog.LOCALE_TO_CURRENCY.get(locale, "")
                html_sink: dict[str, str] = {}
                try:
                    locale_configs = await catalog.fetch_full_catalog(
                        client,
                        region=region,
                        locale=locale,
                        currency=currency,
                        family_html_sink=html_sink,
                    )
                except Exception as exc:
                    log.warning("Catalog crawl failed for %s/%s: %s", locale, region, exc)
                    continue
                log.info("Locale %s (region %s): %d preconfigured SKUs", locale, region, len(locale_configs))
                configs.extend(locale_configs)
                by_locale_html[locale] = html_sink
                if locale == canonical_locale:
                    canonical_html = html_sink
        else:
            html_sink = {}
            configs = await catalog.fetch_full_catalog(
                client,
                region=args.region,
                locale=args.locale,
                currency=args.currency,
                family_html_sink=html_sink,
            )
            by_locale_html[args.locale] = html_sink
            if args.locale == canonical_locale:
                canonical_html = html_sink
        log.info("Fetched %d preconfigured SKUs", len(configs))

        # Persist initial config rows + bootstrap JSON before enrichment so we
        # can re-parse later if the enrichment pass discovers new fields.
        observed_at = datetime.now(timezone.utc).isoformat()
        with db.connect(args.db) as conn:
            if store_records:
                n = db.upsert_stores(conn, store_records)
                log.info("Upserted %d store rows", n)
                cache = store_coords.load_cache()
                coord_map = {
                    sid: (entry["latitude"], entry["longitude"])
                    for sid, entry in cache.items()
                    if isinstance(entry.get("latitude"), (int, float))
                    and isinstance(entry.get("longitude"), (int, float))
                }
                if coord_map:
                    applied = db.update_store_coords(conn, coord_map)
                    log.info("Applied cached coordinates to %d store rows", applied)
            n = db.upsert_skus(conn, configs, observed_at=observed_at)
            log.info("Upserted %d SKU rows", n)

            for locale, family_html in by_locale_html.items():
                for family_slug, html in family_html.items():
                    bootstrap = catalog.extract_bootstrap_data(html)
                    if bootstrap is None:
                        continue
                    db.upsert_family_bootstrap(
                        conn,
                        family=family_slug,
                        locale=locale,
                        observed_at=observed_at,
                        raw_json=_json.dumps(bootstrap, separators=(",", ":")),
                    )
            log.info("Persisted family bootstrap JSON for %d (family, locale) pairs",
                     sum(len(v) for v in by_locale_html.values()))

        if args.skip_enrichment or not canonical_html:
            return

        canonical_targets = {
            cfg.part_number for cfg in configs
            if cfg.locale == canonical_locale and cfg.memory_gb is None
        }
        if not canonical_targets:
            log.info("No part numbers needing enrichment in %s", canonical_locale)
            return

        log.info(
            "Enriching %d %s part numbers with memory/storage from per-config pages",
            len(canonical_targets),
            canonical_locale,
        )
        enriched = await catalog.enrich_specs_from_slugs(
            client,
            region=canonical_region,
            locale=canonical_locale,
            family_html=canonical_html,
            target_part_numbers=canonical_targets,
        )

    # Apply specs back: first to the matched part numbers directly, then
    # propagate the (family, price_key) → specs map across all locales so
    # equivalent SKUs in other regions inherit memory/storage.
    if not enriched:
        log.warning("Enrichment pass produced no specs")
        return

    with db.connect(args.db) as conn:
        n_direct = db.update_sku_specs(conn, enriched)
        log.info("Direct enrichment updated %d SKU rows", n_direct)

        # Build (family, price_key) → (memory_gb, storage_gb) from canonical results.
        propagation: dict[tuple[str, str], dict] = {}
        for cfg in configs:
            if cfg.locale != canonical_locale:
                continue
            specs = enriched.get(cfg.part_number)
            if specs is None:
                continue
            propagation[(cfg.family, cfg.price_key)] = {
                "memory_gb": specs.get("memory_gb"),
                "storage_gb": specs.get("storage_gb"),
            }

        cross_locale_updates: dict[str, dict] = {}
        for cfg in configs:
            if cfg.locale == canonical_locale:
                continue
            key = (cfg.family, cfg.price_key)
            specs = propagation.get(key)
            if specs is None:
                continue
            cross_locale_updates[cfg.part_number] = specs

        n_propagated = db.update_sku_specs(conn, cross_locale_updates)
        log.info(
            "Propagated specs to %d SKU rows in non-canonical locales (matched on family + price_key)",
            n_propagated,
        )


def main() -> None:
    import sys

    asyncio.run(_amain(sys.argv[1:]))


if __name__ == "__main__":
    main()
