"""Open per-config page, click 'Pick up from Store', enter ZIP, log XHRs."""
from __future__ import annotations

import sys
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

URL = (
    "https://www.apple.com/shop/buy-mac/macbook-pro/"
    "14-inch-spaceblack-standard-display-apple-m5-pro-chip-15-core-cpu-16-core-gpu-24gb-memory-1tb-storage"
)
ZIP = "94103"

captured: list[dict] = []

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
        ),
        viewport={"width": 1280, "height": 1200},
    )

    def on_response(r):
        ct = r.headers.get("content-type", "")
        url = r.url
        path = urlparse(url).path
        if path.startswith(("/v/", "/ac/", "/cm-static/", "/aos-shared/", "/cmsUtils/", "/js/")):
            return
        if "json" not in ct.lower():
            return
        try:
            body = r.text()
        except Exception:
            body = ""
        captured.append({"method": r.request.method, "status": r.status, "url": url, "body": body[:600], "len": len(body)})

    page = context.new_page()
    page.on("response", on_response)

    page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(2_000)
    for y in (0, 800, 1600, 2400):
        page.evaluate(f"window.scrollTo(0, {y})")
        page.wait_for_timeout(400)
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(500)

    print("Network captured during initial load (kept).")
    captured_before = len(captured)

    print("\nClicking first 'Pick up from Store' element ...")
    clicked = page.evaluate("""
        () => {
            const all = document.querySelectorAll('button, a, div[role=button], span[role=button]');
            for (const el of all) {
                const txt = (el.innerText || '').trim();
                if (/^pick up from store$/i.test(txt) || /^pickup$/i.test(txt) || /^check stock$/i.test(txt) || /pick.*near you/i.test(txt)) {
                    const rect = el.getBoundingClientRect();
                    return { matched: txt, x: rect.x|0, y: rect.y|0, tag: el.tagName, id: el.id, cls: (el.className+'').slice(0,80) };
                }
            }
            return null;
        }
    """)
    print(f"  candidate: {clicked}")
    if clicked:
        try:
            page.evaluate(r"""
                () => {
                    const all = document.querySelectorAll('button, a, div[role=button], span[role=button]');
                    for (const el of all) {
                        const txt = (el.innerText || '').trim();
                        if (/^pick up from store$/i.test(txt) || /^pickup$/i.test(txt) || /^check stock$/i.test(txt) || /pick.*near you/i.test(txt)) {
                            el.scrollIntoView({behavior:'instant', block:'center'});
                            el.click();
                            return;
                        }
                    }
                }
            """)
            page.wait_for_timeout(3_000)
            print("  click sent")
        except Exception as exc:
            print(f"  click failed: {exc}")

    # After click, look for ZIP input
    page.wait_for_timeout(2_000)
    zip_filled = page.evaluate(rf"""
        () => {{
            const inputs = document.querySelectorAll('input');
            for (const inp of inputs) {{
                const ph = (inp.placeholder || '').toLowerCase();
                const id = (inp.id || '').toLowerCase();
                const name = (inp.name || '').toLowerCase();
                if (ph.includes('zip') || ph.includes('postal') || id.includes('zip') || id.includes('postal') || name.includes('zip') || name.includes('postal')) {{
                    inp.focus();
                    inp.value = '{ZIP}';
                    inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                    inp.dispatchEvent(new Event('change', {{bubbles: true}}));
                    return {{found: true, id: inp.id, name: inp.name, placeholder: inp.placeholder}};
                }}
            }}
            return null;
        }}
    """)
    print(f"  zip input: {zip_filled}")
    if zip_filled:
        page.keyboard.press("Enter")
        page.wait_for_timeout(4_000)

    # Wait for any deferred XHR
    page.wait_for_timeout(3_000)
    browser.close()

print(f"\n=== {len(captured) - captured_before} new XHRs after pickup click + ZIP entry ===")
for ev in captured[captured_before:]:
    print(f"\n{ev['method']:<5} {ev['status']:<3} {ev['url']}")
    print(f"  bytes={ev['len']}  preview: {ev['body'][:300]!r}")

print(f"\n=== distinct URL paths captured AFTER initial page load ===")
new_paths = sorted({urlparse(ev['url']).path for ev in captured[captured_before:]})
for p in new_paths:
    print(f"  {p}")
