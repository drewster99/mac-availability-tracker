"""Drive a real Mac Studio configuration in a Playwright browser and capture
every XHR Apple fires. The goal is to learn how the BTO ("Z"-prefixed) part
numbers come into being — server-side via a POST, client-side via JavaScript,
or only after Add-to-Bag.

Outputs a JSON file with every captured request + response (URL, method,
status, content-type, size, and a body snippet for JSON responses).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth


def _is_interesting(url: str) -> bool:
    """Filter out static asset noise."""
    if any(s in url for s in ("/v/", "/digitalassets/", ".woff", ".woff2", ".ttf", ".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif", ".css", "metrics", "logging")):
        return False
    return any(s in url for s in ("/shop/", "fulfillment", "configure", "process", "buyflow", "/api/", "purchase"))


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--out", default="data/bto_capture.json", help="JSON file to write")
    p.add_argument("--family-url", default="https://www.apple.com/shop/buy-mac/mac-studio", help="Buy-mac landing page to start from")
    p.add_argument("--config-url", default=None,
                   help="Optional deep-link to a specific configuration (slug URL); if omitted, "
                        "navigates the family page and triggers the configurable variant.")
    p.add_argument("--settle-seconds", type=float, default=10.0)
    p.add_argument("--clicks", action="append", default=[], help="CSS selectors to click in order before capture (one per --clicks)")
    args = p.parse_args()

    captured: list[dict] = []

    def on_response(resp):
        try:
            url = resp.url
            if not _is_interesting(url):
                return
            ct = resp.headers.get("content-type", "")
            entry = {
                "method": resp.request.method,
                "url": url,
                "status": resp.status,
                "content_type": ct,
                "request_post_data": resp.request.post_data if resp.request.method != "GET" else None,
            }
            if "json" in ct or "javascript" in ct or "text" in ct:
                try:
                    text = resp.text()
                    entry["body_len"] = len(text)
                    # Store small JSON in full, large bodies as snippet + part-number hits
                    if "json" in ct and len(text) < 80_000:
                        try:
                            entry["body_json"] = json.loads(text)
                        except Exception:
                            entry["body_snippet"] = text[:1500]
                    else:
                        entry["body_snippet"] = text[:1500]
                    z_parts = sorted(set(re.findall(r"\bZ[A-Z0-9]{5,8}LL/A\b", text)))
                    if z_parts:
                        entry["z_parts"] = z_parts
                    any_parts = sorted(set(re.findall(r'"partNumber"\s*:\s*"([A-Z0-9/]+)"', text)))
                    if any_parts:
                        entry["partNumbers"] = any_parts
                except Exception as exc:
                    entry["body_error"] = str(exc)
            captured.append(entry)
        except Exception:
            pass

    stealth = Stealth()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=False)  # headed so we can watch
        try:
            ctx = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
                viewport={"width": 1280, "height": 1200},
                locale="en-US",
            )
            stealth.apply_stealth_sync(ctx)
            page = ctx.new_page()
            page.on("response", on_response)
            start_url = args.config_url or args.family_url
            print(f"Navigating to {start_url}", file=sys.stderr)
            page.goto(start_url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(int(args.settle_seconds * 1000))
            for sel in args.clicks:
                try:
                    page.click(sel, timeout=8_000)
                    page.wait_for_timeout(2500)
                except Exception as exc:
                    print(f"click {sel} failed: {exc}", file=sys.stderr)
        finally:
            browser.close()

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(captured, indent=2, default=str))
    print(f"Captured {len(captured)} XHRs -> {out_path}", file=sys.stderr)

    # Quick textual summary
    z_parts = sorted({z for c in captured for z in c.get("z_parts", [])})
    all_parts = sorted({p for c in captured for p in c.get("partNumbers", [])})
    print(f"\nUnique Z-prefixed part numbers seen: {len(z_parts)}")
    for p in z_parts[:20]:
        print(f"  {p}")
    print(f"\nAll partNumber occurrences: {len(all_parts)}")
    for p in all_parts[:20]:
        print(f"  {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
