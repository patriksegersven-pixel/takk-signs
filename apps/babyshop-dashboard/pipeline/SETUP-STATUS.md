# ROAS Simulations — direct Google Ads API pipeline: setup status

Working handoff document for the migration from the Google-Sheets pipeline to the
direct Google Ads API pipeline. Architecture and full reference live in
`PIPELINE.md`; this file tracks where the one-time setup actually stands so any
session (or human) can pick up exactly where things left off.

Last updated: 2026-08-07.

## Where things stand

| Piece | Status |
|---|---|
| Sheet pipeline v2 (Actuals tab, both attribution schemes) | **Live.** Merged to `main` (`f3ec5c8`), MCC script + `webapp.gs` updated in Google, dashboard deployed. First Actuals snapshot written 2026-08-07 (61 rows, all 8 accounts). |
| API pipeline code (`refresh_roas_sims.py`, `/api/roas-sims`, `/internal/refresh-roas-sims`, setup script, docs) | **Complete on branch `claude/roas-simulations-data-fjsgiw`** (through commit `c4affa7`). Built by an implementation agent, independently adversarially reviewed, all findings fixed, 442 automated checks green. |
| Merge to `main` | **Deliberately held.** The branch points the dashboard at `/api/roas-sims`; merging before the first snapshot exists would swap live sheet data for an empty API. Merge AFTER the first successful refresh (see "Remaining steps"). |

## Credential checklist

| Item | Status | Notes |
|---|---|---|
| Developer token | ✅ Obtained | Shared privately; goes ONLY into Secret Manager via the setup script. Never commit it. Owner can rotate it in MCC → API Center if it ever leaks. |
| MCC customer id | ✅ `689-979-0415` | As env var: `GOOGLE_ADS_LOGIN_CUSTOMER_ID=6899790415` (digits only). |
| OAuth client (id + secret) | ✅ Created | **Web application** client `roas-sims-collector` in project `project-a7ade44e-e7e3-4871-a83`, redirect URI `https://developers.google.com/oauthplayground`. (Web-app + Playground instead of the Desktop-app flow in PIPELINE.md — equivalent for the google-ads library as long as the client pair matches the one that minted the refresh token.) |
| Consent screen published | ✅ Decision made 2026-08-07 | Project consent screen is "Babyshop Internal Ads Tool" (External). Published Testing → **In production** so refresh tokens don't expire after 7 days. Publishing only removes the test-user restriction on the consent flow; it exposes no data and does not invalidate existing tokens (incl. the pre-existing "Google Ads MCP" client, which is being retired anyway). App name mismatch vs the client name is cosmetic. |
| Refresh token | ✅ Minted 2026-08-07 | Via OAuth Playground with the roas-sims-collector client, as patrik.segersven@gmail.com, scope `adwords`. |
| Credentials validated | ✅ 2026-08-07 | Live test: refresh-token → access-token exchange OK; `customers:listAccessibleCustomers` with the developer token returned 6 accounts including the MCC `6899790415`. Full credential set works against the production API. |

## Remaining steps (in order)

1. Mint the refresh token (table above).
2. On a machine with authenticated `gcloud`:

   ```bash
   export GOOGLE_ADS_DEVELOPER_TOKEN='<developer token>'
   export GOOGLE_ADS_CLIENT_ID='<oauth client id>'
   export GOOGLE_ADS_CLIENT_SECRET='<oauth client secret>'
   export GOOGLE_ADS_REFRESH_TOKEN='<refresh token>'
   export GOOGLE_ADS_LOGIN_CUSTOMER_ID='6899790415'
   export INTERNAL_TOKEN='<existing internal token>'

   cd apps/babyshop-dashboard
   ./pipeline/setup-roas-sims.sh
   ```

   Idempotent — safe to re-run. Creates/updates the five `GOOGLE_ADS_*` secrets,
   grants the runtime SA `secretAccessor`, wires them onto the `babyshop-dashboard`
   Cloud Run service, and creates/updates the daily Cloud Scheduler job
   `roas-sims-daily` (POST + `X-Internal-Token` header).

3. Trigger the first collection:

   ```bash
   curl -sX POST "https://<service-url>/internal/refresh-roas-sims" \
     -H "X-Internal-Token: $INTERNAL_TOKEN"
   ```

4. First-live-run verification (also in PIPELINE.md):
   - every account in `accounts[]` reports ok (or an explainable error);
   - row 0 of the raw grid has a **real campaign name**, not `campaign <id>` —
     validates the `campaign_simulation.campaign_id` join, the one query with no
     live precedent;
   - `counts.raw` comfortably under the snapshot budget (`DATASET_BUDGET_BYTES`
     950 KB ≈ 5,300 sim points); `dropped_datasets` empty.

5. Merge `claude/roas-simulations-data-fjsgiw` → `main`. Cloud Build redeploys
   `babyshop-dashboard`; the dashboard flips to `/api/roas-sims` with data already
   in place. The sheet path remains a one-line fallback (`LEGACY_SHEET_ENDPOINT`
   in `babyshop-roas-simulations.html`).

6. Optional, once the API path has run happily for a while: pause the MCC script's
   schedule in Google Ads and mark the sheet pipeline retired in PIPELINE.md.

## Security notes

- No secret values in this file, the repo, or commit history — secrets live in
  Secret Manager only (`GOOGLE_ADS_DEVELOPER_TOKEN`, `GOOGLE_ADS_CLIENT_ID`,
  `GOOGLE_ADS_CLIENT_SECRET`, `GOOGLE_ADS_REFRESH_TOKEN`,
  `GOOGLE_ADS_LOGIN_CUSTOMER_ID`).
- The developer token was shared in a chat session; rotating it after setup
  (MCC → API Center) is cheap insurance — update the secret version and bump the
  service (`gcloud run services update babyshop-dashboard --region=europe-north1
  --update-labels="secret-rotation=$(date +%s)"`; running revisions cache secrets).
- `/internal/refresh-roas-sims` is POST-only, requires `X-Internal-Token`
  regardless of `DEV_MODE`, and holds a self-expiring Firestore run lock
  (`roas_sim_locks/refresh`) against overlapping Scheduler retries.
- The dashboard access key travels in the `X-Roas-Sims-Key` header (not the URL,
  so not in Cloud Run request logs); the gate only engages when `ROAS_SIMS_TOKEN`
  is set on the service.

## Key identifiers

- GCP project: `project-a7ade44e-e7e3-4871-a83` ("Data Visualization"), region `europe-north1`
- Cloud Run service: `babyshop-dashboard`; Scheduler job: `roas-sims-daily`
- Firestore: `roas_sim_snapshots/<YYYY-MM-DD>` (daily snapshots, 90-day retention),
  `roas_sim_config/config` (user-editable multipliers/factors, seeded on first refresh),
  `roas_sim_locks/refresh` (run lock)
- MCC: `689-979-0415`; 8 child accounts listed in `refresh_roas_sims.py` `ACCOUNTS`
- Legacy sheet (still live until step 6): spreadsheet id
  `1x4GJxXSPzmJ-53hpal-0KN6_tLzhFjvJMzH2GRy0KD8`, tabs Raw / Shares / Actuals / Config
