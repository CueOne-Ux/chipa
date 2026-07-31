# Chipa

South African cross-store grocery price comparison. Build one list, see it
priced across every shop, move items between shops, and get told honestly
whether the second stop is worth the petrol.

Built 31 July 2026. Supersedes the Troli Companion / Chipa v1 scraper build.

---

## What's different about this build

| | Chipa v1 (retired) | Chipa v2 (this) |
|---|---|---|
| Data source | Scraped troli.co.za | Licensed RapidAPI feed + OCR'd PDF catalogues |
| Refresh | In-process scheduler (died when Render slept) | External cron -> `/api/admin/sync` |
| Matching | None — one row per store, no cross-store identity | Taxonomy-vetoed canonical matching |
| Price history | None | Own append-only snapshots |
| Retailer coverage | 3 | 3 via feed + unlimited via catalogue upload |

---

## The matching engine (`app/taxonomy.py`, `app/matching.py`)

This is the core of the product, and the reason it beats what's currently on
the market.

Competitor testing in July 2026 showed a search for **"chicken fillet"**
returning **"Steakhouse Classic Beef Steak Fillet"** as the top result. That
is what pure fuzzy/trigram similarity does: "fillet" is a strong shared
token and nothing in the model knows chicken and beef are different things.

Chipa scores in three stages:

1. **Hard veto** — an explicit taxonomy of mutually exclusive facets
   (protein, milk fat, sugar, caffeine, preparation, pet). Chicken vs beef
   is rejected outright, not merely ranked lower.
2. **Similarity** — token-set scoring on brand- and size-stripped text.
3. **Structured adjustment** — pack size compatibility, brand agreement,
   category agreement, soft-facet penalties.

Two refinements worth knowing about:

- **Silence is not always neutral.** "Milk 2L" *may* be full cream, so it
  doesn't conflict with "Full Cream Milk 2L". But "Coca-Cola 2L" is not a
  variant that might turn out to be Coke Zero — it *is* the regular one.
  Facets in `DEFAULTED_FACETS` treat an absent marker as a positive claim.
- **Uncertainty is surfaced, not hidden.** Matches carry `auto` / `review` /
  `reject` confidence. A competitor's answer to bad matching is a manual
  "hold down to group products across stores" gesture; Chipa flags a shaky
  match instead of silently showing the wrong product.

Regression test: `tests/test_matching.py::test_chicken_query_never_returns_beef`.

---

## PDF catalogue upload with OCR (`app/ocr.py`)

Food Lover's Market, Boxer and independent SPARs publish specials only as
printed-leaflet PDFs — no online store, no feed, nothing to scrape. No
competitor can compare them at all.

Chipa lets you upload the leaflet:

1. Try the embedded text layer (fast, exact).
2. If a page is a flattened graphic — which most leaflets are — rasterise
   at 300 DPI and run Tesseract.
3. Parse product/price pairs with a proximity heuristic that handles both
   common layouts (name above price, name beside price).
4. Normalise through the same pipeline as feed products, so a leaflet price
   competes directly in search and basket comparison.

Handles `R49.99`, `R 49,99`, multibuys (`2 for R50` -> R25 each), `Was`
prices and `Save` amounts. Every offer carries an OCR confidence score.

Tested against both a text-layer PDF and a genuinely flattened image PDF:
8/8 offers extracted from each, including correct per-unit maths on the
multibuy.

---

## Basket economics (`app/basket.py`)

Three views: per-shop totals, cheapest possible split, and a verdict.

The verdict is the opinionated bit. Competitors optimise for the lowest
achievable number and will happily send you to three shops to save R8.
Chipa prices in petrol (round trip, configurable consumption and fuel price)
and a notional cost per extra stop, then applies a threshold
(`MIN_SPLIT_SAVING_RAND`, default R25). Below it, the answer is
"just shop at Checkers" — with the numbers shown.

Ranking a single shop also accounts for fuel, so a nearer shop that's
slightly dearer on paper can correctly win overall.

Pinned items are honoured: move an item to a shop and every plan keeps it
there.

---

## Setup

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# OCR needs the tesseract binary
sudo apt-get install tesseract-ocr      # Debian/Ubuntu
brew install tesseract                  # macOS

