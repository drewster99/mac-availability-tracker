"""Build a single self-contained HTML page for browsing Mac availability locally.

Reads the SQLite db, embeds all data inline, and writes ``data/browser.html`` —
opens directly in Safari with no server, no CORS, no fetch() needed.
"""
from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from mac_availability.catalog import LOCALE_TO_REGION

log = logging.getLogger("render_browser")

HTML_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Mac Availability</title>
<style>
  :root {
    --bg: #f5f5f7;
    --panel: #ffffff;
    --ink: #1d1d1f;
    --muted: #6e6e73;
    --line: #d2d2d7;
    --accent: #0071e3;
    --avail: #2ecc71;
    --inelig: #e3a936;
    --unavail: #c43c3c;
    --nodata: #b0b0b8;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; height: 100%; }
  body {
    font: 14px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    color: var(--ink); background: var(--bg);
  }
  header {
    background: #fff; border-bottom: 1px solid var(--line);
    padding: 16px 24px; position: sticky; top: 0; z-index: 10;
  }
  h1 { margin: 0 0 4px; font-size: 20px; font-weight: 600; }
  .meta { color: var(--muted); font-size: 12px; }
  .controls { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-top: 16px; }
  .controls fieldset {
    border: 1px solid var(--line); border-radius: 10px; padding: 10px 14px; margin: 0;
    background: #fff;
  }
  .controls legend { font-size: 12px; color: var(--muted); padding: 0 6px; text-transform: uppercase; letter-spacing: 0.04em; }
  .chip-row { display: flex; flex-wrap: wrap; gap: 6px; }
  .chip {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 4px 10px; border: 1px solid var(--line); border-radius: 999px;
    background: #fff; cursor: pointer; user-select: none; font-size: 13px;
  }
  .chip:hover { border-color: #b0b0b6; }
  .chip.active { background: var(--ink); color: #fff; border-color: var(--ink); }
  select { font: inherit; padding: 4px 6px; border-radius: 6px; border: 1px solid var(--line); background: #fff; }

  main { padding: 16px 24px 32px; display: grid; grid-template-columns: minmax(280px, 360px) 1fr; gap: 16px; }
  .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; overflow: hidden; }
  .panel-header { padding: 10px 14px; border-bottom: 1px solid var(--line); display: flex; justify-content: space-between; align-items: center; gap: 8px; }
  .panel-header h2 { margin: 0; font-size: 14px; font-weight: 600; }
  .panel-header .small { font-size: 12px; color: var(--muted); }
  .panel-body { max-height: calc(100vh - 280px); overflow: auto; }

  ul.sku-list { list-style: none; margin: 0; padding: 0; }
  ul.sku-list li {
    padding: 10px 14px; border-bottom: 1px solid var(--line);
    display: grid; grid-template-columns: 22px 1fr auto; gap: 10px; align-items: center; cursor: pointer;
  }
  ul.sku-list li:hover { background: #fafafb; }
  ul.sku-list li.selected { background: #eff6ff; }
  ul.sku-list .sku-title { font-weight: 500; }
  ul.sku-list .sku-meta { font-size: 12px; color: var(--muted); }
  ul.sku-list .sku-price { font-variant-numeric: tabular-nums; font-weight: 500; }

  .agg-table, .store-table { width: 100%; border-collapse: collapse; }
  .agg-table th, .agg-table td, .store-table th, .store-table td { padding: 8px 12px; text-align: left; border-bottom: 1px solid var(--line); vertical-align: top; }
  .agg-table th, .store-table th { background: #fafafb; font-weight: 600; font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; position: sticky; top: 0; }
  .agg-table td.num, .store-table td.num { font-variant-numeric: tabular-nums; text-align: right; }
  .pct-bar { display: inline-block; height: 6px; background: var(--line); border-radius: 3px; vertical-align: middle; width: 80px; overflow: hidden; }
  .pct-bar > div { height: 100%; background: var(--avail); }

  .pill { display: inline-block; font-size: 11px; padding: 2px 8px; border-radius: 999px; font-weight: 500; }
  .pill.available { background: rgba(46,204,113,0.18); color: #1c7d4a; }
  .pill.ineligible { background: rgba(227,169,54,0.2); color: #876310; }
  .pill.unavailable { background: rgba(196,60,60,0.18); color: #8c2828; }
  .pill.nodata { background: #ebebef; color: var(--muted); }

  .specs { padding: 12px 14px; background: #fafafb; border-top: 1px solid var(--line); display: none; }
  .specs.visible { display: block; }
  .specs dl { display: grid; grid-template-columns: max-content 1fr; gap: 4px 12px; margin: 0; font-size: 12px; }
  .specs dt { color: var(--muted); }

  .empty { padding: 32px 14px; text-align: center; color: var(--muted); }

  details summary { cursor: pointer; padding: 8px 14px; border-bottom: 1px solid var(--line); font-size: 13px; }
</style>
</head>
<body>
<header>
  <h1>Mac Availability</h1>
  <div class="meta" id="metaLine">Loading…</div>
  <div class="controls">
    <fieldset>
      <legend>Region</legend>
      <select id="regionSel"></select>
      <span class="small" id="regionStats" style="margin-left: 12px; font-size:12px; color:var(--muted)"></span>
    </fieldset>
    <fieldset>
      <legend>Mac family</legend>
      <div class="chip-row" id="familyChips"></div>
    </fieldset>
  </div>
</header>

<main>
  <section class="panel" aria-label="Models">
    <div class="panel-header">
      <h2>Models</h2>
      <div>
        <span class="small" id="modelsCount"></span>
        <button class="chip" id="selectAll" type="button">Select all</button>
        <button class="chip" id="selectNone" type="button">None</button>
      </div>
    </div>
    <div class="panel-body">
      <ul class="sku-list" id="skuList"></ul>
    </div>
  </section>

  <section class="panel" aria-label="Availability">
    <div class="panel-header">
      <h2 id="rightTitle">Availability summary</h2>
      <div class="small" id="rightSubtitle"></div>
    </div>
    <div class="panel-body" id="rightBody"></div>
  </section>
</main>

<script id="data" type="application/json">__DATA_JSON__</script>
<script>
(function() {
  const DATA = JSON.parse(document.getElementById('data').textContent);
  const state = {
    region: 'US',
    families: new Set(),
    selectedSkus: new Set(),
    expandedSpecsFor: null,
    activeSku: null, // for store-detail view
  };

  const $ = (id) => document.getElementById(id);

  // --- helpers ---------------------------------------------------------------
  function skuLabel(sku) {
    const d = sku.dimensions || {};
    const parts = [
      d['chassis-dimensionScreensize'],
      d['chassis-dimensionColor'],
      d['processor-dimensionChip'],
      d['processor-dimensionChip-cpuCoreCount-gpuCoreCount'],
      d['display-dimensionFinish'],
    ].filter(Boolean);
    if (parts.length) return parts.join(' · ');
    if (d['chassis-dimensionScreensize']) return d['chassis-dimensionScreensize'];
    return sku.part_number;
  }

  function skuFamilyLabel(family) {
    return ({
      'macbook-pro': 'MacBook Pro',
      'macbook-air': 'MacBook Air',
      'macbook-neo': 'MacBook Neo',
      'imac': 'iMac',
      'mac-mini': 'Mac mini',
      'mac-studio': 'Mac Studio',
      'studio-display': 'Studio Display',
      'studio-display-xdr': 'Studio Display XDR',
    })[family] || family;
  }

  // SKUs visible for a region: skus tagged with a locale that maps to the region
  function skusForRegion(region) {
    return DATA.skus.filter(s => DATA.localeToRegion[s.locale] === region);
  }
  function storesForRegion(region) {
    return DATA.stores.filter(s => DATA.localeToRegion[s.locale] === region);
  }

  // availability key: part_number+''+store_id  (sorted keys for fast lookups)
  function availabilityFor(partNumber, storeId) {
    return DATA.availabilityIndex[partNumber + '' + storeId] || null;
  }

  function pillClass(status) {
    if (status === 'available') return 'pill available';
    if (status === 'ineligible') return 'pill ineligible';
    if (status === 'unavailable') return 'pill unavailable';
    return 'pill nodata';
  }
  function statusLabel(status) {
    return status || 'no data';
  }

  // --- ui builders -----------------------------------------------------------
  function buildRegionSelector() {
    const select = $('regionSel');
    const regions = Object.keys(DATA.regionStoreCounts).sort();
    for (const r of regions) {
      const opt = document.createElement('option');
      opt.value = r;
      opt.textContent = `${r} (${DATA.regionStoreCounts[r]} stores)`;
      if (r === 'US') opt.selected = true;
      select.appendChild(opt);
    }
    select.addEventListener('change', () => {
      state.region = select.value;
      onRegionChange();
    });
  }

  function buildFamilyChips() {
    const wrap = $('familyChips');
    wrap.innerHTML = '';
    const families = Array.from(new Set(skusForRegion(state.region).map(s => s.family))).sort();
    for (const family of families) {
      state.families.add(family);
      const chip = document.createElement('button');
      chip.type = 'button';
      chip.className = 'chip active';
      chip.dataset.family = family;
      chip.textContent = `${skuFamilyLabel(family)} (${skusForRegion(state.region).filter(s => s.family === family).length})`;
      chip.addEventListener('click', () => {
        if (state.families.has(family)) state.families.delete(family);
        else state.families.add(family);
        chip.classList.toggle('active', state.families.has(family));
        renderModels();
      });
      wrap.appendChild(chip);
    }
  }

  function renderModels() {
    const ul = $('skuList');
    ul.innerHTML = '';
    const visible = skusForRegion(state.region).filter(s => state.families.has(s.family));
    visible.sort((a, b) => {
      if (a.family !== b.family) return a.family.localeCompare(b.family);
      return (a.raw_amount || 0) - (b.raw_amount || 0);
    });

    // Reconcile selected set: drop any not visible; default to all-on
    const visibleParts = new Set(visible.map(s => s.part_number));
    if (state.selectedSkus.size === 0) {
      visible.forEach(s => state.selectedSkus.add(s.part_number));
    } else {
      for (const p of Array.from(state.selectedSkus)) {
        if (!visibleParts.has(p)) state.selectedSkus.delete(p);
      }
    }

    for (const sku of visible) {
      const li = document.createElement('li');
      li.dataset.partNumber = sku.part_number;
      const isSel = state.selectedSkus.has(sku.part_number);
      const isActive = state.activeSku === sku.part_number;
      if (isActive) li.classList.add('selected');

      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = isSel;
      cb.addEventListener('click', e => e.stopPropagation());
      cb.addEventListener('change', () => {
        if (cb.checked) state.selectedSkus.add(sku.part_number);
        else state.selectedSkus.delete(sku.part_number);
        renderRight();
      });

      const mid = document.createElement('div');
      const t = document.createElement('div');
      t.className = 'sku-title';
      t.textContent = skuLabel(sku);
      const m = document.createElement('div');
      m.className = 'sku-meta';
      m.textContent = `${skuFamilyLabel(sku.family)} · ${sku.part_number}`;
      mid.appendChild(t);
      mid.appendChild(m);

      const price = document.createElement('div');
      price.className = 'sku-price';
      price.textContent = sku.formatted_amount || '';

      li.appendChild(cb);
      li.appendChild(mid);
      li.appendChild(price);

      // specs row
      const specs = document.createElement('div');
      specs.className = 'specs';
      if (state.expandedSpecsFor === sku.part_number) specs.classList.add('visible');
      const dl = document.createElement('dl');
      const dims = sku.dimensions || {};
      const rows = [
        ['Family', skuFamilyLabel(sku.family)],
        ['Part number', sku.part_number],
        ['Price', sku.formatted_amount + (sku.currency ? ' ' + sku.currency : '')],
        ['Locale', sku.locale],
      ];
      for (const [k, v] of Object.entries(dims)) {
        rows.push([k.replace(/-/g, ' '), v]);
      }
      for (const [k, v] of rows) {
        const dt = document.createElement('dt'); dt.textContent = k;
        const dd = document.createElement('dd'); dd.style.margin = 0; dd.textContent = v;
        dl.appendChild(dt); dl.appendChild(dd);
      }
      specs.appendChild(dl);

      li.addEventListener('click', () => {
        // toggle drill-down + specs expanded
        state.activeSku = state.activeSku === sku.part_number ? null : sku.part_number;
        state.expandedSpecsFor = state.activeSku;
        renderModels();
        renderRight();
      });

      ul.appendChild(li);
      ul.appendChild(specs);
    }
    $('modelsCount').textContent = `${state.selectedSkus.size} of ${visible.length} selected`;
    renderRight();
  }

  // --- right panel: aggregate or per-store -----------------------------------
  function renderRight() {
    const body = $('rightBody');
    body.innerHTML = '';
    const stores = storesForRegion(state.region);
    const visibleSkus = skusForRegion(state.region).filter(s => state.families.has(s.family) && state.selectedSkus.has(s.part_number));

    if (state.activeSku) {
      $('rightTitle').textContent = 'Per-store availability';
      const sku = DATA.skus.find(s => s.part_number === state.activeSku);
      $('rightSubtitle').textContent = sku ? `${skuFamilyLabel(sku.family)} · ${skuLabel(sku)} · ${sku.part_number}` : '';
      renderStoreView(body, sku, stores);
    } else {
      $('rightTitle').textContent = 'Availability summary';
      $('rightSubtitle').textContent = `${visibleSkus.length} models · ${stores.length} stores in ${state.region}`;
      renderAggregate(body, visibleSkus, stores);
    }
  }

  function aggregateForSku(partNumber, stores) {
    let avail = 0, inelig = 0, unavail = 0, nodata = 0;
    for (const st of stores) {
      const a = availabilityFor(partNumber, st.id);
      if (!a) nodata++;
      else if (a.d === 'available') avail++;
      else if (a.d === 'ineligible') inelig++;
      else if (a.d === 'unavailable') unavail++;
      else nodata++;
    }
    return { avail, inelig, unavail, nodata, total: stores.length };
  }

  function renderAggregate(body, skus, stores) {
    if (!skus.length) {
      body.innerHTML = '<div class="empty">Select at least one model on the left.</div>';
      return;
    }
    const tbl = document.createElement('table');
    tbl.className = 'agg-table';
    tbl.innerHTML = `
      <thead><tr>
        <th>Model</th><th>Part</th><th class="num">Price</th>
        <th class="num">Available</th><th class="num">% of stores</th>
        <th class="num">Ineligible</th><th class="num">Unavailable</th><th class="num">No data</th>
      </tr></thead><tbody></tbody>`;
    const tbody = tbl.querySelector('tbody');
    const rows = skus.map(sku => ({ sku, agg: aggregateForSku(sku.part_number, stores) }));
    rows.sort((a, b) => (b.agg.avail - a.agg.avail) || (a.sku.raw_amount - b.sku.raw_amount));
    for (const { sku, agg } of rows) {
      const pct = agg.total ? (100 * agg.avail / agg.total) : 0;
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>
          <div>${skuFamilyLabel(sku.family)}</div>
          <div style="font-size:12px;color:var(--muted)">${skuLabel(sku)}</div>
        </td>
        <td><code>${sku.part_number}</code></td>
        <td class="num">${sku.formatted_amount || ''}</td>
        <td class="num">${agg.avail}</td>
        <td class="num">${pct.toFixed(1)}%
          <span class="pct-bar"><div style="width:${Math.max(2, pct)}%"></div></span>
        </td>
        <td class="num">${agg.inelig}</td>
        <td class="num">${agg.unavail}</td>
        <td class="num">${agg.nodata}</td>
      `;
      tr.addEventListener('click', () => {
        state.activeSku = sku.part_number;
        state.expandedSpecsFor = sku.part_number;
        renderModels();
        renderRight();
      });
      tr.style.cursor = 'pointer';
      tbody.appendChild(tr);
    }
    body.appendChild(tbl);
  }

  function renderStoreView(body, sku, stores) {
    const back = document.createElement('div');
    back.style.padding = '10px 14px';
    back.innerHTML = `<button class="chip" id="backToAgg" type="button">← Back to summary</button>`;
    body.appendChild(back);
    document.getElementById('backToAgg').addEventListener('click', () => {
      state.activeSku = null;
      renderModels();
      renderRight();
    });

    const tbl = document.createElement('table');
    tbl.className = 'store-table';
    tbl.innerHTML = `
      <thead><tr>
        <th>Store</th><th>City</th><th>State</th><th>Status</th><th>Quote</th><th>Observed</th>
      </tr></thead><tbody></tbody>`;
    const tbody = tbl.querySelector('tbody');
    const rows = stores.map(st => ({ st, a: availabilityFor(sku.part_number, st.id) }));
    rows.sort((a, b) => {
      const order = { available: 0, ineligible: 1, unavailable: 2 };
      const av = a.a ? (order[a.a.d] ?? 4) : 5;
      const bv = b.a ? (order[b.a.d] ?? 4) : 5;
      if (av !== bv) return av - bv;
      return a.st.name.localeCompare(b.st.name);
    });
    for (const { st, a } of rows) {
      const status = a ? a.d : null;
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>${st.name}</td>
        <td>${st.city || ''}</td>
        <td>${st.state_code || ''}</td>
        <td><span class="${pillClass(status)}">${statusLabel(status)}</span></td>
        <td>${a && a.q ? a.q : ''}</td>
        <td style="font-size:12px;color:var(--muted)">${a ? a.t.replace('T', ' ').split('.')[0] : ''}</td>
      `;
      tbody.appendChild(tr);
    }
    body.appendChild(tbl);
  }

  function onRegionChange() {
    state.activeSku = null;
    state.expandedSpecsFor = null;
    state.families = new Set();
    state.selectedSkus = new Set();
    buildFamilyChips();
    renderModels();
    updateMeta();
  }

  function updateMeta() {
    const stores = storesForRegion(state.region);
    const skus = skusForRegion(state.region);
    $('regionStats').textContent = `${stores.length} stores · ${skus.length} configurations`;
    $('metaLine').textContent =
      `Snapshot generated ${DATA.generated_at} · ${DATA.totals.stores} stores worldwide · ${DATA.totals.skus} unique part numbers · ${DATA.totals.availability_observations} observations`;
  }

  // --- wiring ---------------------------------------------------------------
  buildRegionSelector();
  $('selectAll').addEventListener('click', () => {
    skusForRegion(state.region).filter(s => state.families.has(s.family)).forEach(s => state.selectedSkus.add(s.part_number));
    renderModels();
  });
  $('selectNone').addEventListener('click', () => { state.selectedSkus.clear(); renderModels(); });

  onRegionChange();
})();
</script>
</body>
</html>
"""


def build_dataset(db_path: Path) -> dict:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    stores = []
    for row in conn.execute(
        """
        SELECT id, locale, name, slug, city, state_code, state_name, postal_code,
               address1, latitude, longitude
        FROM stores
        WHERE locale NOT IN ('zh_CN','en_MO')
        """
    ):
        stores.append(dict(row))

    skus: list[dict] = []
    seen_parts: set[str] = set()
    for row in conn.execute(
        "SELECT * FROM skus ORDER BY family, raw_amount, part_number"
    ):
        if row["part_number"] in seen_parts:
            continue
        seen_parts.add(row["part_number"])
        skus.append(
            {
                "part_number": row["part_number"],
                "family": row["family"],
                "raw_amount": row["raw_amount"],
                "formatted_amount": row["formatted_amount"],
                "currency": row["currency"],
                "locale": row["locale"],
                "dimensions": json.loads(row["dimensions_json"]) if row["dimensions_json"] else {},
            }
        )

    availability_index: dict[str, dict] = {}
    for row in conn.execute(
        """
        WITH ranked AS (
            SELECT
                r.part_number, r.store_id, r.pickup_display, r.pickup_quote,
                s.observed_at,
                ROW_NUMBER() OVER (
                    PARTITION BY r.part_number, r.store_id
                    ORDER BY s.observed_at DESC
                ) AS rn
            FROM availability_rows r
            JOIN availability_snapshots s ON s.id = r.snapshot_id
        )
        SELECT part_number, store_id, pickup_display, pickup_quote, observed_at
        FROM ranked WHERE rn = 1
        """
    ):
        key = f"{row['part_number']}\x01{row['store_id']}"
        availability_index[key] = {
            "d": row["pickup_display"],
            "q": row["pickup_quote"],
            "t": row["observed_at"],
        }

    region_store_counts: dict[str, int] = {}
    for store in stores:
        region = LOCALE_TO_REGION.get(store["locale"])
        if region is None:
            continue
        region_store_counts[region] = region_store_counts.get(region, 0) + 1

    conn.close()

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "totals": {
            "stores": len(stores),
            "skus": len(skus),
            "availability_observations": len(availability_index),
        },
        "stores": stores,
        "skus": skus,
        "availabilityIndex": availability_index,
        "localeToRegion": dict(LOCALE_TO_REGION),
        "regionStoreCounts": region_store_counts,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/mac.sqlite")
    parser.add_argument("--out", default="data/browser.html")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    dataset = build_dataset(Path(args.db))
    raw = json.dumps(dataset, separators=(",", ":"), ensure_ascii=False)
    safe = raw.replace("</script", "<\\/script")
    html = HTML_TEMPLATE.replace("__DATA_JSON__", safe)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    log.info(
        "Wrote %s (%d KB) — open with `open %s`",
        out,
        len(html) // 1024,
        out,
    )


if __name__ == "__main__":
    main()
