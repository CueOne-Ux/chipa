"""
API-level tests.

No Postgres is required: the db layer is stubbed with an in-memory catalog.
This validates endpoint wiring, ranking integration and the basket response
contract — i.e. that the taxonomy veto actually reaches the HTTP layer.
"""

from __future__ import annotations

from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from app import db as db_module
from app import main
from app.config import settings

# ─────────────────────────────────────────────────────────────────────────────
# Stub catalog
# ─────────────────────────────────────────────────────────────────────────────

STORE_META = {
    "checkers": ("Checkers", "#E4610F"),
    "pnp": ("Pick n Pay", "#0057B8"),
    "woolworths": ("Woolworths", "#000000"),
}

CATALOG: List[Dict[str, Any]] = [
    {
        "id": "sp-1", "store_id": "checkers", "product_name": "Douglasdale Full Cream Milk",
        "raw_product_name": "Douglasdale Full Cream Milk 2L", "brand": "DOUGLASDALE",
        "price": 31.99, "was_price": None, "on_promotion": False, "promotion_details": None,
        "unit_price": 1.60, "unit_price_label": "per 100ml", "quantity": 2.0, "unit": "L",
        "pack_count": None, "category": "Dairy", "image_url": None, "product_url": None,
    },
    {
        "id": "sp-2", "store_id": "pnp", "product_name": "Douglasdale Full Cream Milk",
        "raw_product_name": "Douglasdale Full Cream Milk 2L", "brand": "DOUGLASDALE",
        "price": 37.99, "was_price": None, "on_promotion": False, "promotion_details": None,
        "unit_price": 1.90, "unit_price_label": "per 100ml", "quantity": 2.0, "unit": "L",
        "pack_count": None, "category": "Dairy", "image_url": None, "product_url": None,
    },
    {
        "id": "sp-3", "store_id": "woolworths", "product_name": "Fat Free Milk",
        "raw_product_name": "Woolworths Fat Free Milk 2L", "brand": "WOOLWORTHS",
        "price": 34.99, "was_price": None, "on_promotion": False, "promotion_details": None,
        "unit_price": 1.75, "unit_price_label": "per 100ml", "quantity": 2.0, "unit": "L",
        "pack_count": None, "category": "Dairy", "image_url": None, "product_url": None,
    },
    {
        "id": "sp-4", "store_id": "pnp", "product_name": "Skinless Chicken Fillet Breast",
        "raw_product_name": "PnP Skinless Chicken Fillet Breast 12s", "brand": "PNP",
        "price": 159.98, "was_price": None, "on_promotion": False, "promotion_details": None,
        "unit_price": None, "unit_price_label": None, "quantity": None, "unit": None,
        "pack_count": 12, "category": "Meat, Poultry & Seafood", "image_url": None, "product_url": None,
    },
    {
        "id": "sp-5", "store_id": "checkers",
        "product_name": "Steakhouse Classic Beef Steak Fillet Per kg",
        "raw_product_name": "Steakhouse Classic Beef Steak Fillet Per kg", "brand": "OTHER",
        "price": 159.99, "was_price": None, "on_promotion": False, "promotion_details": None,
        "unit_price": None, "unit_price_label": None, "quantity": None, "unit": None,
        "pack_count": None, "category": "Meat, Poultry & Seafood", "image_url": None, "product_url": None,
    },
    {
        "id": "sp-6", "store_id": "checkers", "product_name": "Tastic Rice",
        "raw_product_name": "Tastic Rice 2kg", "brand": "TASTIC",
        "price": 27.99, "was_price": 34.99, "on_promotion": True, "promotion_details": "Save R7.00",
        "unit_price": 1.40, "unit_price_label": "per 100g", "quantity": 2.0, "unit": "kg",
        "pack_count": None, "category": "Pantry", "image_url": None, "product_url": None,
    },
    {
        "id": "sp-7", "store_id": "pnp", "product_name": "Tastic Rice",
        "raw_product_name": "Tastic Rice 2kg", "brand": "TASTIC",
        "price": 29.99, "was_price": None, "on_promotion": False, "promotion_details": None,
        "unit_price": 1.50, "unit_price_label": "per 100g", "quantity": 2.0, "unit": "kg",
        "pack_count": None, "category": "Pantry", "image_url": None, "product_url": None,
    },
]

