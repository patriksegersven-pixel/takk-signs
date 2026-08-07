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
- `requirements.txt`, `Dockerfile` — runtime (uvicorn on `python:3.12-slim`)
- `pipeline/` — the Google Ads GP3 data pipeline (dockerignored, not deployed):
  - `gp3-simulations.js` — MCC script: Target ROAS bid simulations + impression
    shares + measured actuals (click time and conversion time) → Google Sheet
    ("Raw", "Shares" and "Actuals" tabs, append-only snapshots)
  - `webapp.gs` — Apps Script web app serving that sheet as the JSON endpoint
    consumed by the ROAS Simulations page (token-gated)
  - `PIPELINE.md` — full pipeline documentation and setup

## Endpoints
- `GET /` and `GET /babyshop-dashboard.html` — KV Overview
- `GET /babyshop-*.html` — sibling dashboard pages
- `GET /api/*` — live data (Firestore snapshots; `/api/breakdown` and
  `/api/filtered` query BigQuery live — `/api/filtered` also returns a
  per-day series (`series=daily`) so the KV Overview charts follow the
  market/shop/channel filter)
- `POST /internal/refresh` — Cloud Scheduler refresh (`X-Internal-Token`)
- `GET /healthz` — liveness probe (unauthenticated)

All routes except `/healthz` support HTTP Basic auth (`DASH_USER` /
`DASH_PASS`); `DEV_MODE=true` bypasses it. NOTE: the live service currently
runs with auth bypassed — to enforce a password, set `DEV_MODE=false` and a
strong `DASH_PASS` on the Cloud Run service (via Secret Manager), then let
the next deploy promote.
