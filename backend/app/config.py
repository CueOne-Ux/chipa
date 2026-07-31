"""Runtime configuration, read from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


@dataclass
class Settings:
    # ── Database ────────────────────────────────────────────────────────────
    database_url: str = field(
        default_factory=lambda: os.getenv("DATABASE_URL", "")
    )

    # ── RapidAPI (South African Grocery Prices API) ─────────────────────────
    rapidapi_key: str = field(default_factory=lambda: os.getenv("RAPIDAPI_KEY", ""))
    rapidapi_host: str = field(
        default_factory=lambda: os.getenv(
            "RAPIDAPI_HOST", "south-african-grocery-prices-api.p.rapidapi.com"
        )
    )

    # Page size for catalog sync.
    # VERIFIED 24 July 2026: the feed caps `limit` at 50. Requesting 100
    # returns 404 "No products found matching the input query parameters".
    # So a full catalog sync costs ~742 requests (37,074 products / 50).
    # Quota planning: Ultra (25k/mo) allows ~33 full syncs — comfortably
    # daily. Pro (5k/mo) allows ~6. Basic (50/mo) cannot complete even one.
    sync_page_size: int = field(default_factory=lambda: min(_int("SYNC_PAGE_SIZE", 50), 50))
    # Hard ceiling per run so a misconfiguration cannot burn the whole quota.
    sync_max_pages: int = field(default_factory=lambda: _int("SYNC_MAX_PAGES", 400))
    sync_timeout_s: float = field(default_factory=lambda: _float("SYNC_TIMEOUT_S", 30.0))

    # ── Admin / cron ────────────────────────────────────────────────────────
    # An external scheduler (GitHub Actions, cron-job.org) posts to
    # /api/admin/sync with this token. This is the robust fix for Render's
    # free tier sleeping and killing in-process schedulers.
    admin_token: str = field(default_factory=lambda: os.getenv("ADMIN_TOKEN", ""))
    enable_internal_scheduler: bool = field(
        default_factory=lambda: _bool("ENABLE_INTERNAL_SCHEDULER", False)
    )

    # ── Basket economics ────────────────────────────────────────────────────
    # Used for the "is a second stop actually worth it?" calculation.
    fuel_price_per_litre: float = field(
        default_factory=lambda: _float("FUEL_PRICE_PER_LITRE", 21.50)
    )
    fuel_consumption_l_per_100km: float = field(
        default_factory=lambda: _float("FUEL_CONSUMPTION_L_PER_100KM", 8.0)
    )
    # A split basket must beat the single-store option by at least this much
    # (after fuel) before Chipa recommends it. Prevents "drive 12km to save R3".
    min_split_saving_rand: float = field(
        default_factory=lambda: _float("MIN_SPLIT_SAVING_RAND", 25.0)
    )
    # Notional value of the shopper's time per extra stop.
    time_cost_per_stop_rand: float = field(
        default_factory=lambda: _float("TIME_COST_PER_STOP_RAND", 20.0)
    )

    # ── Uploads / OCR ───────────────────────────────────────────────────────
    max_upload_mb: int = field(default_factory=lambda: _int("MAX_UPLOAD_MB", 25))
    ocr_dpi: int = field(default_factory=lambda: _int("OCR_DPI", 300))
    ocr_lang: str = field(default_factory=lambda: os.getenv("OCR_LANG", "eng"))
    upload_dir: str = field(
        default_factory=lambda: os.getenv("UPLOAD_DIR", "/tmp/chipa_uploads")
    )

    # ── Matching ────────────────────────────────────────────────────────────
    # Trigram prefilter threshold — how wide a net the DB casts before the
    # precise Python scorer runs. Lower = more candidates, slower, more recall.
    trigram_threshold: float = field(
        default_factory=lambda: _float("TRIGRAM_THRESHOLD", 0.18)
    )
    candidate_limit: int = field(default_factory=lambda: _int("CANDIDATE_LIMIT", 400))

    cors_origins: List[str] = field(
        default_factory=lambda: [
            o.strip()
            for o in os.getenv("CORS_ORIGINS", "*").split(",")
            if o.strip()
        ]
    )

    @property
    def has_db(self) -> bool:
        return bool(self.database_url)

    @property
    def has_feed(self) -> bool:
        return bool(self.rapidapi_key)


settings = Settings()
