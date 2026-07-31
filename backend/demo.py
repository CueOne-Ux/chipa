"""
End-to-end demo — no database required.

Proves the whole pipeline in one run:

  1. Load real feed products (captured RapidAPI response).
  2. OCR a flattened PDF specials leaflet from a shop with NO digital feed.
  3. Match the leaflet's products against the feed products using the
     taxonomy-vetoed matching engine.
  4. Run a cross-store basket comparison in which the OCR'd shop competes
     directly with the tracked retailers.

Run:  python demo.py
"""

from __future__ import annotations

import json
from pathlib import Path

from app.basket import BasketItem, Offer, recommend, single_store_totals
from app.matching import score_pair, score_query
from app.normalize import parse
from app.ocr import process_pdf
from app.rapidapi import to_db_row

HERE = Path(__file__).parent
FIXTURES = HERE / "tests" / "fixtures"

STORE_NAMES = {
    "checkers": ("Checkers", "#E4610F"),
    "pnp": ("Pick n Pay", "#0057B8"),
    "woolworths": ("Woolworths", "#000000"),
}
CATALOG_STORE = "Food Lover's Market Rivonia"

RULE = "─" * 74


def banner(text: str) -> None:
    print(f"\n{RULE}\n{text}\n{RULE}")


def main() -> None:
    # ── 1. Feed products ────────────────────────────────────────────────────
    banner("1. FEED — licensed RapidAPI data (3 retailers)")
    feed_raw = json.loads((FIXTURES / "products_page1.json").read_text())
    feed = [to_db_row(p) for p in feed_raw["products"]]
    for row in feed:
        name, colour = STORE_NAMES.get(row["store_id"], (row["store_id"], "#666"))
        row["store_name"], row["store_colour"] = name, colour
    print(f"  {len(feed)} products across {len({r['store_id'] for r in feed})} retailers")

    # ── 2. OCR the leaflet ──────────────────────────────────────────────────
    banner(f"2. OCR — PDF specials leaflet from {CATALOG_STORE}")
    result = process_pdf((FIXTURES / "catalog_flat.pdf").read_bytes())
    print(f"  extraction: {result['extraction']}  (flattened graphic, no text layer)")
    print(f"  {len(result['offers'])} offers read, valid to {result['valid_to_text']}")
    catalog = [o.to_row() for o in result["offers"]]

    # ── 3. Cross-source matching ────────────────────────────────────────────
    banner("3. MATCHING — leaflet products vs feed products")
    linked: dict[str, list[dict]] = {}
    for offer in catalog:
        offer_attrs = parse(offer["product_name"])
        for row in feed:
            if row["price"] is None:
                continue
            row_attrs = parse(row["raw_product_name"], known_brand=row["brand"])
            match = score_pair(offer_attrs, row_attrs)
            if match.confidence == "auto":
                linked.setdefault(offer["product_name"], []).append(row)

    for name, rows in linked.items():
        stores = ", ".join(f"{r['store_name']} R{r['price']}" for r in rows)
        print(f"  {name[:36]:36} -> {stores}")

    vetoed = score_pair(
        parse("Fresh Chicken Braai Pack 2kg"),
        parse("Steakhouse Classic Beef Steak Fillet Per kg"),
    )
    print(f"\n  Veto check — chicken vs beef: rejected={vetoed.vetoed}")
    print(f"    reason: {vetoed.reasons[0]}")

    # ── 4. Basket comparison ────────────────────────────────────────────────
    banner("4. BASKET — leaflet shop competing against tracked retailers")

    wanted = [
        ("Douglasdale Full Cream Milk 2L", 1),
        ("Tastic Rice 2kg", 2),
        ("Albany Superior White Bread 700g", 1),
    ]

    items: list[BasketItem] = []
    for index, (query, qty) in enumerate(wanted):
        offers: list[Offer] = []

        # Feed retailers
        for row in feed:
            if row["price"] is None:
                continue
            match = score_query(query, row["raw_product_name"], brand=row["brand"])
            if match.vetoed or match.score < 0.75:
                continue
            existing = next((o for o in offers if o.store_id == row["store_id"]), None)
            if existing and existing.price <= float(row["price"]):
                continue
            offers = [o for o in offers if o.store_id != row["store_id"]]
            offers.append(
                Offer(
                    store_id=row["store_id"],
                    store_name=row["store_name"],
                    store_colour=row["store_colour"],
                    price=float(row["price"]),
                    product_name=row["raw_product_name"],
                    unit_price=row["unit_price"],
                    unit_price_label=row["unit_price_label"],
                    match_score=match.score,
                )
            )

        # OCR'd leaflet — the shop nobody else can compare
        for offer in catalog:
            if offer["price"] is None:
                continue
            match = score_query(query, offer["product_name"])
            if match.vetoed or match.score < 0.75:
                continue
            offers.append(
                Offer(
                    store_id="catalog:flm",
                    store_name=CATALOG_STORE,
                    store_colour="#8B6BFF",
                    price=float(offer["price"]),
                    product_name=offer["product_name"],
                    unit_price=offer["unit_price"],
                    unit_price_label=offer["unit_price_label"],
                    match_score=match.score,
                    source="catalog",
                )
            )
            break

        items.append(
            BasketItem(
                item_id=str(index),
                query_text=query,
                canonical_id=None,
                display_name=query,
                quantity=qty,
                offers=offers,
            )
        )

    for item in items:
        print(f"\n  {item.display_name}  (x{item.quantity})")
        for offer in sorted(item.offers, key=lambda o: o.price):
            tag = " [leaflet]" if offer.source == "catalog" else ""
            unit = (
                f"  {offer.unit_price:.2f} {offer.unit_price_label}"
                if offer.unit_price
                else ""
            )
            print(f"      {offer.store_name:28} R{offer.price:>7.2f}{unit}{tag}")

    distances = {"checkers": 4.1, "pnp": 2.1, "woolworths": 6.0, "catalog:flm": 3.2}

    banner("5. VERDICT")
    for total in single_store_totals(items, distances=distances):
        missing = (
            f"  missing {total.items_missing}: {', '.join(total.missing_names)}"
            if total.items_missing
            else ""
        )
        allin = (
            f"  (R{total.total_with_fuel:.2f} with petrol)"
            if total.total_with_fuel is not None
            else ""
        )
        print(f"  {total.store_name:28} R{total.subtotal:>7.2f}{allin}{missing}")

    rec = recommend(items, distances=distances)
    print(f"\n  {rec.headline}")
    print(f"  {rec.detail}")
    if rec.split and rec.split.stop_count > 1:
        print("\n  Split plan:")
        for a in rec.split.assignments:
            print(f"      {a.display_name[:34]:34} -> {a.store_name:28} R{a.line_total:>7.2f}")
    print()


if __name__ == "__main__":
    main()
