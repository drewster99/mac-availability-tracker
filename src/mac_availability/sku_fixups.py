"""Apply hand-curated SKU spec fixups for fields Apple obscures.

Each fixup rule patches a small set of columns on every ``skus`` row that
matches a ``where`` predicate. Used for cases like the MacBook Neo, where the
chip name and memory size are absent from the structured shop pages and only
appear as human-readable text on the marketing site (``apple.com/<product>/``).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

DEFAULT_FIXUPS_PATH = Path("data/sku_fixups.json")
ALLOWED_SET_COLUMNS = {"chip", "cpu_cores", "gpu_cores", "memory_gb", "storage_gb"}
ALLOWED_WHERE_COLUMNS = {
    "family", "locale", "part_number", "price_key",
    "chip", "cpu_cores", "gpu_cores", "memory_gb", "storage_gb",
}


def load_rules(path: Path = DEFAULT_FIXUPS_PATH) -> list[dict]:
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("Failed to read fixups file %s: %s", path, exc)
        return []
    return data.get("rules", []) if isinstance(data, dict) else []


def _build_where(where: dict) -> tuple[str, list]:
    clauses, params = [], []
    for column, value in where.items():
        if column not in ALLOWED_WHERE_COLUMNS:
            raise ValueError(f"Disallowed where column: {column}")
        clauses.append(f"{column} = ?")
        params.append(value)
    return (" AND ".join(clauses) if clauses else "1=1"), params


def apply_rules(conn: sqlite3.Connection, rules: Iterable[dict]) -> int:
    """Apply each rule's ``set`` to all rows matching its ``where`` predicate.

    Updates use ``COALESCE(?, existing)`` so we never overwrite a value Apple
    *did* provide; we only fill in nulls. Returns the total number of rows
    affected across all rules.
    """
    total = 0
    for rule in rules:
        sets = rule.get("set") or {}
        where = rule.get("where") or {}
        sets = {k: v for k, v in sets.items() if k in ALLOWED_SET_COLUMNS}
        if not sets:
            continue
        where_sql, where_params = _build_where(where)
        set_clauses = []
        set_params = []
        for column, value in sets.items():
            set_clauses.append(f"{column} = COALESCE({column}, ?)")
            set_params.append(value)
        sql = f"UPDATE skus SET {', '.join(set_clauses)} WHERE {where_sql}"
        cursor = conn.execute(sql, set_params + where_params)
        log.info("fixup '%s' updated %d rows", rule.get("name") or "(unnamed)", cursor.rowcount)
        total += cursor.rowcount
    conn.commit()
    return total
