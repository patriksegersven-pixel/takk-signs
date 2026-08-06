#!/usr/bin/env python3
"""
One-time helper: mint a long-lived Google Ads API refresh token.

Run this LOCALLY on a machine with a browser (it opens a Google consent
page and catches the redirect on localhost) — it cannot run in a headless
remote session. Same pattern as the Funnel bootstrap described in
apps/babyshop-dashboard/funnel_client.py.

Prerequisites (see README.md):
  1. An OAuth client of type "Desktop app" in the GCP project
     (APIs & Services -> Credentials -> Create credentials -> OAuth client ID).
  2. The Google account you log in with must have access to the target
     Google Ads account(s).

Usage:
    python3 bootstrap_google_ads_oauth.py --client-id X --client-secret Y
    # or export GOOGLE_ADS_CLIENT_ID / GOOGLE_ADS_CLIENT_SECRET first

Prints the refresh token and the commands to store it in Secret Manager.
Stdlib only — no pip install needed.
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import secrets
import sys
import urllib.parse
import urllib.request
import webbrowser

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPE = "https://www.googleapis.com/auth/adwords"


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--client-id", default=os.environ.get("GOOGLE_ADS_CLIENT_ID"))
    p.add_argument("--client-secret", default=os.environ.get("GOOGLE_ADS_CLIENT_SECRET"))
    p.add_argument("--port", type=int, default=8123)
    args = p.parse_args()
    if not args.client_id or not args.client_secret:
        sys.exit("Provide --client-id/--client-secret or set "
                 "GOOGLE_ADS_CLIENT_ID / GOOGLE_ADS_CLIENT_SECRET")

    redirect_uri = f"http://localhost:{args.port}"
    state = secrets.token_urlsafe(16)
    auth_url = AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": args.client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPE,
        "access_type": "offline",   # required to get a refresh token
        "prompt": "consent",        # force refresh_token even if previously granted
        "state": state,
    })

    print("Opening browser for Google consent (log in with the account that "
          "has Google Ads access)…\nIf nothing opens, visit:\n\n" + auth_url + "\n")
    webbrowser.open(auth_url)

    code_holder: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            if params.get("state", [""])[0] != state:
                self.wfile.write(b"State mismatch - ignoring. Retry the script.")
                return
            if "code" in params:
                code_holder["code"] = params["code"][0]
                self.wfile.write(b"Done - you can close this tab and return to the terminal.")
            else:
                self.wfile.write(f"No code in callback: {params}".encode())

        def log_message(self, *a):  # silence request logging
            pass

    with http.server.HTTPServer(("localhost", args.port), Handler) as srv:
        while "code" not in code_holder:
            srv.handle_request()

    data = urllib.parse.urlencode({
        "client_id": args.client_id,
        "client_secret": args.client_secret,
        "code": code_holder["code"],
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }).encode()
    req = urllib.request.Request(TOKEN_URL, data=data, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req, timeout=30) as resp:
        tok = json.load(resp)

    refresh = tok.get("refresh_token")
    if not refresh:
        sys.exit(f"No refresh_token in response (got keys: {sorted(tok)}). "
                 "Rerun — the 'prompt=consent' parameter should force one.")

    print("\nRefresh token minted. Store it (and the client credentials) in "
          "Secret Manager:\n")
    print(f"  echo -n '{refresh}' | gcloud secrets create google-ads-refresh-token "
          f"--data-file=- --project=project-a7ade44e-e7e3-4871-a83")
    print("  # plus google-ads-client-id, google-ads-client-secret, "
          "google-ads-developer-token the same way\n")
    print("Then export as env vars to use scripts/ads/set_troas.py "
          "(see README.md).")


if __name__ == "__main__":
    main()
