"""
Product taxonomy and mutual-exclusion rules.

THIS MODULE IS THE CORE DIFFERENTIATOR.

Background
----------
Competitor testing (Grocify, July 2026) showed that a search for "chicken
fillet" returned "Steakhouse Classic Beef Steak Fillet" as the top result.
That is the classic failure mode of raw trigram / fuzzy string similarity:
"fillet" is a strong shared token, and nothing in the model knows that
*chicken* and *beef* are mutually exclusive.

Chipa fixes this with an explicit conflict taxonomy. Two product names that
each assert a value from the same exclusive facet, with DIFFERENT values,
can never be the same product — regardless of how similar the strings look.

This turns matching from "how similar are these strings" into
"are these the same thing, and how confident am I".

Facets
------
Each facet is a group of mutually exclusive terms. If product A asserts
`protein=chicken` and product B asserts `protein=beef`, they conflict. For
hard-veto facets the match is rejected outright; for soft facets it is
heavily penalised but may still surface as a suggested substitute.

Terms are matched on normalised word boundaries, so "beefsteak" will not
accidentally trip the "beef" rule via substring matching.
"""

from __future__ import annotations

import re
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

# ─────────────────────────────────────────────────────────────────────────────
# Exclusive facets
#
# Format: facet_name -> { canonical_value: (synonyms...) }
# A product "asserts" a value if any synonym appears as a whole word/phrase.
# ─────────────────────────────────────────────────────────────────────────────

EXCLUSIVE_FACETS: Dict[str, Dict[str, Tuple[str, ...]]] = {
    # ── Protein / animal source ──────────────────────────────────────────────
    # The single highest-value facet: it is the one Grocify gets wrong.
    "protein": {
        "chicken": ("chicken", "chickens", "poultry", "hoender"),
        "beef": ("beef", "steak", "steaks", "rump", "sirloin", "brisket"),
        "pork": ("pork", "bacon", "gammon", "ham", "vienna", "viennas"),
        "lamb": ("lamb", "mutton", "skaap"),
        "fish": (
            "fish", "hake", "tuna", "salmon", "pilchard", "pilchards",
            "sardine", "sardines", "snoek", "kingklip", "prawn", "prawns",
            "shrimp", "calamari", "mussel", "mussels", "anchovy", "anchovies",
        ),
        "turkey": ("turkey",),
        "duck": ("duck",),
        "ostrich": ("ostrich",),
        "vegetarian": (
            "vegetarian", "vegan", "meat free", "meatfree", "plant based",
            "plantbased", "soya mince", "tofu",
        ),
    },

    # ── Dairy fat content ────────────────────────────────────────────────────
    # "Full cream milk" vs "fat free milk" are different products at
    # different prices. Trigram similarity treats them as near-identical.
    "milk_fat": {
        "full_cream": ("full cream", "fullcream", "full-cream", "volroom"),
        "low_fat": ("low fat", "lowfat", "low-fat"),
        "fat_free": ("fat free", "fatfree", "fat-free", "skim", "skimmed"),
    },

    # ── Bread / flour type ───────────────────────────────────────────────────
    "bread_type": {
        "white": ("white",),
        "brown": ("brown",),
        "whole_wheat": (
            "whole wheat", "wholewheat", "wholemeal", "whole grain", "wholegrain",
        ),
        "rye": ("rye",),
        "low_gi": ("low gi", "lowgi", "low-gi"),
        "seeded": ("seeded", "multiseed", "multi seed"),
    },

    # ── Caffeine ─────────────────────────────────────────────────────────────
    "caffeine": {
        "regular": ("caffeinated",),
        "decaf": ("decaf", "decaffeinated"),
    },

    # ── Sugar content ────────────────────────────────────────────────────────
    # Coke vs Coke Zero: same brand, same size, completely different SKU.
    "sugar": {
        "diet": ("diet", "lite"),
        "zero": ("zero", "no sugar", "sugar free", "sugarfree", "sugar-free"),
    },

    # ── Preparation state ────────────────────────────────────────────────────
    # Frozen peas and fresh peas are not substitutes at the shelf.
    "preparation": {
        "fresh": ("fresh", "chilled"),
        "frozen": ("frozen",),
        "tinned": ("tinned", "canned", "in brine", "in oil"),
        "dried": ("dried", "dehydrated"),
    },

    # ── Cut / form (within the same protein) ─────────────────────────────────
    # Soft facet: a shopper may accept thighs instead of breasts.
    "cut": {
        "fillet": ("fillet", "fillets", "filet"),
        "breast": ("breast", "breasts"),
        "thigh": ("thigh", "thighs"),
        "drumstick": ("drumstick", "drumsticks"),
        "wing": ("wing", "wings"),
        "mince": ("mince", "ground"),
        "rib": ("rib", "ribs", "spare rib"),
    },

    # ── Pet food target ──────────────────────────────────────────────────────
    "pet": {
        "dog": ("dog", "puppy"),
        "cat": ("cat", "kitten"),
    },
}

