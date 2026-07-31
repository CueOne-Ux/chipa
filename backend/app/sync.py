"""
Feed sync and canonical linking.

Two jobs:

  sync_feed()      pull the RapidAPI catalog into store_products
  build_links()    group store products into canonical products so the
                   basket engine can price one item across retailers

Deployment note
---------------
The previous version of this project ran its scheduler in-process on Render's
free tier. Render sleeps a free web service after ~15 minutes without inbound
traffic, which kills the scheduler with it — that was the actual root cause of
prices going stale, not the scraper logic.

The fix is to drive sync from OUTSIDE the web process: an external cron
(GitHub Actions, cron-job.org) POSTs to /api/admin/sync with ADMIN_TOKEN.
The in-process scheduler remains available via ENABLE_INTERNAL_SCHEDULER for
environments that don't sleep, but it is off by default and is NOT the
recommended path.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Optional, Sequence, Tuple

from . import db
from .config import settings
from .matching import score_pair
from .normalize import ProductAttrs, parse
from .rapidapi import (
    GroceryFeedClient,
    NotSubscribedError,
    QuotaExceededError,
    RapidAPIError,
    to_db_row,
)

logger = logging.getLogger(__name__)

BATCH_SIZE = 200


async def _start_run() -> int:
    row = await db.fetch_one(
        "INSERT INTO sync_runs (status) VALUES ('running') RETURNING id;"
    )
    return int(row["id"]) if row else 0


async def _finish_run(
    run_id: int,
    *,
    status: str,
    pages: int = 0,
    rows: int = 0,
    links: int = 0,
    error: Optional[str] = None,
) -> None:
    await db.execute(
        """
        UPDATE sync_runs
           SET finished_at = NOW(), status = %(status)s,
               pages_fetched = %(pages)s, rows_upserted = %(rows)s,
               links_created = %(links)s, error = %(error)s
         WHERE id = %(id)s;
        """,
        {
            "id": run_id,
            "status": status,
            "pages": pages,
            "rows": rows,
            "links": links,
            "error": error,
        },
    )


async def ensure_stores() -> None:
    """Refresh the stores table from the feed."""
    async with GroceryFeedClient() as client:
        stores = await client.stores()

    palette = {
        "woolworths": "#000000",
        "pnp": "#0057B8",
        "checkers": "#E4610F",
        "shoprite": "#E11B22",
        "spar": "#009639",
    }

    for store in stores:
        store_id = (store.get("id") or "").strip().lower()
        if not store_id:
            continue
        await db.execute(
            """
            INSERT INTO stores (id, name, website, icon, colour, source)
            VALUES (%(id)s, %(name)s, %(website)s, %(icon)s, %(colour)s, 'feed')
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                website = EXCLUDED.website,
                icon = EXCLUDED.icon,
                colour = COALESCE(stores.colour, EXCLUDED.colour);
            """,
            {
                "id": store_id,
                "name": store.get("name") or store_id.title(),
                "website": store.get("website"),
                "icon": store.get("icon"),
                "colour": palette.get(store_id),
            },
        )


async def sync_feed(
    *,
    store: Optional[str] = None,
    category: Optional[str] = None,
    max_pages: Optional[int] = None,
) -> Dict[str, object]:
    """
    Pull the catalog into store_products.

    Returns a summary dict. Safe to call repeatedly — everything upserts.
    """
    run_id = await _start_run()
    pages = 0
    written = 0
    buffer: List[dict] = []

    try:
        await ensure_stores()

        async with GroceryFeedClient() as client:
            page = 1
            ceiling = max_pages or settings.sync_max_pages

            while page <= ceiling:
                data = await client.products_page(
                    page=page, store=store, category=category
                )
                products = data.get("products") or []
                if not products:
                    break

                pages += 1
                for product in products:
                    row = to_db_row(product)
                    if not row["store_id"] or not row["store_product_id"]:
                        continue
                    buffer.append(row)

                if len(buffer) >= BATCH_SIZE:
                    written += await db.upsert_store_products(buffer)
                    buffer.clear()

                if not data.get("has_next"):
                    break
                page += 1

            requests_used = client.requests_made

        if buffer:
            written += await db.upsert_store_products(buffer)

        await _finish_run(run_id, status="done", pages=pages, rows=written)
        return {
            "status": "done",
            "pages_fetched": pages,
            "rows_upserted": written,
            "requests_used": requests_used,
        }

    except (NotSubscribedError, QuotaExceededError) as exc:
        await _finish_run(run_id, status="failed", pages=pages, rows=written, error=str(exc))
        logger.error("Sync halted: %s", exc)
        return {"status": "failed", "error": str(exc), "rows_upserted": written}

    except Exception as exc:  # noqa: BLE001
        await _finish_run(run_id, status="failed", pages=pages, rows=written, error=str(exc))
        logger.exception("Sync failed")
        return {"status": "failed", "error": str(exc), "rows_upserted": written}


# ─────────────────────────────────────────────────────────────────────────────
# Canonical linking
# ─────────────────────────────────────────────────────────────────────────────

UNLINKED_SQL = """
SELECT sp.*
FROM store_products sp
LEFT JOIN product_links pl ON pl.store_product_id = sp.id
WHERE pl.store_product_id IS NULL
  AND sp.core_text IS NOT NULL
  AND sp.core_text <> ''
