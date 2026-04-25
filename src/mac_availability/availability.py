"""Poll Apple's ``pickup-message`` endpoint for in-store pickup availability.

A single call returns pickup availability across roughly a dozen Apple Stores
near the supplied ZIP/postal code. The richer ``fulfillment-messages`` endpoint
exists in the page source but is gated behind frontend session state; the older
``pickup-message`` endpoint serves clean JSON to anonymous callers.

``canary_ok`` on the returned snapshot is a structural-sanity bit: true iff the
response contained stores and at least one store reported parts availability.
False means Apple returned an empty or shadow-banned response — treat the
snapshot as suspect rather than real data.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import Optional

from pydantic import BaseModel, Field

from .client import AppleShopClient
from .catalog import REGION_TO_PATH

log = logging.getLogger(__name__)


class PartAvailability(BaseModel):
    """Pickup availability for a single SKU at a single store."""

    part_number: str
    pickup_display: Optional[str] = None
    pickup_quote: Optional[str] = None
    store_pick_eligible: Optional[bool] = None


class StoreAvailability(BaseModel):
    """All SKU pickup statuses returned for a single store in one poll."""

    store_id: str
    store_name: str
    city: Optional[str] = None
    state: Optional[str] = None
    distance_with_unit: Optional[str] = None
    parts: list[PartAvailability] = Field(default_factory=list)


class AvailabilitySnapshot(BaseModel):
    """One ``pickup-message`` response, normalized."""

    observed_at: str
    location: str
    region: str
    parts_queried: list[str]
    canary_part_number: Optional[str] = None
    canary_ok: bool = True
    stores: list[StoreAvailability] = Field(default_factory=list)
    raw: dict = Field(default_factory=dict)


def _build_url(
    *,
    parts: list[str],
    location: str,
    region: str,
    store_id: Optional[str] = None,
) -> str:
    from urllib.parse import quote

    region_path = REGION_TO_PATH.get(region, "")
    base = f"https://www.apple.com{region_path}/shop/retail/pickup-message"
    params: list[str] = ["pl=true"]
    for index, part in enumerate(parts):
        params.append(f"parts.{index}={quote(part, safe='')}")
    params.append(f"location={quote(location, safe='')}")
    if store_id is not None:
        params.append(f"store={store_id}")
    return base + "?" + "&".join(params)


def parse_fulfillment_response(
    payload: dict,
    *,
    location: str,
    region: str,
    parts_queried: list[str],
    canary_part_number: Optional[str],
    observed_at: str,
) -> AvailabilitySnapshot:
    """Normalize the JSON returned by pickup-message into our schema."""
    body = payload.get("body") or {}
    raw_stores = body.get("stores") or []
    if not raw_stores:
        content = body.get("content") or {}
        pickup_message = content.get("pickupMessage") or {}
        raw_stores = pickup_message.get("stores") or []

    stores: list[StoreAvailability] = []
    any_parts_returned = False
    for store_record in raw_stores:
        parts_avail = store_record.get("partsAvailability") or {}
        if parts_avail:
            any_parts_returned = True
        normalized_parts: list[PartAvailability] = []
        for sku, part_data in parts_avail.items():
            pickup = part_data.get("pickupDisplay")
            quote = part_data.get("pickupSearchQuote") or part_data.get("storePickupQuote")
            eligible_raw = part_data.get("storePickEligible")
            normalized_parts.append(
                PartAvailability(
                    part_number=sku,
                    pickup_display=pickup,
                    pickup_quote=quote,
                    store_pick_eligible=bool(eligible_raw) if eligible_raw is not None else None,
                )
            )
        stores.append(
            StoreAvailability(
                store_id=store_record.get("storeNumber") or store_record.get("storeId") or "?",
                store_name=store_record.get("storeName") or "",
                city=store_record.get("city"),
                state=store_record.get("state"),
                distance_with_unit=store_record.get("storeDistanceWithUnit") or store_record.get("storedistance"),
                parts=normalized_parts,
            )
        )

    canary_ok = bool(stores) and any_parts_returned

    return AvailabilitySnapshot(
        observed_at=observed_at,
        location=location,
        region=region,
        parts_queried=parts_queried,
        canary_part_number=canary_part_number,
        canary_ok=canary_ok,
        stores=stores,
        raw=payload,
    )


async def fetch_availability(
    parts: list[str],
    location: str,
    *,
    region: str = "US",
    store_id: Optional[str] = None,
    canary_part_number: Optional[str] = None,
    client: Optional[AppleShopClient] = None,
) -> AvailabilitySnapshot:
    """Single pickup-message call covering pickup availability for the given SKUs."""
    from datetime import datetime, timezone

    owns_client = client is None
    if client is None:
        client = AppleShopClient()
    try:
        all_parts = list(parts)
        if canary_part_number and canary_part_number not in all_parts:
            all_parts.append(canary_part_number)
        url = _build_url(
            parts=all_parts,
            location=location,
            region=region,
            store_id=store_id,
        )
        response = await client.get(
            url,
            region=region,
            accept="application/json",
            referer=f"https://www.apple.com{REGION_TO_PATH.get(region, '')}/shop/buy-mac",
        )
        payload = response.json()
        observed_at = datetime.now(timezone.utc).isoformat()
        return parse_fulfillment_response(
            payload,
            location=location,
            region=region,
            parts_queried=all_parts,
            canary_part_number=canary_part_number,
            observed_at=observed_at,
        )
    finally:
        if owns_client:
            await client.aclose()


async def _amain() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if len(sys.argv) < 3:
        print("Usage: python -m mac_availability.availability <PART_NUMBER> <ZIP> [REGION]", file=sys.stderr)
        sys.exit(2)
    part = sys.argv[1]
    zip_code = sys.argv[2]
    region = sys.argv[3] if len(sys.argv) > 3 else "US"
    snapshot = await fetch_availability([part], zip_code, region=region)
    print(json.dumps(snapshot.model_dump(exclude={"raw"}), indent=2))


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
