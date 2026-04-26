"""Quickly dump every clickable that mentions pickup/availability/store on the per-config page."""
from __future__ import annotations

from playwright.sync_api import sync_playwright

URL = (
    "https://www.apple.com/shop/buy-mac/macbook-pro/"
    "14-inch-spaceblack-standard-display-apple-m5-pro-chip-15-core-cpu-16-core-gpu-24gb-memory-1tb-storage"
)

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15"
        ),
        viewport={"width": 1280, "height": 1200},
    )
    page = context.new_page()
    page.goto(URL, wait_until="domcontentloaded", timeout=60_000)
    try:
        page.wait_for_load_state("load", timeout=10_000)
    except Exception:
        pass
    page.wait_for_timeout(2_000)

    for y in (0, 800, 1600, 2400, 3200):
        page.evaluate(f"window.scrollTo(0, {y})")
        page.wait_for_timeout(500)

    print("=== text-content elements mentioning pickup/availability/store ===")
    matches = page.evaluate(r"""
        () => {
            const wanted = /pickup|pick up|availability|in[- ]store|check stock|deliver/i;
            const out = [];
            const all = document.querySelectorAll('button, a, span, div, label');
            for (const el of all) {
                const text = (el.innerText || el.textContent || '').trim();
                if (text && text.length < 80 && wanted.test(text)) {
                    const tag = el.tagName.toLowerCase();
                    const id = el.id || '';
                    const cls = (el.className && el.className.toString().slice(0, 80)) || '';
                    const dataAutom = el.getAttribute('data-autom') || '';
                    out.push(`${tag}#${id} .${cls.split(' ').filter(Boolean)[0] || ''} [data-autom=${dataAutom}]: ${text}`);
                    if (out.length >= 30) break;
                }
            }
            return out;
        }
    """)
    for m in matches:
        print(" ", m)

    print("\n=== Add-to-Bag buttons ===")
    addbag = page.evaluate(r"""
        () => {
            const btns = document.querySelectorAll('button, a, input[type=submit]');
            const out = [];
            for (const el of btns) {
                const txt = (el.innerText || el.textContent || el.value || '').trim();
                if (/add to bag|add to cart|buy now/i.test(txt)) {
                    out.push({tag: el.tagName, id: el.id, class: (el.className+'').slice(0,80), dataAutom: el.getAttribute('data-autom') || '', text: txt});
                }
            }
            return out;
        }
    """)
    for m in addbag:
        print(" ", m)

    browser.close()