for _row in CATALOG:
    _name, _colour = STORE_META[_row["store_id"]]
    _row["store_name"] = _name
    _row["store_colour"] = _colour


@pytest.fixture(autouse=True)
def stub_db(monkeypatch):
    """Replace the database layer with the in-memory catalog above."""
    monkeypatch.setattr(settings, "database_url", "postgresql://stub", raising=False)

    async def candidate_products(query_core: str, limit: int):
        # Deliberately wide, exactly like the real trigram prefilter: it is
        # the matching layer's job to discard the wrong ones.
        return list(CATALOG)

    async def fetch_all(sql: str, params=None):
        if "WITH ranked" in sql:
            # Emulate the window function: rank promos per store by discount.
            per_store = (params or {}).get("per_store", 5)
            out = []
            by_store: Dict[str, list] = {}
            for row in CATALOG:
                if not row["on_promotion"] or row["price"] is None:
                    continue
                by_store.setdefault(row["store_id"], []).append(row)
            for store_id, rows in by_store.items():
                ranked = sorted(
                    rows,
                    key=lambda r: (
                        (r["was_price"] - r["price"]) / r["was_price"]
                        if r["was_price"]
                        else 0
                    ),
                    reverse=True,
                )
                for rank, row in enumerate(ranked[:per_store], start=1):
                    discount = (
                        (row["was_price"] - row["price"]) / row["was_price"]
                        if row["was_price"]
                        else 0
                    )
                    out.append({**row, "discount": discount, "rn": rank})
            return out
        if "catalog_offers" in sql:
            return []
        if "FROM stores" in sql:
            return [
                {"id": k, "name": v[0], "website": None, "icon": None,
                 "colour": v[1], "source": "feed", "active": True}
                for k, v in STORE_META.items()
            ]
        if "on_promotion" in sql:
            return [dict(r) for r in CATALOG if r["on_promotion"]]
        return []

    async def fetch_one(sql: str, params=None):
        return None

    async def execute(sql: str, params=None):
        return None

    monkeypatch.setattr(db_module, "candidate_products", candidate_products)
    monkeypatch.setattr(db_module, "fetch_all", fetch_all)
    monkeypatch.setattr(db_module, "fetch_one", fetch_one)
    monkeypatch.setattr(db_module, "execute", execute)
    yield


@pytest.fixture
def client(monkeypatch):
    # Skip real pool creation during app startup.
    async def noop_pool():
        return None

    monkeypatch.setattr(db_module, "init_pool", noop_pool)
    monkeypatch.setattr(db_module, "close_pool", noop_pool)
    with TestClient(main.app) as c:
        yield c


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

def test_health(client):
    res = client.get("/health")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_stores_endpoint(client):
    res = client.get("/api/stores")
    assert res.status_code == 200
    assert {s["id"] for s in res.json()} == set(STORE_META)


def test_search_applies_taxonomy_veto_over_http(client):
    """The regression test, end to end through the API."""
    res = client.get("/api/search", params={"q": "chicken fillet"})
    assert res.status_code == 200
    names = [r["raw_product_name"] or r["product_name"] for r in res.json()["results"]]
    assert names, "expected results"
    assert not any("Beef" in n for n in names), names
    assert any("Chicken" in n for n in names)


def test_search_exposes_parsed_facets(client):
    res = client.get("/api/search", params={"q": "chicken fillet"})
    assert res.json()["parsed"]["facets"]["protein"] == "chicken"


def test_search_store_filter(client):
    res = client.get("/api/search", params={"q": "milk", "stores": "checkers"})
    assert all(r["store_id"] == "checkers" for r in res.json()["results"])


def test_compare_returns_one_row_per_store_sorted_by_price(client):
    res = client.get("/api/compare", params={"q": "douglasdale full cream milk 2l"})
    assert res.status_code == 200
    data = res.json()
    stores = [o["store_id"] for o in data["offers"]]
    assert len(stores) == len(set(stores)), "one offer per store expected"
    prices = [o["price"] for o in data["offers"]]
    assert prices == sorted(prices)
    assert data["cheapest_store"] == "checkers"


