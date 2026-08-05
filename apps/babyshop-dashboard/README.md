# babyshop-dashboard

The Babyshop **KV Performance dashboard** on Cloud Run in `europe-north1`:
multi-page HTML dashboard (KV Overview, Products, Inventory, Stoy Test,
ROAS Impact, ROAS Simulations) with a backend serving live Funnel.io
snapshots from Firestore (`/api/kv-data`, `/api/breakdown`, `/api/filtered`,
`/api/budget`, `/api/refresh-status`).

**This directory is the single source of truth.** Edit here, push to `main`,
and the `babyshop-dashboard-main` Cloud Build trigger builds and deploys a new
revision. Traffic may be pinned to a specific revision — after a deploy, move
traffic deliberately (Cloud Run → Revisions → Manage traffic).

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
    shares → Google Sheet ("Raw" and "Shares" tabs, append-only snapshots)
  - `webapp.gs` — Apps Script web app serving that sheet as the JSON endpoint
    consumed by the ROAS Simulations page (token-gated)
  - `PIPELINE.md` — full pipeline documentation and setup

## Endpoints
- `GET /` and `GET /babyshop-dashboard.html` — KV Overview
- `GET /babyshop-*.html` — sibling dashboard pages
- `GET /api/*` — live data (Firestore-backed)
- `POST /internal/refresh` — Cloud Scheduler refresh (`X-Internal-Token`)
- `GET /healthz` — liveness probe (unauthenticated)

All routes except `/healthz` require HTTP Basic auth (`DASH_USER` / `DASH_PASS`);
set `DEV_MODE=true` to bypass locally.