ORDER BY sp.store_id, sp.core_text
LIMIT %(limit)s;
"""


def _attrs_from_row(row: dict) -> ProductAttrs:
    return parse(
        row.get("raw_product_name") or row.get("product_name") or "",
        known_brand=row.get("brand"),
        known_quantity=float(row["quantity"]) if row.get("quantity") is not None else None,
        known_unit=row.get("unit"),
        known_pack_count=row.get("pack_count"),
    )


async def build_links(limit: int = 2000) -> Dict[str, int]:
    """
    Group unlinked store products into canonical products.

    For each unlinked product we look for an existing canonical product that
    matches; if none does, the product becomes the seed of a new canonical
    product. Cross-store siblings then attach to it on subsequent passes.
    """
    unlinked = await db.fetch_all(UNLINKED_SQL, {"limit": limit})
    created = 0
    linked = 0
    review = 0

    for row in unlinked:
        attrs = _attrs_from_row(row)
        core = row.get("core_text") or attrs.core_text
        if not core:
            continue

        candidates = await db.fetch_all(
            """
            SELECT * FROM canonical_products
            WHERE core_text %% %(q)s OR core_text ILIKE %(like)s
            ORDER BY similarity(core_text, %(q)s) DESC
            LIMIT 25;
            """,
            {"q": core, "like": f"%{core}%"},
        )

        best_id: Optional[str] = None
        best_score = 0.0
        best_conf = "review"
        best_reasons: List[str] = []

        for candidate in candidates:
            cand_attrs = parse(candidate["display_name"])
            result = score_pair(
                attrs,
                cand_attrs,
                category_a=row.get("category"),
                category_b=candidate.get("category"),
            )
            if result.is_match and result.score > best_score:
                best_id = str(candidate["id"])
                best_score = result.score
                best_conf = result.confidence
                best_reasons = result.reasons

        if best_id is None:
            new_row = await db.fetch_one(
                """
                INSERT INTO canonical_products
                    (display_name, core_text, facets, brand,
                     total_quantity, base_unit, category, image_url)
                VALUES
                    (%(display_name)s, %(core_text)s, %(facets)s, %(brand)s,
                     %(total_quantity)s, %(base_unit)s, %(category)s, %(image_url)s)
                RETURNING id;
                """,
                {
                    "display_name": row.get("raw_product_name")
                    or row.get("product_name"),
                    "core_text": core,
                    "facets": __import__("json").dumps(attrs.facets),
                    "brand": attrs.brand,
                    "total_quantity": attrs.total_quantity,
                    "base_unit": attrs.base_unit,
                    "category": row.get("category"),
                    "image_url": row.get("image_url"),
                },
            )
            best_id = str(new_row["id"])
            best_score = 1.0
            best_conf = "auto"
            best_reasons = ["seed product"]
            created += 1

        await db.execute(
            """
            INSERT INTO product_links
                (canonical_id, store_product_id, score, confidence, reasons)
            VALUES (%(cid)s, %(spid)s, %(score)s, %(conf)s, %(reasons)s)
            ON CONFLICT (canonical_id, store_product_id) DO NOTHING;
            """,
            {
                "cid": best_id,
                "spid": str(row["id"]),
                "score": round(best_score, 4),
                "conf": best_conf,
                "reasons": best_reasons,
            },
        )
        linked += 1
        if best_conf == "review":
            review += 1

    return {
        "processed": len(unlinked),
        "canonical_created": created,
        "links_created": linked,
        "needs_review": review,
    }


async def full_refresh(max_pages: Optional[int] = None) -> Dict[str, object]:
    """Sync the feed, then rebuild links. This is what the cron calls."""
    feed = await sync_feed(max_pages=max_pages)
    links = await build_links()
    return {"feed": feed, "links": links}