cp .env.example .env                    # then fill in DATABASE_URL + RAPIDAPI_KEY
uvicorn app.main:app --reload
```

Open http://localhost:8000.

### First run

```bash
python -m app.cli budget      # how many requests a full sync actually costs
python -m app.cli sync 5      # sync 5 pages to sanity-check the pipeline
python -m app.cli sync        # full sync (~742 requests)
```

---

## RapidAPI quota planning

Measured 24 July 2026: **37,074 products, 742 pages** at the feed's maximum
`limit=50` (higher values return 404). So one full sync ≈ 742 requests.

| Tier | Quota/month | Full syncs | Verdict |
|---|---|---|---|
| Basic | 50 | 0 | Cannot complete one sync. Testing only. |
| Pro | 5,000 | ~6 | Weekly at best, and no price history. |
| Ultra | 25,000 | ~33 | **Daily sync with headroom.** |
| Mega | 100,000 | ~134 | Only if syncing several times a day. |

Run `python -m app.cli budget` against the live feed before committing —
it prints this table with current numbers.

---

## Featured specials

`GET /api/specials?per_store=5` returns current promotions grouped per
retailer, ranked by discount depth, for the home-screen carousels. Each
group carries the retailer's brand colour so the card borders match the
shop. Uploaded PDF catalogues get their own row too — a Food Lover's
leaflet sits alongside the tracked retailers rather than being buried.

Frontend behaviour: horizontal scroll-snap rails with an autoloop that
advances every ~4s, staggered per row so they don't move in lockstep. The
loop pauses on hover, touch, wheel or when the tab is hidden — a carousel
that slides while you're reading it is worse than no carousel. Tapping a
card runs a full cross-store search for that product.

---

## Deployment

`render.yaml` deploys the API. **Do not rely on the in-process scheduler on
a free plan** — Render sleeps the service after ~15 minutes idle and takes
the scheduler with it. That was the root cause of v1's stale prices.

Instead, copy `deploy/sync-cron.yml` to `.github/workflows/` in the repo and
set two secrets: `CHIPA_API_URL` and `CHIPA_ADMIN_TOKEN`. GitHub calls the
sync endpoint on a schedule from outside, which also wakes the service.

### Hosting on a CueRated domain

The API serves the frontend from `/`, so a single service covers both.
Point a subdomain (e.g. `chipa.cuerated.co.za`) at the Render service:

1. Render dashboard -> the `chipa-api` service -> Settings -> Custom Domains
   -> add `chipa.cuerated.co.za`.
2. At your DNS host, add the `CNAME` Render gives you. TLS is issued
   automatically; allow up to an hour.
3. Lock CORS down once the domain is live — leaving `*` in production means
   any site can call your API and burn your RapidAPI quota:

   ```
   CORS_ORIGINS=https://chipa.cuerated.co.za
   ```

If you'd rather serve the frontend from existing CueRated hosting (cPanel)
and keep only the API on Render, upload `frontend/index.html` there and set
`const API = 'https://chipa-api.onrender.com';` at the top of its script
block. Same CORS rule applies — list the cPanel origin, not `*`.

Note the free-tier cold start: a sleeping service takes ~30-50s to answer
the first request. Fine for testing, poor as a first impression on a public
domain. The GitHub Actions cron keeps it warm once a day; a paid instance
or a more frequent ping is the real fix before you publicise the URL.

---

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness |
| GET | `/api/stores` | Retailers, including catalogue-only ones |
| GET | `/api/search?q=` | Ranked search with taxonomy veto |
| GET | `/api/compare?q=` | One item priced across every shop |
| POST | `/api/basket/compare` | Full cross-store basket comparison |
| POST | `/api/catalog/upload` | Upload a PDF specials leaflet (OCR) |
| GET | `/api/catalog/uploads` | List uploaded catalogues |
| GET | `/api/specials` | Featured specials grouped per shop (carousels) |
| GET | `/api/deals` | Current promotions, flat list |
| GET | `/api/price-history/{id}` | Price over time |
| POST | `/api/admin/sync` | Trigger sync (needs `X-Admin-Token`) |
| GET | `/api/admin/status` | Row counts, last sync, links needing review |

Interactive docs at `/docs`.

---

## Tests

```bash
cd backend && python -m pytest tests/ -q
```

60 tests: taxonomy and matching (including the beef regression),
normalisation and unit pricing, OCR against both PDF types, basket
economics, and API contract tests against a stubbed database.

**Not covered:** anything requiring a live Postgres — schema application,
the sync job's DB writes, and canonical link building are unit-tested in
logic but have not been run against a real database. Run
`python -m app.cli sync 2` against a Neon instance as the first
integration check.

---

## Known issues

- **The upstream `/products` endpoint is intermittently unavailable.** On
  31 July 2026 it returned `404 "No products found matching the input query
  parameters"` for every parameter combination, including ones that had
  worked 30 minutes earlier, while `/stores` and `/categories` stayed
  healthy. Provider-side. `tests/fixtures/products_page1.json` holds a real
  captured response so development isn't blocked. Worth monitoring before
  committing to a paid tier — reliability of this feed is now an open
  question.
- The RapidAPI key has been exposed on screen several times. Rotate before
  any production deploy.
- Licence/redistribution terms for the feed are still unconfirmed; a ToS
  update dated 25 May 2026 has not been reviewed.
