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

REGION_NAMES: dict[str, str] = {
    "US": "United States",
    "CA": "Canada",
    "MX": "Mexico",
    "BR": "Brazil",
    "UK": "United Kingdom",
    "FR": "France",
    "DE": "Germany",
    "IT": "Italy",
    "ES": "Spain",
    "NL": "Netherlands",
    "AT": "Austria",
    "BE_FR": "Belgium (French)",
    "BE_NL": "Belgium (Dutch)",
    "CH_DE": "Switzerland (German)",
    "CH_FR": "Switzerland (French)",
    "SE": "Sweden",
    "TR": "Turkey",
    "AE": "United Arab Emirates",
    "IN": "India",
    "JP": "Japan",
    "KR": "South Korea",
    "HK": "Hong Kong",
    "TW": "Taiwan",
    "TH": "Thailand",
    "MY": "Malaysia",
    "SG": "Singapore",
    "AU": "Australia",
}

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
  .controls { display: grid; grid-template-columns: minmax(240px, 320px) 1fr; gap: 16px; margin-top: 16px; }
  .controls fieldset {
    border: 1px solid var(--line); border-radius: 10px; padding: 10px 14px; margin: 0;
    background: #fff;
  }
  .controls legend { font-size: 12px; color: var(--muted); padding: 0 6px; text-transform: uppercase; letter-spacing: 0.04em; }
  .facet-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; }
  .facet h3 { margin: 0 0 6px; font-size: 11px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; font-weight: 600; }
  .chip-row { display: flex; flex-wrap: wrap; gap: 6px; }
  .chip {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 3px 8px; border: 1px solid var(--line); border-radius: 999px;
    background: #fff; cursor: pointer; user-select: none; font-size: 12px;
  }
  .chip:hover { border-color: #b0b0b6; }
  .chip.active { background: var(--ink); color: #fff; border-color: var(--ink); }
  .chip.muted { color: var(--muted); }
  select { font: inherit; padding: 4px 6px; border-radius: 6px; border: 1px solid var(--line); background: #fff; }

  main { padding: 16px 24px 32px; display: grid; grid-template-columns: minmax(280px, 360px) 1fr; gap: 16px; }
  .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 12px; overflow: hidden; }
  .panel-header { padding: 10px 14px; border-bottom: 1px solid var(--line); display: flex; justify-content: space-between; align-items: center; gap: 8px; flex-wrap: wrap; }
  .panel-header h2 { margin: 0; font-size: 14px; font-weight: 600; }
  .panel-header .small { font-size: 12px; color: var(--muted); }
  .panel-body { max-height: calc(100vh - 320px); overflow: auto; }

  ul.sku-list { list-style: none; margin: 0; padding: 0; }
  ul.sku-list li {
    padding: 10px 14px; border-bottom: 1px solid var(--line);
    display: grid; grid-template-columns: 22px 1fr auto; gap: 10px; align-items: center; cursor: pointer;
  }
  ul.sku-list li:hover { background: #fafafb; }
  ul.sku-list li.selected { background: #eff6ff; }
  ul.sku-list .sku-title { font-weight: 500; }
  ul.sku-list .sku-meta { font-size: 12px; color: var(--muted); }
  ul.sku-list .sku-price { font-variant-numeric: tabular-nums; font-weight: 500; text-align: right; }

  .agg-table, .store-table { width: 100%; border-collapse: collapse; }
  .agg-table th, .agg-table td, .store-table th, .store-table td { padding: 8px 12px; text-align: left; border-bottom: 1px solid var(--line); vertical-align: top; }
  .agg-table th, .store-table th { background: #fafafb; font-weight: 600; font-size: 12px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; position: sticky; top: 0; }
  .agg-table th.num, .store-table th.num,
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

  .legend {
    display: flex; gap: 14px; flex-wrap: wrap;
    padding: 8px 14px; background: #fafafb; border-bottom: 1px solid var(--line); font-size: 12px;
  }
  .legend .pill { font-size: 11px; }
  .legend-row { display: inline-flex; align-items: center; gap: 6px; }

  .toolbar { padding: 8px 14px; border-bottom: 1px solid var(--line); display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .toolbar label { font-size: 12px; color: var(--muted); }

  .empty { padding: 32px 14px; text-align: center; color: var(--muted); }
  code { background: #f0f0f3; padding: 1px 4px; border-radius: 3px; font-size: 12px; }
</style>
</head>
<body>
<header>
  <h1>Mac Availability</h1>
  <div class="meta" id="metaLine">Loading…</div>
  <div class="controls">
    <fieldset>
      <legend>Region</legend>
      <select id="regionSel" style="width: 100%"></select>
      <div class="small" id="regionStats" style="margin-top: 6px; font-size:12px; color:var(--muted)"></div>
    </fieldset>
    <fieldset>
      <legend>Filters</legend>
      <div class="facet-grid">
        <div class="facet"><h3>Family</h3><div class="chip-row" id="facetFamily"></div></div>
        <div class="facet"><h3>Chip</h3><div class="chip-row" id="facetChip"></div></div>
        <div class="facet"><h3>Memory (GB)</h3><div class="chip-row" id="facetMemory"></div></div>
        <div class="facet"><h3>Storage</h3><div class="chip-row" id="facetStorage"></div></div>
        <div class="facet"><h3>CPU cores</h3><div class="chip-row" id="facetCpu"></div></div>
        <div class="facet"><h3>GPU cores</h3><div class="chip-row" id="facetGpu"></div></div>
      </div>
      <div style="margin-top: 8px"><button class="chip" id="resetFilters" type="button">Reset filters</button></div>
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
    <div class="legend">
      <span class="legend-row"><span class="pill available">available</span> in stock for pickup at this store</span>
      <span class="legend-row"><span class="pill ineligible">ineligible</span> store doesn't carry this SKU</span>
      <span class="legend-row"><span class="pill unavailable">unavailable</span> carried but currently out of stock</span>
      <span class="legend-row"><span class="pill nodata">no data</span> not observed in our last sweep</span>
    </div>
    <div class="toolbar" id="toolbarRollup">
      <label>Roll-up:</label>
      <select id="rollupSel">
        <option value="">— off (show every SKU) —</option>
        <option value="family">group by family</option>
        <option value="chip">group by chip</option>
        <option value="memory_gb">group by memory</option>
        <option value="storage_gb">group by storage</option>
        <option value="family,chip">group by family + chip</option>
        <option value="family,memory_gb">group by family + memory</option>
        <option value="chip,memory_gb">group by chip + memory</option>
        <option value="chip,memory_gb,storage_gb">group by chip + memory + storage</option>
      </select>
      <button class="chip" id="clearRollup" type="button" style="display:none">Clear roll-up</button>
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
    filters: {
      family: new Set(),
      chip: new Set(),
      memory_gb: new Set(),
      storage_gb: new Set(),
      cpu_cores: new Set(),
      gpu_cores: new Set(),
    },
    selectedSkus: new Set(),
    expandedSpecsFor: null,
    activeSku: null,
    rollup: '',
  };

  const $ = (id) => document.getElementById(id);

  // --- helpers ---------------------------------------------------------------
  function familyLabel(family) {
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
  function chipLabel(chip) {
    if (!chip) return '—';
    const map = {m4: 'M4', m4pro: 'M4 Pro', m4max: 'M4 Max',
                 m5: 'M5', m5pro: 'M5 Pro', m5max: 'M5 Max',
                 m3ultra: 'M3 Ultra', m2ultra: 'M2 Ultra',
                 a18pro: 'A18 Pro', a17pro: 'A17 Pro'};
    return map[chip] || chip;
  }
  function storageLabel(gb) {
    if (gb == null) return '—';
    if (gb >= 1000) return (gb / 1000) + 'TB';
    return gb + 'GB';
  }
  function memoryLabel(gb) { return gb == null ? '—' : gb + 'GB'; }

  function skuShortLabel(sku) {
    const dims = sku.dimensions || {};
    const parts = [];
    if (dims['chassis-dimensionScreensize']) parts.push(dims['chassis-dimensionScreensize']);
    if (dims['chassis-dimensionColor']) parts.push(dims['chassis-dimensionColor']);
    if (sku.chip) parts.push(chipLabel(sku.chip));
    if (sku.cpu_cores) parts.push(sku.cpu_cores + 'C/' + (sku.gpu_cores ?? '?') + 'G');
    if (sku.memory_gb) parts.push(memoryLabel(sku.memory_gb));
    if (sku.storage_gb) parts.push(storageLabel(sku.storage_gb));
    if (parts.length === 0) parts.push(sku.part_number);
    return parts.join(' · ');
  }

  function skusForRegion(region) {
    if (region === 'ALL') return DATA.skus.filter(s => DATA.localeToRegion[s.locale]);
    return DATA.skus.filter(s => DATA.localeToRegion[s.locale] === region);
  }
  function storesForRegion(region) {
    if (region === 'ALL') return DATA.stores.filter(s => DATA.localeToRegion[s.locale]);
    return DATA.stores.filter(s => DATA.localeToRegion[s.locale] === region);
  }

  function availabilityFor(partNumber, storeId) {
    return DATA.availabilityIndex[partNumber + '\x01' + storeId] || null;
  }

  function pillClass(status) {
    if (status === 'available') return 'pill available';
    if (status === 'ineligible') return 'pill ineligible';
    if (status === 'unavailable') return 'pill unavailable';
    return 'pill nodata';
  }
  function statusLabel(status) { return status || 'no data'; }

  // --- facet machinery -------------------------------------------------------
  function regionalSkus() { return skusForRegion(state.region); }

  function applyFilters(skus) {
    const f = state.filters;
    return skus.filter(s => {
      if (f.family.size && !f.family.has(s.family)) return false;
      if (f.chip.size && !f.chip.has(s.chip || '__none__')) return false;
      if (f.memory_gb.size && !f.memory_gb.has(String(s.memory_gb || '__none__'))) return false;
      if (f.storage_gb.size && !f.storage_gb.has(String(s.storage_gb || '__none__'))) return false;
      if (f.cpu_cores.size && !f.cpu_cores.has(String(s.cpu_cores || '__none__'))) return false;
      if (f.gpu_cores.size && !f.gpu_cores.has(String(s.gpu_cores || '__none__'))) return false;
      return true;
    });
  }

  function buildFacet(elementId, key, getter, formatter) {
    const wrap = $(elementId);
    wrap.innerHTML = '';
    const skus = regionalSkus();
    const counts = new Map();
    for (const s of skus) {
      const v = getter(s);
      const k = (v == null || v === '') ? '__none__' : String(v);
      counts.set(k, (counts.get(k) || 0) + 1);
    }
    const entries = Array.from(counts.entries());
    // Sort: numeric where possible, otherwise alpha; "__none__" last
    entries.sort((a, b) => {
      if (a[0] === '__none__') return 1;
      if (b[0] === '__none__') return -1;
      const an = Number(a[0]), bn = Number(b[0]);
      if (!Number.isNaN(an) && !Number.isNaN(bn)) return an - bn;
      return a[0].localeCompare(b[0]);
    });
    for (const [val, count] of entries) {
      const chip = document.createElement('button');
      chip.type = 'button';
      const isActive = state.filters[key].has(val);
      chip.className = 'chip' + (isActive ? ' active' : '') + (val === '__none__' ? ' muted' : '');
      const label = val === '__none__' ? 'unknown' : formatter(val);
      chip.textContent = `${label} (${count})`;
      chip.addEventListener('click', () => {
        if (state.filters[key].has(val)) state.filters[key].delete(val);
        else state.filters[key].add(val);
        state.activeSku = null;
        state.expandedSpecsFor = null;
        rebuildFacets(); renderModels();
      });
      wrap.appendChild(chip);
    }
  }

  function rebuildFacets() {
    buildFacet('facetFamily', 'family', s => s.family, v => familyLabel(v));
    buildFacet('facetChip', 'chip', s => s.chip, v => chipLabel(v));
    buildFacet('facetMemory', 'memory_gb', s => s.memory_gb, v => memoryLabel(Number(v)));
    buildFacet('facetStorage', 'storage_gb', s => s.storage_gb, v => storageLabel(Number(v)));
    buildFacet('facetCpu', 'cpu_cores', s => s.cpu_cores, v => v + ' core');
    buildFacet('facetGpu', 'gpu_cores', s => s.gpu_cores, v => v + ' core');
  }

  // --- model list ------------------------------------------------------------
  function renderModels() {
    const ul = $('skuList');
    ul.innerHTML = '';
    const visible = applyFilters(regionalSkus());
    visible.sort((a, b) => {
      if (a.family !== b.family) return a.family.localeCompare(b.family);
      return (a.raw_amount || 0) - (b.raw_amount || 0);
    });

    const visibleParts = new Set(visible.map(s => s.part_number));
    if (state.selectedSkus.size === 0) visible.forEach(s => state.selectedSkus.add(s.part_number));
    else for (const p of Array.from(state.selectedSkus)) if (!visibleParts.has(p)) state.selectedSkus.delete(p);

    for (const sku of visible) {
      const li = document.createElement('li');
      li.dataset.partNumber = sku.part_number;
      const isActive = state.activeSku === sku.part_number;
      if (isActive) li.classList.add('selected');
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = state.selectedSkus.has(sku.part_number);
      cb.addEventListener('click', e => e.stopPropagation());
      cb.addEventListener('change', () => {
        if (cb.checked) state.selectedSkus.add(sku.part_number); else state.selectedSkus.delete(sku.part_number);
        renderRight();
      });
      const mid = document.createElement('div');
      const t = document.createElement('div'); t.className = 'sku-title';
      t.textContent = skuShortLabel(sku);
      const m = document.createElement('div'); m.className = 'sku-meta';
      m.textContent = `${familyLabel(sku.family)} · ${sku.part_number}`;
      mid.appendChild(t); mid.appendChild(m);
      const price = document.createElement('div'); price.className = 'sku-price';
      price.textContent = sku.formatted_amount || '';

      li.appendChild(cb); li.appendChild(mid); li.appendChild(price);

      const specs = document.createElement('div'); specs.className = 'specs';
      if (state.expandedSpecsFor === sku.part_number) specs.classList.add('visible');
      const dl = document.createElement('dl');
      const rows = [
        ['Family', familyLabel(sku.family)],
        ['Part number', sku.part_number],
        ['Price', (sku.formatted_amount || '') + (sku.currency ? ' ' + sku.currency : '')],
        ['Chip', chipLabel(sku.chip)],
        ['CPU / GPU cores', (sku.cpu_cores ?? '?') + ' / ' + (sku.gpu_cores ?? '?')],
        ['Memory', memoryLabel(sku.memory_gb)],
        ['Storage', storageLabel(sku.storage_gb)],
        ['Locale', sku.locale],
      ];
      const dims = sku.dimensions || {};
      for (const [k, v] of Object.entries(dims)) rows.push(['dim/' + k, v]);
      for (const [k, v] of rows) {
        const dt = document.createElement('dt'); dt.textContent = k;
        const dd = document.createElement('dd'); dd.style.margin = 0; dd.textContent = v;
        dl.appendChild(dt); dl.appendChild(dd);
      }
      specs.appendChild(dl);

      li.addEventListener('click', () => {
        state.activeSku = state.activeSku === sku.part_number ? null : sku.part_number;
        state.expandedSpecsFor = state.activeSku;
        renderModels(); renderRight();
      });
      ul.appendChild(li); ul.appendChild(specs);
    }
    $('modelsCount').textContent = `${state.selectedSkus.size} of ${visible.length} selected`;
    renderRight();
  }

  // --- right panel -----------------------------------------------------------
  function renderRight() {
    const body = $('rightBody');
    body.innerHTML = '';
    const stores = storesForRegion(state.region);
    const visibleSkus = applyFilters(regionalSkus()).filter(s => state.selectedSkus.has(s.part_number));
    const toolbar = $('toolbarRollup');
    toolbar.style.display = state.activeSku ? 'none' : '';

    if (state.activeSku) {
      $('rightTitle').textContent = 'Per-store availability';
      const sku = DATA.skus.find(s => s.part_number === state.activeSku && DATA.localeToRegion[s.locale] === state.region) ||
                  DATA.skus.find(s => s.part_number === state.activeSku);
      $('rightSubtitle').textContent = sku ? `${familyLabel(sku.family)} · ${skuShortLabel(sku)} · ${sku.part_number}` : '';
      renderStoreView(body, sku, stores);
      return;
    }

    if (state.rollup) {
      const rollupLabel = state.rollup.replace(/,/g, ' + ').replace(/_gb/g, '');
      $('rightTitle').textContent = `Roll-up: ${rollupLabel}`;
      $('rightSubtitle').textContent = `${visibleSkus.length} models pooled across ${stores.length} stores in ${regionDisplayName(state.region)}`;
      renderRollup(body, visibleSkus, stores, state.rollup.split(','));
      return;
    }
    $('rightTitle').textContent = 'Availability summary';
    $('rightSubtitle').textContent = `${visibleSkus.length} models · ${stores.length} stores in ${regionDisplayName(state.region)}`;
    renderAggregate(body, visibleSkus, stores);
  }

  function aggregateForSku(partNumber, stores) {
    let avail=0, inelig=0, unavail=0, nodata=0;
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
    if (!skus.length) { body.innerHTML = '<div class="empty">Select at least one model on the left.</div>'; return; }
    const tbl = document.createElement('table');
    tbl.className = 'agg-table';
    tbl.innerHTML = `
      <thead><tr>
        <th>Model</th><th>Part</th>
        <th class="num">Price</th>
        <th class="num">Available</th><th class="num">% of stores</th>
        <th class="num">Ineligible</th><th class="num">Unavailable</th><th class="num">No data</th>
      </tr></thead><tbody></tbody>`;
    const tbody = tbl.querySelector('tbody');
    const rows = skus.map(sku => ({ sku, agg: aggregateForSku(sku.part_number, stores) }));
    rows.sort((a, b) => (b.agg.avail - a.agg.avail) || ((a.sku.raw_amount || 0) - (b.sku.raw_amount || 0)));
    for (const { sku, agg } of rows) {
      const pct = agg.total ? (100 * agg.avail / agg.total) : 0;
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td>
          <div>${familyLabel(sku.family)}</div>
          <div style="font-size:12px;color:var(--muted)">${skuShortLabel(sku)}</div>
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
      tr.style.cursor = 'pointer';
      tr.addEventListener('click', () => {
        state.activeSku = sku.part_number; state.expandedSpecsFor = sku.part_number;
        renderModels(); renderRight();
      });
      tbody.appendChild(tr);
    }
    body.appendChild(tbl);
  }

  function rollupKey(sku, dimensions) {
    return dimensions.map(d => {
      const v = sku[d];
      return v == null || v === '' ? 'unknown' : String(v);
    }).join(' · ');
  }

  function rollupKeyLabel(keyParts, dimensions) {
    return keyParts.split(' · ').map((v, i) => {
      const d = dimensions[i];
      if (v === 'unknown') return d + ': unknown';
      if (d === 'family') return familyLabel(v);
      if (d === 'chip') return chipLabel(v);
      if (d === 'memory_gb') return memoryLabel(Number(v));
      if (d === 'storage_gb') return storageLabel(Number(v));
      return v;
    }).join(' · ');
  }

  function renderRollup(body, skus, stores, dimensions) {
    if (!skus.length) { body.innerHTML = '<div class="empty">Select at least one model on the left.</div>'; return; }
    const groups = new Map();
    for (const sku of skus) {
      const key = rollupKey(sku, dimensions);
      if (!groups.has(key)) groups.set(key, { skus: [], avail: 0, inelig: 0, unavail: 0, nodata: 0 });
      const g = groups.get(key);
      g.skus.push(sku);
      const agg = aggregateForSku(sku.part_number, stores);
      g.avail += agg.avail; g.inelig += agg.inelig; g.unavail += agg.unavail; g.nodata += agg.nodata;
    }

    const banner = document.createElement('div');
    banner.style.padding = '10px 14px';
    banner.style.background = '#f5f7ff';
    banner.style.borderBottom = '1px solid var(--line)';
    banner.style.fontSize = '12px';
    banner.style.color = 'var(--ink)';
    banner.textContent =
      `${groups.size} group${groups.size === 1 ? '' : 's'} · ${skus.length} SKU${skus.length === 1 ? '' : 's'} · grouped by ${dimensions.join(' + ').replace(/_gb/g, '')}`;
    if (groups.size === 1) {
      banner.textContent += ' — try a different roll-up dimension to see more groups, or widen the filters.';
    }
    body.appendChild(banner);

    const tbl = document.createElement('table');
    tbl.className = 'agg-table';
    tbl.innerHTML = `
      <thead><tr>
        <th></th>
        <th>Group</th>
        <th class="num">SKUs</th>
        <th class="num">Available</th><th class="num">% available</th>
        <th class="num">Ineligible</th><th class="num">Unavailable</th><th class="num">No data</th>
      </tr></thead><tbody></tbody>`;
    const tbody = tbl.querySelector('tbody');
    const rows = Array.from(groups.entries()).map(([key, g]) => {
      const total = g.avail + g.inelig + g.unavail + g.nodata;
      const pct = total ? (100 * g.avail / total) : 0;
      return { key, g, total, pct };
    });
    rows.sort((a, b) => b.pct - a.pct);
    let idx = 0;
    for (const { key, g, total, pct } of rows) {
      const rowId = 'group-' + (idx++);
      const tr = document.createElement('tr');
      tr.style.cursor = 'pointer';
      tr.innerHTML = `
        <td style="width:24px;text-align:center;color:var(--muted)" data-toggle="${rowId}">▸</td>
        <td>${rollupKeyLabel(key, dimensions)}</td>
        <td class="num">${g.skus.length}</td>
        <td class="num">${g.avail}</td>
        <td class="num">${pct.toFixed(1)}%
          <span class="pct-bar"><div style="width:${Math.max(2, pct)}%"></div></span>
        </td>
        <td class="num">${g.inelig}</td>
        <td class="num">${g.unavail}</td>
        <td class="num">${g.nodata}</td>
      `;
      tbody.appendChild(tr);

      const expandTr = document.createElement('tr');
      expandTr.id = rowId;
      expandTr.style.display = 'none';
      const expandTd = document.createElement('td');
      expandTd.colSpan = 8;
      expandTd.style.padding = '0';
      expandTd.style.background = '#fafafb';
      const inner = document.createElement('table');
      inner.style.width = '100%';
      inner.style.margin = '0';
      inner.innerHTML = `<thead><tr>
          <th style="width:24px"></th>
          <th>Configuration</th><th>Part</th>
          <th class="num">Price</th>
          <th class="num">Available</th><th class="num">% of stores</th>
        </tr></thead><tbody></tbody>`;
      const innerBody = inner.querySelector('tbody');
      g.skus.sort((a, b) => (a.raw_amount || 0) - (b.raw_amount || 0));
      for (const sku of g.skus) {
        const skuAgg = aggregateForSku(sku.part_number, stores);
        const skuTotal = skuAgg.avail + skuAgg.inelig + skuAgg.unavail + skuAgg.nodata;
        const skuPct = skuTotal ? (100 * skuAgg.avail / skuTotal) : 0;
        const skuTr = document.createElement('tr');
        skuTr.style.cursor = 'pointer';
        skuTr.innerHTML = `
          <td></td>
          <td>${familyLabel(sku.family)} · ${skuShortLabel(sku)}</td>
          <td><code>${sku.part_number}</code></td>
          <td class="num">${sku.formatted_amount || ''}</td>
          <td class="num">${skuAgg.avail}</td>
          <td class="num">${skuPct.toFixed(1)}%
            <span class="pct-bar"><div style="width:${Math.max(2, skuPct)}%"></div></span>
          </td>`;
        skuTr.addEventListener('click', () => {
          state.activeSku = sku.part_number; state.expandedSpecsFor = sku.part_number;
          renderModels(); renderRight();
        });
        innerBody.appendChild(skuTr);
      }
      expandTd.appendChild(inner);
      expandTr.appendChild(expandTd);
      tbody.appendChild(expandTr);

      tr.addEventListener('click', () => {
        const target = document.getElementById(rowId);
        const isOpen = target.style.display !== 'none';
        target.style.display = isOpen ? 'none' : '';
        const tog = tr.querySelector('[data-toggle]');
        if (tog) tog.textContent = isOpen ? '▸' : '▾';
      });
    }
    body.appendChild(tbl);
  }

  function renderStoreView(body, sku, stores) {
    const back = document.createElement('div');
    back.style.padding = '10px 14px';
    back.innerHTML = `<button class="chip" id="backToAgg" type="button">← Back to summary</button>`;
    body.appendChild(back);
    document.getElementById('backToAgg').addEventListener('click', () => {
      state.activeSku = null; renderModels(); renderRight();
    });
    if (!sku) { body.appendChild(Object.assign(document.createElement('div'), { className: 'empty', textContent: 'SKU not found in this region.' })); return; }
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
      const stateCmp = (a.st.state_code || a.st.state_name || '').localeCompare(b.st.state_code || b.st.state_name || '');
      if (stateCmp !== 0) return stateCmp;
      const cityCmp = (a.st.city || '').localeCompare(b.st.city || '');
      if (cityCmp !== 0) return cityCmp;
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

  function buildRegionSelector() {
    const select = $('regionSel');
    const totalStores = Object.values(DATA.regionStoreCounts).reduce((a, b) => a + b, 0);
    const allOpt = document.createElement('option');
    allOpt.value = 'ALL';
    allOpt.textContent = `Worldwide — all regions (${totalStores} stores)`;
    select.appendChild(allOpt);
    const regions = Object.keys(DATA.regionStoreCounts).sort((a, b) => {
      const an = (DATA.regionNames || {})[a] || a;
      const bn = (DATA.regionNames || {})[b] || b;
      return an.localeCompare(bn);
    });
    for (const r of regions) {
      const opt = document.createElement('option');
      opt.value = r;
      const name = (DATA.regionNames || {})[r] || r;
      opt.textContent = `${r} — ${name} (${DATA.regionStoreCounts[r]} stores)`;
      if (r === 'US') opt.selected = true;
      select.appendChild(opt);
    }
    select.addEventListener('change', () => { state.region = select.value; onRegionChange(); });
  }

  function onRegionChange() {
    state.activeSku = null; state.expandedSpecsFor = null;
    Object.values(state.filters).forEach(s => s.clear());
    state.selectedSkus = new Set();
    rebuildFacets();
    renderModels();
    updateMeta();
  }

  function regionDisplayName(region) {
    if (region === 'ALL') return 'Worldwide';
    return (DATA.regionNames || {})[region] || region;
  }

  function updateMeta() {
    const stores = storesForRegion(state.region);
    const skus = skusForRegion(state.region);
    $('regionStats').textContent = `${stores.length} stores · ${skus.length} configurations · ${regionDisplayName(state.region)}`;
    $('metaLine').textContent =
      `Snapshot generated ${DATA.generated_at} · ${DATA.totals.stores} stores worldwide · ${DATA.totals.skus} unique part numbers · ${DATA.totals.availability_observations} observations`;
  }

  buildRegionSelector();
  $('selectAll').addEventListener('click', () => {
    applyFilters(regionalSkus()).forEach(s => state.selectedSkus.add(s.part_number));
    renderModels();
  });
  $('selectNone').addEventListener('click', () => { state.selectedSkus.clear(); renderModels(); });
  $('resetFilters').addEventListener('click', () => {
    Object.values(state.filters).forEach(s => s.clear());
    state.activeSku = null;
    state.expandedSpecsFor = null;
    rebuildFacets(); renderModels();
  });
  $('rollupSel').addEventListener('change', e => {
    state.rollup = e.target.value;
    $('clearRollup').style.display = state.rollup ? '' : 'none';
    renderRight();
  });
  $('clearRollup').addEventListener('click', () => {
    state.rollup = '';
    $('rollupSel').value = '';
    $('clearRollup').style.display = 'none';
    renderRight();
  });
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
                "chip": row["chip"],
                "cpu_cores": row["cpu_cores"],
                "gpu_cores": row["gpu_cores"],
                "memory_gb": row["memory_gb"],
                "storage_gb": row["storage_gb"],
                "slug": row["slug"],
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
        "regionNames": REGION_NAMES,
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
