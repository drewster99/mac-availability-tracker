"""Crawl Apple's Mac product catalog: families → standard configurations → SKUs + prices.

The buy page for each Mac family carries a ``window.PRODUCT_SELECTION_BOOTSTRAP``
script with the full configurator dataset. From this we extract every
``PRECONFIGURED_BTR`` and ``PRECONFIGURED_FORWARD_DEPLOY`` entry — the standard
configurations with stable part numbers — along with their prices and dimensions.

Build-to-order configurations are deliberately out of scope: their part numbers
are generated dynamically and have no fixed identity for tracking over time.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Optional

from pydantic import BaseModel, Field

from .client import AppleShopClient

log = logging.getLogger(__name__)

REGION_TO_PATH: dict[str, str] = {
    "US": "",
    "CA": "/ca",
    "AU": "/au",
    "DE": "/de",
    "UK": "/uk",
    "FR": "/fr",
    "JP": "/jp",
    "IT": "/it",
    "ES": "/es",
    "NL": "/nl",
    "MX": "/mx",
    "BR": "/br",
    "IN": "/in",
    "HK": "/hk",
    "KR": "/kr",
    "SG": "/sg",
    "AT": "/at",
    "BE_FR": "/be-fr",
    "BE_NL": "/be-nl",
    "CH_DE": "/ch-de",
    "CH_FR": "/ch-fr",
    "SE": "/se",
    "TR": "/tr",
    "TH": "/th",
    "MY": "/my",
    "TW": "/tw",
    "AE": "/ae",
}

LOCALE_TO_REGION: dict[str, str] = {
    "en_US": "US",
    "en_CA": "CA",
    "fr_CA": "CA",
    "en_AU": "AU",
    "de_DE": "DE",
    "en_GB": "UK",
    "fr_FR": "FR",
    "ja_JP": "JP",
    "it_IT": "IT",
    "es_ES": "ES",
    "nl_NL": "NL",
    "es_MX": "MX",
    "pt_BR": "BR",
    "en_IN": "IN",
    "en_HK": "HK",
    "ko_KR": "KR",
    "en_SG": "SG",
    "de_AT": "AT",
    "fr_BE": "BE_FR",
    "fr_CH": "CH_FR",
    "de_CH": "CH_DE",
    "sv_SE": "SE",
    "tr_TR": "TR",
    "th_TH": "TH",
    "en_MY": "MY",
    "zh_TW": "TW",
    "en_AE": "AE",
}

UNSUPPORTED_LOCALES: set[str] = {"zh_CN", "en_MO"}
"""Locales served by separate hostnames (e.g. apple.com.cn) or with non-standard
shop endpoints; out of scope for the baseline crawl."""

LOCALE_TO_CURRENCY: dict[str, str] = {
    "en_US": "USD", "en_CA": "CAD", "fr_CA": "CAD", "en_AU": "AUD",
    "de_DE": "EUR", "en_GB": "GBP", "fr_FR": "EUR", "ja_JP": "JPY",
    "it_IT": "EUR", "es_ES": "EUR", "nl_NL": "EUR", "es_MX": "MXN",
    "pt_BR": "BRL", "en_IN": "INR", "en_HK": "HKD", "ko_KR": "KRW",
    "en_SG": "SGD", "de_AT": "EUR", "fr_BE": "EUR", "fr_CH": "CHF",
    "de_CH": "CHF", "sv_SE": "SEK", "tr_TR": "TRY", "th_TH": "THB",
    "en_MY": "MYR", "zh_TW": "TWD", "en_AE": "AED",
}


class MacFamily(BaseModel):
    """A top-level Mac product family link discovered on ``/shop/buy-mac``."""

    slug: str
    url: str


class MacConfig(BaseModel):
    """A single Apple-published standard configuration with a stable part number."""

    family: str
    part_number: str
    price_key: str
    container_part_number: Optional[str] = None
    is_coming_soon: bool = False
    raw_amount: Optional[float] = None
    formatted_amount: Optional[str] = None
    currency: str = "USD"
    locale: str = "en_US"
    dimensions: dict = Field(default_factory=dict)
    product_configuration: dict = Field(default_factory=dict)


def _extract_balanced_brace(text: str, start_idx: int) -> str:
    """Return the substring from a ``{`` to its matching ``}``, honoring quoted strings."""
    depth = 0
    in_str = False
    escape = False
    i = start_idx
    while i < len(text):
        ch = text[i]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start_idx : i + 1]
        i += 1
    raise ValueError("Unbalanced braces while extracting bootstrap JSON")


def parse_buy_mac_families(html: str) -> list[MacFamily]:
    """Extract the slugs of each Mac family from the ``/shop/buy-mac`` index page.

    Handles both the unprefixed US path (``/shop/buy-mac/macbook-pro``) and the
    region-prefixed path (``/uk/shop/buy-mac/macbook-pro``).
    """
    pattern = re.compile(
        r'href="((?:/[a-z][a-z0-9-]+)?/shop/buy-mac/([a-z0-9-]+))(?:"|/[^"]*")',
        re.IGNORECASE,
    )
    seen: dict[str, MacFamily] = {}
    for match in pattern.finditer(html):
        path = match.group(1)
        slug = match.group(2)
        if slug in seen:
            continue
        url = f"https://www.apple.com{path}"
        seen[slug] = MacFamily(slug=slug, url=url)
    return list(seen.values())


def parse_family_configs(
    family: str, html: str, *, locale: str = "en_US", currency: str = "USD"
) -> list[MacConfig]:
    """Extract every standard configuration for a Mac family from its buy page.

    Looks for the ``window.PRODUCT_SELECTION_BOOTSTRAP`` script and parses the
    embedded ``productSelectionData`` JSON literal.
    """
    bootstrap_data: Optional[dict] = None
    for match in re.finditer(r"<script[^>]*>(.*?)</script>", html, re.DOTALL):
        body = match.group(1)
        if "PRODUCT_SELECTION_BOOTSTRAP" not in body or "productSelectionData" not in body:
            continue
        marker = body.find("productSelectionData:")
        if marker == -1:
            continue
        brace = body.find("{", marker)
        if brace == -1:
            continue
        bootstrap_data = json.loads(_extract_balanced_brace(body, brace))
        break

    if bootstrap_data is None:
        log.warning("No PRODUCT_SELECTION_BOOTSTRAP found for family=%s", family)
        return []

    products = bootstrap_data.get("products", [])
    prices = bootstrap_data.get("mainDisplayValues", {}).get("prices", {})

    configs: list[MacConfig] = []
    for product in products:
        if product.get("type") not in ("PRECONFIGURED_BTR", "PRECONFIGURED_FORWARD_DEPLOY"):
            continue
        sku = product.get("btrOrFdPartNumber")
        price_key = product.get("priceKey")
        if not sku or not price_key:
            continue
        price_entry = prices.get(price_key, {})
        current = price_entry.get("currentPrice") if isinstance(price_entry, dict) else None
        raw_amount: Optional[float] = None
        formatted: Optional[str] = None
        if isinstance(current, dict):
            try:
                raw_amount = float(current.get("raw_amount")) if current.get("raw_amount") is not None else None
            except (TypeError, ValueError):
                raw_amount = None
            formatted = current.get("amount")
        configs.append(
            MacConfig(
                family=family,
                part_number=sku,
                price_key=price_key,
                container_part_number=product.get("aosContainerPartNumber"),
                is_coming_soon=bool(product.get("isComingSoon")),
                raw_amount=raw_amount,
                formatted_amount=formatted,
                currency=currency,
                locale=locale,
                dimensions=product.get("dimensions") or {},
                product_configuration=product.get("productConfiguration") or {},
            )
        )
    return configs


async def fetch_families(
    client: AppleShopClient, *, region: str = "US"
) -> list[MacFamily]:
    """Discover Mac product families for a given region."""
    region_path = REGION_TO_PATH.get(region, "")
    url = f"https://www.apple.com{region_path}/shop/buy-mac"
    response = await client.get(url, region=region, accept="text/html")
    return parse_buy_mac_families(response.text)


async def fetch_family_configs(
    client: AppleShopClient,
    family: MacFamily,
    *,
    region: str = "US",
    locale: str = "en_US",
    currency: str = "USD",
) -> list[MacConfig]:
    """Fetch and parse every standard configuration for a single Mac family."""
    response = await client.get(
        family.url,
        region=region,
        accept="text/html",
        referer="https://www.apple.com/shop/buy-mac",
    )
    return parse_family_configs(family.slug, response.text, locale=locale, currency=currency)


async def fetch_full_catalog(
    client: Optional[AppleShopClient] = None,
    *,
    region: str = "US",
    locale: str = "en_US",
    currency: str = "USD",
) -> list[MacConfig]:
    """Fetch every standard Mac configuration available in a region."""
    owns_client = client is None
    if client is None:
        client = AppleShopClient()
    try:
        families = await fetch_families(client, region=region)
        log.info("Discovered %d Mac families: %s", len(families), [f.slug for f in families])
        results: list[MacConfig] = []
        for family in families:
            configs = await fetch_family_configs(
                client, family, region=region, locale=locale, currency=currency
            )
            log.info("  %s: %d preconfigured SKUs", family.slug, len(configs))
            results.extend(configs)
        return results
    finally:
        if owns_client:
            await client.aclose()


async def fetch_catalog_for_locales(
    locales: list[str], client: Optional[AppleShopClient] = None
) -> list[MacConfig]:
    """Fetch the preconfigured Mac catalog across multiple Apple Store locales.

    Locales not present in ``LOCALE_TO_REGION`` (or in ``UNSUPPORTED_LOCALES``,
    such as zh_CN which is served from a separate hostname) are skipped with a
    warning. When several locales share the same region (e.g. en_CA/fr_CA both
    serve the Canadian shop), the region is crawled once and the resulting SKUs
    are tagged once with a single representative locale to avoid clobbering each
    other on the part-number primary key.
    """
    owns_client = client is None
    if client is None:
        client = AppleShopClient()
    try:
        seen_regions: dict[str, str] = {}
        for locale in locales:
            if locale in UNSUPPORTED_LOCALES:
                continue
            region = LOCALE_TO_REGION.get(locale)
            if region is None:
                continue
            seen_regions.setdefault(region, locale)

        results: list[MacConfig] = []
        for region, locale in seen_regions.items():
            currency = LOCALE_TO_CURRENCY.get(locale, "")
            try:
                configs = await fetch_full_catalog(
                    client, region=region, locale=locale, currency=currency
                )
            except Exception as exc:
                log.warning("Catalog crawl failed for %s/%s: %s", locale, region, exc)
                continue
            log.info("Locale %s (region %s): %d total preconfigured SKUs", locale, region, len(configs))
            results.extend(configs)

        for locale in locales:
            if locale in UNSUPPORTED_LOCALES:
                continue
            if LOCALE_TO_REGION.get(locale) is None:
                log.warning("No region mapping for locale %s; skipping", locale)
        return results
    finally:
        if owns_client:
            await client.aclose()


async def _amain() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    catalog = await fetch_full_catalog()
    print(f"\nTotal standard SKUs across all Mac families: {len(catalog)}\n")
    for cfg in catalog:
        print(f"  {cfg.part_number}  {cfg.family:<18}  {cfg.formatted_amount or '?':>10}  {cfg.price_key}")


def main() -> None:
    asyncio.run(_amain())


if __name__ == "__main__":
    main()
