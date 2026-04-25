"""Fetch and parse Apple's global retail store list.

The single page at ``/retail/storelist/`` carries a Next.js JSON payload covering
every Apple Store in every locale Apple sells from. One request, roughly 500+
stores across ~25 country locales (exact counts drift over time).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Optional

from pydantic import BaseModel, Field
from selectolax.parser import HTMLParser

from .client import AppleShopClient

log = logging.getLogger(__name__)

STORE_LIST_URL = "https://www.apple.com/retail/storelist/"

_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
    re.DOTALL,
)


class Store(BaseModel):
    """A single Apple retail store in a specific locale."""

    id: str
    locale: str
    state_name: Optional[str] = None
    state_code: Optional[str] = None
    name: str
    slug: Optional[str] = None
    telephone: Optional[str] = None
    address1: Optional[str] = None
    address2: Optional[str] = None
    city: Optional[str] = None
    postal_code: Optional[str] = None
    raw: dict = Field(default_factory=dict)


def parse_storelist_html(html: str) -> list[Store]:
    """Extract every store from the ``/retail/storelist/`` HTML page.

    Falls back to the ``selectolax`` DOM if the regex misses; the regex path is
    fast on the typical 600 KB+ payload.
    """
    match = _NEXT_DATA_RE.search(html)
    raw_json: Optional[str] = match.group(1) if match else None
    if raw_json is None:
        tree = HTMLParser(html)
        node = tree.css_first('script#__NEXT_DATA__')
        if node is not None:
            raw_json = node.text()
    if raw_json is None:
        raise ValueError("__NEXT_DATA__ not found in storelist HTML")

    data = json.loads(raw_json)
    locale_groups = data["props"]["pageProps"]["storeList"]

    stores: list[Store] = []
    for group in locale_groups:
        locale = group.get("locale") or group.get("calledLocale") or "unknown"
        for state_block in group.get("state", []) or []:
            state_name = state_block.get("name")
            for record in state_block.get("store", []) or []:
                address = record.get("address") or {}
                stores.append(
                    Store(
                        id=record["id"],
                        locale=locale,
                        state_name=state_name,
                        state_code=address.get("stateCode"),
                        name=record.get("name") or record.get("storeName") or "",
                        slug=record.get("slug"),
                        telephone=record.get("telephone"),
                        address1=address.get("address1"),
                        address2=address.get("address2"),
                        city=address.get("city"),
                        postal_code=address.get("postalCode"),
                        raw=record,
                    )
                )
    return stores


async def fetch_all_stores(client: Optional[AppleShopClient] = None) -> list[Store]:
    """Fetch and parse the global Apple Store list."""
    owns_client = client is None
    if client is None:
        client = AppleShopClient()
    try:
        response = await client.get(
            STORE_LIST_URL,
            region="storelist",
            accept="text/html",
        )
        return parse_storelist_html(response.text)
    finally:
        if owns_client:
            await client.aclose()


async def _amain() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    stores = await fetch_all_stores()
    by_locale: dict[str, int] = {}
    for store in stores:
        by_locale[store.locale] = by_locale.get(store.locale, 0) + 1
    print(f"Total stores: {len(stores)}")
    for locale, count in sorted(by_locale.items()):
        print(f"  {locale}: {count}")


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
