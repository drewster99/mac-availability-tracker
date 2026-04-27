"""Walk the buy-mac/mac-studio DOM in headed Chromium and dump every plausibly
clickable element (buttons, role=button, role=radio, links to /shop/...) so we
can identify the right entry point into the configurator.

Run once, eyeball the output, then refine probe_bto_config.py with the matching
selectors.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright
from playwright_stealth import Stealth


UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", default="https://www.apple.com/shop/buy-mac/mac-studio")
    p.add_argument("--out", default="data/bto_dom.json")
    p.add_argument("--settle-seconds", type=float, default=8.0)
    p.add_argument("--headless", action="store_true", help="Run headless (defaults to headed for human inspection)")
    args = p.parse_args()

    discovered: list[dict] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless)
        try:
            ctx = browser.new_context(
                user_agent=UA,
                viewport={"width": 1400, "height": 1100},
                locale="en-US",
            )
            Stealth().apply_stealth_sync(ctx)
            page = ctx.new_page()
            page.goto(args.url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(int(args.settle_seconds * 1000))

            # Grab every interactive element with its visible label.
            handles = page.evaluate(
                """() => {
                    const out = [];
                    const sel = 'button, [role="button"], [role="radio"], [role="link"], a[href], input[type="button"], input[type="submit"]';
                    document.querySelectorAll(sel).forEach((el, idx) => {
                        const text = (el.textContent || '').trim().slice(0, 120);
                        const aria = el.getAttribute('aria-label') || '';
                        const href = el.getAttribute('href') || '';
                        const dataAutom = el.getAttribute('data-autom') || '';
                        const role = el.getAttribute('role') || el.tagName.toLowerCase();
                        if (!text && !aria && !href && !dataAutom) return;
                        const rect = el.getBoundingClientRect();
                        out.push({
                            idx, role, text, aria, href, dataAutom,
                            x: Math.round(rect.x), y: Math.round(rect.y),
                            visible: rect.width > 0 && rect.height > 0
                        });
                    });
                    return out;
                }"""
            )
            discovered.extend(handles)
            print(f"\nFound {len(handles)} interactive elements", file=sys.stderr)

            # Try clicking the first M4 Max chip card if it's labelled by data-autom
            print("\nLook for 'select' or chip cards in the data-autom attributes:", file=sys.stderr)
            interesting = [h for h in handles if h["dataAutom"]]
            for h in interesting[:30]:
                print(f"  data-autom={h['dataAutom']!r:40s} role={h['role']:6s} aria={h['aria'][:40]!r}", file=sys.stderr)

        finally:
            browser.close()

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(discovered, indent=2, default=str))
    print(f"\nDOM dump -> {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
