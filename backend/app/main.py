"""
Chipa API.

Endpoints
---------
GET  /health                        liveness
GET  /api/stores                    retailers, including catalog-only ones
GET  /api/search?q=                 ranked search with taxonomy veto
GET  /api/compare?q=                one item priced across every retailer
POST /api/basket/compare            full cross-store basket comparison
POST /api/catalog/upload            upload a PDF specials leaflet (OCR)
GET  /api/catalog/uploads           list uploaded catalogs
GET  /api/deals                     current promotions
GET  /api/price-history/{id}        price over time for a store product
POST /api/admin/sync                trigger a feed sync (token protected)

The frontend is served from /.
"""

from __future__ import annotations

import json
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse

from . import basket as basket_engine
from . import db, ocr, sync
from .config import settings
from .matching import rank_search_results, score_pair
from .normalize import parse

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

FRONTEND = Path(__file__).resolve().parents[2] / "frontend" / "index.html"


@asynccontextmanager
async def lifespan(app: FastAPI):
    if settings.has_db:
        try:
            await db.init_pool()
        except Exception as exc:  # noqa: BLE001
            logger.error("Database unavailable at startup: %s", exc)
    else:
        logger.warning("DATABASE_URL not set — API will run in limited mode")

    scheduler = None
    if settings.enable_internal_scheduler and settings.has_db and settings.has_feed:
        # Off by default. On Render's free tier the process sleeps and takes
        # the scheduler with it — use the external cron instead.
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        scheduler = AsyncIOScheduler(timezone="Africa/Johannesburg")
        scheduler.add_job(sync.full_refresh, "interval", hours=12)
        scheduler.start()
        logger.info("Internal scheduler started (12h interval)")

    yield

    if scheduler:
        scheduler.shutdown(wait=False)
    await db.close_pool()


app = FastAPI(title="Chipa API", version="2.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins or ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _require_db() -> None:
    if not settings.has_db:
        raise HTTPException(503, "DATABASE_URL is not configured")


async def _admin(x_admin_token: Optional[str] = Header(None)) -> None:
    if not settings.admin_token:
        raise HTTPException(503, "ADMIN_TOKEN is not configured")
    if x_admin_token != settings.admin_token:
        raise HTTPException(401, "Invalid admin token")


# ─────────────────────────────────────────────────────────────────────────────
# Health & stores
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "database": settings.has_db,
        "feed": settings.has_feed,
        "time": datetime.utcnow().isoformat(),
    }


