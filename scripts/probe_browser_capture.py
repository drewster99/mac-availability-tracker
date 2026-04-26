"""Open a real browser, drive the buy/pickup flow, log every XHR Apple makes.

If our 541s are coming from a stale URL, this will surface the endpoint
Apple's frontend is actually calling today.
"""
from __future__ import annotations

import json
import sys
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

PRODUCT_SLUG_URL = (
    "https://www.apple.com/shop/buy-mac/macbook-pro/"
    "14-inch-spaceblack-standard-display-apple-m5-pro-chip-15-core-cpu-16-core-gpu-24gb-memory-1tb-storage"
)
TARGET_ZIP = "94103"


def main() -> int:
    captured: list[dict] = []
    seen: set[tuple[str, str]] = set()

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
            ),
            locale="en-US",
            viewport={"width": 1280, "height": 1200},
        )

        def on_response(response) -> None:
            url = response.url
            ct = response.headers.get("content-type", "")
            # Only capture API-style responses (json or empty body), skip image/css/font/js bundles
            if "json" not in ct.lower() and not url.lower().rstrip("/").endswith(("/messages", "/api")):
                return
            parsed = urlparse(url)
            path = parsed.path
            if path.startswith(("/v/", "/ac/", "/cm-static/", "/aos-shared/", "/cmsUtils/", "/js/")):
                return
            key = (response.request.method, path + "?" + parsed.query[:120])
            if key in seen:
                return
            seen.add(key)
            try:
                body = response.text()
            except Exception:
                body = "(unreadable)"
            captured.append({
                "method": response.request.method,
                "status": response.status,
                "url": url,
                "content_type": ct,
                "body_preview": body[:400],
                "body_length": len(body),
            })

        page = context.new_page()
        page.on("response", on_response)

        print(f"Loading {PRODUCT_SLUG_URL} …")
        page.goto(PRODUCT_SLUG_URL, wait_until="domcontentloaded", timeout=60_000)
        try:
            page.wait_for_load_state("load", timeout=15_000)
        except Exception:
            pass
        print("  page loaded; waiting 3s for deferred XHRs.")
        page.wait_for_timeout(3_000)

        # Scroll to trigger lazy fetches
        for y in (0, 600, 1200, 1800, 2400, 3000):
            page.evaluate(f"window.scrollTo(0, {y})")
            page.wait_for_timeout(700)

        # Look for any buttons/links containing pickup/availability text
        print("\nProbing DOM for pickup-flow controls…")
        candidate_texts = ["Check availability", "Pick up", "Pickup", "in store", "In-Store Pick Up"]
        for txt in candidate_texts:
            try:
                elements = page.query_selector_all(f'text="{txt}"')
                if elements:
                    print(f"  found {len(elements)} elements matching text {txt!r}")
                    try:
                        elements[0].click(timeout=3_000)
                        print(f"    clicked first match for {txt!r}")
                        page.wait_for_timeout(3_000)
                        break
                    except Exception as exc:
                        print(f"    click failed: {exc}")
            except Exception:
                pass

        # Try a broader role-based search
        try:
            buttons = page.query_selector_all("button, a")
            for btn in buttons[:200]:
                try:
                    label = (btn.inner_text() or "").strip().lower()
                except Exception:
                    label = ""
                if any(k in label for k in ("check availability", "pick up", "pickup", "in store", "available at apple")):
                    print(f"  trying button label: {label!r}")
                    try:
                        btn.click(timeout=3_000)
                        page.wait_for_timeout(3_000)
                        break
                    except Exception as exc:
                        print(f"    click failed: {exc}")
        except Exception as exc:
            print(f"  button search failed: {exc}")

        # Try filling ZIP if a relevant input appeared
        try:
            zip_input = page.query_selector(
                'input[id*="zip" i], input[id*="postal" i], input[name*="zip" i], input[placeholder*="ZIP" i]'
            )
            if zip_input:
                print(f"\nFilling ZIP input with {TARGET_ZIP}")
                zip_input.fill(TARGET_ZIP)
                page.keyboard.press("Enter")
                page.wait_for_timeout(4_000)
        except Exception as exc:
            print(f"  zip fill failed: {exc}")

        # Final wait for trailing XHRs
        page.wait_for_timeout(4_000)
        browser.close()

    print(f"\n=== {len(captured)} JSON-ish responses captured ===")
    for ev in captured:
        print(f"\n{ev['method']:<5} {ev['status']:<3} {ev['url']}")
        print(f"  type: {ev['content_type']}  bytes: {ev['body_length']}")
        snippet = ev['body_preview'].replace("\n", " ")
        print(f"  preview: {snippet[:300]!r}")

    print(f"\n=== distinct URL paths ===")
    for path in sorted({urlparse(ev['url']).path for ev in captured}):
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
