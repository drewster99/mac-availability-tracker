"""SQLite schema, upserts, and queries for catalog and availability data."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Optional

from .availability import AvailabilitySnapshot
from .catalog import MacConfig
from .stores import Store

_SCHEMA = """
CREATE TABLE IF NOT EXISTS stores (
    id           TEXT PRIMARY KEY,
    locale       TEXT NOT NULL,
    state_name   TEXT,
    state_code   TEXT,
    name         TEXT NOT NULL,
    slug         TEXT,
    telephone    TEXT,
    address1     TEXT,
    address2     TEXT,
    city         TEXT,
    postal_code  TEXT,
    latitude     REAL,
    longitude    REAL,
    raw_json     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS skus (
    part_number             TEXT NOT NULL,
    locale                  TEXT NOT NULL,
    family                  TEXT NOT NULL,
    price_key               TEXT,
    container_part_number   TEXT,
    is_coming_soon          INTEGER NOT NULL DEFAULT 0,
    raw_amount              REAL,
    formatted_amount        TEXT,
    currency                TEXT NOT NULL,
    dimensions_json         TEXT NOT NULL,
    product_configuration_json TEXT NOT NULL,
    chip                    TEXT,
    cpu_cores               INTEGER,
    gpu_cores               INTEGER,
    memory_gb               INTEGER,
    storage_gb              INTEGER,
    slug                    TEXT,
    last_seen_at            TEXT NOT NULL,
    PRIMARY KEY (part_number, locale)
);

CREATE TABLE IF NOT EXISTS family_bootstraps (
    family       TEXT NOT NULL,
    locale       TEXT NOT NULL,
    observed_at  TEXT NOT NULL,
    raw_json     TEXT NOT NULL,
    PRIMARY KEY (family, locale)
);

CREATE TABLE IF NOT EXISTS availability_snapshots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    observed_at     TEXT NOT NULL,
    region          TEXT NOT NULL,
    location        TEXT NOT NULL,
    canary_ok       INTEGER NOT NULL,
    raw_json        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS availability_rows (
    snapshot_id    INTEGER NOT NULL,
    store_id       TEXT NOT NULL,
    part_number    TEXT NOT NULL,
    pickup_display TEXT,
    pickup_quote   TEXT,
    PRIMARY KEY (snapshot_id, store_id, part_number),
    FOREIGN KEY (snapshot_id) REFERENCES availability_snapshots(id)
);

CREATE INDEX IF NOT EXISTS idx_avail_part_time
  ON availability_snapshots(observed_at DESC);

CREATE INDEX IF NOT EXISTS idx_avail_rows_part
  ON availability_rows(part_number, store_id);
"""


_STORE_COLUMN_MIGRATIONS = (
    ("latitude", "REAL"),
    ("longitude", "REAL"),
)
_SKU_COLUMN_MIGRATIONS = (
    ("chip", "TEXT"),
    ("cpu_cores", "INTEGER"),
    ("gpu_cores", "INTEGER"),
    ("memory_gb", "INTEGER"),
    ("storage_gb", "INTEGER"),
    ("slug", "TEXT"),
)


def _migrate_table_columns(
    conn: sqlite3.Connection, table: str, columns: tuple[tuple[str, str], ...]
) -> None:
    """Add the given columns to ``table`` if they don't already exist."""
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, sql_type in columns:
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")


def _migrate_stores_columns(conn: sqlite3.Connection) -> None:
    """Add columns to ``stores`` that may not exist on older databases."""
    _migrate_table_columns(conn, "stores", _STORE_COLUMN_MIGRATIONS)


def _migrate_skus_columns(conn: sqlite3.Connection) -> None:
    """Add columns to ``skus`` that may not exist on older databases."""
    _migrate_table_columns(conn, "skus", _SKU_COLUMN_MIGRATIONS)


@contextmanager
def connect(db_path: str | Path) -> Iterator[sqlite3.Connection]:
    """Yield a connection to the SQLite db, ensuring the schema is in place."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_SCHEMA)
        _migrate_stores_columns(conn)
        _migrate_skus_columns(conn)
        conn.commit()
        yield conn
    finally:
        conn.close()


def upsert_stores(conn: sqlite3.Connection, stores: Iterable[Store]) -> int:
    """Insert or replace store records, preserving cached lat/long if any.

    A fresh storelist crawl does not include geolocation, so we don't want to
    null out coordinates we already had — fall back to the existing row's
    latitude/longitude when the incoming Store has none.
    """
    existing_coords: dict[str, tuple[Optional[float], Optional[float]]] = {
        row["id"]: (row["latitude"], row["longitude"])
        for row in conn.execute("SELECT id, latitude, longitude FROM stores")
    }
    rows = []
    for s in stores:
        prev_lat, prev_lon = existing_coords.get(s.id, (None, None))
        rows.append(
            (
                s.id,
                s.locale,
                s.state_name,
                s.state_code,
                s.name,
                s.slug,
                s.telephone,
                s.address1,
                s.address2,
                s.city,
                s.postal_code,
                s.latitude if s.latitude is not None else prev_lat,
                s.longitude if s.longitude is not None else prev_lon,
                json.dumps(s.raw, separators=(",", ":")),
            )
        )
    cursor = conn.executemany(
        """
        INSERT OR REPLACE INTO stores
            (id, locale, state_name, state_code, name, slug, telephone,
             address1, address2, city, postal_code, latitude, longitude, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    return cursor.rowcount


def update_store_coords(
    conn: sqlite3.Connection, coords: dict[str, tuple[float, float]]
) -> int:
    """Set ``latitude``/``longitude`` for the given store IDs. Returns rows affected."""
    rows = [(lat, lon, store_id) for store_id, (lat, lon) in coords.items()]
    cursor = conn.executemany(
        "UPDATE stores SET latitude = ?, longitude = ? WHERE id = ?", rows
    )
    conn.commit()
    return cursor.rowcount


def update_sku_specs(
    conn: sqlite3.Connection,
    specs: dict[str, dict],
) -> int:
    """Update enrichment fields (memory_gb, storage_gb, slug) for the given part numbers.

    ``specs[part_number] = {"memory_gb": int|None, "storage_gb": int|None, "slug": str|None}``
    """
    rows = [
        (s.get("memory_gb"), s.get("storage_gb"), s.get("slug"), part)
        for part, s in specs.items()
    ]
    cursor = conn.executemany(
        """UPDATE skus
           SET memory_gb = COALESCE(?, memory_gb),
               storage_gb = COALESCE(?, storage_gb),
               slug = COALESCE(?, slug)
           WHERE part_number = ?""",
        rows,
    )
    conn.commit()
    return cursor.rowcount


def upsert_family_bootstrap(
    conn: sqlite3.Connection,
    *,
    family: str,
    locale: str,
    observed_at: str,
    raw_json: str,
) -> None:
    """Persist the raw PRODUCT_SELECTION_BOOTSTRAP JSON for a family + locale crawl."""
    conn.execute(
        """INSERT OR REPLACE INTO family_bootstraps
           (family, locale, observed_at, raw_json) VALUES (?, ?, ?, ?)""",
        (family, locale, observed_at, raw_json),
    )
    conn.commit()


def upsert_skus(
    conn: sqlite3.Connection, configs: Iterable[MacConfig], *, observed_at: str
) -> int:
    """Insert or replace SKU records, stamping ``last_seen_at`` for each."""
    rows = [
        (
            cfg.part_number,
            cfg.locale,
            cfg.family,
            cfg.price_key,
            cfg.container_part_number,
            int(cfg.is_coming_soon),
            cfg.raw_amount,
            cfg.formatted_amount,
            cfg.currency,
            json.dumps(cfg.dimensions, separators=(",", ":")),
            json.dumps(cfg.product_configuration, separators=(",", ":")),
            cfg.chip,
            cfg.cpu_cores,
            cfg.gpu_cores,
            cfg.memory_gb,
            cfg.storage_gb,
            cfg.slug,
            observed_at,
        )
        for cfg in configs
    ]
    cursor = conn.executemany(
        """
        INSERT OR REPLACE INTO skus
            (part_number, locale, family, price_key, container_part_number, is_coming_soon,
             raw_amount, formatted_amount, currency,
             dimensions_json, product_configuration_json,
             chip, cpu_cores, gpu_cores, memory_gb, storage_gb, slug,
             last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    return cursor.rowcount


def insert_snapshot(conn: sqlite3.Connection, snapshot: AvailabilitySnapshot) -> int:
    """Insert a snapshot and its per-(store, SKU) rows. Returns the new snapshot id."""
    cursor = conn.execute(
        """
        INSERT INTO availability_snapshots
            (observed_at, region, location, canary_ok, raw_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        (
            snapshot.observed_at,
            snapshot.region,
            snapshot.location,
            int(snapshot.canary_ok),
            json.dumps(snapshot.raw, separators=(",", ":")),
        ),
    )
    snapshot_id = cursor.lastrowid
    if snapshot_id is None:
        raise RuntimeError("Failed to obtain snapshot id after insert")
    rows = [
        (snapshot_id, store.store_id, part.part_number, part.pickup_display, part.pickup_quote)
        for store in snapshot.stores
        for part in store.parts
    ]
    if rows:
        conn.executemany(
            """
            INSERT OR REPLACE INTO availability_rows
                (snapshot_id, store_id, part_number, pickup_display, pickup_quote)
            VALUES (?, ?, ?, ?, ?)
            """,
            rows,
        )
    conn.commit()
    return snapshot_id


def latest_availability_per_part_store(
    conn: sqlite3.Connection,
) -> list[sqlite3.Row]:
    """For each (part_number, store_id), return the most recent pickup status.

    A given store can appear in many snapshot responses (when the store turns up
    as one of the nearby results for several different location queries). We pick
    the most recent observation per (part_number, store_id) by ranking on
    ``observed_at``.
    """
    return list(
        conn.execute(
            """
            WITH ranked AS (
                SELECT
                    r.part_number       AS part_number,
                    r.store_id          AS store_id,
                    r.pickup_display    AS pickup_display,
                    r.pickup_quote      AS pickup_quote,
                    s.observed_at       AS observed_at,
                    s.region            AS region,
                    ROW_NUMBER() OVER (
                        PARTITION BY r.part_number, r.store_id
                        ORDER BY s.observed_at DESC
                    ) AS rn
                FROM availability_rows r
                JOIN availability_snapshots s ON s.id = r.snapshot_id
            )
            SELECT
                part_number,
                store_id,
                pickup_display,
                pickup_quote,
                observed_at,
                region
            FROM ranked
            WHERE rn = 1
            ORDER BY part_number, store_id
            """
        )
    )


def all_stores_keyed_by_id(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """Map store_id → its store row for naming/labels in viz."""
    return {row["id"]: row for row in conn.execute("SELECT * FROM stores")}


def all_skus_keyed_by_part(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """Map part_number → one of its SKU rows for naming/labels in viz.

    The same part number can be sold in multiple locales (notably new products
    that share US SKUs in non-US regions); we just need one row for labelling.
    """
    out: dict[str, sqlite3.Row] = {}
    for row in conn.execute("SELECT * FROM skus"):
        out.setdefault(row["part_number"], row)
    return out
