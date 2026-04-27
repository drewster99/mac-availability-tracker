"""Poll Apple's pickup-message endpoint once per (locale, city) anchor.

Each anchor query returns ~12 nearby stores, so one anchor per city gives
broad coverage in far fewer requests than querying every store individually.
Designed to stay well under Apple's WAF rate thresholds: 5s global minimum
gap, 120s cool-off when a 541 response comes back, emphatically polite.

After the anchor sweep, a "straggler" pass directly queries any eligible
store that still hasn't appeared in any response — typically a few stores
in regions where the city-anchor heuristic missed (a store that's the only
one in its city and that no other anchor's nearby-search reached). Iterates
this pass until no new stores show up.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path
from typing import Iterable, Optional

from mac_availability import anchors, availability, catalog, db, shield
from mac_availability.client import AppleShopClient


log = logging.getLogger("poll_anchors")

DEFAULT_BATCH_SIZE = 16
"""Apple's pickup-message endpoint silently truncates the parts list at ~20 SKUs
per request, dropping the alphabetically-later ones. 16 leaves headroom."""


def _chunk(seq: list, size: int) -> Iterable[list]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


async def _query_one_anchor(
    client: AppleShopClient,
    db_path: str,
    *,
    anchor_store_id: str,
    anchor_city: str,
    anchor_locale: str,
    region: str,
    location: str,
    parts: list[str],
    canary: Optional[str],
    batch_size: int,
) -> int:
    persisted = 0
    for batch in _chunk(parts, batch_size):
        try:
            snapshot = await availability.fetch_availability(
                batch,
                location,
                region=region,
                canary_part_number=canary,
                client=client,
            )
        except Exception as exc:
            log.warning(
                "Query failed anchor=%s (%s/%s, %d parts): %s",
                anchor_store_id,
                anchor_locale,
                location,
                len(batch),
                exc,
            )
            continue
        with db.connect(db_path) as conn:
            db.insert_snapshot(conn, snapshot)
        persisted += 1
        log.info(
            "anchor %s (%s, %s) %s: +1 snapshot (%d stores, canary_ok=%s)",
            anchor_store_id,
            anchor_city,
            anchor_locale,
            location,
            len(snapshot.stores),
            snapshot.canary_ok,
        )
    return persisted


async def _amain(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/mac.sqlite")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument(
        "--include-locale",
        action="append",
        default=None,
        help="Only poll these locale(s); pass repeatedly for multiple",
    )
    parser.add_argument(
        "--global-gap-seconds",
        type=float,
        default=5.0,
        help="Minimum seconds between any two requests (across all regions)",
    )
    parser.add_argument(
        "--region-gap-seconds",
        type=float,
        default=3.0,
        help="Minimum seconds between two requests hitting the same region",
    )
    parser.add_argument(
        "--cooloff-seconds",
        type=float,
        default=120.0,
        help="Initial pause when a 541 or 429 arrives without a Retry-After hint (doubles each time)",
    )
    parser.add_argument(
        "--rate-file",
        default="data/rate.json",
        help="Where to persist the learned global gap across runs",
    )
    parser.add_argument(
        "--limit-anchors",
        type=int,
        default=0,
        help="If > 0, stop after polling this many anchors (smoke tests)",
    )
    parser.add_argument(
        "--no-stragglers",
        action="store_true",
        help="Skip the second-pass direct-query of stores not reached by any anchor",
    )
    parser.add_argument(
        "--max-straggler-passes",
        type=int,
        default=3,
        help="Cap on follow-up straggler passes (each pass directly queries every still-uncovered store)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    exclude = set(catalog.UNSUPPORTED_LOCALES)
    include = set(args.include_locale) if args.include_locale else None

    with db.connect(args.db) as conn:
        anchor_rows = anchors.pick_anchors(conn, include_locales=include, exclude_locales=exclude)
        sku_rows = list(conn.execute("SELECT * FROM skus"))
        store_rows = list(conn.execute("SELECT * FROM stores ORDER BY locale, id"))

    if not sku_rows:
        raise RuntimeError(
            "skus table is empty; run refresh_catalog.py --all-locales first"
        )

    parts_by_region: dict[str, list[str]] = {}
    for row in sku_rows:
        region = catalog.LOCALE_TO_REGION.get(row["locale"])
        if region is None:
            continue
        parts_by_region.setdefault(region, []).append(row["part_number"])

    # Dedupe part numbers within each region (same SKU may be tagged under multiple
    # locales of the same region in rare cases).
    for region, parts in parts_by_region.items():
        parts_by_region[region] = sorted(set(parts))

    log.info(
        "Selected %d anchors across %d locales",
        len(anchor_rows),
        len({a["locale"] for a in anchor_rows}),
    )

    if args.limit_anchors > 0:
        anchor_rows = anchor_rows[: args.limit_anchors]
        log.info("Limiting to first %d anchors for this run", len(anchor_rows))

    total_persisted = 0
    skipped_covered = 0
    observed_store_ids: set[str] = set()

    session = await shield.aget_session()

    async def refresh_shield(_client: AppleShopClient, _url: str, _status: int) -> None:
        log.warning("Re-bootstrapping SHIELD cookies after rate-limit response")
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
        for anchor in anchor_rows:
            locale = anchor["locale"]
            anchor_id = anchor["id"]
            if anchor_id in observed_store_ids:
                skipped_covered += 1
                continue
            region = catalog.LOCALE_TO_REGION.get(locale)
            if region is None:
                log.warning("No region for locale %s; skipping anchor %s", locale, anchor_id)
                continue
            parts = parts_by_region.get(region)
            if not parts:
                log.warning("No SKUs for region %s (locale %s); skipping", region, locale)
                continue
            location = anchor["postal_code"] or anchor["city"]
            if not location:
                continue
            canary = parts[0]
            persisted = await _query_one_anchor(
                client,
                args.db,
                anchor_store_id=anchor_id,
                anchor_city=anchor["city"] or "?",
                anchor_locale=locale,
                region=region,
                location=location,
                parts=parts,
                canary=canary,
                batch_size=args.batch_size,
            )
            total_persisted += persisted
            if persisted > 0:
                observed_store_ids.update(_observed_stores_in_latest(args.db, persisted))

    log.info(
        "Anchor pass done. Snapshots persisted: %d, anchors skipped (already covered): %d, unique stores observed: %d",
        total_persisted,
        skipped_covered,
        len(observed_store_ids),
    )

    if args.no_stragglers:
        return

    eligible_store_ids = {
        row["id"]
        for row in store_rows
        if row["locale"] not in catalog.UNSUPPORTED_LOCALES
        and (row["postal_code"] or row["city"])
        and catalog.LOCALE_TO_REGION.get(row["locale"]) is not None
        and parts_by_region.get(catalog.LOCALE_TO_REGION.get(row["locale"], ""))
    }
    store_by_id = {row["id"]: row for row in store_rows}

    async with AppleShopClient(
        global_min_gap_seconds=args.global_gap_seconds,
        region_min_gap_seconds=args.region_gap_seconds,
        initial_cooloff_seconds=args.cooloff_seconds,
        rate_file=Path(args.rate_file) if args.rate_file else None,
        default_user_agent=session.user_agent,
        default_cookies=session.cookies,
    ) as client:
        client.set_rate_limit_callback(refresh_shield)
        for pass_index in range(1, args.max_straggler_passes + 1):
            uncovered = sorted(eligible_store_ids - observed_store_ids)
            if not uncovered:
                log.info("All eligible stores covered — no straggler pass needed")
                break
            log.info(
                "Straggler pass %d/%d: directly querying %d still-uncovered stores",
                pass_index,
                args.max_straggler_passes,
                len(uncovered),
            )
            pass_persisted = 0
            pass_new_observed = 0
            for store_id in uncovered:
                store = store_by_id[store_id]
                locale = store["locale"]
                region = catalog.LOCALE_TO_REGION[locale]
                parts = parts_by_region[region]
                location = store["postal_code"] or store["city"]
                canary = parts[0]
                persisted = await _query_one_anchor(
                    client,
                    args.db,
                    anchor_store_id=store_id,
                    anchor_city=store["city"] or "?",
                    anchor_locale=locale,
                    region=region,
                    location=location,
                    parts=parts,
                    canary=canary,
                    batch_size=args.batch_size,
                )
                if persisted > 0:
                    new_obs = _observed_stores_in_latest(args.db, persisted)
                    pass_new_observed += len(new_obs - observed_store_ids)
                    observed_store_ids.update(new_obs)
                pass_persisted += persisted
                total_persisted += persisted
            log.info(
                "Straggler pass %d: %d snapshots, %d newly observed stores",
                pass_index,
                pass_persisted,
                pass_new_observed,
            )
            if pass_new_observed == 0:
                log.info(
                    "Straggler pass %d found no new stores; %d stores remain unreachable: %s",
                    pass_index,
                    len(uncovered),
                    sorted(eligible_store_ids - observed_store_ids),
                )
                break

    final_uncovered = sorted(eligible_store_ids - observed_store_ids)
    log.info(
        "Final coverage: %d / %d eligible stores observed; total snapshots persisted: %d",
        len(observed_store_ids & eligible_store_ids),
        len(eligible_store_ids),
        total_persisted,
    )
    if final_uncovered:
        log.warning(
            "%d eligible stores remained unreachable: %s",
            len(final_uncovered),
            final_uncovered,
        )


def _observed_stores_in_latest(db_path: str, n_snapshots: int) -> set[str]:
    """Return the set of store IDs touched by the most recent ``n_snapshots``."""
    with db.connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT store_id FROM availability_rows
            WHERE snapshot_id IN (
                SELECT id FROM availability_snapshots
                ORDER BY id DESC
                LIMIT ?
            )
            """,
            (n_snapshots,),
        )
        return {row[0] for row in rows}


def main() -> None:
    import sys

    asyncio.run(_amain(sys.argv[1:]))


if __name__ == "__main__":
    main()
