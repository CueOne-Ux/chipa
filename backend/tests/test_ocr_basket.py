"""OCR pipeline and basket engine tests."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from app.basket import (
    BasketItem,
    Offer,
    best_split,
    find_substitutions,
    fuel_cost_for_km,
    recommend,
    single_store_totals,
)
from app.config import settings
from app.normalize import parse
from app.ocr import extract_prices, parse_offers, process_pdf

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module", autouse=True)
def build_catalogs():
    """Generate the test leaflets if they aren't present."""
    if not (FIXTURES / "catalog_flat.pdf").exists():
        subprocess.run(
            [sys.executable, str(Path(__file__).parent / "make_test_catalog.py")],
            check=True,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Normalisation
# ─────────────────────────────────────────────────────────────────────────────

def test_multipack_parsing():
    attrs = parse("PnP UHT Full Cream Milk 6 x 1L")
    assert attrs.pack_count == 6
    assert attrs.quantity == 1.0
    assert attrs.total_quantity == 6000.0
    assert attrs.base_unit == "ml"
    assert attrs.brand == "pnp"


def test_unit_price_matches_retailer_display():
    """Douglasdale 2L at R31.99 shows as R1.60/100ml in the wild."""
    attrs = parse("Douglasdale Full Cream Milk 2L")
    value, label = attrs.unit_price(31.99)
    assert label == "per 100ml"
    assert round(value, 2) == 1.60


def test_orphan_unit_tokens_are_dropped():
    attrs = parse("Pork Roast Per kg")
    assert "kg" not in attrs.core_tokens


# ─────────────────────────────────────────────────────────────────────────────
# Price extraction
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "text,expected",
    [
        ("R49.99", [49.99]),
        ("R 49,99", [49.99]),
        ("ZAR 12.50", [12.50]),
        ("R29.99 Was R34.99", [29.99, 34.99]),
        ("no price here", []),
    ],
)
def test_extract_prices(text, expected):
    assert extract_prices(text) == expected


# ─────────────────────────────────────────────────────────────────────────────
# OCR pipeline
# ─────────────────────────────────────────────────────────────────────────────

EXPECTED = {
    "Douglasdale Full Cream Milk 2L": 29.99,
    "Albany Superior White Bread 700g": 18.99,
    "Tastic Rice 2kg": 25.99,
    "Fresh Chicken Braai Pack 2kg": 89.99,
    "Eskort Smoked Viennas 500g": 44.99,
    "Coca-Cola 2L": 25.00,          # "2 for R50" -> per-unit
    "Broccoli 350g": 27.99,
    "Woolworths Fat Free Milk 2L": 31.99,
}


def _offers_by_name(path: Path):
    result = process_pdf(path.read_bytes())
    return result, {o.product_name.strip(): o for o in result["offers"]}


def test_text_layer_pdf_extracts_all_offers():
    result, offers = _offers_by_name(FIXTURES / "catalog_text.pdf")
    assert result["extraction"] == "text_layer"
    for name, price in EXPECTED.items():
        assert name in offers, f"missing {name}"
        assert offers[name].price == pytest.approx(price, abs=0.01)


def test_flattened_image_pdf_goes_through_ocr():
    """A real supermarket leaflet is a flattened graphic — this is the path."""
    result, offers = _offers_by_name(FIXTURES / "catalog_flat.pdf")
    assert result["extraction"] == "ocr", "should have fallen back to OCR"
    for name, price in EXPECTED.items():
        assert name in offers, f"OCR missed {name}"
        assert offers[name].price == pytest.approx(price, abs=0.01)


def test_was_price_is_not_borrowed_from_neighbouring_items():
    _, offers = _offers_by_name(FIXTURES / "catalog_text.pdf")
    assert offers["Douglasdale Full Cream Milk 2L"].was_price == pytest.approx(34.99)
    # Bread has no "Was" of its own and must not inherit one.
    assert offers["Albany Superior White Bread 700g"].was_price is None


def test_price_modifier_words_never_become_products():
    _, offers = _offers_by_name(FIXTURES / "catalog_text.pdf")
    for name in offers:
        assert name.strip().lower() not in {"was", "save", "now", "from"}


def test_multibuy_is_converted_to_unit_price():
    _, offers = _offers_by_name(FIXTURES / "catalog_text.pdf")
    coke = offers["Coca-Cola 2L"]
    assert coke.price == pytest.approx(25.00)
    assert "2 for" in (coke.promo_text or "")


def test_validity_window_detected():
    result = process_pdf((FIXTURES / "catalog_text.pdf").read_bytes())
    assert result["valid_to_text"] and "2026" in result["valid_to_text"]


