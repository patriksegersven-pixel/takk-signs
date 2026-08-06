# Google Ads target-ROAS tooling

CLI to inspect and change target ROAS (tROAS) on Google Ads campaigns and
portfolio bidding strategies, via the Google Ads REST API (`v24`).

Why this exists: **Funnel** (the connector in place) is a data-ingestion
platform; its API is read-only and cannot push changes back to Google Ads.

## Auth: service account (preferred)

Google Ads supports adding a service-account email **directly as a user**
on the Ads account — no Workspace domain-wide delegation needed.
The SA in use: `google-ads-mcp@project-a7ade44e-e7e3-4871-a83.iam.gserviceaccount.com`.

One-time setup:

1. **Add the SA to the Ads account** — Google Ads UI, signed in as an
   account admin: **Admin → Access and security → Users → ⊕** → paste the
   SA email → access level **Standard** → Add. ("Email" and "Admin" levels
   are not supported for service accounts; Standard is enough to change
   bidding. Do this on the manager account to cover all client accounts.)
2. **Developer token** — in the Google Ads *manager* (MCC) account:
   Tools & settings → API Center. "Basic access" is enough for managing
   your own accounts. (No manager account yet? Create one free at
   ads.google.com/home/tools/manager-accounts and link the client account.)
3. **Enable the API + get credentials for the SA** (locally):

   ```bash
   PROJ=project-a7ade44e-e7e3-4871-a83
   gcloud services enable googleads.googleapis.com --project=$PROJ

   # keyless (local use): let your user mint tokens as the SA
   gcloud iam service-accounts add-iam-policy-binding \
     google-ads-mcp@$PROJ.iam.gserviceaccount.com --project=$PROJ \
     --member="user:patrik.segersven@gmail.com" \
     --role="roles/iam.serviceAccountTokenCreator"

   # OR a JSON key (for headless use, e.g. Claude remote sessions):
   gcloud iam service-accounts keys create google-ads-mcp.json \
     --iam-account=google-ads-mcp@$PROJ.iam.gserviceaccount.com
   ```

## Auth: OAuth refresh token (fallback)

Create a **Desktop app** OAuth client in the GCP console, then mint a
refresh token locally, on a machine with a browser:

```bash
python3 scripts/ads/bootstrap_google_ads_oauth.py \
  --client-id <id> --client-secret <secret>
```

Log in with the Google account that has access to the Ads account. The
script prints the refresh token and Secret Manager storage commands.

## Usage

```bash
export GOOGLE_ADS_DEVELOPER_TOKEN=…

# Pick ONE auth mode (checked in this order):
export GOOGLE_ADS_SA_KEY_FILE=/path/to/google-ads-mcp.json   # SA key (headless)
export GOOGLE_ADS_IMPERSONATE_SA=google-ads-mcp@project-a7ade44e-e7e3-4871-a83.iam.gserviceaccount.com  # keyless, needs local gcloud
export GOOGLE_ADS_CLIENT_ID=… GOOGLE_ADS_CLIENT_SECRET=… GOOGLE_ADS_REFRESH_TOKEN=…  # fallback

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
