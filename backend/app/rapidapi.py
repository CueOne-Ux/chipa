"""
Client for the RapidAPI "South African Grocery Prices API".

Response shape confirmed by live call on 24 July 2026:

    {
      "total": 37074, "page": 1, "limit": 50,
      "stores": ["checkers", "woolworths", "pnp"],
      "total_pages": 742, "has_next": true, "has_prev": false,
      "products": [
        {
          "id": "...", "store_product_id": "...", "store_name": "checkers",
          "product_name": "Smoked Viennas", "brand": "ESKORT",
          "currency": "ZAR", "price": 49.99,
          "unit": "g", "quantity": "500.0",
          "pack_count": null, "pack_quantity": null, "pack_unit": null,
          "category": "Deli", "sub_categories": ["Viennas & Sausages"],
          "on_promotion": false, "promotion_details": null,
          "in_stock": true, "image_url": "...", "product_url": "...",
          "last_updated": "2026-07-24T00:00:00",
          "raw_product_name": "Eskort Smoked Viennas 500g",
          "raw_price_text": "49.99"
        }
      ]
    }

IMPORTANT — the feed is siloed per store. There is no cross-retailer product
identity in it; `store_product_id` and name formatting differ per retailer.
Chipa builds that layer itself (see app.matching).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, AsyncIterator, Dict, List, Optional

import httpx

from .config import settings

logger = logging.getLogger(__name__)


class RapidAPIError(RuntimeError):
    pass


class NotSubscribedError(RapidAPIError):
    """Raised on 403 — the key is valid but not subscribed to this API."""


class QuotaExceededError(RapidAPIError):
    """Raised on 429 — monthly request allowance exhausted."""


class GroceryFeedClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        host: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> None:
        self.api_key = api_key or settings.rapidapi_key
        self.host = host or settings.rapidapi_host
        self.timeout = timeout or settings.sync_timeout_s
        if not self.api_key:
            raise RapidAPIError("RAPIDAPI_KEY is not set")
        self._client: Optional[httpx.AsyncClient] = None
        self.requests_made = 0

    @property
    def base_url(self) -> str:
        return f"https://{self.host}/v1"

    @property
    def headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "x-rapidapi-host": self.host,
            "x-rapidapi-key": self.api_key,
        }

    async def __aenter__(self) -> "GroceryFeedClient":
        self._client = httpx.AsyncClient(
            base_url=self.base_url, headers=self.headers, timeout=self.timeout
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    async def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        if self._client is None:
            raise RapidAPIError("Client used outside an async context manager")

        resp = await self._client.get(path, params=params or {})
        self.requests_made += 1

        if resp.status_code == 403:
            raise NotSubscribedError(
                "Not subscribed to the API. Subscribe to a plan in the "
                "RapidAPI dashboard (Basic is free)."
            )
        if resp.status_code == 429:
            raise QuotaExceededError(
                "Monthly request quota exhausted for the current plan."
            )
        if resp.status_code >= 400:
            raise RapidAPIError(
                f"{resp.status_code} from {path}: {resp.text[:300]}"
            )
        return resp.json()

    # ── Endpoints ───────────────────────────────────────────────────────────

    async def stores(self) -> List[dict]:
        data = await self._get("/stores")
        return data.get("stores", [])

    async def categories(self) -> List[Any]:
        data = await self._get("/categories")
        return data.get("categories", data if isinstance(data, list) else [])

    async def products_page(
        self,
        page: int = 1,
        limit: Optional[int] = None,
        store: Optional[str] = None,
        category: Optional[str] = None,
    ) -> dict:
        params: Dict[str, Any] = {"page": page, "limit": limit or settings.sync_page_size}
        if category:
            params["category"] = category
        path = f"/{store}/products" if store else "/products"
        return await self._get(path, params)

    async def iter_products(
        self,
        *,
        store: Optional[str] = None,
        category: Optional[str] = None,
        max_pages: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> AsyncIterator[dict]:
        """
        Yield every product across pages.

        Respects `settings.sync_max_pages` so a runaway loop cannot burn the
        monthly quota.
        """
        ceiling = max_pages or settings.sync_max_pages
        page = 1
        while page <= ceiling:
            data = await self.products_page(
                page=page, limit=limit, store=store, category=category
            )
            products = data.get("products", [])
            if not products:
                return
            for product in products:
                yield product

            if not data.get("has_next"):
                return
            page += 1

        logger.warning(
            "Stopped at page ceiling (%d). Increase SYNC_MAX_PAGES to go further.",
            ceiling,
        )

    async def promotions(self, store: Optional[str] = None, page: int = 1) -> dict:
        path = f"/{store}/promotions" if store else "/promotions"
        return await self._get(path, {"page": page, "limit": settings.sync_page_size})

    async def price_history(self, product_name: str) -> Any:
        """Requires the Ultra plan or above."""
        return await self._get("/price-history", {"product_name": product_name})

    async def catalog_size(self) -> dict:
        """
        One cheap request that reports total products and page count.

        Use this to budget the monthly quota BEFORE committing to a paid tier:
        total_pages at your chosen limit == requests per full sync.
        """
        data = await self.products_page(page=1, limit=settings.sync_page_size)
        return {
            "total": data.get("total"),
            "limit": data.get("limit"),
            "total_pages": data.get("total_pages"),
            "stores": data.get("stores", []),
        }


# ─────────────────────────────────────────────────────────────────────────────
# Feed row -> DB row
# ─────────────────────────────────────────────────────────────────────────────

def _to_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def _to_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _to_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def to_db_row(product: dict) -> dict:
    """
    Convert one feed product into a store_products row, running it through
    the normaliser so core_text / facets / unit_price are populated at
    write time rather than on every read.
    """
    from .normalize import parse  # local import avoids a cycle

    raw_name = product.get("raw_product_name") or product.get("product_name") or ""
    attrs = parse(
        raw_name,
        known_brand=product.get("brand"),
        known_quantity=_to_float(product.get("quantity")),
        known_unit=product.get("unit"),
        known_pack_count=_to_int(product.get("pack_count")),
    )

    price = _to_float(product.get("price"))
    unit_price_value: Optional[float] = None
    unit_price_label: Optional[str] = None
    if price is not None:
        computed = attrs.unit_price(price)
        if computed:
            unit_price_value, unit_price_label = computed

    sub_categories = product.get("sub_categories") or []
    if isinstance(sub_categories, str):
        sub_categories = [sub_categories]

    return {
        "store_id": (product.get("store_name") or "").strip().lower(),
        "store_product_id": str(product.get("store_product_id") or product.get("id") or ""),
        "feed_id": product.get("id"),
        "product_name": product.get("product_name") or raw_name,
        "raw_product_name": raw_name,
        "brand": (product.get("brand") or None),
        "price": price,
        "currency": product.get("currency") or "ZAR",
        "on_promotion": bool(product.get("on_promotion")),
        "promotion_details": product.get("promotion_details"),
        "unit": product.get("unit"),
        "quantity": _to_float(product.get("quantity")),
        "pack_count": _to_int(product.get("pack_count")),
        "pack_quantity": _to_float(product.get("pack_quantity")),
        "pack_unit": product.get("pack_unit"),
        "core_text": attrs.core_text,
        "facets": attrs.facets,
        "total_quantity": attrs.total_quantity,
        "base_unit": attrs.base_unit,
        "unit_price": unit_price_value,
        "unit_price_label": unit_price_label,
        "category": product.get("category"),
        "sub_categories": sub_categories,
        "in_stock": bool(product.get("in_stock", True)),
        "image_url": product.get("image_url"),
        "product_url": product.get("product_url"),
        "last_updated": _to_dt(product.get("last_updated")),
    }
