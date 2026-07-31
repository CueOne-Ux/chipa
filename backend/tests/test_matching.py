"""
Matching and taxonomy tests.

The headline test is `test_chicken_query_never_returns_beef` — a direct
regression test against the competitor failure that motivated this engine.
"""

from __future__ import annotations

import pytest

from app.matching import rank_search_results, score_pair, score_query
from app.normalize import parse
from app.taxonomy import extract_facets, facet_conflict


# ─────────────────────────────────────────────────────────────────────────────
# Taxonomy
# ─────────────────────────────────────────────────────────────────────────────

def test_extracts_protein_facet():
    assert extract_facets("PnP Skinless Chicken Fillet Breast 12s")["protein"] == "chicken"
    assert extract_facets("Steakhouse Classic Beef Steak Fillet")["protein"] == "beef"
    assert extract_facets("Pork Spare Rib Per kg")["protein"] == "pork"


def test_longest_synonym_wins():
    # "full cream" must beat a bare "cream" match.
    assert extract_facets("Douglasdale Full Cream Milk 2L")["milk_fat"] == "full_cream"
    assert extract_facets("Clover Fat Free Milk 2L")["milk_fat"] == "fat_free"


def test_word_boundaries_prevent_false_positives():
    # "hamper" must not assert protein=pork via the "ham" synonym.
    assert "protein" not in extract_facets("Christmas Hamper Box")


def test_conflict_only_when_both_sides_assert():
    milk = extract_facets("Milk 2L")                      # says nothing
    full = extract_facets("Full Cream Milk 2L")
    has_hard, _, _ = facet_conflict(milk, full)
    assert has_hard is False, "less-specific names must not conflict"


def test_hard_veto_on_protein_mismatch():
    a = extract_facets("Chicken Fillet")
    b = extract_facets("Beef Fillet")
    has_hard, hard, _ = facet_conflict(a, b)
    assert has_hard is True
    assert "protein" in hard


# ─────────────────────────────────────────────────────────────────────────────
# The regression test
# ─────────────────────────────────────────────────────────────────────────────

CATALOG = [
    {"raw_product_name": "Steakhouse Classic Beef Steak Fillet Per kg", "brand": "OTHER"},
    {"raw_product_name": "PnP Skinless Chicken Fillet Breast 12s", "brand": "PNP"},
    {"raw_product_name": "Woolworths Free Range Chicken Breast Fillets 800g", "brand": "WOOLWORTHS"},
    {"raw_product_name": "Pork Spare Rib Per kg", "brand": "OTHER"},
    {"raw_product_name": "Rainbow Frozen Chicken Braai Pack 2kg", "brand": "RAINBOW"},
    {"raw_product_name": "Hake Fish Fillets 800g", "brand": "SEA HARVEST"},
]


def test_chicken_query_never_returns_beef():
    """The exact failure observed in a competitor app, July 2026."""
    results = rank_search_results("chicken fillet", CATALOG)
    names = [r.product["raw_product_name"] for r in results]

    assert names, "expected at least one chicken result"
    assert not any("Beef" in n for n in names), f"beef leaked into results: {names}"
    assert not any("Pork" in n for n in names)
    assert not any("Hake" in n or "Fish" in n for n in names)
    assert "Chicken" in names[0]


def test_beef_is_explicitly_vetoed_not_merely_ranked_low():
    result = score_query("chicken fillet", "Steakhouse Classic Beef Steak Fillet Per kg")
    assert result.vetoed is True
    assert result.score == 0.0
    assert "protein" in result.reasons[0]


def test_exact_match_ranks_above_partial():
    results = rank_search_results("chicken fillet", CATALOG)
    assert results[0].score >= results[-1].score
    assert "Chicken Fillet" in results[0].product["raw_product_name"]


def test_fish_query_returns_only_fish():
    results = rank_search_results("hake fillet", CATALOG)
    names = [r.product["raw_product_name"] for r in results]
    assert all("Chicken" not in n and "Beef" not in n for n in names), names


# ─────────────────────────────────────────────────────────────────────────────
# Cross-retailer linking
# ─────────────────────────────────────────────────────────────────────────────

def test_same_product_across_retailers_links():
    a = parse("Douglasdale Full Cream Milk 2L")
    b = parse("Douglasdale Full Cream Milk 2L")
    result = score_pair(a, b)
    assert result.confidence == "auto"
    assert result.score >= 0.82


def test_different_fat_content_never_links():
    a = parse("Douglasdale Full Cream Milk 2L")
    b = parse("Douglasdale Fat Free Milk 2L")
    result = score_pair(a, b)
    assert result.vetoed is True
    assert result.is_match is False


def test_different_size_is_penalised_not_vetoed():
    a = parse("Tastic Rice 2kg")
    b = parse("Tastic Rice 1kg")
    result = score_pair(a, b)
    assert result.vetoed is False
    assert result.score < 0.95, "a different size should not be a perfect match"


def test_different_brand_same_product_is_a_substitute_not_a_match():
    a = parse("Tastic Rice 2kg")
    b = parse("Spekko Rice 2kg")
    result = score_pair(a, b)
    assert result.vetoed is False
    assert any("brand" in r for r in result.reasons)


def test_zero_sugar_variant_never_links_to_regular():
    a = parse("Coca-Cola 2L")
    b = parse("Coca-Cola Zero 2L")
    result = score_pair(a, b)
    assert result.vetoed is True


def test_frozen_and_fresh_never_link():
    a = parse("Frozen Peas 1kg")
    b = parse("Fresh Peas 1kg")
    result = score_pair(a, b)
    assert result.vetoed is True


def test_dog_and_cat_food_never_link():
    a = parse("Bobtail Dog Food 8kg")
    b = parse("Whiskas Cat Food 8kg")
    result = score_pair(a, b)
    assert result.vetoed is True


@pytest.mark.parametrize(
    "query,product,should_veto",
    [
        ("chicken", "Beef Mince 500g", True),
        ("chicken", "Chicken Mince 500g", False),
        ("full cream milk", "Fat Free Milk 2L", True),
        ("full cream milk", "Full Cream Milk 2L", False),
        ("decaf coffee", "Decaffeinated Coffee 200g", False),
        ("white bread", "Brown Bread 700g", False),  # soft facet, not a veto
    ],
)
def test_veto_matrix(query, product, should_veto):
    assert score_query(query, product).vetoed is should_veto
