"""
Scraper for troli.co.za search pages.

Parsing reference (confirmed against live HTML 2026-04-02):
  - Product card:  div[class contains 'rounded-md flex-col hover:shadow-lg']
  - Product ID:    button.product-item[hx-get]  →  regex id=(\w+)
  - Name:          p.text-xs.font-semibold.break-words  (text content)
  - Current price: span.text-sm.mr-1  (first match, strip 'R')
  - Orig price:    span.line-through   (exists only when on promo)
  - Deal label:    span.text-VERMILIOM-1000.font-normal  (may be empty)
  - Retailer:      img[alt='logo'] src
      checkers.co.za / shoprite  → Checkers   (c)
      PIK.JO / pnp / picknpay   → Pick n Pay  (p)
      woolworths / woolies       → Woolworths  (w)
"""

from __future__ import annotations

import re
import logging

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-ZA,en;q=0.9",
}

BASE_URL = "https://troli.co.za/app/search"


def _retailer_from_logo(src: str) -> tuple[str, str]:
    s = src.lower()
    if "checkers" in s or "shoprite" in s:
        return "Checkers", "c"
    if "pnp" in s or "picknpay" in s or "pik.jo" in s:
        return "Pick n Pay", "p"
    if "woolworths" in s or "woolies" in s:
        return "Woolworths", "w"
    return "Unknown", "u"


def _parse_price(text: str) -> float | None:
    m = re.search(r"[\d]+(?:[.,]\d+)?", text.replace(",", "."))
    return float(m.group().replace(",", ".")) if m else None


def _parse_card(card) -> dict | None:
    # ── Product ID ────────────────────────────────────────────────────────────
    btn = card.find("button", class_=lambda c: c and "product-item" in c)
    if not btn:
        return None
    m = re.search(r"id=([^&\"]+)", btn.get("hx-get", ""))
    if not m:
        return None
    product_id = m.group(1)

    # ── Name ─────────────────────────────────────────────────────────────────
    name_el = card.find("p", class_=lambda c: c and "break-words" in c)
    if not name_el:
        return None
    name = name_el.get_text(strip=True)
    if not name:
        return None

    # ── Current price ─────────────────────────────────────────────────────────
    price_span = card.find("span", class_=lambda c: c and "text-sm" in c and "mr-1" in c)
    price = _parse_price(price_span.get_text(strip=True)) if price_span else None

    # ── Original / strikethrough price (present when item is on promo) ────────
    orig_span = card.find("span", class_=lambda c: c and "line-through" in c)
    original_price = _parse_price(orig_span.get_text(strip=True)) if orig_span else None

    # ── Deal label ────────────────────────────────────────────────────────────
    deal_span = card.find(
        "span",
        class_=lambda c: c and "text-VERMILIOM-1000" in c and "font-normal" in c,
    )
    deal_text: str | None = None
    if deal_span:
        t = deal_span.get_text(strip=True)
        deal_text = t or None

    # ── If no deal label but original price exists, mark as promo ─────────────
    if original_price and not deal_text:
        deal_text = "On promo"

    # ── Retailer ─────────────────────────────────────────────────────────────
    logo_img = card.find("img", alt="logo")
    retailer, retailer_code = ("Unknown", "u")
    if logo_img:
        retailer, retailer_code = _retailer_from_logo(logo_img.get("src", ""))

    # ── Product image ─────────────────────────────────────────────────────────
    prod_img = card.find("img", class_=lambda c: c and "object-contain" in c and "pt-1" in c)
    image_url = prod_img["src"] if prod_img else None

    return {
        "product_id": product_id,
        "name": name,
        "retailer": retailer,
        "retailer_code": retailer_code,
        "price": price,
        "original_price": original_price,
        "deal_text": deal_text,
        "image_url": image_url,
    }


def _parse_html(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    cards = soup.find_all(
        "div",
        class_=lambda c: (
            c
            and "rounded-md" in c
            and "flex-col" in c
            and "hover:shadow-lg" in c
        ),
    )
    results = []
    for card in cards:
        parsed = _parse_card(card)
        if parsed:
            results.append(parsed)
    return results


async def scrape_search(term: str, pages: int = 3) -> list[dict]:
    """Fetch up to `pages` result pages from troli.co.za for `term`."""
    all_products: list[dict] = []
    seen_ids: set[str] = set()

    async with httpx.AsyncClient(headers=HEADERS, timeout=15, follow_redirects=True) as client:
        for page in range(1, pages + 1):
            params = {
                "search_term": term,
                "search_sort_by": "promo_price",
                "search_category": "",
                "search_retailer": "",
                "search_page": str(page),
            }
            try:
                resp = await client.get(BASE_URL, params=params)
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning("troli.co.za request failed (page %d): %s", page, exc)
                break

            products = _parse_html(resp.text)
            if not products:
                break  # empty page — stop paginating

            for p in products:
                if p["product_id"] not in seen_ids:
                    seen_ids.add(p["product_id"])
                    all_products.append(p)

    logger.info("Scraped '%s': %d unique products across %d page(s)", term, len(all_products), page)
    return all_products
