"""
APScheduler background scheduler.

Refreshes a fixed list of common search terms every 6 hours.
Uses the synchronous psycopg path so APScheduler threads don't
need to manage an async event loop.
"""

import asyncio
import logging

from apscheduler.schedulers.background import BackgroundScheduler

from scraper import scrape_search
from database import upsert_products_sync

logger = logging.getLogger(__name__)

COMMON_TERMS = [
    "milk",
    "bread",
    "eggs",
    "rice",
    "chicken",
    "butter",
    "coffee",
    "pilchards",
    "sugar",
    "cooking oil",
    "cheese",
    "yoghurt",
    "maize meal",
    "pasta",
    "tuna",
]


def _refresh_term(term: str) -> None:
    """Scrape troli.co.za for `term` and persist results. Runs in a thread."""
    try:
        # asyncio.run() is safe here because APScheduler calls this in a
        # plain thread, not inside an existing event loop.
        products = asyncio.run(scrape_search(term))
        upsert_products_sync(products, term)
        logger.info("Scheduler: refreshed '%s' → %d products", term, len(products))
    except Exception:
        logger.exception("Scheduler: failed to refresh '%s'", term)


def start_scheduler() -> BackgroundScheduler:
    """
    Create and start the background scheduler.

    Each term gets its own 6-hour interval job.  Jobs are staggered by
    90 seconds so the first sweep doesn't hammer troli all at once.
    """
    scheduler = BackgroundScheduler(timezone="Africa/Johannesburg")

    from datetime import datetime, timedelta

    now = datetime.now()
    for i, term in enumerate(COMMON_TERMS):
        first_run = now + timedelta(seconds=90 * (i + 1))
        scheduler.add_job(
            _refresh_term,
            trigger="interval",
            hours=6,
            args=[term],
            id=f"refresh_{term.replace(' ', '_')}",
            next_run_time=first_run,
            misfire_grace_time=300,
        )

    scheduler.start()
    logger.info(
        "Scheduler started — %d term(s) scheduled, first run staggered over ~%.0f min",
        len(COMMON_TERMS),
        90 * len(COMMON_TERMS) / 60,
    )
    return scheduler
