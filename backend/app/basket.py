"""
Cross-store basket engine.

This is the product. A basket is a list of *canonical* items (store-agnostic).
Each item resolves to a real SKU at each retailer, which makes three views
possible:

  1. **Single-store** — what this basket costs at each retailer on its own,
     including which items that retailer cannot supply.
  2. **Best split** — cheapest possible basket if you shop across stores.
  3. **Worth-the-trip** — whether that split is *actually* worth it once
     fuel and time are priced in.

Point 3 is where Chipa differs. A competitor will happily tell you to visit
three shops to save R8. Chipa applies a threshold and says "not worth it" —
and shows the best realistic option (usually a two-store split) instead.

Catalog offers (OCR'd PDF specials from retailers with no digital feed) are
first-class citizens here: a Food Lover's Market leaflet price competes in
the same comparison as the live feed retailers.
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .config import settings

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Models
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Offer:
    """One retailer's price for one canonical item."""

    store_id: str
    store_name: str
    price: float
    product_name: str
    store_product_id: Optional[str] = None
    store_colour: Optional[str] = None
    was_price: Optional[float] = None
    on_promotion: bool = False
    promotion_details: Optional[str] = None
    unit_price: Optional[float] = None
    unit_price_label: Optional[str] = None
    image_url: Optional[str] = None
    product_url: Optional[str] = None
    in_stock: bool = True
    match_score: Optional[float] = None
    match_confidence: Optional[str] = None
    source: str = "feed"          # 'feed' | 'catalog'
    catalog_note: Optional[str] = None   # e.g. "from uploaded leaflet, valid to 30 Jul"

    @property
    def saving_vs_was(self) -> Optional[float]:
        if self.was_price and self.was_price > self.price:
            return round(self.was_price - self.price, 2)
        return None


@dataclass
class BasketItem:
    """A line in the basket, with every retailer's offer for it."""

    item_id: str
    query_text: str
    canonical_id: Optional[str]
    display_name: str
    quantity: int = 1
    offers: List[Offer] = field(default_factory=list)
    pinned_store: Optional[str] = None
    image_url: Optional[str] = None
    needs_review: bool = False       # low-confidence match — tell the user

    def offer_for(self, store_id: str) -> Optional[Offer]:
        for offer in self.offers:
            if offer.store_id == store_id and offer.in_stock:
                return offer
        return None

    @property
    def stores_available(self) -> Set[str]:
        return {o.store_id for o in self.offers if o.in_stock}

    def cheapest(self) -> Optional[Offer]:
        available = [o for o in self.offers if o.in_stock]
        return min(available, key=lambda o: o.price) if available else None

    def best_unit_price(self) -> Optional[Offer]:
        """
        Cheapest per 100g/100ml rather than per pack.

        A 2kg bag at R55 beats a 1kg bag at R30 even though the sticker
        price is higher. Surfacing this by default is a real edge — the
        competition shows unit price inconsistently.
        """
        priced = [o for o in self.offers if o.in_stock and o.unit_price]
        return min(priced, key=lambda o: o.unit_price or 0) if priced else None


@dataclass
class StoreTotal:
    store_id: str
    store_name: str
    store_colour: Optional[str]
    subtotal: float
    items_found: int
    items_missing: int
    missing_names: List[str] = field(default_factory=list)
    distance_km: Optional[float] = None
    fuel_cost: Optional[float] = None
    total_with_fuel: Optional[float] = None
    source: str = "feed"


@dataclass
class SplitAssignment:
    item_id: str
    display_name: str
    store_id: str
    store_name: str
    price: float
    quantity: int = 1

    @property
    def line_total(self) -> float:
        return round(self.price * self.quantity, 2)


@dataclass
class SplitPlan:
    assignments: List[SplitAssignment]
    stores: List[str]
    subtotal: float
    fuel_cost: float
    time_cost: float
    total_cost: float
    items_missing: List[str] = field(default_factory=list)

    @property
    def stop_count(self) -> int:
        return len(self.stores)


# ─────────────────────────────────────────────────────────────────────────────
# Economics
# ─────────────────────────────────────────────────────────────────────────────

def fuel_cost_for_km(distance_km: float) -> float:
    """
    Rand cost of driving `distance_km`.

    Assumes a round trip: the shopper drives there and back.
    """
    if distance_km <= 0:
        return 0.0
    litres = (distance_km * 2) * (settings.fuel_consumption_l_per_100km / 100.0)
    return round(litres * settings.fuel_price_per_litre, 2)


