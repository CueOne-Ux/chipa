"""
Database access layer (psycopg 3, async).

A single connection pool is created at startup. All queries live here so the
SQL surface is auditable in one place.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from .config import settings

logger = logging.getLogger(__name__)

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

_pool: Optional[AsyncConnectionPool] = None


async def init_pool() -> AsyncConnectionPool:
    """Create the pool and apply the schema. Idempotent."""
    global _pool
    if _pool is not None:
        return _pool
    if not settings.has_db:
        raise RuntimeError("DATABASE_URL is not set")

    _pool = AsyncConnectionPool(
        settings.database_url,
        min_size=1,
        max_size=8,
        open=False,
        # Neon can close an idle SSL connection while Render remains awake.
        # Validate each checkout so the pool replaces stale connections
        # instead of handing the first scheduled sync a broken socket.
        check=AsyncConnectionPool.check_connection,
        kwargs={"row_factory": dict_row},
    )
    await _pool.open(wait=True, timeout=30)
    await apply_schema()
    logger.info("Database pool ready")
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def pool() -> AsyncConnectionPool:
    if _pool is None:
        raise RuntimeError("Database pool not initialised — call init_pool() first")
    return _pool


async def apply_schema() -> None:
    sql = SCHEMA_PATH.read_text(encoding="utf-8")
    async with pool().connection() as conn:
        await conn.execute(sql)
        await conn.commit()
    logger.info("Schema applied")


async def fetch_all(sql: str, params: Sequence[Any] | Dict[str, Any] | None = None) -> List[dict]:
    async with pool().connection() as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchall()


async def fetch_one(sql: str, params: Sequence[Any] | Dict[str, Any] | None = None) -> Optional[dict]:
    async with pool().connection() as conn:
        cur = await conn.execute(sql, params)
        return await cur.fetchone()


async def execute(sql: str, params: Sequence[Any] | Dict[str, Any] | None = None) -> None:
    async with pool().connection() as conn:
        await conn.execute(sql, params)
        await conn.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Store products
# ─────────────────────────────────────────────────────────────────────────────

UPSERT_STORE_PRODUCT = """
INSERT INTO store_products (
    store_id, store_product_id, feed_id,
    product_name, raw_product_name, brand,
    price, currency, on_promotion, promotion_details,
    unit, quantity, pack_count, pack_quantity, pack_unit,
    core_text, facets, total_quantity, base_unit, unit_price, unit_price_label,
    category, sub_categories, in_stock, image_url, product_url,
    last_updated, synced_at
) VALUES (
    %(store_id)s, %(store_product_id)s, %(feed_id)s,
    %(product_name)s, %(raw_product_name)s, %(brand)s,
    %(price)s, %(currency)s, %(on_promotion)s, %(promotion_details)s,
    %(unit)s, %(quantity)s, %(pack_count)s, %(pack_quantity)s, %(pack_unit)s,
    %(core_text)s, %(facets)s, %(total_quantity)s, %(base_unit)s,
    %(unit_price)s, %(unit_price_label)s,
    %(category)s, %(sub_categories)s, %(in_stock)s, %(image_url)s, %(product_url)s,
    %(last_updated)s, NOW()
)
ON CONFLICT (store_id, store_product_id) DO UPDATE SET
    feed_id           = EXCLUDED.feed_id,
    product_name      = EXCLUDED.product_name,
    raw_product_name  = EXCLUDED.raw_product_name,
    brand             = EXCLUDED.brand,
    -- keep the previous price around so promo deltas can be computed
    was_price         = CASE
                          WHEN EXCLUDED.price <> store_products.price
                          THEN store_products.price
                          ELSE store_products.was_price
                        END,
    price             = EXCLUDED.price,
    currency          = EXCLUDED.currency,
    on_promotion      = EXCLUDED.on_promotion,
    promotion_details = EXCLUDED.promotion_details,
    unit              = EXCLUDED.unit,
    quantity          = EXCLUDED.quantity,
    pack_count        = EXCLUDED.pack_count,
    pack_quantity     = EXCLUDED.pack_quantity,
    pack_unit         = EXCLUDED.pack_unit,
    core_text         = EXCLUDED.core_text,
    facets            = EXCLUDED.facets,
    total_quantity    = EXCLUDED.total_quantity,
    base_unit         = EXCLUDED.base_unit,
    unit_price        = EXCLUDED.unit_price,
    unit_price_label  = EXCLUDED.unit_price_label,
    category          = EXCLUDED.category,
    sub_categories    = EXCLUDED.sub_categories,
    in_stock          = EXCLUDED.in_stock,
    image_url         = EXCLUDED.image_url,
    product_url       = EXCLUDED.product_url,
    last_updated      = EXCLUDED.last_updated,
    synced_at         = NOW()
