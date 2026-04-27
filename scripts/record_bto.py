"""Open a real Apple buy-mac page and record every BTO (Z-prefixed) part
number Apple mints during your session.

Usage::

    .venv/bin/python scripts/record_bto.py --family mac-studio --locale en_US
    # browser opens; click through every config you care about
    # close the window when done
    # discovered parts are written to data/mac.sqlite (bto_skus table)

The recorder only listens — it does not drive any clicks. That keeps it
robust against Apple's React app changes; you stay in control of the
configurator and we log every Z-part we see.
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from mac_availability import bto, catalog, db


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--family", default="mac-studio")
    p.add_argument("--locale", default="en_US")
    p.add_argument("--db", default="data/mac.sqlite")
    p.add_argument(
        "--raw-out",
        default=None,
        help="If set, write the raw JSON XHRs containing Z-parts to this path for debugging.",
    )
    p.add_argument("--headless", action="store_true", help="Headless run (skips human interaction; mainly for tests)")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    region = catalog.LOCALE_TO_REGION.get(args.locale)
    region_path = catalog.REGION_TO_PATH.get(region, "")

    parts = bto.record_session(
        family=args.family,
        locale=args.locale,
        region_path=region_path,
        out_path=Path(args.raw_out) if args.raw_out else None,
        headless=args.headless,
    )

    if not parts:
        print("No Z-prefixed part numbers were captured.", file=sys.stderr)
        print("Tip: try a non-headless session and click through actual configs.", file=sys.stderr)
        return 1

    observed_at = datetime.now(timezone.utc).isoformat()
    with db.connect(args.db) as conn:
        for rec in parts:
            db.upsert_bto_sku(
                conn,
                part_number=rec.part_number,
                locale=rec.locale,
                family=rec.family,
                chip=rec.chip,
                cpu_cores=rec.cpu_cores,
                gpu_cores=rec.gpu_cores,
                memory_gb=rec.memory_gb,
                storage_gb=rec.storage_gb,
                price_string=rec.price_string,
                raw_amount=rec.raw_amount,
                currency=rec.currency,
                config_summary=rec.config_summary,
                raw_json=rec.raw_json,
                observed_at=observed_at,
            )

    print(f"\nRecorded {len(parts)} BTO part number(s) into {args.db}:")
    for rec in parts:
        summary = rec.config_summary or "(no summary)"
        if rec.price_string:
            summary = f"{summary} — {rec.price_string}"
        print(f"  {rec.part_number}  {summary}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
