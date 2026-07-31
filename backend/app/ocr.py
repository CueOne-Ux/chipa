"""
PDF catalog ingestion with OCR.

Why this exists
---------------
Several South African retailers have NO structured price feed and no online
store — Food Lover's Market being the clearest example (franchise model,
marketing site only, weekly specials published as a printed-leaflet PDF).
Boxer, Usave and independent SPAR branches are similar.

Competitors cannot compare those retailers at all. Chipa can, by letting the
user upload the specials PDF and extracting offers from it.

Pipeline
--------
1. Open the PDF (PyMuPDF).
2. Per page, try the embedded text layer first — fast, exact, free.
3. If a page has little or no text (a flattened graphic — which is what
   most supermarket leaflets are), rasterise it at `settings.ocr_dpi` and
   run Tesseract over it.
4. Parse product/price pairs out of the resulting text.
5. Normalise each offer through the same `app.normalize` pipeline used for
   feed products, so catalog offers are directly comparable to feed prices.

Price parsing is deliberately conservative: a leaflet is noisy, and a wrong
price is worse than a missing one. Everything carries a confidence score.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional, Tuple

from .config import settings
from .normalize import parse

logger = logging.getLogger(__name__)

# A page with fewer than this many characters in its text layer is treated
# as a scanned/flattened image and sent to OCR.
MIN_TEXT_LAYER_CHARS = 120


# ─────────────────────────────────────────────────────────────────────────────
# Price patterns
#
# SA leaflets write prices many ways:
#   R49.99   R 49.99   4999 (with superscript cents)   49.99
#   2 for R50    Any 3 for R100    Save R10
# ─────────────────────────────────────────────────────────────────────────────

RE_PRICE = re.compile(
    r"""
    (?:R|ZAR)\s*
    (?P<rand>\d{1,4})
    (?:
        [.,](?P<cents>\d{2})
      | \s*(?P<cents_sup>\d{2})(?![\d])
    )?
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Bare decimal price with no R prefix, e.g. "49.99" on its own line.
RE_BARE_PRICE = re.compile(r"^\s*(?P<rand>\d{1,4})[.,](?P<cents>\d{2})\s*$")

RE_MULTIBUY = re.compile(
    r"\b(?:any\s+)?(?P<count>\d+)\s*for\s*(?:R|ZAR)?\s*(?P<amount>\d{1,4}(?:[.,]\d{2})?)",
    re.IGNORECASE,
)

RE_SAVE = re.compile(
    r"\bsave\s*(?:R|ZAR)?\s*(?P<amount>\d{1,4}(?:[.,]\d{2})?)", re.IGNORECASE
)

RE_WAS = re.compile(
    r"\b(?:was|orig(?:inal)?)\s*(?:R|ZAR)?\s*(?P<amount>\d{1,4}(?:[.,]\d{2})?)",
    re.IGNORECASE,
)

RE_VALID = re.compile(
    r"\b(?:valid|offers?\s+valid|specials?\s+valid)[^\d]{0,20}"
    r"(?P<from>\d{1,2}\s*\w*\s*\w*)\s*(?:to|-|until|–)\s*(?P<to>\d{1,2}\s+\w+\s+\d{2,4})",
    re.IGNORECASE,
)

# Lines that are pure noise and never a product name.
NOISE_LINE = re.compile(
    r"^\s*(?:"
    r"terms?\s+(?:and|&)\s+conditions|e\s*&\s*oe|while\s+stocks?\s+last|"
    r"page\s*\d+|www\.|https?:|tel:|\d{3}\s*\d{3}\s*\d{4}|"
    r"prices?\s+valid|excludes?|selected\s+stores?|"
    r"[^a-z0-9]*"
    r")\s*$",
    re.IGNORECASE,
)


def _money(rand: str, cents: Optional[str]) -> float:
    return float(rand) + (float(cents) / 100.0 if cents else 0.0)