RETURNING id, (xmax = 0) AS inserted, price;
"""

RECORD_PRICE = """
INSERT INTO price_history (store_product_id, price, on_promotion)
SELECT %(id)s, %(price)s, %(on_promotion)s
WHERE NOT EXISTS (
    SELECT 1 FROM price_history
    WHERE store_product_id = %(id)s
      AND price = %(price)s
      AND observed_at > NOW() - INTERVAL '20 hours'
);
"""


async def upsert_store_products(rows: List[dict]) -> int:
    """Upsert a batch of store products and append price history."""
    if not rows:
        return 0
    written = 0
    async with pool().connection() as conn:
        for row in rows:
            payload = dict(row)
            payload["facets"] = json.dumps(payload.get("facets") or {})
            cur = await conn.execute(UPSERT_STORE_PRODUCT, payload)
            result = await cur.fetchone()
            if result and payload.get("price") is not None:
                await conn.execute(
                    RECORD_PRICE,
                    {
                        "id": result["id"],
                        "price": payload["price"],
                        "on_promotion": payload.get("on_promotion", False),
                    },
                )
            written += 1
        await conn.commit()
    return written


# ─────────────────────────────────────────────────────────────────────────────
# Candidate retrieval (trigram prefilter)
# ─────────────────────────────────────────────────────────────────────────────

CANDIDATES_SQL = """
SELECT sp.*, s.name AS store_name, s.colour AS store_colour
FROM store_products sp
JOIN stores s ON s.id = sp.store_id
WHERE sp.in_stock
  AND (
        sp.core_text %% %(q)s
     OR sp.product_name ILIKE %(like)s
     OR sp.core_text ILIKE %(like)s
  )
ORDER BY similarity(sp.core_text, %(q)s) DESC
LIMIT %(limit)s;
"""


async def candidate_products(query_core: str, limit: int) -> List[dict]:
    """
    Cheap, wide net via pg_trgm + ILIKE. Precision comes afterwards from
    app.matching, which applies the taxonomy veto and structured scoring.
    """
    async with pool().connection() as conn:
        await conn.execute(
            "SELECT set_limit(CAST(%s AS real));", (settings.trigram_threshold,)
        )
        cur = await conn.execute(
            CANDIDATES_SQL,
            {"q": query_core, "like": f"%{query_core}%", "limit": limit},
        )
        return await cur.fetchall()


LEGACY_CANDIDATES_SQL = """
SELECT
    product_id AS id,
    CASE retailer_code
      WHEN 'c' THEN 'checkers'
      WHEN 'p' THEN 'pnp'
      WHEN 'w' THEN 'woolworths'
      ELSE LOWER(REGEXP_REPLACE(retailer, '[^a-zA-Z0-9]+', '', 'g'))
    END AS store_id,
    retailer AS store_name,
    CASE retailer_code
      WHEN 'c' THEN '#E4610F'
      WHEN 'p' THEN '#0057B8'
      WHEN 'w' THEN '#000000'
      ELSE '#666666'
    END AS store_colour,
    name AS product_name,
    name AS raw_product_name,
    price,
    original_price AS was_price,
    (original_price IS NOT NULL AND price < original_price) AS on_promotion,
    deal_text AS promotion_details,
    image_url,
    updated_at
