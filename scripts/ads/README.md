# Google Ads target-ROAS tooling

CLI to inspect and change target ROAS (tROAS) on Google Ads campaigns and
portfolio bidding strategies, via the Google Ads REST API (`v24`).

Why this exists: the connectors already in place can't do it —

- **Funnel** is a data-ingestion platform; its API is read-only and cannot
  push changes back to Google Ads.
- A plain **GCP service account** cannot be granted Google Ads access. The
  Google Ads API only accepts service accounts through Google Workspace
  **domain-wide delegation** (impersonating a Workspace user). The accounts
  here are consumer `@gmail.com`, so that path is unavailable. The supported
  route is a user OAuth refresh token — same pattern as the Funnel client
  (`apps/babyshop-dashboard/funnel_client.py`).

## One-time setup

1. **Developer token** — in the Google Ads *manager* (MCC) account:
   Tools & settings → API Center. "Basic access" is enough for managing
   your own accounts. (No manager account yet? Create one free at
   ads.google.com/home/tools/manager-accounts and link the client account.)
2. **OAuth client** — GCP console (project `project-a7ade44e-e7e3-4871-a83`):
   APIs & Services → Credentials → Create credentials → OAuth client ID →
   type **Desktop app**. Also enable the "Google Ads API" on the project.
3. **Refresh token** — locally, on a machine with a browser:

   ```bash
   python3 scripts/ads/bootstrap_google_ads_oauth.py \
     --client-id <id> --client-secret <secret>
   ```

   Log in with the Google account that has access to the Ads account.
   The script prints the refresh token and the Secret Manager commands
   to store all four credentials.

## Usage

```bash
export GOOGLE_ADS_DEVELOPER_TOKEN=…
export GOOGLE_ADS_CLIENT_ID=…
export GOOGLE_ADS_CLIENT_SECRET=…
export GOOGLE_ADS_REFRESH_TOKEN=…
# Only if the account is reached through a manager account:
export GOOGLE_ADS_LOGIN_CUSTOMER_ID=<MCC id, digits only>

# Which accounts can I touch?
python3 scripts/ads/set_troas.py accounts

# Current campaigns, bidding schemes, and tROAS values
python3 scripts/ads/set_troas.py list --customer-id 1234567890

# Dry run (full server-side validation, writes nothing)
python3 scripts/ads/set_troas.py set --customer-id 1234567890 \
  --campaign-id 111 --troas 4.5 --validate-only

# Apply
python3 scripts/ads/set_troas.py set --customer-id 1234567890 \
  --campaign-id 111 --troas 4.5
```

Notes:

- **tROAS is a ratio**: `4.5` = 450% (the Google Ads UI shows percent).
  Passing `450%` also works.
- Works for the two schemes that have a tROAS setting:
  `MAXIMIZE_CONVERSION_VALUE` (tROAS optional) and `TARGET_ROAS` (legacy).
- If a campaign uses a **portfolio** strategy, the script refuses
  `--campaign-id` and tells you the `--strategy-id` to use instead —
  portfolio changes affect every attached campaign, so that has to be
  explicit.
- Stdlib only; runs anywhere with Python 3.10+ — including a Claude Code
  remote session, once the four secrets are provided as env vars.
- API version pinned via `GOOGLE_ADS_API_VERSION` (default `v24`,
  current as of 2026-08). Google sunsets majors ~1 year after release —
  bump when needed.
