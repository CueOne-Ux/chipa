-- ============================================================================
-- Chipa schema
--
-- Design notes
-- ------------
-- * `store_products` holds one row per retailer SKU. This is the raw,
--   per-store truth — mirrors the RapidAPI feed shape exactly.
-- * `canonical_products` is Chipa's own cross-retailer identity layer. The
--   feed does NOT provide this (confirmed by live testing, July 2026): every
--   retailer ships its own product_id and its own name formatting. Building
--   this layer is what makes cross-store basket comparison possible.
-- * `price_history` is append-only. We never overwrite a price; the current
--   price lives on store_products for fast reads, history accumulates
--   separately. Deliberately NOT a materialized view — an earlier iteration
--   of this project used one and stale data was impossible to diagnose.
-- * `catalog_*` tables hold OCR'd PDF specials from retailers with no digital
--   feed (Food Lover's Market, Boxer, Spar promotional leaflets).
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ── Stores ──────────────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS stores (
    id            TEXT PRIMARY KEY,          -- 'checkers', 'pnp', 'foodlovers'
    name          TEXT NOT NULL,
    website       TEXT,
    icon          TEXT,
    colour        TEXT,                      -- hex, for retailer colour-coding
    source        TEXT NOT NULL DEFAULT 'feed'
                  CHECK (source IN ('feed', 'catalog')),
    active        BOOLEAN NOT NULL DEFAULT TRUE
);

-- ── Store products (per-retailer SKUs) ──────────────────────────────────────

CREATE TABLE IF NOT EXISTS store_products (
    id                UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    store_id          TEXT NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    store_product_id  TEXT NOT NULL,
    feed_id           TEXT,                  -- upstream row id, if provided

    product_name      TEXT NOT NULL,
    raw_product_name  TEXT,
    brand             TEXT,

    price             NUMERIC(10,2),
    currency          TEXT NOT NULL DEFAULT 'ZAR',
    on_promotion      BOOLEAN NOT NULL DEFAULT FALSE,
    promotion_details TEXT,
    was_price         NUMERIC(10,2),         -- pre-promo price when known

    unit              TEXT,
    quantity          NUMERIC(12,3),
    pack_count        INTEGER,
    pack_quantity     NUMERIC(12,3),
    pack_unit         TEXT,

    -- Derived by app.normalize — the comparable representation.
    core_text         TEXT,                  -- brand+size stripped
    facets            JSONB NOT NULL DEFAULT '{}'::jsonb,
    total_quantity    NUMERIC(14,3),         -- in base_unit
    base_unit         TEXT,                  -- 'g' | 'ml' | 'ea'
    unit_price        NUMERIC(12,4),         -- per 100g / 100ml / each
    unit_price_label  TEXT,

    category          TEXT,
    sub_categories    TEXT[] DEFAULT '{}',
    in_stock          BOOLEAN NOT NULL DEFAULT TRUE,
    image_url         TEXT,
    product_url       TEXT,

    last_updated      TIMESTAMPTZ,           -- as reported by the feed
    synced_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE (store_id, store_product_id)
);

-- Trigram index powers cheap candidate retrieval before precise scoring.
CREATE INDEX IF NOT EXISTS idx_sp_core_trgm
    ON store_products USING GIN (core_text gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_sp_name_trgm
    ON store_products USING GIN (product_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_sp_store       ON store_products(store_id);
CREATE INDEX IF NOT EXISTS idx_sp_category    ON store_products(category);
CREATE INDEX IF NOT EXISTS idx_sp_promo       ON store_products(on_promotion)
    WHERE on_promotion = TRUE;
CREATE INDEX IF NOT EXISTS idx_sp_facets      ON store_products USING GIN (facets);
CREATE INDEX IF NOT EXISTS idx_sp_synced      ON store_products(synced_at);

-- ── Canonical products (Chipa's cross-retailer identity layer) ──────────────

CREATE TABLE IF NOT EXISTS canonical_products (
    id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    display_name   TEXT NOT NULL,
    core_text      TEXT NOT NULL,
    facets         JSONB NOT NULL DEFAULT '{}'::jsonb,
    brand          TEXT,
    total_quantity NUMERIC(14,3),
    base_unit      TEXT,
    category       TEXT,
    image_url      TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_cp_core_trgm
    ON canonical_products USING GIN (core_text gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_cp_facets ON canonical_products USING GIN (facets);

CREATE TABLE IF NOT EXISTS product_links (
    canonical_id      UUID NOT NULL REFERENCES canonical_products(id) ON DELETE CASCADE,
    store_product_id  UUID NOT NULL REFERENCES store_products(id) ON DELETE CASCADE,
    score             REAL NOT NULL,
    confidence        TEXT NOT NULL CHECK (confidence IN ('auto','review','manual')),
    reasons           TEXT[] DEFAULT '{}',
    linked_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (canonical_id, store_product_id)
);

CREATE INDEX IF NOT EXISTS idx_pl_store_product ON product_links(store_product_id);
CREATE INDEX IF NOT EXISTS idx_pl_confidence    ON product_links(confidence);

-- ── Price history (append-only) ─────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS price_history (
    id               BIGSERIAL PRIMARY KEY,
    store_product_id UUID NOT NULL REFERENCES store_products(id) ON DELETE CASCADE,
    price            NUMERIC(10,2) NOT NULL,
    on_promotion     BOOLEAN NOT NULL DEFAULT FALSE,
    observed_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ph_product_time
    ON price_history(store_product_id, observed_at DESC);

-- ── PDF catalog uploads (retailers with no digital feed) ────────────────────

CREATE TABLE IF NOT EXISTS catalog_uploads (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    filename      TEXT NOT NULL,
    store_id      TEXT REFERENCES stores(id) ON DELETE SET NULL,
    store_label   TEXT NOT NULL,             -- free text, e.g. "Food Lover's Market"
    uploaded_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    valid_from    DATE,
    valid_to      DATE,
    page_count    INTEGER,
    extraction    TEXT,                      -- 'text_layer' | 'ocr' | 'mixed'
    offer_count   INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','processing','done','failed')),
    error         TEXT
);

CREATE TABLE IF NOT EXISTS catalog_offers (
    id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    upload_id      UUID NOT NULL REFERENCES catalog_uploads(id) ON DELETE CASCADE,
    store_label    TEXT NOT NULL,
    page           INTEGER,
    product_name   TEXT NOT NULL,
    raw_text       TEXT,
    price          NUMERIC(10,2),
    was_price      NUMERIC(10,2),
    promo_text     TEXT,

    core_text      TEXT,
    facets         JSONB NOT NULL DEFAULT '{}'::jsonb,
    total_quantity NUMERIC(14,3),
    base_unit      TEXT,
    unit_price     NUMERIC(12,4),
    unit_price_label TEXT,

    ocr_confidence REAL,                     -- 0-1, from the extractor
    canonical_id   UUID REFERENCES canonical_products(id) ON DELETE SET NULL,
    match_score    REAL,
    match_confidence TEXT,
    valid_to       DATE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_co_upload    ON catalog_offers(upload_id);
CREATE INDEX IF NOT EXISTS idx_co_canonical ON catalog_offers(canonical_id);
CREATE INDEX IF NOT EXISTS idx_co_core_trgm
    ON catalog_offers USING GIN (core_text gin_trgm_ops);

-- ── Saved baskets ───────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS baskets (
    id          UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    owner_key   TEXT NOT NULL,               -- anonymous device key or user id
    name        TEXT NOT NULL DEFAULT 'My list',
    share_code  TEXT UNIQUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_basket_owner ON baskets(owner_key);

CREATE TABLE IF NOT EXISTS basket_items (
    id             UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    basket_id      UUID NOT NULL REFERENCES baskets(id) ON DELETE CASCADE,
    query_text     TEXT NOT NULL,            -- what the user asked for
    canonical_id   UUID REFERENCES canonical_products(id) ON DELETE SET NULL,
    pinned_store   TEXT REFERENCES stores(id) ON DELETE SET NULL,
    pinned_product UUID REFERENCES store_products(id) ON DELETE SET NULL,
    quantity       INTEGER NOT NULL DEFAULT 1,
    position       INTEGER NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_bi_basket ON basket_items(basket_id);

-- ── Price watches ───────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS price_watches (
    id            UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    owner_key     TEXT NOT NULL,
    canonical_id  UUID REFERENCES canonical_products(id) ON DELETE CASCADE,
    basket_id     UUID REFERENCES baskets(id) ON DELETE CASCADE,
    target_price  NUMERIC(10,2),
    last_seen     NUMERIC(10,2),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (canonical_id IS NOT NULL OR basket_id IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_pw_owner ON price_watches(owner_key);

-- ── Sync bookkeeping (request-budget tracking for the RapidAPI quota) ───────

CREATE TABLE IF NOT EXISTS sync_runs (
    id             BIGSERIAL PRIMARY KEY,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at    TIMESTAMPTZ,
    pages_fetched  INTEGER NOT NULL DEFAULT 0,
    rows_upserted  INTEGER NOT NULL DEFAULT 0,
    links_created  INTEGER NOT NULL DEFAULT 0,
    status         TEXT NOT NULL DEFAULT 'running'
                   CHECK (status IN ('running','done','failed')),
    error          TEXT
);

-- Seed the three feed retailers plus colour coding from the product spec.
INSERT INTO stores (id, name, website, colour, source) VALUES
    ('woolworths', 'Woolworths',  'https://www.woolworths.co.za', '#000000', 'feed'),
    ('pnp',        'Pick n Pay',  'https://www.pnp.co.za',        '#0057B8', 'feed'),
    ('checkers',   'Checkers',    'https://www.checkers.co.za',   '#E4610F', 'feed')
ON CONFLICT (id) DO NOTHING;