FROM products
WHERE name ILIKE %(like)s
   OR similarity(name, %(q)s) >= 0.25
ORDER BY
    CASE WHEN name ILIKE %(like)s THEN 0 ELSE 1 END,
    similarity(name, %(q)s) DESC,
    updated_at DESC
LIMIT %(limit)s;
"""


async def legacy_candidate_products(query_core: str, limit: int) -> List[dict]:
    """Read v1's retained cache when the licensed v2 feed has no matches."""
    return await fetch_all(
        LEGACY_CANDIDATES_SQL,
        {"q": query_core, "like": f"%{query_core}%", "limit": limit},
    )


CANDIDATES_BY_FACET_SQL = """
SELECT sp.*, s.name AS store_name, s.colour AS store_colour
FROM store_products sp
JOIN stores s ON s.id = sp.store_id
WHERE sp.in_stock
  AND sp.store_id <> %(exclude_store)s
  AND (sp.core_text %% %(q)s OR sp.core_text ILIKE %(like)s)
ORDER BY similarity(sp.core_text, %(q)s) DESC
LIMIT %(limit)s;
"""


async def link_candidates(core_text: str, exclude_store: str, limit: int = 60) -> List[dict]:
    async with pool().connection() as conn:
        await conn.execute(
            "SELECT set_limit(CAST(%s AS real));",
            (settings.trigram_threshold,),
        )
        cur = await conn.execute(
            CANDIDATES_BY_FACET_SQL,
            {
                "q": core_text,
                "like": f"%{core_text}%",
                "exclude_store": exclude_store,
                "limit": limit,
            },
        )
        return await cur.fetchall()


# ─────────────────────────────────────────────────────────────────────────────
# Offers for a canonical product (used by the basket engine)
# ─────────────────────────────────────────────────────────────────────────────

OFFERS_FOR_CANONICAL = """
SELECT
    sp.id, sp.store_id, s.name AS store_name, s.colour AS store_colour,
    sp.product_name, sp.raw_product_name, sp.brand,
    sp.price, sp.was_price, sp.on_promotion, sp.promotion_details,
    sp.unit_price, sp.unit_price_label,
    sp.total_quantity, sp.base_unit,
    sp.image_url, sp.product_url, sp.in_stock,
    pl.score, pl.confidence
FROM product_links pl
JOIN store_products sp ON sp.id = pl.store_product_id
JOIN stores s ON s.id = sp.store_id
WHERE pl.canonical_id = %(canonical_id)s
  AND sp.price IS NOT NULL
ORDER BY sp.price ASC;
"""


async def offers_for_canonical(canonical_id: str) -> List[dict]:
    return await fetch_all(OFFERS_FOR_CANONICAL, {"canonical_id": canonical_id})


CATALOG_OFFERS_FOR_CANONICAL = """
SELECT
    co.id, co.store_label, co.product_name, co.price, co.was_price,
    co.promo_text, co.unit_price, co.unit_price_label, co.page,
    co.ocr_confidence, co.match_score, co.match_confidence,
    cu.filename, cu.valid_to, cu.uploaded_at
FROM catalog_offers co
JOIN catalog_uploads cu ON cu.id = co.upload_id
WHERE co.canonical_id = %(canonical_id)s
  AND co.price IS NOT NULL
  AND (cu.valid_to IS NULL OR cu.valid_to >= CURRENT_DATE)
ORDER BY co.price ASC;
"""


async def catalog_offers_for_canonical(canonical_id: str) -> List[dict]:
    return await fetch_all(
        CATALOG_OFFERS_FOR_CANONICAL, {"canonical_id": canonical_id}
    )
