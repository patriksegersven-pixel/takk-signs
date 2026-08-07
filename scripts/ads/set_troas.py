#!/usr/bin/env python3
"""
Google Ads target-ROAS CLI — inspect and change tROAS on campaigns and
portfolio bidding strategies via the Google Ads REST API.

Auth — three modes, tried in this order (first match wins):

1. Service-account key (works headless, e.g. remote sessions):
       GOOGLE_ADS_SA_KEY_JSON   — the key JSON content itself, or
       GOOGLE_ADS_SA_KEY_FILE (or GOOGLE_APPLICATION_CREDENTIALS) — a path
   The SA email must be added as a user
   (Standard access) on the Google Ads account — supported directly,
   no Workspace domain-wide delegation needed. Optionally set
   GOOGLE_ADS_SUBJECT_EMAIL to impersonate a Workspace user via DWD.
   Signing shells out to `openssl` (no pip installs).

2. Keyless gcloud impersonation (local machines with gcloud login):
       GOOGLE_ADS_IMPERSONATE_SA=<sa-email>
   Your gcloud user needs roles/iam.serviceAccountTokenCreator on the SA.

3. OAuth refresh token (mint with bootstrap_google_ads_oauth.py):
       GOOGLE_ADS_CLIENT_ID, GOOGLE_ADS_CLIENT_SECRET,
       GOOGLE_ADS_REFRESH_TOKEN

Always required:
    GOOGLE_ADS_DEVELOPER_TOKEN    from the manager account's API Center

Optional env vars:
    GOOGLE_ADS_LOGIN_CUSTOMER_ID  manager (MCC) customer ID, digits only.
                                  Required when the account you operate on
                                  is accessed through a manager account.
    GOOGLE_ADS_API_VERSION        default "v24" (current as of 2026-08;
                                  Google sunsets each major version ~1 year
                                  after release — bump this when it EOLs)

Usage:
    python3 set_troas.py accounts
    python3 set_troas.py list --customer-id 1234567890
    python3 set_troas.py set --customer-id 1234567890 --campaign-id 111 --troas 4.5
    python3 set_troas.py set --customer-id 1234567890 --strategy-id 222 --troas 450%
    ...                                               --validate-only   # dry run

tROAS is a ratio: 4.5 means 450% (conversion value / ad spend). A trailing
"%" is accepted and converted (450% -> 4.5).

Stdlib only — no pip install needed.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API_VERSION = os.environ.get("GOOGLE_ADS_API_VERSION", "v24")
API_BASE = f"https://googleads.googleapis.com/{API_VERSION}"
OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
ADWORDS_SCOPE = "https://www.googleapis.com/auth/adwords"


def _env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        sys.exit(f"Missing required env var: {name} (see README.md)")
    return val


def _digits(customer_id: str) -> str:
    """Normalize '123-456-7890' -> '1234567890'."""
    cleaned = customer_id.replace("-", "").strip()
    if not cleaned.isdigit():
        sys.exit(f"Customer ID must be digits (got {customer_id!r})")
    return cleaned


def _post_form(url: str, form: dict) -> dict:
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def _b64url(data: bytes) -> bytes:
    import base64
    return base64.urlsafe_b64encode(data).rstrip(b"=")


def _sa_key_token(info: dict) -> str:
    """OAuth2 JWT-bearer flow with a service-account key. RS256 signing is
    delegated to the openssl binary so no crypto pip package is needed."""
    import subprocess
    import tempfile
    import time

    try:
        sa_email, private_key = info["client_email"], info["private_key"]
    except KeyError as e:
        sys.exit(f"SA key JSON is missing field {e} — is this a service-account key?")

    now = int(time.time())
    claims = {"iss": sa_email, "scope": ADWORDS_SCOPE, "aud": OAUTH_TOKEN_URL,
              "iat": now, "exp": now + 3600}
    subject = os.environ.get("GOOGLE_ADS_SUBJECT_EMAIL")
    if subject:  # Workspace domain-wide delegation only
        claims["sub"] = subject
    enc = lambda obj: _b64url(json.dumps(obj, separators=(",", ":")).encode())
    signing_input = enc({"alg": "RS256", "typ": "JWT"}) + b"." + enc(claims)

    pem = tempfile.NamedTemporaryFile("w", suffix=".pem", delete=False)
    try:
        pem.write(private_key)
        pem.close()
        sig = subprocess.run(
            ["openssl", "dgst", "-sha256", "-sign", pem.name],
            input=signing_input, capture_output=True, check=True).stdout
    except FileNotFoundError:
        sys.exit("openssl binary not found — needed to sign the SA JWT")
    except subprocess.CalledProcessError as e:
        sys.exit(f"openssl signing failed: {e.stderr.decode(errors='replace')}")
    finally:
        os.unlink(pem.name)

    assertion = (signing_input + b"." + _b64url(sig)).decode()
    try:
        tok = _post_form(OAUTH_TOKEN_URL, {
            "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
            "assertion": assertion,
        })
    except urllib.error.HTTPError as e:
        sys.exit(f"SA token exchange failed ({e.code}): {e.read().decode(errors='replace')}")
    return tok["access_token"]


def _impersonation_token(sa_email: str) -> str:
    """Keyless: shell out to gcloud to mint an SA token with the Ads scope."""
    import subprocess
    try:
        res = subprocess.run(
            ["gcloud", "auth", "print-access-token",
             f"--impersonate-service-account={sa_email}",
             f"--scopes={ADWORDS_SCOPE}"],
            capture_output=True, text=True, check=True)
    except FileNotFoundError:
        sys.exit("gcloud not found — GOOGLE_ADS_IMPERSONATE_SA needs a local "
                 "gcloud login, or use GOOGLE_ADS_SA_KEY_FILE instead")
    except subprocess.CalledProcessError as e:
        sys.exit(f"gcloud impersonation failed: {e.stderr.strip()}")
    return res.stdout.strip()


def get_access_token() -> str:
    key_json = os.environ.get("GOOGLE_ADS_SA_KEY_JSON")
    if key_json:
        try:
            return _sa_key_token(json.loads(key_json))
        except ValueError as e:
            sys.exit(f"GOOGLE_ADS_SA_KEY_JSON is not valid JSON: {e}")
    key_file = (os.environ.get("GOOGLE_ADS_SA_KEY_FILE")
                or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"))
    if key_file:
        try:
            with open(key_file) as f:
                return _sa_key_token(json.load(f))
        except (OSError, ValueError) as e:
            sys.exit(f"Cannot read SA key file {key_file!r}: {e}")
    impersonate = os.environ.get("GOOGLE_ADS_IMPERSONATE_SA")
    if impersonate:
        return _impersonation_token(impersonate)
    try:
        tok = _post_form(OAUTH_TOKEN_URL, {
            "client_id": _env("GOOGLE_ADS_CLIENT_ID"),
            "client_secret": _env("GOOGLE_ADS_CLIENT_SECRET"),
            "refresh_token": _env("GOOGLE_ADS_REFRESH_TOKEN"),
            "grant_type": "refresh_token",
        })
    except urllib.error.HTTPError as e:
        sys.exit(f"OAuth token refresh failed ({e.code}): {e.read().decode(errors='replace')}")
    return tok["access_token"]


def api_call(access_token: str, path: str, body: dict | None = None,
             send_login_cid: bool = True) -> dict:
    """GET (body=None) or POST to the Google Ads REST API."""
    url = f"{API_BASE}/{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("developer-token", _env("GOOGLE_ADS_DEVELOPER_TOKEN"))
    if data:
        req.add_header("Content-Type", "application/json")
    login_cid = os.environ.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID")
    if login_cid and send_login_cid:
        req.add_header("login-customer-id", _digits(login_cid))
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raw = e.read().decode(errors="replace")
        try:
            detail = json.dumps(json.loads(raw), indent=2)
        except ValueError:
            detail = raw
        sys.exit(f"Google Ads API error {e.code} on {path}:\n{detail}")


def search(access_token: str, customer_id: str, query: str) -> list[dict]:
    """Run a GAQL query, following pagination."""
    results: list[dict] = []
    body: dict = {"query": query}
    while True:
        page = api_call(access_token, f"customers/{customer_id}/googleAds:search", body)
        results.extend(page.get("results", []))
        token = page.get("nextPageToken")
        if not token:
            return results
        body = {"query": query, "pageToken": token}


def _id_int(raw: str, what: str) -> int:
    try:
        return int(raw.replace("-", ""))
    except ValueError:
        sys.exit(f"{what} must be numeric (got {raw!r})")


def parse_troas(raw: str) -> float:
    """'4.5' -> 4.5; '450%' -> 4.5. Sanity-checks the range."""
    raw = raw.strip()
    if raw.endswith("%"):
        value = float(raw[:-1]) / 100.0
    else:
        value = float(raw)
        if value > 50:
            sys.exit(f"tROAS {value} looks like a percentage — pass it as "
                     f"'{value:g}%' or as a ratio (e.g. 4.5 for 450%)")
    if value <= 0:
        sys.exit("tROAS must be > 0")
    return value


def fmt_troas(value) -> str:
    if value in (None, ""):
        return "—"
    return f"{float(value):g} ({float(value) * 100:g}%)"


# ── Subcommands ──────────────────────────────────────────────────────────────

def cmd_accounts(_args) -> None:
    token = get_access_token()
    # listAccessibleCustomers ignores login-customer-id; lists accounts the
    # OAuth user can access directly (including manager accounts).
    res = api_call(token, "customers:listAccessibleCustomers", send_login_cid=False)
    names = res.get("resourceNames", [])
    if not names:
        print("No directly accessible customer accounts for this OAuth user.")
        return
    print("Accessible customer IDs (pass to --customer-id):")
    for rn in names:
        print(f"  {rn.removeprefix('customers/')}")


CAMPAIGN_FIELDS = (
    "campaign.id, campaign.name, campaign.status, "
    "campaign.bidding_strategy_type, campaign.bidding_strategy, "
    "campaign.maximize_conversion_value.target_roas, "
    "campaign.target_roas.target_roas"
)


def _campaign_troas(c: dict):
    mcv = c.get("maximizeConversionValue", {}).get("targetRoas")
    tr = c.get("targetRoas", {}).get("targetRoas")
    return mcv if mcv is not None else tr


def cmd_list(args) -> None:
    token = get_access_token()
    cid = _digits(args.customer_id)

    campaigns = search(token, cid, f"""
        SELECT {CAMPAIGN_FIELDS}
        FROM campaign
        WHERE campaign.status IN ('ENABLED', 'PAUSED')
        ORDER BY campaign.id""")
    print(f"Campaigns ({len(campaigns)}):")
    for row in campaigns:
        c = row["campaign"]
        portfolio = c.get("biddingStrategy")
        note = f"  [portfolio: {portfolio}]" if portfolio else ""
        print(f"  {c['id']:>12}  {c.get('status', '?'):<8} "
              f"{c.get('biddingStrategyType', '?'):<28} "
              f"tROAS={fmt_troas(_campaign_troas(c)):<16} {c.get('name', '')}{note}")

    strategies = search(token, cid, """
        SELECT bidding_strategy.id, bidding_strategy.name,
               bidding_strategy.type, bidding_strategy.status,
               bidding_strategy.target_roas.target_roas,
               bidding_strategy.maximize_conversion_value.target_roas
        FROM bidding_strategy
        WHERE bidding_strategy.status = 'ENABLED'""")
    print(f"\nPortfolio bidding strategies ({len(strategies)}):")
    for row in strategies:
        s = row["biddingStrategy"]
        print(f"  {s['id']:>12}  {s.get('type', '?'):<28} "
              f"tROAS={fmt_troas(_campaign_troas(s)):<16} {s.get('name', '')}")


def _troas_update(kind_enum: str, resource_name: str, troas: float, what: str):
    """Return (update_object, update_mask) for the given bidding scheme."""
    if kind_enum == "MAXIMIZE_CONVERSION_VALUE":
        return ({"resourceName": resource_name,
                 "maximizeConversionValue": {"targetRoas": troas}},
                "maximizeConversionValue.targetRoas")
    if kind_enum == "TARGET_ROAS":
        return ({"resourceName": resource_name,
                 "targetRoas": {"targetRoas": troas}},
                "targetRoas.targetRoas")
    sys.exit(f"{what} uses bidding scheme {kind_enum}, which has no target-ROAS "
             f"setting. Only MAXIMIZE_CONVERSION_VALUE and TARGET_ROAS do.")


def cmd_set(args) -> None:
    token = get_access_token()
    cid = _digits(args.customer_id)
    troas = parse_troas(args.troas)

    if args.campaign_id:
        rows = search(token, cid, f"""
            SELECT {CAMPAIGN_FIELDS} FROM campaign
            WHERE campaign.id = {_id_int(args.campaign_id, '--campaign-id')}""")
        if not rows:
            sys.exit(f"Campaign {args.campaign_id} not found in customer {cid}")
        c = rows[0]["campaign"]
        if c.get("biddingStrategy"):
            strategy_id = c["biddingStrategy"].rsplit("/", 1)[-1]
            sys.exit(f"Campaign {c['id']} ({c.get('name', '')}) uses PORTFOLIO "
                     f"strategy {c['biddingStrategy']}.\n"
                     f"Changing it affects every attached campaign — rerun with "
                     f"--strategy-id {strategy_id} instead of --campaign-id.")
        resource = f"customers/{cid}/campaigns/{c['id']}"
        update, mask = _troas_update(c.get("biddingStrategyType", ""), resource,
                                     troas, f"Campaign {c['id']}")
        mutate_path = f"customers/{cid}/campaigns:mutate"
        label = f"campaign {c['id']} ({c.get('name', '')})"
        old = _campaign_troas(c)
    else:
        rows = search(token, cid, f"""
            SELECT bidding_strategy.id, bidding_strategy.name,
                   bidding_strategy.type,
                   bidding_strategy.target_roas.target_roas,
                   bidding_strategy.maximize_conversion_value.target_roas
            FROM bidding_strategy
            WHERE bidding_strategy.id = {_id_int(args.strategy_id, '--strategy-id')}""")
        if not rows:
            sys.exit(f"Bidding strategy {args.strategy_id} not found in customer {cid}")
        s = rows[0]["biddingStrategy"]
        resource = f"customers/{cid}/biddingStrategies/{s['id']}"
        update, mask = _troas_update(s.get("type", ""), resource, troas,
                                     f"Strategy {s['id']}")
        mutate_path = f"customers/{cid}/biddingStrategies:mutate"
        label = f"portfolio strategy {s['id']} ({s.get('name', '')})"
        old = _campaign_troas(s)

    print(f"{label}: tROAS {fmt_troas(old)} -> {fmt_troas(troas)}"
          + ("  [VALIDATE ONLY — no write]" if args.validate_only else ""))
    body = {"operations": [{"update": update, "updateMask": mask}]}
    if args.validate_only:
        body["validateOnly"] = True
    res = api_call(token, mutate_path, body)
    if args.validate_only:
        print("Validation passed — the change would be accepted.")
    else:
        applied = (res.get("results") or [{}])[0].get("resourceName", resource)
        print(f"Updated: {applied}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("accounts", help="list customer IDs the OAuth user can access")

    p_list = sub.add_parser("list", help="list campaigns + portfolio strategies with tROAS")
    p_list.add_argument("--customer-id", required=True)

    p_set = sub.add_parser("set", help="change target ROAS")
    p_set.add_argument("--customer-id", required=True)
    target = p_set.add_mutually_exclusive_group(required=True)
    target.add_argument("--campaign-id", help="campaign with its own (non-portfolio) bidding")
    target.add_argument("--strategy-id", help="portfolio bidding strategy ID")
    p_set.add_argument("--troas", required=True,
                       help="new target ROAS as ratio (4.5) or percent (450%%)")
    p_set.add_argument("--validate-only", action="store_true",
                       help="server-side dry run — validates but writes nothing")

    args = p.parse_args()
    {"accounts": cmd_accounts, "list": cmd_list, "set": cmd_set}[args.cmd](args)


if __name__ == "__main__":
    main()
