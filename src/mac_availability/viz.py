"""Render a Plotly heatmap of Mac pickup availability across SKUs and Apple Stores."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional

import pandas as pd
import plotly.graph_objects as go

from . import db

log = logging.getLogger(__name__)


def _label_for_sku(row) -> str:
    family = row["family"] if row else "?"
    sku = row["part_number"] if row else "?"
    dims = row["dimensions_json"] if row else None
    if dims:
        try:
            d = json.loads(dims)
            chip = d.get("processor-dimensionChip", "")
            cores = d.get("processor-dimensionChip-cpuCoreCount-gpuCoreCount", "")
            color = d.get("chassis-dimensionColor", "")
            screen = d.get("chassis-dimensionScreensize", "")
            descriptor = " ".join(
                token for token in [screen, color, chip, cores] if token
            )
            if descriptor:
                return f"{family} {descriptor} ({sku})"
        except Exception:
            pass
    return f"{family} ({sku})"


_PICKUP_TO_VALUE = {
    "available": 2,
    "ineligible": 1,
    "unavailable": 0,
}


def _label_for_store(row) -> str:
    if row is None:
        return "?"
    parts = [row["name"]]
    city = row["city"] or ""
    state = row["state_code"] or row["state_name"] or ""
    region = row["locale"] or ""
    geo = ", ".join(token for token in [city, state] if token)
    if geo:
        parts.append(f"({geo})")
    parts.append(f"[{row['id']} · {region}]")
    return " ".join(parts)


def render_heatmap(
    db_path: str | Path,
    *,
    out_path: str | Path,
    title: Optional[str] = None,
) -> Path:
    """Read the latest availability per (SKU, store) and render an interactive heatmap.

    Cell color encodes pickup status (green=available, yellow=ineligible,
    red=unavailable, grey=no data). Hover reveals the store's pickup quote and
    the time of the observation.
    """
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with db.connect(db_path) as conn:
        rows = db.latest_availability_per_part_store(conn)
        sku_by_part = db.all_skus_keyed_by_part(conn)
        store_by_id = db.all_stores_keyed_by_id(conn)

    if not rows:
        raise RuntimeError("No availability data found in db; poll first")

    records = []
    for row in rows:
        sku_row = sku_by_part.get(row["part_number"])
        store_row = store_by_id.get(row["store_id"])
        records.append(
            {
                "sku_label": _label_for_sku(sku_row) if sku_row else row["part_number"],
                "store_label": _label_for_store(store_row),
                "part_number": row["part_number"],
                "store_id": row["store_id"],
                "store_locale": store_row["locale"] if store_row else "?",
                "pickup_display": row["pickup_display"] or "unknown",
                "pickup_quote": row["pickup_quote"] or "",
                "value": _PICKUP_TO_VALUE.get(row["pickup_display"], 0),
                "observed_at": row["observed_at"],
            }
        )

    frame = pd.DataFrame.from_records(records)
    frame.sort_values(["store_locale", "store_label", "sku_label"], inplace=True)

    pivot = frame.pivot_table(
        index="sku_label",
        columns="store_label",
        values="value",
        aggfunc="max",
        fill_value=-1,
    )
    text_pivot = frame.pivot_table(
        index="sku_label",
        columns="store_label",
        values="pickup_quote",
        aggfunc="last",
        fill_value="",
    )
    status_pivot = frame.pivot_table(
        index="sku_label",
        columns="store_label",
        values="pickup_display",
        aggfunc="last",
        fill_value="—",
    )

    text_grid = [
        [
            f"{status_pivot.loc[sku, store]}<br>{text_pivot.loc[sku, store]}"
            for store in pivot.columns
        ]
        for sku in pivot.index
    ]

    figure = go.Figure(
        data=go.Heatmap(
            z=pivot.values,
            x=list(pivot.columns),
            y=list(pivot.index),
            colorscale=[
                [0.00, "#888888"],
                [0.25, "#c43c3c"],
                [0.50, "#e3a936"],
                [1.00, "#2ecc71"],
            ],
            zmin=-1,
            zmax=2,
            text=text_grid,
            hovertemplate="<b>%{y}</b><br>%{x}<br>%{text}<extra></extra>",
            colorbar={
                "title": "Pickup",
                "tickvals": [-1, 0, 1, 2],
                "ticktext": ["no data", "unavailable", "ineligible", "available"],
            },
        )
    )
    figure.update_layout(
        title=title or "Mac pickup availability — Apple Stores × preconfigured SKUs",
        xaxis_title="Apple Store",
        yaxis_title="Configuration",
        height=max(500, 24 * len(pivot.index) + 220),
        width=max(900, 18 * len(pivot.columns) + 320),
        margin={"l": 320, "r": 60, "t": 80, "b": 220},
        xaxis={"tickangle": -55},
    )
    figure.write_html(str(out_path), include_plotlyjs="cdn", full_html=True)
    log.info("Wrote heatmap to %s", out_path)
    return out_path