def _trip_cost(
    store_ids: Sequence[str],
    distances: Dict[str, float],
) -> Tuple[float, float]:
    """
    (fuel_cost, time_cost) for visiting a set of stores.

    Fuel is charged for the furthest store plus a surcharge per extra stop —
    a rough but defensible model of a multi-stop trip without needing a
    routing engine. Time cost is a flat per-extra-stop penalty.
    """
    if not store_ids:
        return 0.0, 0.0

    known = [distances.get(s, 0.0) for s in store_ids]
    furthest = max(known) if known else 0.0
    base_fuel = fuel_cost_for_km(furthest)

    extra_stops = max(0, len(store_ids) - 1)
    # Each additional stop adds a detour — model it as half the average leg.
    average_leg = (sum(known) / len(known)) if known else 0.0
    detour_fuel = fuel_cost_for_km(average_leg * 0.5) * extra_stops

    time_cost = settings.time_cost_per_stop_rand * extra_stops
    return round(base_fuel + detour_fuel, 2), round(time_cost, 2)


# ─────────────────────────────────────────────────────────────────────────────
# Views
# ─────────────────────────────────────────────────────────────────────────────

def single_store_totals(
    items: Sequence[BasketItem],
    *,
    distances: Optional[Dict[str, float]] = None,
    stores: Optional[Dict[str, Tuple[str, Optional[str], str]]] = None,
) -> List[StoreTotal]:
    """
    What the basket costs at each retailer individually.

    `stores` maps store_id -> (display_name, colour, source).
    """
    distances = distances or {}
    all_stores: Dict[str, Tuple[str, Optional[str], str]] = dict(stores or {})
    for item in items:
        for offer in item.offers:
            all_stores.setdefault(
                offer.store_id, (offer.store_name, offer.store_colour, offer.source)
            )

    totals: List[StoreTotal] = []
    for store_id, (name, colour, source) in all_stores.items():
        subtotal = 0.0
        found = 0
        missing: List[str] = []

        for item in items:
            offer = item.offer_for(store_id)
            if offer:
                subtotal += offer.price * item.quantity
                found += 1
            else:
                missing.append(item.display_name)

        distance = distances.get(store_id)
        fuel = fuel_cost_for_km(distance) if distance else None

        totals.append(
            StoreTotal(
                store_id=store_id,
                store_name=name,
                store_colour=colour,
                subtotal=round(subtotal, 2),
                items_found=found,
                items_missing=len(missing),
                missing_names=missing,
                distance_km=distance,
                fuel_cost=fuel,
                total_with_fuel=round(subtotal + fuel, 2) if fuel is not None else None,
                source=source,
            )
        )

    # Complete baskets first, then cheapest *all in*. Ranking on subtotal
    # alone picks the wrong shop whenever a nearer store is slightly dearer
    # on paper but cheaper once petrol is counted.
    totals.sort(
        key=lambda t: (
            t.items_missing,
            t.total_with_fuel if t.total_with_fuel is not None else t.subtotal,
        )
    )
    return totals


def best_split(
    items: Sequence[BasketItem],
    *,
    distances: Optional[Dict[str, float]] = None,
    max_stores: int = 3,
) -> Optional[SplitPlan]:
    """
    Cheapest assignment of items to stores, limited to `max_stores` stops.

    Brute-forces store combinations. With 3-8 candidate retailers and a cap
    of 3 stops this is at most a few hundred combinations — trivial, and
    exact rather than greedy.

    Pinned items are honoured: if the user has moved an item to a specific
    store, that store must be part of the plan.
    """
    distances = distances or {}
    if not items:
        return None

    candidate_stores = sorted({s for item in items for s in item.stores_available})
    if not candidate_stores:
        return None

    pinned_stores = {item.pinned_store for item in items if item.pinned_store}

    best_plan: Optional[SplitPlan] = None

    for size in range(1, min(max_stores, len(candidate_stores)) + 1):
        for combo in itertools.combinations(candidate_stores, size):
            combo_set = set(combo)
            if not pinned_stores.issubset(combo_set):
                continue

            assignments: List[SplitAssignment] = []
            missing: List[str] = []
            subtotal = 0.0
            used: Set[str] = set()

            for item in items:
                if item.pinned_store:
                    offer = item.offer_for(item.pinned_store)
                    if offer is None:
                        missing.append(item.display_name)
                        continue
                else:
                    options = [
                        o for o in item.offers if o.store_id in combo_set and o.in_stock
                    ]
                    if not options:
                        missing.append(item.display_name)
                        continue
                    offer = min(options, key=lambda o: o.price)

                assignments.append(
                    SplitAssignment(
                        item_id=item.item_id,
                        display_name=item.display_name,
                        store_id=offer.store_id,
                        store_name=offer.store_name,
                        price=offer.price,
                        quantity=item.quantity,
                    )
                )
                subtotal += offer.price * item.quantity
                used.add(offer.store_id)

            if not assignments:
                continue

            fuel, time_cost = _trip_cost(sorted(used), distances)
            total = subtotal + fuel + time_cost

            # Prefer fewer missing items, then lower true cost.
            def rank(plan_missing: int, plan_total: float) -> Tuple[int, float]:
                return (plan_missing, round(plan_total, 2))

            if best_plan is None or rank(len(missing), total) < rank(
                len(best_plan.items_missing), best_plan.total_cost
            ):
                best_plan = SplitPlan(
                    assignments=assignments,
                    stores=sorted(used),
                    subtotal=round(subtotal, 2),
                    fuel_cost=fuel,
                    time_cost=time_cost,
                    total_cost=round(total, 2),
                    items_missing=missing,
                )

    return best_plan


