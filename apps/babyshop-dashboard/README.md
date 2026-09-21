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
- `brand.css` — the Babyshop design system: tokens, Jost `@font-face`, and all
  header/nav chrome. Linked from every page before its inline `<style>`, so a
  page's own `:root` only carries its page-specific accent/series colours
- `nav.js` — the header. One `PAGES` registry renders the logo, the core tabs,
  the "More reports" dropdown and the per-page controls slot; **adding a new
  dashboard page is one line there** plus a route in `app.py`
- `babyshop-logo.svg`, `fonts/jost-*.woff2`, `chart.umd.js`, `table-tools.js` —
  vendored front-end assets, all served same-origin behind the same Basic auth
  (external CDNs are blocked on some of the networks this is viewed from)
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
- `roas_sims_bq.py` — BigQuery export of every ROAS-sims snapshot into dataset
  `roas_sims` (EU): `sim_points` / `impression_shares` / `actuals` partitioned
  by run_date, the `target_changes` prediction log, and views `v_kappa`
  (per-campaign sim-optimism factor), `v_change_scoring` (predicted vs
  realized per applied change), `v_marginal_scoring`/`v_lambda` (realized vs
  sim-implied marginal GP2-per-cost across each applied step) and
  `v_calibrated_recs`, whose `rec_final` is gated by that marginal evidence —
  it reverts, and never deepens, a spend move the measured marginal already
  refuted. Called best-effort from `refresh()` — Firestore
  keeps 90 days for serving, BigQuery keeps everything for model training.
  Missed days: `python3 roas_sims_bq.py --backfill`. Every applied tROAS change
  MUST be logged to `target_changes` with its predicted Δcost/ΔGP3 at apply
  time — unlogged changes cannot be scored and the calibration never learns.
  Two mechanisms enforce that: `pipeline/apply_troas.py` (below) logs as a side
  effect of applying, and `reconcile_target_changes()` — run by every daily
  refresh, or `python3 roas_sims_bq.py --reconcile` — reads the Google Ads
  `change_event` history and logs any target change that arrived by another
  route (source `reconciled-change-event`, raw-curve prediction). WHY: 20
  campaigns were changed on 2026-09-15 by an ad-hoc script and none reached
  the log for six days. `v_calibrated_recs` also exposes `days_since_change`
  / `cooldown` (any logged change inside `COOLDOWN_DAYS` = 14) so a campaign
  is not stepped again before its post-change window is clean; the page shows
  it as a "hold" badge next to the calibrated rec.
- `requirements.txt`, `Dockerfile` — runtime (uvicorn on `python:3.12-slim`)
- `pipeline/` — docs + operator tooling (dockerignored, not deployed):
  - `PIPELINE.md` — full pipeline documentation, data semantics and setup
  - `setup-roas-sims.sh` — the one-time Google Ads API setup (secrets, IAM,
    `--update-secrets`, Cloud Scheduler); idempotent
  - `apply_troas.py` — **the only sanctioned way to change a tROAS target.**
    Takes a JSON plan, reads the live targets, refuses campaigns in cooldown
    (`--force-cooldown`) or steps over ±20 % (`--uncapped`), runs every mutate
    with `validate_only` first, and on `--apply --source <tag>` mutates, reads
    back, and appends one `target_changes` row per campaign with the κ-deflated
    curve's predicted Δcost/ΔGP3. Creds: export the five `GOOGLE_ADS_*` secrets
    inline (never to a file); an older local `google-ads` needs
    `GOOGLE_ADS_API_VERSION=v23`. Never write a one-off mutate script instead.
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
