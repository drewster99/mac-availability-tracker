"""Add an item to bag, open the bag, click pickup option, log every XHR.

The hypothesis: /shop/fulfillment-messages has been retired and renamed.
By driving the actual buy flow we'll see what URL Apple's frontend now
uses to fetch pickup availability.
"""
from __future__ import annotations
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

PART = "MGDR4LL/A"
ZIP = "94103"

captured: list[dict] = []
seen: set[tuple[str, str]] = set()

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
        viewport={"width": 1280, "height": 1200},
    )

    def on_response(r):
        ct = r.headers.get("content-type", "")
        if "json" not in ct.lower():
            return
        path = urlparse(r.url).path
        if path.startswith(("/v/", "/ac/", "/cm-static/", "/aos-shared/", "/cmsUtils/", "/js/")):
            return
        key = (r.request.method, path)
        if key in seen:
            return
        seen.add(key)
        try:
            body = r.text()
        except Exception:
            body = ""
        captured.append({"method": r.request.method, "status": r.status, "url": r.url, "body": body[:500], "len": len(body)})

    page = context.new_page()
    page.on("response", on_response)

    # Step 1: load buy page so cookies are set
    print("Step 1: visiting buy-mac landing page (set cookies, solve any challenges)")
    page.goto("https://www.apple.com/shop/buy-mac/macbook-pro", wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(3_000)

    # Step 2: add to bag via direct URL
    add_url = f"https://www.apple.com/shop/bag/add?product={PART}&fnode=home/shop_mac/family/macbook_pro"
    print(f"\nStep 2: GET {add_url}")
    resp = page.goto(add_url, wait_until="domcontentloaded", timeout=60_000)
    print(f"  bag-add HTTP {resp.status if resp else '?'}, current URL: {page.url}")
    page.wait_for_timeout(2_000)

    # Step 3: visit the bag page (likely redirected here)
    print("\nStep 3: visiting /shop/bag")
    page.goto("https://www.apple.com/shop/bag", wait_until="domcontentloaded", timeout=60_000)
    page.wait_for_timeout(3_000)
    print(f"  bag URL: {page.url}")

    # Step 4: scroll & screenshot
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(1_500)
    page.screenshot(path="/tmp/bag-page.png", full_page=True)
    print("  saved /tmp/bag-page.png")

    # Step 5: search for any pickup-related element
    print("\nStep 5: looking for pickup buttons/inputs in the bag page")
    found = page.evaluate(rf"""
        () => {{
            const out = [];
            const all = document.querySelectorAll('button, a, input, label');
            for (const el of all) {{
                const txt = (el.innerText || el.value || '').trim();
                if (/pickup|pick up|in[- ]store|check stock|availability|deliver|ship/i.test(txt) && txt.length < 80) {{
                    out.push({{
                        tag: el.tagName,
                        text: txt,
                        id: el.id || '',
                        cls: ((el.className || '')+'').slice(0, 80),
                        dataAutom: el.getAttribute('data-autom') || '',
                    }});
                }}
            }}
            return out.slice(0, 30);
        }}
    """)
    for f in found:
        print(f"  {f}")

    # Try clicking the pickup option
    print("\nStep 6: trying to click any pickup-related control")
    page.evaluate(r"""
        () => {
            const all = document.querySelectorAll('button, a, label, input[type=radio]');
            for (const el of all) {
                const txt = (el.innerText || el.value || '').trim().toLowerCase();
                if (/(pickup|pick up|in.store)/i.test(txt) && !/learn more|details/i.test(txt)) {
                    try { el.scrollIntoView({block:'center'}); el.click(); console.log('clicked', txt); return; } catch(e) {}
                }
            }
        }
    """)
    page.wait_for_timeout(3_000)

    # Try filling ZIP
    print("\nStep 7: filling any ZIP input")
    zip_filled = page.evaluate(rf"""
        () => {{
            const inputs = document.querySelectorAll('input[type=text], input[type=tel], input:not([type])');
            for (const inp of inputs) {{
                const ph = (inp.placeholder || '').toLowerCase();
                const id = (inp.id || '').toLowerCase();
                const name = (inp.name || '').toLowerCase();
                if (ph.includes('zip') || ph.includes('postal') || id.includes('zip') || id.includes('postal') || name.includes('zip') || name.includes('postal')) {{
                    inp.scrollIntoView({{block:'center'}});
                    inp.focus();
                    inp.value = '{ZIP}';
                    inp.dispatchEvent(new Event('input', {{bubbles: true}}));
                    inp.dispatchEvent(new Event('change', {{bubbles: true}}));
                    return {{found: true, id: inp.id, name: inp.name}};
                }}
            }}
            return null;
        }}
    """)
    print(f"  zip filled: {zip_filled}")
    if zip_filled:
        page.keyboard.press("Enter")
        page.wait_for_timeout(5_000)

    # final wait
    page.wait_for_timeout(3_000)
    browser.close()

print(f"\n=== {len(captured)} JSON XHRs across the whole flow ===")
for ev in captured:
    print(f"\n{ev['method']:<5} {ev['status']:<3} {ev['url']}")
    print(f"  bytes={ev['len']}  preview: {ev['body'][:300]!r}")

print("\n=== distinct paths ===")
for p in sorted({urlparse(ev['url']).path for ev in captured}):
    print(f"  {p}")
