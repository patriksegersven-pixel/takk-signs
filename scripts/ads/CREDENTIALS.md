# Google Ads API — credentials & identifiers

Working reference for the tROAS tooling in this directory. Actual private
keys do NOT belong in this file or anywhere in git — see "Secret storage"
below.

## Identifiers

| What | Value |
|---|---|
| GCP project ID | `project-a7ade44e-e7e3-4871-a83` (display name "Data Visualization Main Claude") |
| GCP project number | `871631085269` |
| GCP organization | `468825633459` (`patrik-segersven-org`, created 2026-05-25; org admin = patrik.segersven@gmail.com) |
| Service account | `google-ads-mcp@project-a7ade44e-e7e3-4871-a83.iam.gserviceaccount.com` |
| Active SA key | id `806baa1d400a31c6797acfc7405ac2268555438b` (created 2026-08-06) |
| Google Ads manager (MCC) | `689-979-0415` (Babyshop) — login-customer-id `6899790415` |
| Stale parent manager (no access, candidate for unlink) | `560-865-7372` (Babyshop, linked Sep 2017) |
| Developer token | in Google Ads API Center (MCC 689-979-0415 → Tools → API Center) — not stored in git |
| API contact email | `patrik.segersven@gmail.com` (API Center) / `patrik.segersven@babyshop.se` (Basic Access form) |
| Company site | `https://www.babyshop.com` |

The developer token is only usable together with OAuth credentials of a
user/SA that has access to the Ads accounts — low sensitivity alone, but
reset it in API Center if it ever leaks alongside a credential.

## Google Ads account tree (under MCC 689-979-0415)

Active clients: Babyshop SE `4851485396`, NO `8623945183`, DK `2054294342`,
FI `6161399704`, ROW `5541487401`, RU `1929201802`; Lekmer SE `7780114635`,
NO `8308232278`, DK `2756397225`.
Canceled/closed (ignore): Babyshop US / FI-old / DK-old / RU-old,
Display & Video, YouTube, Lekmer FI, Melijoe EU, kids design market,
Oii Design.

Scale reference: Babyshop SE alone has ~105 campaigns and 31 portfolio
bidding strategies (most tROAS lives on portfolio strategies named
"P - Target ROAS - …"; PMax/search campaigns carry campaign-level tROAS
via maximize_conversion_value).

## Environment variables (what the CLI reads)

```bash
GOOGLE_ADS_DEVELOPER_TOKEN=<from API Center>
GOOGLE_ADS_SA_KEY_JSON=<entire SA key JSON on one line>    # preferred (remote sessions)
# or GOOGLE_ADS_SA_KEY_FILE=<path to google-ads-mcp.json>
GOOGLE_ADS_LOGIN_CUSTOMER_ID=6899790415
# alternatives: GOOGLE_ADS_IMPERSONATE_SA (local gcloud), or
# GOOGLE_ADS_CLIENT_ID/SECRET/REFRESH_TOKEN (OAuth fallback)
```

For Claude Code remote sessions these live in claude.ai/code → the
takk-signs environment → Environment variables. Minify the key JSON to a
single line before pasting (`python3 -c "import json,sys;
print(json.dumps(json.load(sys.stdin), separators=(',',':')))" < key.json`).

## Secret storage & org-policy note

- The org enforces `constraints/iam.disableServiceAccountKeyCreation`.
  Creating a NEW key requires: grant `roles/orgpolicy.policyAdmin` on org
  468825633459 (org admin can self-grant), then
  `gcloud resource-manager org-policies disable-enforce
  constraints/iam.disableServiceAccountKeyCreation --project=<project>`,
  **wait 2–3 min for propagation**, create the key, then re-arm with
  `enable-enforce` (verify `enforced: true`). Policy was lifted and
  re-armed this way on 2026-08-06.
- Keep the key in the Claude environment variable and/or Secret Manager.
  NEVER commit it.

## Status (2026-08-06)

- [x] SA added as **Standard** user on MCC 689-979-0415 (allowed domain
      `project-a7ade44e-e7e3-4871-a83.iam.gserviceaccount.com` added first;
      the Allowed-domains lock traced to the stale parent MCC's security
      mandate)
- [x] Developer token exists — access level **Explorer**
- [x] Basic Access application submitted 2026-08-06 (advertiser / internal
      tool / Campaign Management + Reporting / Search, Shopping,
      Performance Max; design doc PDF attached; project number 871631085269;
      brand verification deliberately skipped — OAuth app stays in Testing)
- [ ] Basic Access approval email (API already working meanwhile)
- [x] `googleads.googleapis.com` enabled on the project
- [x] SA key `806baa1d…` created 2026-08-06 (org policy lifted → re-armed)
- [ ] Env vars set in claude.ai environment settings (key JSON + token +
      MCC id) — until done, sessions have no Ads credentials
- [x] Token↔project association (first authenticated API call made)
- [x] Smoke test PASSED (2026-08-06): `accounts` lists MCC; `list` returns
      full campaign + strategy inventory for Babyshop SE;
      `set --validate-only` accepted a write → reads AND mutations are
      authorized even at Explorer access level
- [ ] Key hygiene: SA carries 3 older keys (2× 2026-05-26 exp 2027-05-26,
      1× 2026-07-08 exp 2028-07-21) — disable unaccounted-for ones in the
      IAM console (disable first, delete later) once the new key is stable
- [ ] Optional: unlink stale parent MCC 560-865-7372 — but FIRST check
      billing isn't routed through it and conversions aren't tracked at its
      level (unlinking is one-way without access to that account)

## Usage (once env vars are set, any session)

```bash
python3 scripts/ads/set_troas.py accounts
python3 scripts/ads/set_troas.py list --customer-id 4851485396
python3 scripts/ads/set_troas.py set --customer-id 4851485396 \
  --campaign-id <id> --troas 4.5 --validate-only   # dry run first
python3 scripts/ads/set_troas.py set --customer-id 4851485396 \
  --strategy-id <id> --troas 450%                  # portfolio strategies
```

OAuth consent app ("Babyshop Internal Ads Tool", formerly "Google Ads MCP")
stays in **Testing** publishing status on purpose — SA auth bypasses the
consent screen; brand verification was skipped deliberately.