def extract_prices(text: str) -> List[float]:
    """All prices found in a chunk of text, in order of appearance."""
    prices: List[float] = []
    for m in RE_PRICE.finditer(text):
        cents = m.group("cents") or m.group("cents_sup")
        prices.append(_money(m.group("rand"), cents))
    return prices


# ─────────────────────────────────────────────────────────────────────────────
# Text extraction
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class PageText:
    page: int
    text: str
    source: str          # 'text_layer' | 'ocr'
    ocr_confidence: Optional[float] = None


def _ocr_page(pdf_page, dpi: int, lang: str) -> Tuple[str, Optional[float]]:
    """Rasterise a page and OCR it. Returns (text, mean_confidence 0-1)."""
    try:
        import pytesseract
        from PIL import Image
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "OCR dependencies missing — install pytesseract and pillow, "
            "and ensure the tesseract binary is on PATH."
        ) from exc

    zoom = dpi / 72.0
    import fitz

    pixmap = pdf_page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    image = Image.open(io.BytesIO(pixmap.tobytes("png")))

    # Grayscale improves Tesseract accuracy on colourful leaflets.
    image = image.convert("L")

    data = pytesseract.image_to_data(
        image, lang=lang, output_type=pytesseract.Output.DICT
    )

    words: List[str] = []
    confidences: List[float] = []
    for word, conf in zip(data.get("text", []), data.get("conf", [])):
        if not word or not word.strip():
            continue
        try:
            conf_value = float(conf)
        except (TypeError, ValueError):
            continue
        if conf_value < 0:
            continue
        words.append(word)
        confidences.append(conf_value / 100.0)

    # Rebuild line structure — image_to_data loses newlines, and line
    # grouping matters for associating a product name with its price.
    text = pytesseract.image_to_string(image, lang=lang)
    mean_conf = sum(confidences) / len(confidences) if confidences else None
    return text, mean_conf


def extract_pages(pdf_bytes: bytes, *, dpi: Optional[int] = None, lang: Optional[str] = None) -> List[PageText]:
    """
    Extract text from every page, using the embedded text layer where
    available and falling back to OCR for flattened graphics.
    """
    import fitz

    dpi = dpi or settings.ocr_dpi
    lang = lang or settings.ocr_lang

    pages: List[PageText] = []
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for index, page in enumerate(doc, start=1):
            layer_text = page.get_text("text") or ""
            if len(layer_text.strip()) >= MIN_TEXT_LAYER_CHARS:
                pages.append(PageText(index, layer_text, "text_layer"))
                continue

            try:
                ocr_text, confidence = _ocr_page(page, dpi, lang)
                # Keep whichever is richer — occasionally a page has a thin
                # text layer AND useful graphics.
                if len(layer_text.strip()) > len(ocr_text.strip()):
                    pages.append(PageText(index, layer_text, "text_layer"))
                else:
                    pages.append(PageText(index, ocr_text, "ocr", confidence))
            except Exception as exc:
                logger.warning("OCR failed on page %d: %s", index, exc)
                pages.append(PageText(index, layer_text, "text_layer"))

    return pages


# ─────────────────────────────────────────────────────────────────────────────
# Offer parsing
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ParsedOffer:
    product_name: str
    price: Optional[float]
    was_price: Optional[float] = None
    promo_text: Optional[str] = None
    page: Optional[int] = None
    raw_text: Optional[str] = None
    confidence: float = 0.5
    attrs: Optional[Any] = None

    def to_row(self) -> Dict[str, Any]:
        attrs = self.attrs or parse(self.product_name)
        unit_price_value: Optional[float] = None
        unit_price_label: Optional[str] = None
        if self.price is not None:
            computed = attrs.unit_price(self.price)
            if computed:
                unit_price_value, unit_price_label = computed
        return {
            "product_name": self.product_name,
            "raw_text": self.raw_text,
            "price": self.price,
            "was_price": self.was_price,
            "promo_text": self.promo_text,
            "page": self.page,
            "core_text": attrs.core_text,
            "facets": attrs.facets,
            "total_quantity": attrs.total_quantity,
            "base_unit": attrs.base_unit,
            "unit_price": unit_price_value,
            "unit_price_label": unit_price_label,
            "ocr_confidence": self.confidence,
        }


