# mac-availability-tracker

Track Mac product availability and prices across every Apple retail store, worldwide.

## What it does

1. Crawls Apple's public shop pages to enumerate Mac product families and every preconfigured SKU per region (~991 SKUs across 24 country shops).
2. Fetches the global Apple Store list (~537 stores across ~25 country locales).
3. Polls Apple's `pickup-message` endpoint to record per-store, per-SKU pickup availability.
4. Renders an interactive Plotly heatmap of SKU × Store availability.

Build-to-order configurations are out of scope — only Apple's preconfigured SKUs (which carry stable part numbers) are tracked.

## Quick start

```bash
python3 -m venv .venv
.venv/bin/pip install -e .

# Build the catalog (stores + SKUs across all supported locales)
.venv/bin/python scripts/refresh_catalog.py --all-locales

# Sweep every Apple Store (one anchor per city; takes ~10–20 min)
.venv/bin/python scripts/poll_anchors.py

# Render the heatmap
.venv/bin/python scripts/render_heatmap.py
open data/heatmap.html
```

## Politeness

The polling client stays well under Apple's WAF thresholds:

- Default 5-second global gap between any two requests, regardless of region.
- Per-region gap layered on top.
- 541 / 429 circuit-breaker pauses every coroutine the moment a rate-limit is observed; honors `Retry-After` if Apple supplies one, otherwise grows the cool-off exponentially.
- Permanently widens the global gap on any rate-limit event and persists the learned value to `data/rate.json` so the next session starts at the safer pace.

A typical full sweep makes ~110 requests and produces zero rate-limit responses.

## Layout

```
src/mac_availability/
  client.py        — polite httpx client (global + per-region limiters, adaptive cool-off)
  stores.py        — /retail/storelist parser
  catalog.py       — buy-mac family/config crawler
  anchors.py       — picks one anchor postal per city
  availability.py  — pickup-message client + parser
  db.py            — SQLite schema, upserts, queries
  viz.py           — Plotly heatmap renderer
scripts/
  refresh_catalog.py    — daily catalog refresh
  poll_anchors.py       — every-store sweep via city anchors
  poll_availability.py  — watchlist-driven poll for specific SKUs / ZIPs
  render_heatmap.py     — render heatmap from latest snapshots
```

## Disclaimer

These endpoints are public but undocumented. Apple can change or restrict them at any time. The polling defaults are deliberately gentle; do not turn them down without thinking carefully.
