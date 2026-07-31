"""
Product name normalisation and structured attribute extraction.

Raw product names arrive in wildly different shapes per retailer:

    "PnP UHT Full Cream Milk 6 x 1L"          (Pick n Pay)
    "Eskort Smoked Viennas 500g"              (Checkers)
    "Woolworths Free Range Chicken Fillets 800 g"

This module turns those into a comparable structure:

    ProductAttrs(
        brand="pnp",
        core_tokens=("uht", "full", "cream", "milk"),
        quantity=1.0, unit="l", pack_count=6,
        total_quantity=6.0, base_unit="l",
        facets={"milk_fat": "full_cream"},
    )

`total_quantity` in a canonical base unit is what makes unit-price
comparison ("R/100g", "R/L") possible across retailers — a feature the
competition only exposes inconsistently.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from .taxonomy import extract_facets

# ─────────────────────────────────────────────────────────────────────────────
# Unit handling
# ─────────────────────────────────────────────────────────────────────────────

# Everything reduces to one of three base units so cross-retailer unit
# pricing is always apples-to-apples.
BASE_MASS = "g"
BASE_VOLUME = "ml"
BASE_COUNT = "ea"

UNIT_TO_BASE: Dict[str, Tuple[str, float]] = {
    # mass
    "g": (BASE_MASS, 1.0),
    "gr": (BASE_MASS, 1.0),
    "gram": (BASE_MASS, 1.0),
    "grams": (BASE_MASS, 1.0),
    "kg": (BASE_MASS, 1000.0),
    "kgs": (BASE_MASS, 1000.0),
    "kilogram": (BASE_MASS, 1000.0),
    "mg": (BASE_MASS, 0.001),
    # volume
    "ml": (BASE_VOLUME, 1.0),
    "mls": (BASE_VOLUME, 1.0),
    "millilitre": (BASE_VOLUME, 1.0),
    "l": (BASE_VOLUME, 1000.0),
    "lt": (BASE_VOLUME, 1000.0),
    "ltr": (BASE_VOLUME, 1000.0),
    "litre": (BASE_VOLUME, 1000.0),
    "liter": (BASE_VOLUME, 1000.0),
    # count
    "ea": (BASE_COUNT, 1.0),
    "each": (BASE_COUNT, 1.0),
    "pack": (BASE_COUNT, 1.0),
    "pk": (BASE_COUNT, 1.0),
    "s": (BASE_COUNT, 1.0),
    "pc": (BASE_COUNT, 1.0),
    "pcs": (BASE_COUNT, 1.0),
    "piece": (BASE_COUNT, 1.0),
    "pieces": (BASE_COUNT, 1.0),
    "roll": (BASE_COUNT, 1.0),
    "rolls": (BASE_COUNT, 1.0),
    "bag": (BASE_COUNT, 1.0),
    "tin": (BASE_COUNT, 1.0),
    "tins": (BASE_COUNT, 1.0),
    "can": (BASE_COUNT, 1.0),
    "cans": (BASE_COUNT, 1.0),
    "bottle": (BASE_COUNT, 1.0),
    "sachet": (BASE_COUNT, 1.0),
    "sachets": (BASE_COUNT, 1.0),
}

_UNIT_ALTERNATION = "|".join(
    sorted(UNIT_TO_BASE.keys(), key=len, reverse=True)
)

# "6 x 1L", "6x1L", "4 X 20g"
RE_MULTIPACK = re.compile(
    rf"(?P<count>\d+)\s*[x×]\s*(?P<qty>\d+(?:[.,]\d+)?)\s*(?P<unit>{_UNIT_ALTERNATION})\b",
    re.IGNORECASE,
)

# "500g", "1.5 kg", "750 ml", "2L"
RE_QTY_UNIT = re.compile(
    rf"(?P<qty>\d+(?:[.,]\d+)?)\s*(?P<unit>{_UNIT_ALTERNATION})\b",
    re.IGNORECASE,
)

# "12s", "10-Pack", "8 Pack", "6 pack"
RE_PACK_COUNT = re.compile(
    r"\b(?P<count>\d+)\s*(?:-|\s)?\s*(?:s|pack|pk|pack\b|'s)\b",
    re.IGNORECASE,
)

# Marketing noise that carries no identity information.
STOPWORDS = frozenset(
    {
        "the", "a", "an", "and", "or", "with", "per", "of", "for", "in",
        "new", "value", "special", "offer", "assorted", "asst", "variety",
        "select", "selected", "type", "may", "vary", "each", "approx",
        "approximately", "pack", "packet", "bag", "box", "punnet",
    }
)

# Retailer own-brands and common SA brands. Used to separate brand from the
# product's core identity, so "PnP Rice" and "Tastic Rice" are recognised as
# the same *product type* from different brands (a substitution candidate)
# rather than two unrelated items.
KNOWN_BRANDS = frozenset(
    {
        # retailer own-brands
        "pnp", "pick n pay", "picknpay", "no name", "noname",
        "checkers", "housebrand", "house brand", "ritebrand",
        "woolworths", "woolies", "wcafe",
        "shoprite", "usave", "spar", "freshline", "boxer",
        # common SA FMCG brands
        "albany", "sasko", "blue ribbon", "clover", "parmalat", "douglasdale",
        "first choice", "danone", "nestle", "tastic", "spekko", "aunt caroline",
        "iwisa", "ace", "white star", "eskort", "rainbow", "county fair",
        "farmer brown", "bokomo", "jungle", "kellogg", "kelloggs", "purity",
        "koo", "all gold", "rhodes", "lucky star", "john west", "sea harvest",
        "black cat", "yum yum", "nutella", "beacon", "cadbury", "nestl",
        "coca cola", "coca-cola", "coke", "pepsi", "fanta", "sprite", "stoney",
        "twinsaver", "baby soft", "kleenex", "sunlight", "omo", "surf", "ariel",
        "handy andy", "domestos", "jik", "colgate", "aquafresh", "sensodyne",
        "dettol", "lifebuoy", "protex", "vaseline", "nivea", "dove",
        "huggies", "pampers", "cussons", "johnson", "johnsons",
        "five roses", "freshpak", "joko", "ricoffy", "nescafe", "jacobs",
        "hartlief", "enterprise", "bull brand", "marltons", "bobtail",
        "epol", "montego", "hills", "royal canin", "whiskas", "pedigree",
        "simba", "lays", "doritos", "nik naks", "willards", "bakers",
        "ouma", "tennis", "romany creams", "eet sum mor",
        "castle", "black label", "windhoek", "amstel", "heineken", "savanna",
        "hunters", "brutal fruit", "absolut", "smirnoff", "klipdrift",
    }
)


@dataclass(frozen=True)
class ProductAttrs:
    """Structured, comparable representation of a product name."""

    raw: str
    normalised: str
    brand: Optional[str]
    core_tokens: Tuple[str, ...]
    quantity: Optional[float]
    unit: Optional[str]
    pack_count: Optional[int]
    total_quantity: Optional[float]
    base_unit: Optional[str]
    facets: Dict[str, str] = field(default_factory=dict)

    @property
    def core_text(self) -> str:
        """Brand- and size-stripped text, used for fuzzy scoring."""
        return " ".join(self.core_tokens)

    def unit_price(self, price: float) -> Optional[Tuple[float, str]]:
        """
        Price per canonical unit.

        Returns (price_per_unit, label) e.g. (1.60, "per 100ml"),
        or None when the size is unknown.
        """
        if not self.total_quantity or not self.base_unit or self.total_quantity <= 0:
            return None

        if self.base_unit == BASE_MASS:
            return (price / self.total_quantity * 100.0, "per 100g")
        if self.base_unit == BASE_VOLUME:
            return (price / self.total_quantity * 100.0, "per 100ml")
        return (price / self.total_quantity, "each")


def _strip_accents(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c)
    )


def normalise_text(text: str) -> str:
    """Lowercase, strip accents and punctuation, collapse whitespace."""
    text = _strip_accents(text or "").lower()
    text = text.replace("&", " and ")
    # keep alphanumerics, spaces, decimal points inside numbers, and x
    text = re.sub(r"[^a-z0-9.,%x×\s-]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _extract_brand(text: str) -> Tuple[Optional[str], str]:
    """
    Pull a known brand out of the name.

    Longest brand match wins so "pick n pay" beats "pnp" when both appear.
    Returns (brand_or_none, text_without_brand).
    """
    best: Optional[str] = None
    for brand in KNOWN_BRANDS:
        if re.search(r"\b" + re.escape(brand) + r"\b", text):
            if best is None or len(brand) > len(best):
                best = brand
    if best is None:
        return None, text
    stripped = re.sub(r"\b" + re.escape(best) + r"\b", " ", text)
    return best, re.sub(r"\s+", " ", stripped).strip()


def _extract_size(text: str) -> Tuple[Optional[float], Optional[str], Optional[int], str]:
    """
    Extract quantity, unit and pack count, and return the text with the
    size expression removed.

    Multipack ("6 x 1L") is checked first so the count is not lost.
    """
    quantity: Optional[float] = None
    unit: Optional[str] = None
    pack_count: Optional[int] = None

    multipack = RE_MULTIPACK.search(text)
    if multipack:
        pack_count = int(multipack.group("count"))
        quantity = float(multipack.group("qty").replace(",", "."))
        unit = multipack.group("unit").lower()
        text = text[: multipack.start()] + " " + text[multipack.end() :]
    else:
        qty_match = RE_QTY_UNIT.search(text)
        if qty_match:
            quantity = float(qty_match.group("qty").replace(",", "."))
            unit = qty_match.group("unit").lower()
            text = text[: qty_match.start()] + " " + text[qty_match.end() :]

        pack_match = RE_PACK_COUNT.search(text)
        if pack_match:
            pack_count = int(pack_match.group("count"))
            text = text[: pack_match.start()] + " " + text[pack_match.end() :]

    return quantity, unit, pack_count, re.sub(r"\s+", " ", text).strip()


def parse(
    raw_name: str,
    *,
    known_brand: Optional[str] = None,
    known_quantity: Optional[float] = None,
    known_unit: Optional[str] = None,
    known_pack_count: Optional[int] = None,
) -> ProductAttrs:
    """
    Parse a raw product name into structured attributes.

    The RapidAPI feed already supplies `brand`, `quantity`, `unit` and
    `pack_count` for many rows — pass them in as `known_*` and they take
    priority over anything inferred from the string.
    """
    normalised = normalise_text(raw_name)

    # Facets are read from the FULL normalised text: size and brand removal
    # can destroy signal (e.g. "Woolworths Free Range Chicken").
    facets = extract_facets(normalised)

    brand, without_brand = _extract_brand(normalised)
    quantity, unit, pack_count, core = _extract_size(without_brand)

    if known_brand and known_brand.upper() not in {"OTHER", "NONE", ""}:
        brand = normalise_text(known_brand) or brand
    if known_quantity is not None:
        quantity = known_quantity
    if known_unit:
        unit = known_unit.lower()
    if known_pack_count is not None:
        pack_count = known_pack_count

    # Drop stopwords, bare numbers, and orphaned unit tokens (e.g. the "kg"
    # left behind by "Pork Roast Per kg", which has no numeric quantity).
    tokens = tuple(
        t
        for t in core.split()
        if t
        and t not in STOPWORDS
        and t not in UNIT_TO_BASE
        and not t.isdigit()
        and not re.fullmatch(r"[\d.,]+", t)
    )

    # Reduce to a canonical base unit for cross-retailer unit pricing.
    total_quantity: Optional[float] = None
    base_unit: Optional[str] = None
    if unit and unit in UNIT_TO_BASE:
        base_unit, factor = UNIT_TO_BASE[unit]
        if quantity is not None:
            total_quantity = quantity * factor * (pack_count or 1)
    elif pack_count:
        base_unit, total_quantity = BASE_COUNT, float(pack_count)

    return ProductAttrs(
        raw=raw_name,
        normalised=normalised,
        brand=brand,
        core_tokens=tokens,
        quantity=quantity,
        unit=unit,
        pack_count=pack_count,
        total_quantity=total_quantity,
        base_unit=base_unit,
        facets=facets,
    )