# Words that are price modifiers, never a product name on their own.
# Without this, stripping the price out of "R29.99  Was R34.99" leaves the
# bare word "Was", which would otherwise be stored as a product.
NAME_STOPWORDS = frozenset(
    {
        "was", "save", "now", "from", "only", "each", "per", "ea",
        "special", "specials", "offer", "offers", "deal", "deals",
        "new", "buy", "free", "any", "price", "prices", "off",
        "and", "or", "the", "for", "x", "combo", "value",
    }
)


def _is_plausible_name(line: str) -> bool:
    """A product name needs letters, and must not be pure price furniture."""
    stripped = line.strip()
    if len(stripped) < 3 or len(stripped) > 120:
        return False
    if NOISE_LINE.match(stripped):
        return False
    letters = sum(c.isalpha() for c in stripped)
    if letters < 3:
        return False
    # Mostly-digits lines are prices or page furniture, not names.
    if letters / max(len(stripped), 1) < 0.35:
        return False
    # Reject lines made up entirely of price-modifier words.
    words = re.findall(r"[a-zA-Z]+", stripped.lower())
    if not words or all(word in NAME_STOPWORDS for word in words):
        return False
    return True


def _strip_modifiers(text: str) -> str:
    """
    Remove promo/was/multibuy phrases BEFORE prices.

    Order matters: stripping prices first would turn "Was R34.99" into a
    naked "Was" that the was-pattern can no longer recognise and remove.
    """
    cleaned = RE_MULTIBUY.sub(" ", text)
    cleaned = RE_SAVE.sub(" ", cleaned)
    cleaned = RE_WAS.sub(" ", cleaned)
    return cleaned


