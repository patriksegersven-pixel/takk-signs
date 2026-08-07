# babyshop-dashboard

The Babyshop **KV Performance dashboard** on Cloud Run in `europe-north1`:
multi-page HTML dashboard (KV Overview, Products, Inventory, Stoy Test,
ROAS Impact, ROAS Simulations) with a backend serving live Funnel.io
snapshots from Firestore (`/api/kv-data`, `/api/breakdown`, `/api/filtered`,
`/api/budget`, `/api/refresh-status`).

**This directory is the single source of truth, and deploys are fully
automatic.** Edit here and push to `main`: the `babyshop-dashboard-main`
Cloud Build trigger builds the image, deploys a new revision, and a final
`promote` step routes 100% of traffic to it. **Green builds go live on their
own**; a failed build never takes traffic. Rollback is one click: Cloud Run →
Revisions → Manage traffic → pick an older revision.

(The old standalone `patriksegersven-pixel/babyshop` repo where the ROAS
Simulations page was originally developed is archived and read-only.)

## Layout
- `babyshop-dashboard.html` + sibling `babyshop-*.html` pages — the dashboard
- `app.py` — FastAPI server: page routes, `/api/*`, `/internal/*`, `/healthz`
- `funnel_client.py`, `bq_source.py`, `inventory_client.py`,
  `budget_source.py`, `refresh_roas_impact.py`, `budget_2026.json` — backend
  data sources (Funnel.io OAuth, BigQuery, Channable feed, budget plan)
- `refresh_roas_sims.py` — ROAS Simulations collector: Target ROAS bid
  simulations + impression shares + measured actuals straight off the **Google
  Ads API** (`google-ads` library, credentials from `GOOGLE_ADS_*` secrets) into
  Firestore `roas_sim_snapshots`, plus the reader that rebuilds the payload
  `/api/roas-sims` serves. Config lives in Firestore `roas_sim_config/config`
  and is editable with no deploy.
- `requirements.txt`, `Dockerfile` — runtime (uvicorn on `python:3.12-slim`)
- `pipeline/` — docs + operator tooling (dockerignored, not deployed):
  - `PIPELINE.md` — full pipeline documentation, data semantics and setup
  - `setup-roas-sims.sh` — the one-time Google Ads API setup (secrets, IAM,
    `--update-secrets`, Cloud Scheduler); idempotent
  - `gp3-simulations.js` — **legacy/fallback** MCC script writing the same three
    datasets to a Google Sheet ("Raw", "Shares", "Actuals" tabs)
  - `webapp.gs` — **legacy/fallback** Apps Script web app serving that sheet as a
    token-gated JSON endpoint, in the identical payload shape

## Endpoints
- `GET /` and `GET /babyshop-dashboard.html` — KV Overview
- `GET /babyshop-*.html` — sibling dashboard pages
- `GET /api/*` — live data (Firestore snapshots; `/api/breakdown` and
  `/api/filtered` query BigQuery live — `/api/filtered` also returns a
  per-day series (`series=daily`) so the KV Overview charts follow the
  market/shop/channel filter)
- `GET /api/roas-sims` — ROAS Simulations payload (`runs`, `account`, `token`),
  in exactly the shape the legacy Apps Script endpoint served. Always 200; the
  page reads `error` / `status` to tell "no snapshots yet" from a rejected key
- `POST /internal/refresh` — Cloud Scheduler refresh (`X-Internal-Token`)
- `POST /internal/refresh-roas-sims` — daily Google Ads API collection
  (`X-Internal-Token`); 503 + the missing variable names while the
  `GOOGLE_ADS_*` secrets are not wired yet
- `GET /healthz` — liveness probe (unauthenticated)

All routes except `/healthz` support HTTP Basic auth (`DASH_USER` /
`DASH_PASS`); `DEV_MODE=true` bypasses it. NOTE: the live service currently
runs with auth bypassed — to enforce a password, set `DEV_MODE=false` and a
strong `DASH_PASS` on the Cloud Run service (via Secret Manager), then let
the next deploy promote.
