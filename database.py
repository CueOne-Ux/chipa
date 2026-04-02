"""
Postgres helpers (psycopg3 / psycopg[binary]).

Schema
------
products
  product_id      TEXT  PK
  name            TEXT
  retailer        TEXT
  retailer_code   TEXT  ('c' | 'p' | 'w' | 'u')
  price           NUMERIC(10,2)
  original_price  NUMERIC(10,2)   -- NULL when not on promo
  deal_text       TEXT
  image_url       TEXT
  search_terms    TEXT[]           -- GIN-indexed; every term this product appeared under
  updated_at      TIMESTAMPTZ
"""

from __future__ import annotations

import os
import logging

import psycopg
from psycopg.rows import dict_row

logger = logging.getLogger(__name__)

DATABASE_URL: str = os.environ["DATABASE_URL"]

# ── DDL ───────────────────────────────────────────────────────────────────────

_CREATE = """
CREATE TABLE IF NOT EXISTS products (
    product_id      TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    retailer        TEXT NOT NULL,
    retailer_code   TEXT NOT NULL,
    price           NUMERIC(10,2),
    original_price  NUMERIC(10,2),
    deal_text       TEXT,
    image_url       TEXT,
    search_terms    TEXT[]   DEFAULT '{}',
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_products_search_terms
    ON products USING GIN(search_terms);
CREATE INDEX IF NOT EXISTS idx_products_deal
    ON products(original_price)
    WHERE original_price IS NOT NULL;
"""

# ── Upsert ────────────────────────────────────────────────────────────────────

_UPSERT = """
INSERT INTO products
    (product_id, name, retailer, retailer_code,
     price, original_price, deal_text, image_url,
     search_terms, updated_at)
VALUES
    (%(product_id)s, %(name)s, %(retailer)s, %(retailer_code)s,
     %(price)s, %(original_price)s, %(deal_text)s, %(image_url)s,
     ARRAY[%(search_term)s]::text[], NOW())
ON CONFLICT (product_id) DO UPDATE SET
    name           = EXCLUDED.name,
    retailer       = EXCLUDED.retailer,
    retailer_code  = EXCLUDED.retailer_code,
    price          = EXCLUDED.price,
    original_price = EXCLUDED.original_price,
    deal_text      = EXCLUDED.deal_text,
    image_url      = EXCLUDED.image_url,
    search_terms   = (
        SELECT ARRAY(
            SELECT DISTINCT unnest(products.search_terms || EXCLUDED.search_terms)
        )
    ),
    updated_at     = NOW();
"""

# ── Queries ───────────────────────────────────────────────────────────────────

_SEARCH = """
SELECT
    product_id,
    name,
    retailer,
    retailer_code,
    price::float,
    original_price::float,
    deal_text
FROM products
WHERE search_terms @> ARRAY[%s]::text[]
ORDER BY price NULLS LAST;
"""

_DEALS = """
SELECT
    name,
    retailer,
    retailer_code,
    price::float        AS now,
    original_price::float AS was,
    deal_text           AS deal
FROM products
WHERE original_price IS NOT NULL
  AND price IS NOT NULL
  AND price < original_price
ORDER BY (original_price - price) DESC
LIMIT %s;
"""

_STALE_CHECK = """
SELECT COUNT(*) AS n
FROM products
WHERE search_terms @> ARRAY[%s]::text[]
  AND updated_at > NOW() - INTERVAL '7 hours';
"""

# ── Public async API ──────────────────────────────────────────────────────────

async def init_db() -> None:
    """Create tables / indexes if they don't exist."""
    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
        await conn.execute(_CREATE)
        await conn.commit()
    logger.info("DB initialised")


async def upsert_products(products: list[dict], search_term: str) -> None:
    if not products:
        return
    async with await psycopg.AsyncConnection.connect(DATABASE_URL) as conn:
        for p in products:
            await conn.execute(_UPSERT, {**p, "search_term": search_term})
        await conn.commit()
    logger.info("Upserted %d products for term '%s'", len(products), search_term)


async def search_products(term: str) -> list[dict]:
    async with await psycopg.AsyncConnection.connect(
        DATABASE_URL, row_factory=dict_row
    ) as conn:
        cur = await conn.execute(_SEARCH, (term,))
        return await cur.fetchall()


async def get_deals(limit: int = 20) -> list[dict]:
    async with await psycopg.AsyncConnection.connect(
        DATABASE_URL, row_factory=dict_row
    ) as conn:
        cur = await conn.execute(_DEALS, (limit,))
        return await cur.fetchall()


async def is_cache_fresh(term: str) -> bool:
    """True if we have results for `term` updated within the last 7 hours."""
    async with await psycopg.AsyncConnection.connect(
        DATABASE_URL, row_factory=dict_row
    ) as conn:
        cur = await conn.execute(_STALE_CHECK, (term,))
        row = await cur.fetchone()
        return bool(row and row["n"] > 0)


# ── Sync API (used by APScheduler background jobs) ────────────────────────────

def upsert_products_sync(products: list[dict], search_term: str) -> None:
    if not products:
        return
    with psycopg.connect(DATABASE_URL) as conn:
        for p in products:
            conn.execute(_UPSERT, {**p, "search_term": search_term})
        conn.commit()
    logger.info("Upserted %d products for term '%s' (sync)", len(products), search_term)
