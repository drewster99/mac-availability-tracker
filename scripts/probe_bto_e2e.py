"""End-to-end BTO probe: drive a full configurator click-flow and capture every
XHR that touches partNumber/buyflow/configure/bag.

Strategy:
  1. Land on buy-mac/{family} (chooser page).
  2. Wait long, scroll, click the chip card with the highest data-autom rank
     ("buy-mac-...") that maps to a CONFIGURABLE product.
  3. Page transitions (probably SPA route) to the configurator.
  4. Click distinctive options (e.g. larger memory) to mutate the config.
  5. Click Add to Bag. Capture everything.

Outputs ``data/bto_capture_e2e.json`` with the captured XHRs and a list of any
Z-prefixed part numbers seen.
"""
from __future__ import annotations

import argparse
import json
import re
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
    p.add_argument("--family", default="mac-studio", help="mac family (mac-studio, imac, mac-mini, ...)")
    p.add_argument("--out", default="data/bto_capture_e2e.json")
    p.add_argument("--headless", action="store_true")
    p.add_argument("--settle-seconds", type=float, default=10.0)
    args = p.parse_args()

    captured: list[dict] = []

    def on_response(resp):
        try:
            url = resp.url
            if any(s in url for s in (".woff", ".png", ".jpg", ".svg", ".css", ".gif", ".webp", "/digitalassets/", "/v/", "metrics", "logging")):
                return
            if not any(s in url for s in ("/shop/", "fulfillment", "configure", "process", "buyflow", "/api/", "purchase", "bag")):
                return
            ct = resp.headers.get("content-type", "")
            text = resp.text() if ("json" in ct or "text" in ct) else ""
            zp = sorted(set(re.findall(r"\bZ[A-Z0-9]{5,8}LL/A\b", text)))
            pn = sorted(set(re.findall(r'"partNumber"\s*:\s*"([A-Z0-9/]+)"', text)))
            if not (zp or pn or "fulfillment" in url or "buyflow" in url or "configure" in url or "/bag" in url):
                return
            captured.append({
                "method": resp.request.method,
                "url": url[:300],
                "status": resp.status,
                "ct": ct,
                "len": len(text),
                "post": resp.request.post_data,
                "zparts": zp,
                "partNums": pn[:20],
                "snippet": text[:1000] if (zp or "/configure" in url or "buyflow" in url) else None,
            })
        except Exception:
            pass

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=args.headless)
        try:
            ctx = browser.new_context(user_agent=UA, viewport={"width": 1400, "height": 1100}, locale="en-US")
            Stealth().apply_stealth_sync(ctx)
            page = ctx.new_page()
            page.on("response", on_response)

            url = f"https://www.apple.com/shop/buy-mac/{args.family}"
            print(f"navigating to {url}", file=sys.stderr)
            page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(int(args.settle_seconds * 1000))

            # Force the React app to render below-the-fold content.
            for _ in range(4):
                page.evaluate("window.scrollBy(0, 800)")
                page.wait_for_timeout(800)
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(2000)

            # Try to click the second chip card (CONFIGURABLE) — it's typically the
            # one without a "$X,XXX" preconfigured anchor.
            print("scanning for chip selector cards…", file=sys.stderr)
            handles = page.evaluate(
                """() => {
                    const out = [];
                    document.querySelectorAll('[data-autom], [role="radio"], button, label, a').forEach((el, idx) => {
                        const text = (el.textContent || '').trim();
                        if (!text) return;
                        if (!/M[34] (Pro|Max|Ultra)|core CPU|core GPU|customize|continue|select/i.test(text)) return;
                        const r = el.getBoundingClientRect();
                        out.push({ idx, tag: el.tagName, role: el.getAttribute('role'), dataAutom: el.getAttribute('data-autom'), text: text.slice(0, 100), x: Math.round(r.x), y: Math.round(r.y), visible: r.width > 0 && r.height > 0 });
                    });
                    return out;
                }"""
            )
            for h in handles[:30]:
                print(f"  cand: y={h.get('y')!s:5s} role={h.get('role')!s:6s} autom={h.get('dataAutom')!s:30s} text={h['text'][:70]!r}", file=sys.stderr)

            # Click 'Continue' if visible — this typically takes us into the configurator.
            try:
                page.locator('[data-autom="continueButton"]').first.click(timeout=8_000)
                print("clicked Continue", file=sys.stderr)
                page.wait_for_timeout(5_000)
            except Exception as exc:
                print(f"continue click failed: {exc}", file=sys.stderr)

            # Now look for "Add to Bag"
            try:
                page.locator('[data-autom="add-to-cart"]').first.click(timeout=8_000)
                print("clicked Add to Bag", file=sys.stderr)
                page.wait_for_timeout(8_000)
            except Exception as exc:
                print(f"add-to-bag click failed: {exc}", file=sys.stderr)

            # Final settle
            page.wait_for_timeout(3_000)
        finally:
            browser.close()

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(captured, default=str, indent=2))

    z_all = sorted({z for c in captured for z in c.get("zparts", [])})
    pn_all = sorted({p for c in captured for p in c.get("partNums", [])})
    print(f"\nCaptured {len(captured)} relevant XHRs", file=sys.stderr)
    print(f"Unique Z-prefixed: {len(z_all)} -> {z_all[:30]}", file=sys.stderr)
    print(f"All partNumbers: {len(pn_all)} -> {pn_all[:30]}", file=sys.stderr)
    print("\nXHRs that returned a partNumber:", file=sys.stderr)
    for c in captured:
        if c.get("zparts") or c.get("partNums"):
            print(f"  [{c['status']}] {c['method']} {c['url'][:160]} zparts={c['zparts']} partNums={c['partNums'][:5]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
