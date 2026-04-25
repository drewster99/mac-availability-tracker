"""Poll Apple's pickup-message endpoint for a watchlist of (region, ZIP, SKUs).

The watchlist is supplied as a JSON file. Sample::

    {
      "watchlist": [
        {
          "region": "US",
          "location": "94103",
          "parts": ["MGDR4LL/A", "MGDU4LL/A", "MDE14LL/A"],
          "canary": "MDE14LL/A"
        },
        {
          "region": "US",
          "location": "10003",
          "parts": ["MGDR4LL/A", "MGDU4LL/A"],
          "canary": "MDE14LL/A"
        }
      ]
    }

Run once for a single sweep, or use ``--loop SECONDS`` to poll continuously.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
from typing import Optional

from mac_availability import availability, db
from mac_availability.client import AppleShopClient


log = logging.getLogger("poll_availability")


async def _sweep_once(client: AppleShopClient, db_path: str | Path, watchlist: list[dict]) -> None:
    snapshots = []
    for entry in watchlist:
        snapshot = await availability.fetch_availability(
            entry["parts"],
            entry["location"],
            region=entry.get("region", "US"),
            store_id=entry.get("store"),
            canary_part_number=entry.get("canary"),
            client=client,
        )
        snapshots.append(snapshot)
        log.info(
            "%s/%s: %d stores, canary_ok=%s",
            snapshot.region,
            snapshot.location,
            len(snapshot.stores),
            snapshot.canary_ok,
        )
    with db.connect(db_path) as conn:
        for snapshot in snapshots:
            db.insert_snapshot(conn, snapshot)


async def _amain(argv: list[str]) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/mac.sqlite")
    parser.add_argument("--watchlist", default="data/watchlist.json", help="Path to watchlist JSON")
    parser.add_argument(
        "--loop",
        type=int,
        default=0,
        help="If > 0, repeat the sweep on this cadence (seconds). 0 = run once.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    config = json.loads(Path(args.watchlist).read_text())
    watchlist = config["watchlist"]
    log.info("Watchlist: %d entries", len(watchlist))

    async with AppleShopClient() as client:
        while True:
            await _sweep_once(client, args.db, watchlist)
            if args.loop <= 0:
                return
            log.info("Sleeping %d seconds before next sweep", args.loop)
            await asyncio.sleep(args.loop)


def main() -> None:
    import sys

    asyncio.run(_amain(sys.argv[1:]))


if __name__ == "__main__":
    main()
