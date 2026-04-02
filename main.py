"""
Troli Companion — FastAPI backend.

Endpoints
---------
GET /api/search?q=<term>
    Returns products matching <term> in the format the frontend expects:
    [{"name": str, "offers": [{"r": str, "c": str, "p": float|null, "deal": str|null}]}]

    Products are fetched from the DB cache.  If the cache is empty or stale
    a live scrape is triggered inline before responding.

GET /api/deals
    Returns up to 20 products that have a lower current price than their
    original/was price:
    [{"name", "r", "c", "deal", "now", "was"}]

GET /
    Serves static/index.html (the frontend PWA).
"""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

import database
import scraper
import scheduler as sched

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    await database.init_db()
    bg_scheduler = sched.start_scheduler()
    yield
    bg_scheduler.shutdown(wait=False)


# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="Troli Companion API", version="1.0.0", lifespan=lifespan)


# ── API routes ────────────────────────────────────────────────────────────────

@app.get("/api/search")
async def api_search(q: str = Query(..., min_length=1, max_length=100)):
    term = q.strip().lower()

    # Use cached data if fresh; otherwise scrape live.
    if not await database.is_cache_fresh(term):
        logger.info("Cache miss for '%s' — scraping live", term)
        try:
            products = await scraper.scrape_search(term)
        except Exception as exc:
            logger.error("Live scrape failed for '%s': %s", term, exc)
            products = []

        if products:
            await database.upsert_products(products, term)

    rows = await database.search_products(term)

    # Shape into the frontend's expected format:
    # each DB row → one card with a single-retailer offers array.
    return [
        {
            "name": row["name"],
            "offers": [
                {
                    "r": row["retailer"],
                    "c": row["retailer_code"],
                    "p": row["price"],
                    "deal": row["deal_text"],
                }
            ],
        }
        for row in rows
    ]


@app.get("/api/deals")
async def api_deals():
    rows = await database.get_deals(limit=20)
    return [
        {
            "name": r["name"],
            "r": r["retailer"],
            "c": r["retailer_code"],
            "deal": r["deal"] or "On promo",
            "now": r["now"],
            "was": r["was"],
        }
        for r in rows
    ]


# ── Health check (used by Render) ─────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok"}


# ── Frontend ──────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index = STATIC_DIR / "index.html"
    if not index.exists():
        raise HTTPException(status_code=404, detail="Frontend not found")
    return HTMLResponse(index.read_text(encoding="utf-8"))


# Mount remaining static assets (if any are added later).
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