def test_compare_excludes_fat_free_from_full_cream_query(client):
    """Different milk fat content is a different product, not a cheaper option."""
    data = client.get(
        "/api/compare", params={"q": "douglasdale full cream milk 2l"}
    ).json()
    names = [o["product_name"] for o in data["offers"]]
    assert not any("Fat Free" in n for n in names), names


def test_compare_reports_saving(client):
    data = client.get("/api/compare", params={"q": "tastic rice 2kg"}).json()
    assert data["saving"] == pytest.approx(2.00, abs=0.01)
    assert data["cheapest_store"] == "checkers"


def test_basket_compare_contract(client):
    payload = {
        "items": [
            {"id": "a", "query": "douglasdale full cream milk 2l", "quantity": 1},
            {"id": "b", "query": "tastic rice 2kg", "quantity": 2},
        ],
        "distances": {"checkers": 4.1, "pnp": 2.1},
    }
    res = client.post("/api/basket/compare", json=payload)
    assert res.status_code == 200
    data = res.json()

    assert len(data["items"]) == 2
    assert data["store_totals"]
    assert data["recommendation"]["verdict"] in (
        "single_store", "split", "split_marginal"
    )
    # Quantity must be reflected in the totals.
    checkers = next(t for t in data["store_totals"] if t["store_id"] == "checkers")
    assert checkers["subtotal"] == pytest.approx(31.99 + 27.99 * 2, abs=0.01)
    assert checkers["fuel_cost"] is not None


def test_basket_marginal_split_is_discouraged(client):
    """Two shops for a couple of rand is not worth the petrol."""
    payload = {
        "items": [
            {"id": "a", "query": "douglasdale full cream milk 2l", "quantity": 1},
            {"id": "b", "query": "tastic rice 2kg", "quantity": 1},
        ],
        "distances": {"checkers": 4.1, "pnp": 2.1},
    }
    data = client.post("/api/basket/compare", json=payload).json()
    rec = data["recommendation"]
    assert rec["verdict"] in ("single_store", "split_marginal")
    assert rec["worth_the_trip"] is False or rec["verdict"] == "single_store"


def test_basket_honours_pinned_store(client):
    payload = {
        "items": [
            {"id": "a", "query": "douglasdale full cream milk 2l",
             "quantity": 1, "pinned_store": "pnp"},
        ],
        "distances": {},
    }
    data = client.post("/api/basket/compare", json=payload).json()
    assignments = data["recommendation"]["split"]["assignments"]
    assert assignments[0]["store_id"] == "pnp"


def test_basket_rejects_empty_payload(client):
    assert client.post("/api/basket/compare", json={"items": []}).status_code == 400


def test_deals_endpoint(client):
    res = client.get("/api/deals")
    assert res.status_code == 200
    rows = res.json()
    assert rows and rows[0]["discount_pct"] == pytest.approx(20.0, abs=0.1)


def test_specials_grouped_per_store(client):
    res = client.get("/api/specials", params={"per_store": 5})
    assert res.status_code == 200
    data = res.json()
    assert data["stores"], "expected at least one store carousel"

    for group in data["stores"]:
        assert group["store_colour"], "carousel border needs a colour"
        assert group["items"], "empty carousels should not be returned"
        assert len(group["items"]) <= 5


def test_specials_include_discount_and_saving(client):
    group = client.get("/api/specials").json()["stores"][0]
    item = group["items"][0]
    assert item["discount_pct"] == pytest.approx(20.0, abs=0.1)
    assert item["saving"] == pytest.approx(7.00, abs=0.01)
    assert item["was_price"] > item["price"]


def test_specials_respects_per_store_limit(client):
    data = client.get("/api/specials", params={"per_store": 1}).json()
    assert all(len(g["items"]) <= 1 for g in data["stores"])


def test_admin_requires_token(client):
    assert client.post("/api/admin/sync").status_code in (401, 503)


def test_frontend_served(client):
    res = client.get("/")
    assert res.status_code == 200
    assert "Chipa" in res.text
