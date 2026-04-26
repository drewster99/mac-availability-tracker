"""Fetch Apple's fulfillment-messages endpoint successfully via Playwright+Stealth.

This is the working version. The gate on /shop/fulfillment-messages?fae=true
is Apple's SHIELD bot-detection layer:

  1. The browser loads /shop/shld/v1/verify.js (heavily obfuscated; has
     headless-detection probes for HeadlessChrome, PhantomJS, and friends).
  2. The verify script POSTs canvas/audio/font fingerprints + solves a tiny
     proof-of-work challenge from /shop/shld/work/v1/q.
  3. Apple's edge sets cookies on success: shld_bt_m, sh_spksy, shld_bt_ck.
  4. Only after those cookies are present does fulfillment-messages return
     JSON. Without them it 541s with a Page Not Found body that has
     <link rel=canonical href=...shop/404> — this is genuinely a 404 from
     SHIELD's gating, not a transient WAF block.

Plain Python httpx, plain curl, plain headless Playwright all fail the
verify.js check. ``playwright-stealth`` masks the headless markers so the
verify script accepts the browser, and SHIELD issues the cookies.

This script:
  1. Launches Chromium via Playwright + Stealth.
  2. Navigates to a Mac buy page so SHIELD runs.
  3. In-page fetch()es fulfillment-messages.
  4. Saves the response and pretty-prints store + delivery info.
  5. Optionally exports the SHIELD cookies so subsequent calls can be made
     with plain httpx (no browser needed).
"""
from __future__ import annotations

import argparse
import json
import sys
from urllib.parse import quote
from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth


def fetch_fulfillment(part_number: str, location: str, region_path: str = "") -> dict:
    fm_url = (
        f"https://www.apple.com{region_path}/shop/fulfillment-messages?fae=true&pl=true"
        f"&parts.0={quote(part_number, safe='')}&location={quote(location, safe='')}"
        f"&searchNearby=true"
    )
    nav_url = f"https://www.apple.com{region_path}/shop/buy-mac/macbook-pro"

    stealth = Stealth()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
            ),
            viewport={"width": 1280, "height": 1200},
            locale="en-US",
        )
        stealth.apply_stealth_sync(ctx)
        page = ctx.new_page()

        page.goto(nav_url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(5_000)  # give SHIELD time to fingerprint + solve PoW

        cookies = {c["name"]: c["value"] for c in ctx.cookies()}
        result = page.evaluate(
            """async (u) => {
                const r = await fetch(u, {
                    credentials: 'include',
                    headers: {
                        'Accept': 'application/json, */*',
                        'X-Requested-With': 'XMLHttpRequest',
                    },
                });
                const text = await r.text();
                return {status: r.status, contentType: r.headers.get('content-type'), text};
            }""",
            fm_url,
        )
        browser.close()

    if result["status"] != 200 or "json" not in (result.get("contentType") or "").lower():
        return {
            "ok": False,
            "status": result["status"],
            "url": fm_url,
            "snippet": result["text"][:300],
            "cookies_present": sorted(cookies),
        }

    payload = json.loads(result["text"])
    return {
        "ok": True,
        "status": result["status"],
        "url": fm_url,
        "raw": payload,
        "cookies": cookies,
    }


def summarize(payload: dict) -> None:
    body = payload.get("body") or {}
    content = body.get("content") or {}
    pickup = content.get("pickupMessage") or {}
    delivery = content.get("deliveryMessage") or {}

    stores = pickup.get("stores") or []
    print(f"\nPICKUP — {len(stores)} stores in response")
    for s in stores[:6]:
        parts = s.get("partsAvailability") or {}
        for sku, p in parts.items():
            quote = (
                p.get("pickupSearchQuote")
                or (p.get("messageTypes", {}).get("regular", {}) or {}).get("storePickupQuote")
                or "?"
            )
            print(f"  {s.get('storeNumber'):>5} {s.get('storeName'):<25} {sku:<14} {p.get('pickupDisplay'):<12} {quote!r}")

    if delivery:
        print(f"\nDELIVERY — {len(delivery)} SKU(s)")
        for sku, msg in delivery.items():
            if isinstance(msg, dict):
                regular = msg.get("regular") or msg.get("compact") or msg
            else:
                regular = msg
            print(f"  {sku}: {regular!r}"[:300])
    else:
        print("\nDELIVERY — (none returned in this response)")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--part", default="MGDR4LL/A", help="Part number to query")
    parser.add_argument("--zip", dest="zip_", default="94103", help="Postal code / location")
    parser.add_argument("--region-path", default="", help="Optional region path prefix, e.g. /uk")
    parser.add_argument("--save", default=None, help="Optional path to save raw JSON")
    parser.add_argument("--save-cookies", default=None, help="Optional path to save SHIELD cookies as JSON")
    args = parser.parse_args()

    print(f"Fetching fulfillment-messages for {args.part} @ {args.zip_} (region={args.region_path or 'US'}) …")
    out = fetch_fulfillment(args.part, args.zip_, region_path=args.region_path)
    print(f"HTTP {out['status']}  ok={out['ok']}")
    if not out["ok"]:
        print(f"snippet: {out.get('snippet')!r}")
        print(f"cookies present: {out.get('cookies_present')}")
        return 1

    raw = out["raw"]
    summarize(raw)
    if args.save:
        with open(args.save, "w") as f:
            json.dump(raw, f, indent=2)
        print(f"\nSaved raw JSON to {args.save}")
    if args.save_cookies:
        with open(args.save_cookies, "w") as f:
            json.dump(out["cookies"], f, indent=2)
        print(f"Saved {len(out['cookies'])} cookies to {args.save_cookies}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
