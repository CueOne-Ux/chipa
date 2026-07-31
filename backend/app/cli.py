"""
Command-line entry points.

    python -m app.cli sync          full feed sync + canonical linking
    python -m app.cli link          rebuild links only
    python -m app.cli budget        report the request cost of a full sync
    python -m app.cli ocr FILE.pdf  extract offers from a PDF, print as JSON

`budget` is the one to run before committing to a paid RapidAPI tier: it
reports the real page count so the plan is chosen on measured volume rather
than the pricing page.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from . import db, ocr, sync
from .config import settings
from .rapidapi import GroceryFeedClient


async def _sync(max_pages: int | None = None) -> None:
    await db.init_pool()
    try:
        result = await sync.full_refresh(max_pages=max_pages)
        print(json.dumps(result, indent=2, default=str))
    finally:
        await db.close_pool()


async def _link() -> None:
    await db.init_pool()
    try:
        print(json.dumps(await sync.build_links(), indent=2, default=str))
    finally:
        await db.close_pool()


async def _budget() -> None:
    async with GroceryFeedClient() as client:
        info = await client.catalog_size()

    pages = info.get("total_pages") or 0
    print(json.dumps(info, indent=2))
    print()
    print(f"One full sync = ~{pages} requests (limit={info.get('limit')}).")
    for label, quota in (
        ("Basic", 50), ("Pro", 5_000), ("Ultra", 25_000), ("Mega", 100_000)
    ):
        if pages:
            syncs = quota // pages
            verdict = (
                f"{syncs} full syncs/month"
                + (" — daily is fine" if syncs >= 30 else "")
                if syncs
                else "cannot complete one full sync"
            )
        else:
            verdict = "unknown"
        print(f"  {label:6} ({quota:>7,}/mo): {verdict}")


def _ocr(path: str) -> None:
    result = ocr.process_pdf(Path(path).read_bytes())
    print(
        json.dumps(
            {
                "page_count": result["page_count"],
                "extraction": result["extraction"],
                "valid_from": result["valid_from_text"],
                "valid_to": result["valid_to_text"],
                "offers": [o.to_row() for o in result["offers"]],
            },
            indent=2,
            default=str,
        )
    )


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        raise SystemExit(1)

    command = sys.argv[1]

    if command == "sync":
        pages = int(sys.argv[2]) if len(sys.argv) > 2 else None
        asyncio.run(_sync(pages))
    elif command == "link":
        asyncio.run(_link())
    elif command == "budget":
        asyncio.run(_budget())
    elif command == "ocr":
        if len(sys.argv) < 3:
            raise SystemExit("usage: python -m app.cli ocr FILE.pdf")
        _ocr(sys.argv[2])
    else:
        print(__doc__)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
