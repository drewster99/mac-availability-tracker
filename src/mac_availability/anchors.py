"""Pick a small set of postal codes whose ``pickup-message`` responses cover every
Apple retail store in a locale.

The endpoint returns roughly the 12 closest stores to the supplied location.
Querying once per Apple Store is a lot of requests for little new information —
nearby stores heavily overlap. Instead, we pick one anchor postal code per city
in the stores table. For a single-store city that's the store's own postal; for
a multi-store city, it's the postal of any store in that city (the others will
show up in the nearby response naturally).

This keeps the total request count to roughly the number of distinct cities that
host Apple Stores (a few hundred worldwide, vs ~485 individual stores), which is
gentle enough to stay well clear of Apple's WAF.
"""
from __future__ import annotations

import logging
import sqlite3
from typing import Optional

log = logging.getLogger(__name__)


def pick_anchors(
    conn: sqlite3.Connection,
    *,
    include_locales: Optional[set[str]] = None,
    exclude_locales: Optional[set[str]] = None,
) -> list[sqlite3.Row]:
    """Return one store per (locale, city) as the anchor for that city.

    The returned rows are store records (with postal_code, city, locale, etc.).
    Cities without a postal code fall back to the city name as the ``location``
    parameter — Apple's endpoint accepts plain city names for markets where
    postal codes are not standard (notably the UAE).
    """
    clauses: list[str] = []
    params: list[str] = []
    if include_locales:
        marks = ",".join("?" * len(include_locales))
        clauses.append(f"locale IN ({marks})")
        params.extend(sorted(include_locales))
    if exclude_locales:
        marks = ",".join("?" * len(exclude_locales))
        clauses.append(f"locale NOT IN ({marks})")
        params.extend(sorted(exclude_locales))
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    sql = f"""
        WITH ranked AS (
            SELECT
                s.*,
                ROW_NUMBER() OVER (
                    PARTITION BY locale, COALESCE(city, '')
                    ORDER BY
                        CASE WHEN postal_code IS NULL OR postal_code = '' THEN 1 ELSE 0 END,
                        id
                ) AS rn
            FROM stores s
            {where}
        )
        SELECT * FROM ranked WHERE rn = 1 ORDER BY locale, city
    """
    return list(conn.execute(sql, params))