def parse_offers(
    pages: List[PageText],
    *,
    max_lookback: int = 3,
) -> List[ParsedOffer]:
    """
    Turn page text into offers.

    Leaflet layout is unreliable, so we use a proximity heuristic: when a
    price is found, the product name is the nearest preceding plausible
    text line (within `max_lookback` lines). This handles the two dominant
    layouts — name above price, and name and price on one line.
    """
    offers: List[ParsedOffer] = []

    for page in pages:
        lines = [ln.strip() for ln in page.text.splitlines()]
        base_conf = page.ocr_confidence if page.source == "ocr" else 0.95

        for index, line in enumerate(lines):
            if not line:
                continue

            multibuy = RE_MULTIBUY.search(line)

            # Prices that remain once "Was R34.99" / "Save R4.00" are removed
            # are the actual offer prices. A line containing ONLY a was/save
            # amount is a modifier for a neighbouring item, not an offer.
            offer_line = _strip_modifiers(line)
            inline_prices = extract_prices(offer_line)
            bare = RE_BARE_PRICE.match(offer_line.strip())

            price: Optional[float] = None
            promo_text: Optional[str] = None

            if multibuy:
                count = int(multibuy.group("count"))
                amount = float(multibuy.group("amount").replace(",", "."))
                if count > 0:
                    price = round(amount / count, 2)
                    promo_text = multibuy.group(0).strip()
            elif inline_prices:
                # Lowest remaining price is the offer price.
                price = min(inline_prices)
            elif bare:
                price = _money(bare.group("rand"), bare.group("cents"))

            if price is None:
                continue

            # Ignore implausible values — leaflet page numbers, years, etc.
            if price <= 0 or price > 9999:
                continue

            # ── Find the product name ────────────────────────────────────────
            name: Optional[str] = None

            # Case 1: name and price share a line.
            without_price = RE_PRICE.sub(" ", _strip_modifiers(line))
            without_price = re.sub(r"\s+", " ", without_price).strip(" -–—:•\t")
            if _is_plausible_name(without_price):
                name = without_price

            # Case 2: name sits on a preceding line.
            if not name:
                for back in range(1, max_lookback + 1):
                    if index - back < 0:
                        break
                    candidate = lines[index - back]
                    if not candidate:
                        continue
                    if extract_prices(candidate) or RE_BARE_PRICE.match(candidate.strip()):
                        continue
                    if _is_plausible_name(candidate):
                        name = candidate.strip(" -–—:•\t")
                        break

            if not name:
                continue

            # ── Supporting detail ────────────────────────────────────────────
            # Scope tightly to THIS offer: the price line itself, plus the
            # next line only when that line is a bare modifier (a "Was ..."
            # or "Save ..." with no product name of its own). A wider window
            # bleeds the neighbouring item's promo onto this one.
            context_parts = [line]
            if index + 1 < len(lines):
                nxt = lines[index + 1].strip()
                if nxt and not _is_plausible_name(_strip_modifiers(nxt)):
                    if RE_WAS.search(nxt) or RE_SAVE.search(nxt):
                        context_parts.append(nxt)
            context = " ".join(context_parts)

            was_price: Optional[float] = None
            was_match = RE_WAS.search(context)
            if was_match:
                candidate_was = float(was_match.group("amount").replace(",", "."))
                if candidate_was > price:
                    was_price = candidate_was
            if was_price is None and len(inline_prices) > 1:
                higher = max(inline_prices)
                if higher > price:
                    was_price = higher

            if not promo_text:
                save_match = RE_SAVE.search(context)
                if save_match:
                    promo_text = save_match.group(0).strip()

            # ── Confidence ───────────────────────────────────────────────────
            confidence = base_conf or 0.5
            if was_price or promo_text:
                confidence = min(1.0, confidence + 0.05)
            if len(name) < 6:
                confidence *= 0.8

            attrs = parse(name)
            # Drop OCR garbage — but a name made up entirely of a known brand
            # and a size ("Coca-Cola 2L") is legitimate and leaves no core
            # tokens behind, so accept it when a brand was identified.
            if not attrs.core_tokens and not attrs.brand:
                continue

            offers.append(
                ParsedOffer(
                    product_name=name,
                    price=price,
                    was_price=was_price,
                    promo_text=promo_text,
                    page=page.page,
                    raw_text=line,
                    confidence=round(min(1.0, max(0.0, confidence)), 3),
                    attrs=attrs,
                )
            )

    return _dedupe(offers)


def _dedupe(offers: List[ParsedOffer]) -> List[ParsedOffer]:
    """Collapse repeats of the same name+price, keeping the most confident."""
    best: Dict[Tuple[str, Optional[float]], ParsedOffer] = {}
    for offer in offers:
        key = (offer.product_name.strip().lower(), offer.price)
        current = best.get(key)
        if current is None or offer.confidence > current.confidence:
            best[key] = offer
    return sorted(best.values(), key=lambda o: (o.page or 0, -o.confidence))


def find_validity(pages: List[PageText]) -> Tuple[Optional[str], Optional[str]]:
    """Best-effort extraction of the 'valid from/to' dates on a leaflet."""
    for page in pages[:3]:
        match = RE_VALID.search(page.text)
        if match:
            return match.group("from").strip(), match.group("to").strip()
    return None, None


def process_pdf(
    pdf_bytes: bytes,
    *,
    dpi: Optional[int] = None,
    lang: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Full pipeline entry point.

    Returns a dict with page count, extraction mode, parsed offers and the
    detected validity window.
    """
    pages = extract_pages(pdf_bytes, dpi=dpi, lang=lang)
    offers = parse_offers(pages)
    sources = {page.source for page in pages}
    extraction = (
        "mixed" if len(sources) > 1 else (sources.pop() if sources else "text_layer")
    )
    valid_from, valid_to = find_validity(pages)

    return {
        "page_count": len(pages),
        "extraction": extraction,
        "offers": offers,
        "valid_from_text": valid_from,
        "valid_to_text": valid_to,
        "pages": pages,
    }