# Facets where a conflict is a HARD veto (never the same product).
HARD_VETO_FACETS: FrozenSet[str] = frozenset(
    {"protein", "milk_fat", "sugar", "caffeine", "pet", "preparation"}
)

# Facets where a conflict is a strong penalty but not fatal.
SOFT_CONFLICT_PENALTY = 0.45

# Facets where SILENCE MEANS SOMETHING.
#
# Normally, a product that says nothing about a facet is treated as merely
# less specific — "Milk 2L" does not conflict with "Full Cream Milk 2L",
# because plain milk might well be full cream.
#
# For a few facets that logic is wrong. "Coca-Cola 2L" is not an unspecified
# variant of Coke that might turn out to be Coke Zero — it IS the regular
# one. Absence of the marker is itself a claim. Without this rule, Coke and
# Coke Zero (different products, different prices) would be linked as one.
DEFAULTED_FACETS: Dict[str, str] = {
    "sugar": "regular",       # unmarked => full sugar
    "caffeine": "regular",    # unmarked => caffeinated
}


# ─────────────────────────────────────────────────────────────────────────────
# Compiled matchers
#
# Built once at import. Phrases (multi-word synonyms) are checked before
# single words so that "full cream" wins over a stray "cream".
# ─────────────────────────────────────────────────────────────────────────────

def _compile() -> Dict[str, List[Tuple[str, re.Pattern]]]:
    compiled: Dict[str, List[Tuple[str, re.Pattern]]] = {}
    for facet, values in EXCLUSIVE_FACETS.items():
        entries: List[Tuple[str, re.Pattern, int]] = []
        for value, synonyms in values.items():
            for syn in synonyms:
                # \b word boundaries prevent "beefsteak" tripping "beef"
                # and prevent "ham" matching inside "hamper".
                pattern = re.compile(r"\b" + re.escape(syn) + r"\b", re.IGNORECASE)
                entries.append((value, pattern, len(syn)))
        # Longest synonym first — "full cream" before "cream".
        entries.sort(key=lambda e: e[2], reverse=True)
        compiled[facet] = [(v, p) for v, p, _ in entries]
    return compiled


_COMPILED = _compile()


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def extract_facets(text: str) -> Dict[str, str]:
    """
    Extract asserted facet values from a product name.

    Returns a dict of facet -> value for every facet the text asserts.
    A facet is omitted entirely if the text says nothing about it.

    >>> extract_facets("PnP Skinless Chicken Fillet Breast 12s")
    {'protein': 'chicken', 'cut': 'fillet'}
    >>> extract_facets("Steakhouse Classic Beef Steak Fillet Per kg")
    {'protein': 'beef', 'cut': 'fillet'}
    """
    found: Dict[str, str] = {}
    for facet, entries in _COMPILED.items():
        for value, pattern in entries:
            if pattern.search(text):
                found[facet] = value
                break  # first (longest) match wins for this facet
    return found


def facet_conflict(
    facets_a: Dict[str, str],
    facets_b: Dict[str, str],
) -> Tuple[bool, List[str], List[str]]:
    """
    Compare two facet dicts.

    Returns (has_hard_veto, hard_conflicts, soft_conflicts).

    Only facets asserted by BOTH sides can conflict. If one product says
    nothing about protein, it does not conflict with one that does — it is
    simply less specific (e.g. "Milk 2L" vs "Full Cream Milk 2L").
    """
    hard: List[str] = []
    soft: List[str] = []

    # Facets asserted by both sides.
    comparable: Set[str] = set(facets_a) & set(facets_b)

    # Plus facets where silence is itself an assertion (see DEFAULTED_FACETS):
    # if either side marks it, the other side's silence means "the default".
    for facet in DEFAULTED_FACETS:
        if facet in facets_a or facet in facets_b:
            comparable.add(facet)

    for facet in comparable:
        default = DEFAULTED_FACETS.get(facet)
        value_a = facets_a.get(facet, default)
        value_b = facets_b.get(facet, default)
        if value_a is None or value_b is None:
            continue
        if value_a != value_b:
            if facet in HARD_VETO_FACETS:
                hard.append(facet)
            else:
                soft.append(facet)

    return (len(hard) > 0, sorted(hard), sorted(soft))


def explain_conflict(
    facets_a: Dict[str, str],
    facets_b: Dict[str, str],
    facet: str,
) -> str:
    """Human-readable reason, used for debugging and for the match-audit API."""
    return (
        f"{facet}: {facets_a.get(facet, '—')} != {facets_b.get(facet, '—')}"
    )
