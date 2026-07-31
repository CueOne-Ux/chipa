# Deploying Chipa v2 — testing setup

Written for the existing setup: Neon project **Chipa** (CueRated Concepts org,
AWS US East 1, free tier) and the public repo `github.com/CueOne-Ux/chipa`.

Target for now is a **private test deployment**, not a public launch.

---

## 1. Push the code

```bash
cd /path/to/Chipa/chipa
git init                      # if this folder isn't a repo yet
git add .
git commit -m "Chipa v2 — matching engine, OCR catalogues, basket engine"
git remote add origin https://github.com/CueOne-Ux/chipa.git
git push -u origin main
```

If you'd rather keep v1 history intact, push to a `v2` branch and point
Render at that branch instead.

---

## 2. Get the Neon connection string

Neon console -> project **Chipa** -> **Connect** -> copy the **pooled**
connection string. It looks like:

```
postgresql://USER:PASSWORD@ep-xxxx-pooler.us-east-1.aws.neon.tech/neondb?sslmode=require
```

Use the **pooled** one (`-pooler` in the host). Render's free tier restarts
often and a non-pooled connection burns Neon compute holding idle sessions.

**Put this straight into Render's env vars — don't paste it into chat, a
commit, or the frontend.** It's a live database credential.

### About the existing v1 data

The project already contains v1's `products` table (~31 MB). The v2 schema
is entirely `CREATE TABLE IF NOT EXISTS`, so it installs alongside without
touching it. v2 never reads or writes that table.

Once v2 is confirmed working, reclaim the space:

```sql
DROP TABLE IF EXISTS products;
```

Don't run this before v2 is verified — it's the only copy of the old
scraped data.

---

## 3. Deploy the API to Render

New -> **Blueprint** -> point at the repo. It reads `render.yaml`.

Render's free plan doesn't include cron services, so **delete or comment out
the `chipa-sync` cron block** in `render.yaml` before deploying, or the
blueprint will fail. GitHub Actions covers sync instead (step 6).

Set these environment variables on the `chipa-api` service:

| Variable | Value |
|---|---|
| `DATABASE_URL` | the pooled Neon string from step 2 |
| `RAPIDAPI_KEY` | your RapidAPI key |
| `ADMIN_TOKEN` | a long random string you generate |
| `ENABLE_INTERNAL_SCHEDULER` | `false` |
| `CORS_ORIGINS` | `*` while testing; lock down before public |

Generate an admin token:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

**Tesseract on Render:** the build command installs `tesseract-ocr` via
`apt-get`, which works on Render's native runtime but is not guaranteed to
persist across all plan types. If catalogue upload returns a tesseract error
after deploy, switch the service to Docker and install it in a Dockerfile.
Everything except PDF OCR works without it.

---

## 4. Verify the deploy

```bash
curl https://YOUR-SERVICE.onrender.com/health
```

Expect `"database": true` and `"feed": true`. First request after a sleep
takes 30-50s — that's the free tier cold start, not a fault.

If `database` is false, the schema failed to apply. Check Render logs for
the `pg_trgm` extension — Neon allows it, but the role needs to be the
project owner.

---

## 5. First real integration test

This is the one gap that could not be tested during the build (no Postgres
in the build sandbox). It's the first thing that proves v2 end to end.

```bash
# locally, with DATABASE_URL and RAPIDAPI_KEY set
python -m app.cli budget       # confirms quota maths against the live feed
python -m app.cli sync 2       # 2 pages only — ~100 products
```

Then check what landed:

```bash
curl -H "X-Admin-Token: YOUR_TOKEN" \
  https://YOUR-SERVICE.onrender.com/api/admin/status
```

You want non-zero `store_products`, `canonical_products` and `links`.
`links_needing_review` shows how many matches the engine wasn't confident
about — a useful signal for tuning the taxonomy.

**Known risk:** the upstream `/products` endpoint was returning 404 for all
parameter combinations on 31 July 2026 while `/stores` and `/categories`
stayed healthy. If `sync` fails with that error it's provider-side, not your
config. `/api/admin/status` and `/health` will still work.

Once a sync succeeds, open the service URL — Render serves the frontend at
`/`. Search, specials carousels and basket comparison should all be live.

---

## 6. Keep prices fresh

Copy `deploy/sync-cron.yml` to `.github/workflows/sync-cron.yml`, then add
two repo secrets (Settings -> Secrets and variables -> Actions):

- `CHIPA_API_URL` — `https://YOUR-SERVICE.onrender.com`
- `CHIPA_ADMIN_TOKEN` — same value as `ADMIN_TOKEN` on Render

Run it once manually from the Actions tab to confirm it works.

This is the fix for v1's actual failure: the sync is triggered from outside
the web process, so Render putting the service to sleep can't stop it — the
request wakes the service on its way in.

---

## 7. Later — putting it on a CueRated domain

Only worth doing once it's going somewhere real.

**Simplest:** Render -> service -> Settings -> Custom Domains -> add
`chipa.cuerated.co.za`, then add the CNAME Render gives you at
domains.co.za. One service, no CORS work, TLS automatic.

**If it must sit on cPanel hosting instead:** upload `frontend/index.html`
only — cPanel is PHP/static and will not run FastAPI. Set the config line at
the top of its script block:

```js
const API = 'https://YOUR-SERVICE.onrender.com';
```

Then set `CORS_ORIGINS=https://chipa.cuerated.co.za` on Render. Leaving `*`
in production lets any site call your API and burn your RapidAPI quota.

Before anything public: rotate the RapidAPI key (it's been on screen several
times), confirm the feed's redistribution terms, and move off the free tier
so the first visitor doesn't wait 50s for a cold start.