def test_catalog_offers_get_unit_prices():
    _, offers = _offers_by_name(FIXTURES / "catalog_text.pdf")
    row = offers["Tastic Rice 2kg"].to_row()
    assert row["unit_price_label"] == "per 100g"
    assert round(row["unit_price"], 2) == pytest.approx(1.30, abs=0.01)


# ─────────────────────────────────────────────────────────────────────────────
# Basket engine
# ─────────────────────────────────────────────────────────────────────────────

def _offer(store, name, price, **kw):
    return Offer(store_id=store, store_name=name, price=price, product_name="x", **kw)


def _sample_items():
    return [
        BasketItem("1", "milk", None, "Milk 2L", offers=[
            _offer("checkers", "Checkers", 31.99),
            _offer("pnp", "Pick n Pay", 37.99),
        ]),
        BasketItem("2", "rice", None, "Rice 2kg", offers=[
            _offer("checkers", "Checkers", 27.99),
            _offer("pnp", "Pick n Pay", 29.99),
        ]),
    ]


def test_fuel_cost_is_round_trip():
    # 10km each way at 8L/100km and R21.50/L
    assert fuel_cost_for_km(10) == pytest.approx(
        20 * 0.08 * settings.fuel_price_per_litre, abs=0.01
    )


def test_missing_items_are_reported_per_store():
    items = _sample_items()
    items.append(BasketItem("3", "olives", None, "Olives", offers=[
        _offer("woolworths", "Woolworths", 36.99)
    ]))
    totals = {t.store_id: t for t in single_store_totals(items)}
    assert totals["checkers"].items_missing == 1
    assert "Olives" in totals["checkers"].missing_names


def test_best_store_accounts_for_fuel_not_just_subtotal():
    """A nearer store that is dearer on paper can still win overall."""
    items = [
        BasketItem("1", "x", None, "X", offers=[
            _offer("far", "Far Store", 100.0),
            _offer("near", "Near Store", 108.0),
        ])
    ]
    totals = single_store_totals(items, distances={"far": 40.0, "near": 1.0})
    assert totals[0].store_id == "near", "fuel must be part of the ranking"


def test_marginal_saving_is_not_recommended():
    """Do not send someone to a second shop to save small change."""
    rec = recommend(_sample_items(), distances={"checkers": 4.1, "pnp": 2.1})
    assert rec.verdict in ("single_store", "split_marginal")
    assert rec.worth_the_trip is False or rec.verdict == "single_store"


def test_large_saving_triggers_a_split():
    items = [
        BasketItem("1", "milk", None, "Milk", offers=[
            _offer("checkers", "Checkers", 31.99),
            _offer("pnp", "Pick n Pay", 199.99),
        ]),
        BasketItem("2", "rice", None, "Rice", offers=[
            _offer("checkers", "Checkers", 220.00),
            _offer("pnp", "Pick n Pay", 29.99),
        ]),
    ]
    rec = recommend(items, distances={"checkers": 3.0, "pnp": 2.0})
    assert rec.verdict == "split"
    assert rec.worth_the_trip is True
    assert rec.saving_vs_single > settings.min_split_saving_rand


def test_pinned_store_is_honoured_in_the_split():
    items = _sample_items()
    items[0].pinned_store = "pnp"     # user moved milk to Pick n Pay
    plan = best_split(items)
    assert plan is not None
    milk = next(a for a in plan.assignments if a.item_id == "1")
    assert milk.store_id == "pnp"


def test_split_never_exceeds_max_stores():
    items = [
        BasketItem(str(i), "x", None, f"Item {i}", offers=[
            _offer("a", "A", 10.0 + i), _offer("b", "B", 11.0 + i),
            _offer("c", "C", 12.0 + i), _offer("d", "D", 9.0 + i),
        ])
        for i in range(5)
    ]
    plan = best_split(items, max_stores=2)
    assert plan is not None
    assert plan.stop_count <= 2


def test_substitution_suggests_cheaper_alternative():
    item = BasketItem("1", "rice", None, "Tastic Rice 2kg", offers=[
        _offer("checkers", "Checkers", 45.00, unit_price=2.25, unit_price_label="per 100g")
    ])
    alternatives = [
        _offer("pnp", "Pick n Pay", 30.00, unit_price=1.50, unit_price_label="per 100g")
    ]
    subs = find_substitutions(item, alternatives)
    assert subs and subs[0].saving == pytest.approx(15.0)
    assert "value" in subs[0].reason.lower() or "cheaper" in subs[0].reason.lower()
