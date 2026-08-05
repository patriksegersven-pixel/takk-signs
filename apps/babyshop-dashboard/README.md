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
- `app.py`, `requirements.txt`, `Dockerfile` — the server (placeholder until
  the full backend with the `/api/*` routes and `refresh_funnel.py` is
  committed; see repo history)
- `pipeline/` — the Google Ads GP3 data pipeline (dockerignored, not deployed):
  - `gp3-simulations.js` — MCC script: Target ROAS bid simulations + impression
    shares → Google Sheet ("Raw" and "Shares" tabs, append-only snapshots)
  - `webapp.gs` — Apps Script web app serving that sheet as the JSON endpoint
    consumed by the ROAS Simulations page (token-gated)
  - `PIPELINE.md` — full pipeline documentation and setup

## Endpoints
- `GET /` and `GET /babyshop-dashboard.html` — KV Overview
- `GET /babyshop-*.html` — sibling dashboard pages
- `GET /api/*` — live data (Firestore-backed; requires the full backend)
- `GET /health` — liveness probe
