"""Probe Apple's ``fulfillment-messages`` endpoint with every realistic variation.

Single-file script. No project dependencies — just the Python stdlib + httpx
(already in the project venv). Targets one SKU at one US ZIP, then varies the
URL parameters, request headers, and cookie state to see whether any
combination yields a real JSON body.

Why each variant exists:
- "bare" — minimum query string from worthbak's Node script
- "browser-ua" — adds Safari User-Agent and matching Accept-Language
- "fae" — adds ``fae=true`` (the live apple.com page uses this flag)
- "with-mts" — adds ``mts.0=regular&mts.1=compact`` like the live page
- "warm-cookie" — does a GET to the buy page first to pick up Akamai cookies,
  then sends them on the JSON request
- "uk-region" — repeats with the UK shop variant in case US is hostiler
- "old-pickup-message" — sanity check: the older endpoint we already use,
  same URL except path is ``/shop/retail/pickup-message``

For each variant we print HTTP status, body size, content-type, and the first
200 chars of the body — so it's obvious whether we got JSON or the
"Page Not Found" HTML wrapper Apple returns at 541.
"""
from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import dataclass, field
from urllib.parse import quote

import httpx

PART = "MGDR4LL/A"  # 14-inch MacBook Pro M5 Pro, US
ZIP = "94103"
STORE = "R032"  # Apple Stanford
GAP_BETWEEN_REQUESTS_S = 5.0

BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
)


@dataclass
class Probe:
    name: str
    url: str
    headers: dict = field(default_factory=dict)
    warm_get: str | None = None
    region: str = ""


def build_probes() -> list[Probe]:
    encoded_part = quote(PART, safe="")
    base_us = f"https://www.apple.com/shop/fulfillment-messages?parts.0={encoded_part}&location={ZIP}"
    base_us_store = f"https://www.apple.com/shop/fulfillment-messages?parts.0={encoded_part}&searchNearby=true&store={STORE}"
    base_uk = f"https://www.apple.com/uk/shop/fulfillment-messages?parts.0={quote('MGDR4B/A', safe='')}&location=W1B%202EL"

    safari_headers = {
        "User-Agent": BROWSER_UA,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.apple.com/shop/buy-mac/macbook-pro",
        "X-Requested-With": "XMLHttpRequest",
    }

    return [
        Probe(
            name="bare (worthbak's URL, no headers)",
            url=base_us_store,
            region="US",
        ),
        Probe(
            name="browser-ua (Safari headers, location-based)",
            url=base_us + "&searchNearby=true",
            headers=safari_headers,
            region="US",
        ),
        Probe(
            name="fae=true (mimic the apple.com inline call)",
            url=base_us + "&fae=true&searchNearby=true",
            headers=safari_headers,
            region="US",
        ),
        Probe(
            name="with-mts (regular+compact pickup variants)",
            url=base_us + "&pl=true&mts.0=regular&mts.1=compact&searchNearby=true",
            headers=safari_headers,
            region="US",
        ),
        Probe(
            name="warm-cookie (GET buy page first, send any cookies)",
            url=base_us + "&fae=true&pl=true&mts.0=regular&searchNearby=true",
            headers=safari_headers,
            warm_get="https://www.apple.com/shop/buy-mac/macbook-pro",
            region="US",
        ),
        Probe(
            name="uk-region (try non-US shop)",
            url=base_uk + "&searchNearby=true",
            headers={**safari_headers, "Referer": "https://www.apple.com/uk/shop/buy-mac/macbook-pro"},
            region="UK",
        ),
        Probe(
            name="old-pickup-message (sanity: known-working endpoint)",
            url=f"https://www.apple.com/shop/retail/pickup-message?pl=true&parts.0={encoded_part}&location={ZIP}",
            headers={"Accept": "application/json"},
            region="US",
        ),
    ]


async def run_probe(client: httpx.AsyncClient, probe: Probe) -> None:
    print(f"\n=== {probe.name} ===")
    print(f"URL: {probe.url}")

    cookies_to_send: dict[str, str] = {}
    if probe.warm_get:
        try:
            warmup = await client.get(probe.warm_get, headers=probe.headers)
            cookies_to_send = {c.name: c.value for c in client.cookies.jar}
            print(f"Warm-up GET {warmup.status_code}; cookies received: {len(cookies_to_send)}")
            if cookies_to_send:
                print(f"  cookie names: {sorted(cookies_to_send)[:8]}")
        except Exception as exc:
            print(f"Warm-up failed: {exc}")

    try:
        response = await client.get(probe.url, headers=probe.headers)
    except Exception as exc:
        print(f"Request failed: {exc}")
        return

    body = response.text
    snippet = body[:200].replace("\n", " ")
    is_json_ish = body.lstrip().startswith("{")
    print(f"HTTP {response.status_code}  bytes={len(body)}  content-type={response.headers.get('content-type','?')}  json-ish={is_json_ish}")
    print(f"snippet: {snippet}{'…' if len(body) > 200 else ''}")
    if is_json_ish:
        try:
            data = response.json()
            head = data.get("head") or {}
            body_obj = data.get("body") or {}
            stores = (
                (body_obj.get("stores"))
                or (body_obj.get("content") or {}).get("pickupMessage", {}).get("stores")
                or []
            )
            delivery = (body_obj.get("content") or {}).get("deliveryMessage")
            print(f"  parsed: head.status={head.get('status')!r}  stores={len(stores)}  has deliveryMessage={delivery is not None}")
            if delivery and isinstance(delivery, dict):
                first_part, first_msg = next(iter(delivery.items()))
                print(f"  delivery sample for {first_part}: {first_msg}")
        except Exception as exc:
            print(f"  json parse error: {exc}")


async def main() -> None:
    probes = build_probes()
    print(f"Probing fulfillment-messages with {len(probes)} variants. {GAP_BETWEEN_REQUESTS_S}s gap between requests.")
    async with httpx.AsyncClient(
        timeout=30.0,
        follow_redirects=True,
        cookies=httpx.Cookies(),
    ) as client:
        for i, probe in enumerate(probes):
            if i > 0:
                await asyncio.sleep(GAP_BETWEEN_REQUESTS_S)
            await run_probe(client, probe)
            client.cookies.clear()
    print("\n--- done ---")
    print("If you see HTTP 200 with a JSON body that contains stores or deliveryMessage,")
    print("that variant is the path forward. If everything is HTTP 541 with the 'Page Not Found'")
    print("HTML wrapper, the endpoint is gated for non-browser callers (Akamai bot challenge).")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()) or 0)
