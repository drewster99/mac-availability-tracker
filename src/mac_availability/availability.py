"""Poll Apple's ``fulfillment-messages`` endpoint for pickup *and* delivery.

A single call returns:
  * pickup availability across roughly a dozen Apple Stores near the supplied
    ZIP/postal code (same data the older ``pickup-message`` endpoint served);
  * per-SKU delivery messaging — earliest delivery date, shipping cost,
    order-by cutoff, ``isBuyable`` flag, same-day-delivery eligibility.

The endpoint is gated by Apple's SHIELD bot-detection layer. Cookies must be
bootstrapped via :mod:`mac_availability.shield` before calling.

``canary_ok`` on the returned snapshot is a structural-sanity bit: true iff
the response contained stores and at least one store reported parts
availability. False means Apple returned an empty or shadow-banned response —
treat the snapshot as suspect rather than real data.
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


class DeliveryAvailability(BaseModel):
    """Delivery messaging for a single SKU at the queried location.

    Delivery is a per-SKU concept (independent of store), unlike pickup. Each
    snapshot may carry one of these per part queried.
    """

    part_number: str
    delivery_date: Optional[str] = None
    delivery_cost: Optional[str] = None
    delivery_display: Optional[str] = None
    encoded_date: Optional[str] = None
    order_by_cutoff: Optional[str] = None
    is_buyable: Optional[bool] = None
    commit_code: Optional[str] = None
    commit_reason: Optional[str] = None
    idl_eligible: Optional[bool] = None
    sticky_sth: Optional[str] = None
    sticky_idl: Optional[str] = None


class AvailabilitySnapshot(BaseModel):
    """One ``fulfillment-messages`` response, normalized."""

    observed_at: str
    location: str
    region: str
    parts_queried: list[str]
    canary_part_number: Optional[str] = None
    canary_ok: bool = True
    stores: list[StoreAvailability] = Field(default_factory=list)
    deliveries: list[DeliveryAvailability] = Field(default_factory=list)
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
    base = f"https://www.apple.com{region_path}/shop/fulfillment-messages"
    params: list[str] = ["fae=true", "pl=true"]
    for index, part in enumerate(parts):
        params.append(f"parts.{index}={quote(part, safe='')}")
    params.append(f"location={quote(location, safe='')}")
    params.append("searchNearby=true")
    if store_id is not None:
        params.append(f"store={store_id}")
    return base + "?" + "&".join(params)


def _coerce_bool(value: object) -> Optional[bool]:
    """Apple sometimes ships booleans as the strings 'true'/'false'."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "1"):
            return True
        if lowered in ("false", "no", "0"):
            return False
    return None


def _parse_deliveries(content: Optional[dict]) -> list[DeliveryAvailability]:
    """Normalize the per-SKU deliveryMessage block into our schema."""
    if not isinstance(content, dict):
        return []
    delivery_block = content.get("deliveryMessage") or {}
    if not isinstance(delivery_block, dict):
        return []
    out: list[DeliveryAvailability] = []
    for part_number, msg in delivery_block.items():
        if not isinstance(msg, dict):
            continue
        # Apple groups the messaging by template ('regular', 'compact', etc.).
        # 'regular' is the consumer-purchase template we want; fall back to the
        # whole dict in case Apple ever flattens it.
        regular = msg.get("regular") if isinstance(msg.get("regular"), dict) else msg

        delivery_options = regular.get("deliveryOptions") or []
        primary_option = delivery_options[0] if delivery_options else {}

        delivery_option_messages = regular.get("deliveryOptionMessages") or []
        primary_option_message = delivery_option_messages[0] if delivery_option_messages else {}

        buyability = regular.get("buyability") or {}
        sticky_idl = regular.get("stickyMessageIDL")
        idl_eligible: Optional[bool] = None
        if isinstance(sticky_idl, str):
            # Same-day-delivery eligibility is implicit in the sticky message:
            # the negative form contains 'Unavailable for' / 'Not eligible'.
            lowered = sticky_idl.lower()
            if "unavailable" in lowered or "not eligible" in lowered:
                idl_eligible = False
            elif sticky_idl.strip():
                idl_eligible = True

        out.append(
            DeliveryAvailability(
                part_number=part_number,
                delivery_date=primary_option.get("date"),
                delivery_cost=primary_option.get("shippingCost"),
                delivery_display=primary_option.get("displayName"),
                encoded_date=primary_option_message.get("encodedUpperDateString"),
                order_by_cutoff=regular.get("orderByDeliveryBy"),
                is_buyable=_coerce_bool(buyability.get("isBuyable")),
                commit_code=str(buyability.get("commitCode")) if buyability.get("commitCode") is not None else None,
                commit_reason=buyability.get("reason"),
                idl_eligible=idl_eligible,
                sticky_sth=regular.get("stickyMessageSTH"),
                sticky_idl=sticky_idl,
            )
        )
    return out


def parse_fulfillment_response(
    payload: dict,
    *,
    location: str,
    region: str,
    parts_queried: list[str],
    canary_part_number: Optional[str],
    observed_at: str,
) -> AvailabilitySnapshot:
    """Normalize the JSON returned by fulfillment-messages into our schema.

    Tolerates the older ``pickup-message`` shape (``body.stores`` directly)
    in case we ever fall back to it — the old DB has snapshots in that shape
    and this lets the same parser re-normalize them.
    """
    body = payload.get("body") or {}
    content = body.get("content") if isinstance(body.get("content"), dict) else {}
    pickup_message = content.get("pickupMessage") or {}
    raw_stores = pickup_message.get("stores") or body.get("stores") or []

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

    deliveries = _parse_deliveries(content) if content else []
    canary_ok = bool(stores) and any_parts_returned

    return AvailabilitySnapshot(
        observed_at=observed_at,
        location=location,
        region=region,
        parts_queried=parts_queried,
        canary_part_number=canary_part_number,
        canary_ok=canary_ok,
        stores=stores,
        deliveries=deliveries,
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
    """Single fulfillment-messages call covering pickup + delivery for the given SKUs.

    If ``client`` is not supplied, a fresh one is constructed and
    SHIELD-bootstrapped automatically — the caller doesn't need to know
    about cookies. For repeated calls, pass a long-lived client that already
    carries SHIELD cookies (see :func:`mac_availability.shield.aget_session`).
    """
    from datetime import datetime, timezone

    owns_client = client is None
    if client is None:
        # Smart default — bootstrap SHIELD so a one-off call just works.
        from .shield import aget_session

        session = await aget_session()
        client = AppleShopClient(
            default_user_agent=session.user_agent,
            default_cookies=session.cookies,
        )
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
        region_path = REGION_TO_PATH.get(region, "")
        response = await client.get(
            url,
            region=region,
            accept="application/json, */*",
            referer=f"https://www.apple.com{region_path}/shop/buy-mac/macbook-pro",
            extra_headers={"X-Requested-With": "XMLHttpRequest"},
        )
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            snippet = (response.text or "")[:300]
            raise RuntimeError(
                f"fulfillment-messages returned non-JSON (status={response.status_code}, "
                f"content-type={response.headers.get('content-type')!r}): {snippet!r}"
            ) from exc
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
