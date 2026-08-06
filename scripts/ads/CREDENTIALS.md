# Google Ads API — credentials & identifiers

Working reference for the tROAS tooling in this directory. Actual private
keys do NOT belong in this file or anywhere in git — see "Secret storage"
below.

## Identifiers

| What | Value |
|---|---|
| GCP project ID | `project-a7ade44e-e7e3-4871-a83` |
| GCP project number | `871631085269` |
| Service account | `google-ads-mcp@project-a7ade44e-e7e3-4871-a83.iam.gserviceaccount.com` |
| Google Ads manager (MCC) | `689-979-0415` (Babyshop) — login-customer-id `6899790415` |
| Stale parent manager (no access, candidate for unlink) | `560-865-7372` (Babyshop, linked Sep 2017) |
| Developer token | in Google Ads API Center (MCC 689-979-0415 → Tools → API Center) — not stored in git |
| API contact email | `patrik.segersven@gmail.com` (API Center) / `patrik.segersven@babyshop.se` (Basic Access form) |
| Company site | `https://www.babyshop.com` |

The developer token is only usable together with OAuth credentials of a
user/SA that has access to the Ads accounts — low sensitivity alone, but
reset it in API Center if it ever leaks alongside a credential.

## Environment variables (what the CLI reads)

```bash
GOOGLE_ADS_DEVELOPER_TOKEN=<from API Center>
GOOGLE_ADS_SA_KEY_FILE=<path to google-ads-mcp.json SA key>   # preferred auth
GOOGLE_ADS_LOGIN_CUSTOMER_ID=6899790415
# alternatives: GOOGLE_ADS_IMPERSONATE_SA (local gcloud), or
# GOOGLE_ADS_CLIENT_ID/SECRET/REFRESH_TOKEN (OAuth fallback)
```

## Secret storage

- SA JSON key: create with
  `gcloud iam service-accounts keys create google-ads-mcp.json --iam-account=google-ads-mcp@project-a7ade44e-e7e3-4871-a83.iam.gserviceaccount.com`
  and store in Secret Manager
  (`gcloud secrets create google-ads-mcp-key --data-file=google-ads-mcp.json`).
  NEVER commit the key file.
- For Claude Code remote sessions: set the env vars above in the
  environment settings (paste key JSON content or mount path).

## Status (2026-08-06)

- [x] SA added as **Standard** user on MCC 689-979-0415 (allowed domain
      `project-a7ade44e-e7e3-4871-a83.iam.gserviceaccount.com` added first)
- [x] Developer token exists — access level **Explorer** (test accounts only)
- [x] Basic Access application submitted (advertiser / internal tool /
      Campaign Management + Reporting / Search, Shopping, Performance Max)
- [ ] Basic Access approval email (expected 1–3 business days)
- [ ] `gcloud services enable googleads.googleapis.com` on the project
- [ ] SA key created + env vars set in Claude environment
- [ ] Token↔project association call (first API call with token + SA cred)
- [ ] Smoke test: `set_troas.py accounts` → `list` → `set --validate-only`

OAuth consent app ("Babyshop Internal Ads Tool", formerly "Google Ads MCP")
stays in **Testing** publishing status on purpose — SA auth bypasses the
consent screen; brand verification was skipped deliberately.
