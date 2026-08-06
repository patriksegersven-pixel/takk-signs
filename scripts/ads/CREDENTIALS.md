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
- [ ] Basic Access approval email (submitted; API already working meanwhile)
- [x] `googleads.googleapis.com` enabled on the project
- [x] SA key created (id `806baa1d…`, 2026-08-06; org policy
      `iam.disableServiceAccountKeyCreation` temporarily lifted then re-armed)
- [ ] Env vars set in Claude environment settings (key JSON + token + MCC id)
- [x] Token↔project association (first authenticated API call made)
- [x] Smoke test PASSED (2026-08-06): `accounts` lists MCC; `list` returns
      105 campaigns + 31 portfolio strategies for Babyshop SE (4851485396);
      `set --validate-only` accepted a write → mutations are authorized
      even at Explorer access level
- [ ] Key hygiene: SA carries 3 older keys (2× 2026-05-26, 1× 2026-07-08) —
      disable the unaccounted-for ones in IAM console once stable

Client accounts under the MCC (active): Babyshop SE 4851485396,
NO 8623945183, DK 2054294342, FI 6161399704, ROW 5541487401,
RU 1929201802; Lekmer SE 7780114635, NO 8308232278, DK 2756397225.

OAuth consent app ("Babyshop Internal Ads Tool", formerly "Google Ads MCP")
stays in **Testing** publishing status on purpose — SA auth bypasses the
consent screen; brand verification was skipped deliberately.