@dataclass
class Recommendation:
    verdict: str                 # 'single_store' | 'split' | 'split_marginal'
    headline: str
    detail: str
    best_single: Optional[StoreTotal]
    split: Optional[SplitPlan]
    saving_vs_single: float
    worth_the_trip: bool


def recommend(
    items: Sequence[BasketItem],
    *,
    distances: Optional[Dict[str, float]] = None,
    max_stores: int = 3,
) -> Recommendation:
    """
    Decide what to actually tell the shopper.

    The competition optimises for the lowest possible number. Chipa optimises
    for the best *real* outcome: if driving to a second shop costs more in
    petrol and time than it saves, say so plainly.
    """
    distances = distances or {}
    totals = single_store_totals(items, distances=distances)
    complete = [t for t in totals if t.items_missing == 0]
    best_single = complete[0] if complete else (totals[0] if totals else None)

    split = best_split(items, distances=distances, max_stores=max_stores)

    if best_single is None or split is None:
        return Recommendation(
            verdict="single_store",
            headline="Not enough price data yet",
            detail="We couldn't price this basket at any store.",
            best_single=best_single,
            split=split,
            saving_vs_single=0.0,
            worth_the_trip=False,
        )

    single_true_cost = best_single.subtotal + (best_single.fuel_cost or 0.0)
    saving = round(single_true_cost - split.total_cost, 2)

    # A "split" that lands on one store is just the single-store answer.
    if split.stop_count <= 1:
        return Recommendation(
            verdict="single_store",
            headline=f"Cheapest at {best_single.store_name}",
            detail=(
                f"R{best_single.subtotal:.2f} for {best_single.items_found} item(s)"
                + (
                    f", R{best_single.total_with_fuel:.2f} with petrol"
                    if best_single.total_with_fuel is not None
                    else ""
                )
                + ". One stop — no split needed."
            ),
            best_single=best_single,
            split=split,
            saving_vs_single=0.0,
            worth_the_trip=True,
        )

    if saving < settings.min_split_saving_rand:
        return Recommendation(
            verdict="split_marginal",
            headline=f"Just shop at {best_single.store_name}",
            detail=(
                f"Splitting across {split.stop_count} shops saves only "
                f"R{max(saving, 0):.2f} after petrol and time. "
                f"Not worth the extra stop."
            ),
            best_single=best_single,
            split=split,
            saving_vs_single=saving,
            worth_the_trip=False,
        )

    store_names = ", ".join(
        sorted({a.store_name for a in split.assignments})
    )
    return Recommendation(
        verdict="split",
        headline=f"Split across {split.stop_count} shops — save R{saving:.2f}",
        detail=(
            f"{store_names}. Basket R{split.subtotal:.2f} "
            f"+ R{split.fuel_cost:.2f} petrol = R{split.total_cost:.2f} all in, "
            f"versus R{single_true_cost:.2f} at {best_single.store_name} alone."
        ),
        best_single=best_single,
        split=split,
        saving_vs_single=saving,
        worth_the_trip=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Substitutions
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Substitution:
    reason: str
    saving: float
    from_offer: Offer
    to_offer: Offer


def find_substitutions(
    item: BasketItem,
    alternatives: Sequence[Offer],
    *,
    min_saving: float = 5.0,
) -> List[Substitution]:
    """
    Cheaper alternatives for a basket item — a different brand or a bigger
    pack with a better unit price.

    `alternatives` are offers for *related but not identical* products
    (same category, no hard taxonomy conflict), supplied by the caller.
    """
    current = item.cheapest()
    if current is None:
        return []

    results: List[Substitution] = []
    for alt in alternatives:
        if not alt.in_stock or alt.price >= current.price:
            continue

        saving = round(current.price - alt.price, 2)
        if saving < min_saving:
            continue

        if current.unit_price and alt.unit_price and alt.unit_price < current.unit_price:
            reason = (
                f"Better value per {current.unit_price_label or 'unit'} "
                f"({alt.unit_price:.2f} vs {current.unit_price:.2f})"
            )
        else:
            reason = f"Cheaper at {alt.store_name}"

        results.append(
            Substitution(reason=reason, saving=saving, from_offer=current, to_offer=alt)
        )

    results.sort(key=lambda s: s.saving, reverse=True)
    return results