@app.get("/api/stores")
async def stores() -> List[dict]:
    _require_db()
    return await db.fetch_all(
        "SELECT id, name, website, icon, colour, source, active "
        "FROM stores WHERE active ORDER BY source, name;"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Search
# ─────────────────────────────────────────────────────────────────────────────

async def _search_impl(
    q: str,
    limit: int = 40,
    stores_filter: Optional[str] = None,
    include_catalog: bool = True,
) -> dict:
    """
    Ranked search — plain function.

    Kept separate from the route handler so other endpoints (compare,
    basket) can call it directly. Calling a FastAPI handler from Python
    leaves its `Query(...)` defaults unresolved, which is a subtle and
    nasty class of bug.

    Candidates come from a cheap pg_trgm prefilter; precision comes from
    app.matching, which applies the taxonomy veto. A query for
    "chicken fillet" will never return beef.
    """
    _require_db()

    parsed = parse(q)
    core = parsed.core_text or parsed.normalised

    rows = await db.candidate_products(core, settings.candidate_limit)

    if stores_filter:
        wanted = {s.strip().lower() for s in stores_filter.split(",") if s.strip()}
        rows = [r for r in rows if r["store_id"] in wanted]

    ranked = rank_search_results(q, rows, limit=limit)

    results = []
    for scored in ranked:
        row = scored.product
        results.append(
            {
                "id": str(row["id"]),
                "store_id": row["store_id"],
                "store_name": row.get("store_name"),
                "store_colour": row.get("store_colour"),
                "product_name": row.get("product_name"),
                "raw_product_name": row.get("raw_product_name"),
                "brand": row.get("brand"),
                "price": float(row["price"]) if row.get("price") is not None else None,
                "was_price": float(row["was_price"]) if row.get("was_price") is not None else None,
                "on_promotion": row.get("on_promotion"),
                "promotion_details": row.get("promotion_details"),
                "unit_price": float(row["unit_price"]) if row.get("unit_price") is not None else None,
                "unit_price_label": row.get("unit_price_label"),
                "image_url": row.get("image_url"),
                "product_url": row.get("product_url"),
                "category": row.get("category"),
                "relevance": round(scored.score, 3),
                "confidence": scored.confidence,
                "notes": scored.reasons,
                "source": "feed",
            }
        )

    if include_catalog:
        catalog_rows = await db.fetch_all(
            """
            SELECT co.*, cu.filename, cu.valid_to
            FROM catalog_offers co
            JOIN catalog_uploads cu ON cu.id = co.upload_id
            WHERE (co.core_text %% %(q)s OR co.product_name ILIKE %(like)s)
              AND (cu.valid_to IS NULL OR cu.valid_to >= CURRENT_DATE)
            LIMIT 60;
            """,
            {"q": core, "like": f"%{core}%"},
        )
        for scored in rank_search_results(
            q,
            [
                {**r, "raw_product_name": r["product_name"], "brand": None}
                for r in catalog_rows
            ],
            limit=20,
        ):
            row = scored.product
            results.append(
                {
                    "id": str(row["id"]),
                    "store_id": f"catalog:{row['store_label']}",
                    "store_name": row["store_label"],
                    "store_colour": "#6B4EFF",
                    "product_name": row["product_name"],
                    "price": float(row["price"]) if row.get("price") is not None else None,
                    "was_price": float(row["was_price"]) if row.get("was_price") is not None else None,
                    "on_promotion": True,
                    "promotion_details": row.get("promo_text"),
                    "unit_price": float(row["unit_price"]) if row.get("unit_price") is not None else None,
                    "unit_price_label": row.get("unit_price_label"),
                    "relevance": round(scored.score, 3),
                    "confidence": scored.confidence,
                    "notes": ["from uploaded catalog"],
                    "source": "catalog",
                    "ocr_confidence": row.get("ocr_confidence"),
                    "valid_to": row.get("valid_to").isoformat() if row.get("valid_to") else None,
                }
            )

    results.sort(key=lambda r: r["relevance"], reverse=True)
    return {"query": q, "parsed": {"facets": parsed.facets, "core": core}, "results": results[:limit]}


@app.get("/api/search")
async def search(
    q: str = Query(..., min_length=1, max_length=120),
    limit: int = Query(40, ge=1, le=200),
    stores_filter: Optional[str] = Query(None, alias="stores"),
    include_catalog: bool = Query(True),
) -> dict:
    return await _search_impl(
        q, limit=limit, stores_filter=stores_filter, include_catalog=include_catalog
    )


async def _compare_impl(q: str) -> dict:
    """
    Price one item across every retailer — the single-item comparison view.

    Groups the best match per store, so the shopper sees one row per shop
    rather than a wall of near-duplicates.
    """
    _require_db()
    data = await _search_impl(q, limit=120)

    best_per_store: Dict[str, dict] = {}
    for row in data["results"]:
        if row["price"] is None:
            continue
        store = row["store_id"]
        current = best_per_store.get(store)
        if current is None or row["relevance"] > current["relevance"]:
            best_per_store[store] = row

    offers = sorted(best_per_store.values(), key=lambda r: r["price"])
    cheapest = offers[0] if offers else None
    dearest = offers[-1] if offers else None

    saving = None
    saving_pct = None
    if cheapest and dearest and dearest["price"] > cheapest["price"]:
        saving = round(dearest["price"] - cheapest["price"], 2)
        saving_pct = round(saving / dearest["price"] * 100, 1)

    best_unit = None
    priced = [o for o in offers if o.get("unit_price")]
    if priced:
        best_unit = min(priced, key=lambda o: o["unit_price"])["store_id"]

    return {
        "query": q,
        "offers": offers,
        "cheapest_store": cheapest["store_id"] if cheapest else None,
        "best_unit_price_store": best_unit,
        "saving": saving,
        "saving_pct": saving_pct,
    }


@app.get("/api/compare")
async def compare(q: str = Query(..., min_length=1)) -> dict:
    return await _compare_impl(q)


# ─────────────────────────────────────────────────────────────────────────────
# Basket
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/api/basket/compare")
async def basket_compare(payload: Dict[str, Any]) -> dict:
    """
    Compare a whole basket across retailers.

    Request:
        {
          "items": [{"query": "milk 2l", "quantity": 1, "pinned_store": null}],
          "distances": {"checkers": 4.1, "pnp": 2.1},
          "max_stores": 3
        }
    """
    _require_db()

    raw_items = payload.get("items") or []
    if not raw_items:
        raise HTTPException(400, "No items supplied")

    distances = {
        str(k).lower(): float(v)
        for k, v in (payload.get("distances") or {}).items()
    }
    max_stores = int(payload.get("max_stores") or 3)

    items: List[basket_engine.BasketItem] = []

    for index, raw in enumerate(raw_items):
        query = (raw.get("query") or "").strip()
        if not query:
            continue
        quantity = max(1, int(raw.get("quantity") or 1))
        pinned = raw.get("pinned_store")

        found = await _compare_impl(query)
        offers: List[basket_engine.Offer] = []
        for row in found["offers"]:
            offers.append(
                basket_engine.Offer(
                    store_id=row["store_id"],
                    store_name=row["store_name"] or row["store_id"],
                    store_colour=row.get("store_colour"),
                    price=row["price"],
                    product_name=row.get("product_name") or query,
                    store_product_id=row.get("id"),
                    was_price=row.get("was_price"),
                    on_promotion=bool(row.get("on_promotion")),
                    promotion_details=row.get("promotion_details"),
                    unit_price=row.get("unit_price"),
                    unit_price_label=row.get("unit_price_label"),
                    image_url=row.get("image_url"),
                    product_url=row.get("product_url"),
                    match_score=row.get("relevance"),
                    match_confidence=row.get("confidence"),
                    source=row.get("source", "feed"),
                )
            )

        display = offers[0].product_name if offers else query
        low_confidence = bool(offers) and all(
            (o.match_score or 0) < 0.6 for o in offers
        )

        items.append(
            basket_engine.BasketItem(
                item_id=str(raw.get("id") or index),
                query_text=query,
                canonical_id=None,
                display_name=display,
                quantity=quantity,
                offers=offers,
                pinned_store=pinned,
                image_url=offers[0].image_url if offers else None,
                needs_review=low_confidence,
            )
        )

    totals = basket_engine.single_store_totals(items, distances=distances)
    recommendation = basket_engine.recommend(
        items, distances=distances, max_stores=max_stores
    )

    return {
        "items": [
            {
                "item_id": item.item_id,
                "query": item.query_text,
                "display_name": item.display_name,
                "quantity": item.quantity,
                "pinned_store": item.pinned_store,
                "needs_review": item.needs_review,
                "image_url": item.image_url,
                "cheapest": _offer_json(item.cheapest()),
                "best_unit_price": _offer_json(item.best_unit_price()),
                "offers": [_offer_json(o) for o in item.offers],
            }
            for item in items
        ],
        "store_totals": [
            {
                "store_id": t.store_id,
                "store_name": t.store_name,
                "store_colour": t.store_colour,
                "subtotal": t.subtotal,
                "items_found": t.items_found,
                "items_missing": t.items_missing,
                "missing_names": t.missing_names,
                "distance_km": t.distance_km,
                "fuel_cost": t.fuel_cost,
                "total_with_fuel": t.total_with_fuel,
                "source": t.source,
            }
            for t in totals
        ],
        "recommendation": {
            "verdict": recommendation.verdict,
            "headline": recommendation.headline,
            "detail": recommendation.detail,
            "saving_vs_single": recommendation.saving_vs_single,
            "worth_the_trip": recommendation.worth_the_trip,
            "split": (
                {
                    "stores": recommendation.split.stores,
                    "subtotal": recommendation.split.subtotal,
                    "fuel_cost": recommendation.split.fuel_cost,
                    "time_cost": recommendation.split.time_cost,
                    "total_cost": recommendation.split.total_cost,
                    "items_missing": recommendation.split.items_missing,
                    "assignments": [
                        {
                            "item_id": a.item_id,
                            "display_name": a.display_name,
                            "store_id": a.store_id,
                            "store_name": a.store_name,
                            "price": a.price,
                            "quantity": a.quantity,
                            "line_total": a.line_total,
                        }
                        for a in recommendation.split.assignments
                    ],
                }
                if recommendation.split
                else None
            ),
        },
    }


def _offer_json(offer: Optional[basket_engine.Offer]) -> Optional[dict]:
    if offer is None:
        return None
    return {
        "store_id": offer.store_id,
        "store_name": offer.store_name,
        "store_colour": offer.store_colour,
        "price": offer.price,
        "was_price": offer.was_price,
        "saving": offer.saving_vs_was,
        "product_name": offer.product_name,
        "unit_price": offer.unit_price,
        "unit_price_label": offer.unit_price_label,
        "on_promotion": offer.on_promotion,
        "promotion_details": offer.promotion_details,
        "image_url": offer.image_url,
        "product_url": offer.product_url,
        "match_score": offer.match_score,
        "match_confidence": offer.match_confidence,
        "source": offer.source,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Catalog upload (OCR)
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/api/catalog/upload")
async def upload_catalog(
    file: UploadFile = File(...),
    store_label: str = Form(...),
    valid_to: Optional[str] = Form(None),
) -> dict:
    """
    Upload a PDF specials leaflet.

    Runs the text-layer/OCR pipeline, extracts offers, and stores them so
    they compete in search and basket comparison alongside feed retailers.
    This is how Chipa covers Food Lover's Market, Boxer, independent SPARs
    and anyone else with no digital price feed.
    """
    _require_db()

    if not (file.filename or "").lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are supported")

    payload = await file.read()
    size_mb = len(payload) / (1024 * 1024)
    if size_mb > settings.max_upload_mb:
        raise HTTPException(
            413, f"File is {size_mb:.1f}MB — limit is {settings.max_upload_mb}MB"
        )

    upload_row = await db.fetch_one(
        """
        INSERT INTO catalog_uploads (filename, store_label, status, valid_to)
        VALUES (%(filename)s, %(label)s, 'processing', %(valid_to)s)
        RETURNING id;
        """,
        {
            "filename": file.filename,
            "label": store_label.strip(),
            "valid_to": valid_to or None,
        },
    )
    upload_id = str(upload_row["id"])

    try:
        result = ocr.process_pdf(payload)
    except Exception as exc:  # noqa: BLE001
        await db.execute(
            "UPDATE catalog_uploads SET status='failed', error=%(e)s WHERE id=%(id)s;",
            {"e": str(exc)[:500], "id": upload_id},
        )
        raise HTTPException(500, f"Could not process PDF: {exc}") from exc

    stored = 0
    for offer in result["offers"]:
        row = offer.to_row()
        await db.execute(
            """
            INSERT INTO catalog_offers (
                upload_id, store_label, page, product_name, raw_text,
                price, was_price, promo_text,
                core_text, facets, total_quantity, base_unit,
                unit_price, unit_price_label, ocr_confidence, valid_to
            ) VALUES (
                %(upload_id)s, %(store_label)s, %(page)s, %(product_name)s, %(raw_text)s,
                %(price)s, %(was_price)s, %(promo_text)s,
                %(core_text)s, %(facets)s, %(total_quantity)s, %(base_unit)s,
                %(unit_price)s, %(unit_price_label)s, %(ocr_confidence)s, %(valid_to)s
            );
            """,
            {
                **row,
                "facets": json.dumps(row["facets"]),
                "upload_id": upload_id,
                "store_label": store_label.strip(),
                "valid_to": valid_to or None,
            },
        )
        stored += 1

    await db.execute(
        """
        UPDATE catalog_uploads
           SET status='done', page_count=%(pages)s,
               extraction=%(extraction)s, offer_count=%(count)s
         WHERE id=%(id)s;
        """,
        {
            "pages": result["page_count"],
            "extraction": result["extraction"],
            "count": stored,
            "id": upload_id,
        },
    )

    return {
        "upload_id": upload_id,
        "store_label": store_label,
        "page_count": result["page_count"],
        "extraction": result["extraction"],
        "offers_found": stored,
        "detected_validity": {
            "from": result["valid_from_text"],
            "to": result["valid_to_text"],
        },
        "offers": [
            {
                "product_name": o.product_name,
                "price": o.price,
                "was_price": o.was_price,
                "promo_text": o.promo_text,
                "page": o.page,
                "confidence": o.confidence,
            }
            for o in result["offers"]
        ],
    }


@app.get("/api/catalog/uploads")
async def list_uploads() -> List[dict]:
    _require_db()
    rows = await db.fetch_all(
        """
        SELECT id, filename, store_label, uploaded_at, page_count,
               extraction, offer_count, status, valid_to, error
        FROM catalog_uploads
        ORDER BY uploaded_at DESC
        LIMIT 100;
        """
    )
    for row in rows:
        row["id"] = str(row["id"])
    return rows


@app.delete("/api/catalog/uploads/{upload_id}")
async def delete_upload(upload_id: str) -> dict:
    _require_db()
    await db.execute("DELETE FROM catalog_uploads WHERE id = %s;", (upload_id,))
    return {"deleted": upload_id}


# ─────────────────────────────────────────────────────────────────────────────
# Deals & history
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/api/deals")
async def deals(limit: int = Query(40, ge=1, le=200)) -> List[dict]:
    """
    Current promotions across all retailers.

    Note: unlike the previous build, this reads from synced data and is
    driven by an external cron, so it cannot silently go stale when the
    web process sleeps.
    """
    _require_db()
    rows = await db.fetch_all(
        """
        SELECT sp.id, sp.product_name, sp.raw_product_name, sp.brand,
               sp.price, sp.was_price, sp.promotion_details,
               sp.unit_price, sp.unit_price_label, sp.image_url, sp.product_url,
               sp.category, sp.synced_at,
               s.id AS store_id, s.name AS store_name, s.colour AS store_colour
        FROM store_products sp
        JOIN stores s ON s.id = sp.store_id
        WHERE sp.on_promotion
          AND sp.price IS NOT NULL
          AND sp.in_stock
        ORDER BY
          CASE WHEN sp.was_price IS NOT NULL AND sp.was_price > sp.price
               THEN (sp.was_price - sp.price) / sp.was_price ELSE 0 END DESC,
          sp.synced_at DESC
        LIMIT %(limit)s;
        """,
        {"limit": limit},
    )
    for row in rows:
        row["id"] = str(row["id"])
        if row.get("was_price") and row.get("price"):
            row["discount_pct"] = round(
                (float(row["was_price"]) - float(row["price"]))
                / float(row["was_price"])
                * 100,
                1,
            )
    return rows


SPECIALS_SQL = """
WITH ranked AS (
    SELECT
        sp.id, sp.store_id, sp.product_name, sp.raw_product_name, sp.brand,
        sp.price, sp.was_price, sp.promotion_details,
        sp.unit_price, sp.unit_price_label, sp.image_url, sp.product_url,
        sp.category, sp.synced_at,
        s.name AS store_name, s.colour AS store_colour,
        CASE
            WHEN sp.was_price IS NOT NULL AND sp.was_price > sp.price
            THEN (sp.was_price - sp.price) / sp.was_price
            ELSE 0
        END AS discount,
        ROW_NUMBER() OVER (
            PARTITION BY sp.store_id
            ORDER BY
                CASE
                    WHEN sp.was_price IS NOT NULL AND sp.was_price > sp.price
                    THEN (sp.was_price - sp.price) / sp.was_price
                    ELSE 0
                END DESC,
                sp.synced_at DESC
        ) AS rn
    FROM store_products sp
    JOIN stores s ON s.id = sp.store_id
    WHERE sp.on_promotion
      AND sp.price IS NOT NULL
      AND sp.in_stock
)
SELECT * FROM ranked WHERE rn <= %(per_store)s ORDER BY store_id, rn;
"""

CATALOG_SPECIALS_SQL = """
SELECT
    co.id, co.store_label, co.product_name, co.price, co.was_price,
    co.promo_text, co.unit_price, co.unit_price_label, co.ocr_confidence,
    cu.valid_to,
    ROW_NUMBER() OVER (
        PARTITION BY co.store_label
        ORDER BY
            CASE
                WHEN co.was_price IS NOT NULL AND co.was_price > co.price
                THEN (co.was_price - co.price) / co.was_price
                ELSE 0
            END DESC,
            co.created_at DESC
    ) AS rn
FROM catalog_offers co
JOIN catalog_uploads cu ON cu.id = co.upload_id
WHERE co.price IS NOT NULL
  AND (cu.valid_to IS NULL OR cu.valid_to >= CURRENT_DATE)
"""


@app.get("/api/specials")
async def specials(
    per_store: int = Query(5, ge=1, le=20),
    include_catalog: bool = Query(True),
) -> dict:
    """
    Featured specials, grouped per retailer.

    Powers the home-screen carousels: one row per shop, colour-coded to that
    retailer, showing its best current deals by discount depth.

    Uploaded PDF catalogues get their own row too, so a Food Lover's leaflet
    sits alongside the tracked retailers rather than being second-class.
    """
    _require_db()

    rows = await db.fetch_all(SPECIALS_SQL, {"per_store": per_store})

    groups: Dict[str, dict] = {}
    for row in rows:
        store_id = row["store_id"]
        group = groups.setdefault(
            store_id,
            {
                "store_id": store_id,
                "store_name": row.get("store_name") or store_id.title(),
                "store_colour": row.get("store_colour") or "#666666",
                "source": "feed",
                "items": [],
            },
        )
        price = float(row["price"])
        was = float(row["was_price"]) if row.get("was_price") is not None else None
        group["items"].append(
            {
                "id": str(row["id"]),
                "product_name": row.get("product_name"),
                "raw_product_name": row.get("raw_product_name"),
                "brand": row.get("brand"),
                "price": price,
                "was_price": was,
                "saving": round(was - price, 2) if was and was > price else None,
                "discount_pct": round(float(row["discount"]) * 100, 1)
                if row.get("discount")
                else None,
                "promotion_details": row.get("promotion_details"),
                "unit_price": float(row["unit_price"])
                if row.get("unit_price") is not None
                else None,
                "unit_price_label": row.get("unit_price_label"),
                "image_url": row.get("image_url"),
                "product_url": row.get("product_url"),
                "category": row.get("category"),
            }
        )

    if include_catalog:
        catalog_rows = await db.fetch_all(
            CATALOG_SPECIALS_SQL + " ORDER BY store_label, rn;", None
        )
        for row in catalog_rows:
            if int(row["rn"]) > per_store:
                continue
            label = row["store_label"]
            key = f"catalog:{label}"
            group = groups.setdefault(
                key,
                {
                    "store_id": key,
                    "store_name": label,
                    "store_colour": "#8B6BFF",
                    "source": "catalog",
                    "items": [],
                },
            )
            price = float(row["price"])
            was = float(row["was_price"]) if row.get("was_price") is not None else None
            group["items"].append(
                {
                    "id": str(row["id"]),
                    "product_name": row.get("product_name"),
                    "price": price,
                    "was_price": was,
                    "saving": round(was - price, 2) if was and was > price else None,
                    "discount_pct": round((was - price) / was * 100, 1)
                    if was and was > price
                    else None,
                    "promotion_details": row.get("promo_text"),
                    "unit_price": float(row["unit_price"])
                    if row.get("unit_price") is not None
                    else None,
                    "unit_price_label": row.get("unit_price_label"),
                    "ocr_confidence": row.get("ocr_confidence"),
                    "valid_to": row["valid_to"].isoformat()
                    if row.get("valid_to")
                    else None,
                }
            )

    # Shops with the deepest single discount lead — most interesting first.
    ordered = sorted(
        groups.values(),
        key=lambda g: max((i.get("discount_pct") or 0) for i in g["items"])
        if g["items"]
        else 0,
        reverse=True,
    )
    return {"per_store": per_store, "stores": ordered}


@app.get("/api/price-history/{store_product_id}")
async def price_history(store_product_id: str, days: int = Query(90, ge=1, le=730)) -> dict:
    """
    Price over time.

    Chipa builds this from its own append-only snapshots rather than relying
    on the upstream price-history endpoint, so it works on any plan tier and
    survives a provider outage.
    """
    _require_db()
    rows = await db.fetch_all(
        """
        SELECT price, on_promotion, observed_at
        FROM price_history
        WHERE store_product_id = %(id)s
          AND observed_at > NOW() - (%(days)s || ' days')::interval
        ORDER BY observed_at ASC;
        """,
        {"id": store_product_id, "days": days},
    )
    prices = [float(r["price"]) for r in rows]
    return {
        "store_product_id": store_product_id,
        "points": [
            {
                "price": float(r["price"]),
                "on_promotion": r["on_promotion"],
                "observed_at": r["observed_at"].isoformat(),
            }
            for r in rows
        ],
        "low": min(prices) if prices else None,
        "high": max(prices) if prices else None,
        "current": prices[-1] if prices else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Admin
# ─────────────────────────────────────────────────────────────────────────────

@app.post("/api/admin/sync", dependencies=[Depends(_admin)])
async def admin_sync(max_pages: Optional[int] = Query(None, ge=1)) -> dict:
    """
    Trigger a feed sync + link rebuild.

    Call this from an external scheduler. See deploy/sync-cron.yml.
    """
    _require_db()
    if not settings.has_feed:
        raise HTTPException(503, "RAPIDAPI_KEY is not configured")
    return await sync.full_refresh(max_pages=max_pages)


@app.post("/api/admin/build-links", dependencies=[Depends(_admin)])
async def admin_build_links(limit: int = Query(2000, ge=1, le=20000)) -> dict:
    _require_db()
    return await sync.build_links(limit=limit)


@app.get("/api/admin/status", dependencies=[Depends(_admin)])
async def admin_status() -> dict:
    _require_db()
    last = await db.fetch_one(
        "SELECT * FROM sync_runs ORDER BY started_at DESC LIMIT 1;"
    )
    counts = await db.fetch_one(
        """
        SELECT
          (SELECT COUNT(*) FROM store_products)      AS store_products,
          (SELECT COUNT(*) FROM canonical_products)  AS canonical_products,
          (SELECT COUNT(*) FROM product_links)       AS links,
          (SELECT COUNT(*) FROM product_links WHERE confidence='review') AS links_needing_review,
          (SELECT COUNT(*) FROM catalog_offers)      AS catalog_offers,
          (SELECT MAX(synced_at) FROM store_products) AS last_sync;
        """
    )
    return {"last_run": last, "counts": counts}


# ─────────────────────────────────────────────────────────────────────────────
# Frontend
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    if not FRONTEND.exists():
        return HTMLResponse("<h1>Chipa API</h1><p>Frontend not built.</p>")
    return HTMLResponse(FRONTEND.read_text(encoding="utf-8"))
