"""
Product matching engine.

Two jobs, one scorer:

1. **Search relevance** — query text -> ranked store products.
2. **Canonical linking** — decide whether store product A (Checkers) and
   store product B (Pick n Pay) are THE SAME product, so a basket item can
   be priced across retailers.

Design
------
Pure fuzzy string similarity is not good enough. It is what makes a
competitor return "Beef Steak Fillet" for the query "chicken fillet".

Chipa scores in three stages:

    Stage 1  HARD VETO   — taxonomy conflict (chicken vs beef) => reject.
    Stage 2  SIMILARITY  — token-set similarity on brand/size-stripped text.
    Stage 3  ADJUSTMENTS — size compatibility, brand agreement, category
                           agreement, soft-facet conflicts.

Every match carries a `confidence` and a human-readable `reasons` list, so
the UI can say "we're not sure about this one" instead of silently showing
the wrong product. Confidence tiers:

    >= 0.82  AUTO   — link without asking
    >= 0.60  REVIEW — show, but flag as uncertain
    <  0.60  REJECT — never shown as the same product

The REVIEW tier is what a competitor's "hold down to manually group
products across stores" workaround exists to paper over. Surfacing
uncertainty beats silently guessing wrong.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from rapidfuzz import fuzz

from .normalize import ProductAttrs, parse
from .taxonomy import SOFT_CONFLICT_PENALTY, facet_conflict

# Confidence thresholds
AUTO_LINK = 0.82
REVIEW = 0.60

# Size tolerance: 1kg vs 1.05kg is the same product; 1kg vs 2kg is not.
SIZE_TOLERANCE = 0.12


@dataclass
class MatchResult:
    score: float
    confidence: str  # "auto" | "review" | "reject"
    reasons: List[str] = field(default_factory=list)
    vetoed: bool = False

    @property
    def is_match(self) -> bool:
        return self.confidence in ("auto", "review")


def _tier(score: float) -> str:
    if score >= AUTO_LINK:
        return "auto"
    if score >= REVIEW:
        return "review"
    return "reject"


def _size_factor(a: ProductAttrs, b: ProductAttrs) -> Tuple[float, Optional[str]]:
    """
    Compare pack sizes.

    Returns (multiplier, reason). Unknown sizes are neutral — we do not
    punish a product for having an unparseable name.
    """
    if not a.total_quantity or not b.total_quantity:
        return 1.0, None
    if a.base_unit != b.base_unit:
        # g vs ml vs each — different physical dimension entirely.
        return 0.55, f"unit mismatch ({a.base_unit} vs {b.base_unit})"

    larger = max(a.total_quantity, b.total_quantity)
    smaller = min(a.total_quantity, b.total_quantity)
    if larger <= 0:
        return 1.0, None

    ratio = smaller / larger
    if ratio >= (1.0 - SIZE_TOLERANCE):
        return 1.0, None

    # Graceful decay rather than a cliff: 500g vs 1kg is a weak match,
    # not a non-match — it is a valid "different size" suggestion.
    if ratio >= 0.45:
        return 0.72, f"size differs ({a.total_quantity:g}{a.base_unit} vs {b.total_quantity:g}{b.base_unit})"
    return 0.5, f"size differs sharply ({a.total_quantity:g}{a.base_unit} vs {b.total_quantity:g}{b.base_unit})"


def _brand_factor(a: ProductAttrs, b: ProductAttrs) -> Tuple[float, Optional[str]]:
    """
    Brand agreement.

    Different brands of the same thing are *substitutes*, not the same
    product — penalised, but still surfaced (this powers the
    "cheaper alternative" feature).
    """
    if not a.brand or not b.brand:
        return 1.0, None
    if a.brand == b.brand:
        return 1.06, None  # small boost for confirmed same brand
    return 0.78, f"different brand ({a.brand} vs {b.brand})"


def _category_factor(cat_a: Optional[str], cat_b: Optional[str]) -> Tuple[float, Optional[str]]:
    if not cat_a or not cat_b:
        return 1.0, None
    if cat_a.strip().lower() == cat_b.strip().lower():
        return 1.04, None
    return 0.88, f"different category ({cat_a} vs {cat_b})"


def score_pair(
    a: ProductAttrs,
    b: ProductAttrs,
    *,
    category_a: Optional[str] = None,
    category_b: Optional[str] = None,
) -> MatchResult:
    """Score two parsed products as candidates for being the same item."""
    reasons: List[str] = []

    # ── Stage 1: hard veto ───────────────────────────────────────────────────
    has_hard, hard, soft = facet_conflict(a.facets, b.facets)
    if has_hard:
        detail = ", ".join(
            f"{f}: {a.facets.get(f)} vs {b.facets.get(f)}" for f in hard
        )
        return MatchResult(
            score=0.0,
            confidence="reject",
            reasons=[f"blocked by taxonomy conflict — {detail}"],
            vetoed=True,
        )

    # ── Stage 2: similarity on brand/size-stripped text ──────────────────────
    text_a = a.core_text or a.normalised
    text_b = b.core_text or b.normalised
    if not text_a or not text_b:
        return MatchResult(0.0, "reject", ["empty product name"])

    token_set = fuzz.token_set_ratio(text_a, text_b) / 100.0
    token_sort = fuzz.token_sort_ratio(text_a, text_b) / 100.0
    partial = fuzz.partial_ratio(text_a, text_b) / 100.0
    # token_set is the most forgiving of word-order and extra descriptors;
    # weight it highest, but require the others to agree somewhat.
    base = (0.55 * token_set) + (0.30 * token_sort) + (0.15 * partial)

    # ── Stage 3: structured adjustments ──────────────────────────────────────
    score = base

    size_mult, size_reason = _size_factor(a, b)
    score *= size_mult
    if size_reason:
        reasons.append(size_reason)

    brand_mult, brand_reason = _brand_factor(a, b)
    score *= brand_mult
    if brand_reason:
        reasons.append(brand_reason)

    cat_mult, cat_reason = _category_factor(category_a, category_b)
    score *= cat_mult
    if cat_reason:
        reasons.append(cat_reason)

    for facet in soft:
        score *= 1.0 - SOFT_CONFLICT_PENALTY
        reasons.append(
            f"differs on {facet} ({a.facets.get(facet)} vs {b.facets.get(facet)})"
        )

    score = max(0.0, min(1.0, score))
    return MatchResult(score=score, confidence=_tier(score), reasons=reasons)


# ─────────────────────────────────────────────────────────────────────────────
# Search relevance
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ScoredProduct:
    product: dict
    score: float
    confidence: str
    reasons: List[str]


def score_query(
    query: str,
    product_name: str,
    *,
    brand: Optional[str] = None,
    quantity: Optional[float] = None,
    unit: Optional[str] = None,
    pack_count: Optional[int] = None,
) -> MatchResult:
    """
    Score a free-text search query against a product name.

    Asymmetric on purpose: a query is usually shorter and less specific
    than a product name ("chicken fillet" vs "PnP Skinless Chicken Fillet
    Breast 12s"). We therefore:

      * still apply the taxonomy hard veto (the important part),
      * do NOT penalise the product for having a size when the query has none,
      * reward the product for containing all of the query's tokens.
    """
    q = parse(query)
    p = parse(
        product_name,
        known_brand=brand,
        known_quantity=quantity,
        known_unit=unit,
        known_pack_count=pack_count,
    )

    has_hard, hard, soft = facet_conflict(q.facets, p.facets)
    if has_hard:
        detail = ", ".join(
            f"{f}: query wants {q.facets.get(f)}, product is {p.facets.get(f)}"
            for f in hard
        )
        return MatchResult(0.0, "reject", [f"blocked — {detail}"], vetoed=True)

    q_text = q.core_text or q.normalised
    p_text = p.core_text or p.normalised
    if not q_text or not p_text:
        return MatchResult(0.0, "reject", ["empty text"])

    q_tokens = set(q_text.split())
    p_tokens = set(p_text.split())

    # Coverage: what fraction of the query's meaningful words appear in the
    # product? This is what stops "chicken fillet" ranking a product that
    # only shares "fillet".
    if q_tokens:
        exact_cover = len(q_tokens & p_tokens) / len(q_tokens)
    else:
        exact_cover = 0.0

    # Fuzzy coverage handles plurals/typos ("fillets" vs "fillet").
    fuzzy_hits = 0.0
    for qt in q_tokens:
        best = max((fuzz.ratio(qt, pt) / 100.0 for pt in p_tokens), default=0.0)
        fuzzy_hits += best
    fuzzy_cover = fuzzy_hits / len(q_tokens) if q_tokens else 0.0

    token_set = fuzz.token_set_ratio(q_text, p_text) / 100.0

    score = (0.45 * exact_cover) + (0.35 * fuzzy_cover) + (0.20 * token_set)

    reasons: List[str] = []
    for facet in soft:
        score *= 1.0 - (SOFT_CONFLICT_PENALTY * 0.5)  # softer for search
        reasons.append(f"differs on {facet}")

    # If the query names a brand and the product is a different brand,
    # demote but keep (user may want the alternative).
    if q.brand and p.brand and q.brand != p.brand:
        score *= 0.75
        reasons.append(f"different brand ({p.brand})")

    # If the query specifies a size, reward products at that size.
    if q.total_quantity and p.total_quantity and q.base_unit == p.base_unit:
        ratio = min(q.total_quantity, p.total_quantity) / max(
            q.total_quantity, p.total_quantity
        )
        if ratio >= (1.0 - SIZE_TOLERANCE):
            score = min(1.0, score * 1.10)
        elif ratio < 0.45:
            score *= 0.80
            reasons.append("different size to query")

    score = max(0.0, min(1.0, score))
    return MatchResult(score=score, confidence=_tier(score), reasons=reasons)


def rank_search_results(
    query: str,
    rows: Sequence[dict],
    *,
    min_score: float = 0.35,
    limit: int = 100,
) -> List[ScoredProduct]:
    """
    Rank DB rows against a query, dropping vetoed and low-relevance items.

    `rows` are dicts from the store_products table.
    """
    scored: List[ScoredProduct] = []
    for row in rows:
        result = score_query(
            query,
            row.get("raw_product_name") or row.get("product_name") or "",
            brand=row.get("brand"),
            quantity=row.get("quantity"),
            unit=row.get("unit"),
            pack_count=row.get("pack_count"),
        )
        if result.vetoed or result.score < min_score:
            continue
        scored.append(
            ScoredProduct(
                product=row,
                score=result.score,
                confidence=result.confidence,
                reasons=result.reasons,
            )
        )

    scored.sort(key=lambda s: s.score, reverse=True)
    return scored[:limit]


def best_link(
    target: ProductAttrs,
    candidates: Iterable[Tuple[dict, ProductAttrs]],
    *,
    target_category: Optional[str] = None,
) -> Optional[Tuple[dict, MatchResult]]:
    """
    Pick the best canonical link for `target` from candidate store products.

    Used by the canonicalisation pass to group the same product across
    retailers. Returns None when nothing clears the REVIEW threshold.
    """
    best: Optional[Tuple[dict, MatchResult]] = None
    for row, attrs in candidates:
        result = score_pair(
            target,
            attrs,
            category_a=target_category,
            category_b=row.get("category"),
        )
        if not result.is_match:
            continue
        if best is None or result.score > best[1].score:
            best = (row, result)
    return best
