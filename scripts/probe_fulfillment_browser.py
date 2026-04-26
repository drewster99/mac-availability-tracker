"""Drive a real headless Chromium via Playwright to call fulfillment-messages.

This is the heavy hammer. Every HTTP-level approach in ``probe_fulfillment.py``
returns HTTP 541, which suggests Apple's WAF is screening on something that
only a real browser produces — likely a JavaScript-set cookie or token, or
the order/timing of network requests during normal navigation.

This script opens a real Chromium, navigates to a Mac buy page so JS runs
and any required cookies/tokens are issued, then fires a fetch() inside the
page context to the fulfillment-messages endpoint. If the WAF accepts that
fetch, we'll get JSON back and know that a browser-driven scraper is the
realistic path forward for shipping ETAs.
"""
from __future__ import annotations

import sys
from urllib.parse import quote

from playwright.sync_api import sync_playwright

PART = "MGDR4LL/A"
ZIP = "94103"


def main() -> int:
    encoded = quote(PART, safe="")
    fm_url = (
        f"https://www.apple.com/shop/fulfillment-messages?fae=true&pl=true"
        f"&mts.0=regular&parts.0={encoded}&location={ZIP}&searchNearby=true"
    )

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
            ),
            locale="en-US",
            viewport={"width": 1280, "height": 900},
        )
        page = context.new_page()

        print("Step 1: navigating to apple.com/shop/buy-mac/macbook-pro …")
        nav_response = page.goto(
            "https://www.apple.com/shop/buy-mac/macbook-pro",
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        print(f"  nav HTTP {nav_response.status if nav_response else '?'}")

        # Let any JS-issued cookies settle.
        page.wait_for_timeout(2_000)
        cookies = context.cookies()
        print(f"  cookies after page load: {len(cookies)}")
        for c in cookies[:8]:
            print(f"    - {c['name']}")

        print(f"\nStep 2: in-page fetch → {fm_url}")
        result = page.evaluate(
            """async (url) => {
                const resp = await fetch(url, {
                    method: 'GET',
                    credentials: 'include',
                    headers: {
                        'Accept': 'application/json, text/plain, */*',
                        'X-Requested-With': 'XMLHttpRequest',
                    },
                });
                const text = await resp.text();
                return {
                    status: resp.status,
                    ok: resp.ok,
                    contentType: resp.headers.get('content-type'),
                    size: text.length,
                    snippet: text.slice(0, 500),
                };
            }""",
            fm_url,
        )

        print(f"  HTTP {result['status']}  size={result['size']}  content-type={result['contentType']}")
        snippet = result["snippet"].replace("\n", " ")
        print(f"  snippet: {snippet}{'…' if result['size'] > 500 else ''}")

        is_json = result["snippet"].lstrip().startswith("{")
        if is_json:
            print("\n  ✓ JSON response — Playwright IS the path forward for fulfillment-messages.")
        else:
            print("\n  ✗ Still HTML / 541. Even an in-page fetch from a real browser is being blocked.")
            print("    Most likely Apple's WAF blocks the fetch unless it's preceded by specific")
            print("    user actions (e.g. selecting a pickup ZIP) that issue a one-shot token.")

        browser.close()
        return 0 if is_json else 1


if __name__ == "__main__":
    sys.exit(main())
