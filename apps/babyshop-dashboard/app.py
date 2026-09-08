"""
Babyshop KV Dashboard — Cloud Run service.

Auth: HTTP Basic Auth with a single shared password.
  • Set DASH_PASS via Google Secret Manager (see DEPLOY.md).
  • Username defaults to "babyshop", change with DASH_USER.
  • For local development, set DEV_MODE=true to bypass auth.

Configuration via env vars (typically wired from Google Secret Manager):
  DASH_USER             — basic auth username (default: babyshop)
  DASH_PASS             — basic auth password (required in production)
  DEV_MODE              — set to "true" to skip auth locally
  FUNNEL_CLIENT_ID      — OAuth client ID      (only for /api/funnel/* routes)
  FUNNEL_CLIENT_SECRET  — OAuth client secret  (only for /api/funnel/* routes)
  FUNNEL_REFRESH_TOKEN  — OAuth refresh token  (only for /api/funnel/* routes)
  FUNNEL_WORKSPACE      — Funnel workspace ID (default: AA/BS/LM/MJ Combo)
  CHANNABLE_FEED_URL    — Channable XML feed URL (for inventory refresh)
  INTERNAL_TOKEN        — shared secret for /internal/* endpoints (Cloud Scheduler)

Capture the Funnel OAuth refresh token once by running `bootstrap_oauth.py`
locally.
"""
from __future__ import annotations

import os
import secrets
import threading
import time
from collections import OrderedDict
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials

# ── App ──────────────────────────────────────────────────────────────────────
app = FastAPI(
    title="Babyshop KV Dashboard",
    description="Internal BI dashboard for Babyshop KV metrics.",
    version="0.3.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
security = HTTPBasic(auto_error=False)

# ── Auth (HTTP Basic) ────────────────────────────────────────────────────────
DASH_USER = os.environ.get("DASH_USER", "babyshop")
DASH_PASS = os.environ.get("DASH_PASS")
DEV_MODE = os.environ.get("DEV_MODE", "").lower() == "true"

if DEV_MODE:
    print("⚠️  DEV_MODE=true — authentication is bypassed. Do NOT use in production.")
elif not DASH_PASS:
    # Allow boot without password but warn loudly. Production should always
    # set this via Secret Manager.
    print("⚠️  DASH_PASS env var not set — using insecure dev default 'change-me'.")
    DASH_PASS = "change-me"


def verify(credentials: HTTPBasicCredentials | None = Depends(security)) -> str:
    """Verify the request carries valid HTTP Basic credentials."""
    if DEV_MODE:
        return "dev@local"

    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": 'Basic realm="Babyshop Dashboard"'},
        )

    # _const_eq, not secrets.compare_digest directly: a non-ASCII username or password —
    # supplied by the caller, or a perfectly legitimate DASH_PASS with an accent in it —
    # makes compare_digest raise TypeError, turning a failed login into a 500.
    user_ok = _const_eq(credentials.username, DASH_USER)
    pass_ok = _const_eq(credentials.password, DASH_PASS)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": 'Basic realm="Babyshop Dashboard"'},
        )
    return credentials.username


def _const_eq(supplied: str | None, expected: str | None) -> bool:
    """Constant-time string compare that cannot be crashed by the caller.

    secrets.compare_digest() raises TypeError on a str containing non-ASCII, so passing a
    raw query param or header straight in turns any UTF-8 byte into an unhandled 500. On
    /api/roas-sims that is worse than it sounds: the dashboard's fetchLive() reads
    json.error to decide a key was rejected, so a 500 never reaches its /unauthor/i branch,
    the bad key is never cleared from localStorage, and every reload fails forever.
    Comparing the UTF-8 encodings keeps the timing property and accepts any input."""
    return secrets.compare_digest((supplied or "").encode("utf-8"),
                                  (expected or "").encode("utf-8"))


# ── ROAS Simulations access key ──────────────────────────────────────────────
# The ROAS Simulations page keeps an access key in the viewer's localStorage and sends
# it as ?token=. That gate was built against the public Apps Script endpoint; now that
# the endpoint is same-origin the page is ALREADY behind `verify` (HTTP Basic) like
# every other /api/* route, so the key is a second, optional deterrent rather than the
# only lock.
#   ROAS_SIMS_TOKEN set    -> the key is enforced; a mismatch returns the same
#                             {"error": "Unauthorized"} body the Apps Script returned,
#                             which is what makes the page drop the stored key and
#                             re-prompt instead of treating it as a network blip.
#   ROAS_SIMS_TOKEN unset  -> any key is accepted (the page still asks once and
#                             remembers it). Set the env var to make the gate real.
#
# NOTE: deliberately NOT bypassed by DEV_MODE. The live service runs with DEV_MODE=true,
# so a DEV_MODE short-circuit here would mean setting ROAS_SIMS_TOKEN silently did nothing
# — the exact opposite of what an operator setting it is asking for. Presence of the env
# var is the only switch: don't set it and there is no gate to bypass.
ROAS_SIMS_TOKEN = os.environ.get("ROAS_SIMS_TOKEN")


def _verify_sims_token(token: str):
    """None when the key passes; the JSON body to return when it does not.

    HTTP 200 on purpose — the dashboard's fetchLive() only inspects `json.error` for the
    word "unauthorized"; a 401 would raise "HTTP 401" and be retried as transient."""
    if not ROAS_SIMS_TOKEN:
        return None
    if _const_eq(token, ROAS_SIMS_TOKEN):
        return None
    return JSONResponse({"error": "Unauthorized", "status": "unauthorized"}, status_code=200)


# ── Static dashboard routes ──────────────────────────────────────────────────
STATIC_DIR     = Path(__file__).parent
KV_HTML        = STATIC_DIR / "babyshop-dashboard.html"
PRODUCT_HTML   = STATIC_DIR / "babyshop-product-dashboard.html"
CUSTOMER_HTML  = STATIC_DIR / "babyshop-customer-dashboard.html"
SEGMENTS_HTML  = STATIC_DIR / "babyshop-segments-dashboard.html"
INVENTORY_HTML = STATIC_DIR / "babyshop-inventory-dashboard.html"
STOY_HTML      = STATIC_DIR / "babyshop-stoy-dashboard.html"
ROAS_HTML      = STATIC_DIR / "babyshop-roas-impact.html"
SIM_HTML       = STATIC_DIR / "babyshop-roas-simulations.html"
VOYADO_HTML    = STATIC_DIR / "babyshop-voyado-dashboard.html"
BUNDLES_HTML   = STATIC_DIR / "babyshop-bundles-dashboard.html"
SOS_HTML       = STATIC_DIR / "babyshop-sos-dashboard.html"
META_HTML      = STATIC_DIR / "babyshop-meta-dashboard.html"
TABLE_TOOLS_JS = STATIC_DIR / "table-tools.js"
CHART_JS       = STATIC_DIR / "chart.umd.js"
BRAND_CSS      = STATIC_DIR / "brand.css"
NAV_JS         = STATIC_DIR / "nav.js"
LOGO_SVG       = STATIC_DIR / "babyshop-logo.svg"
FAVICON_SVG    = STATIC_DIR / "babyshop-favicon.svg"
FONTS_DIR      = STATIC_DIR / "fonts"
# Explicit allow-list, not a directory walk: the path segment comes from the
# request, and a whitelist is the only traversal-proof way to use it.
FONT_FILES     = {"jost-latin.woff2", "jost-latin-ext.woff2"}


@app.get("/")
def root(_: str = Depends(verify)):
    return RedirectResponse(url="/babyshop-dashboard.html", status_code=302)


@app.get("/babyshop-dashboard.html")
def kv_dashboard(_: str = Depends(verify)):
    return FileResponse(KV_HTML, media_type="text/html")


@app.get("/babyshop-product-dashboard.html")
def product_dashboard(_: str = Depends(verify)):
    return FileResponse(PRODUCT_HTML, media_type="text/html")


@app.get("/babyshop-customer-dashboard.html")
def customer_dashboard(_: str = Depends(verify)):
    return FileResponse(CUSTOMER_HTML, media_type="text/html")


@app.get("/babyshop-segments-dashboard.html")
def segments_dashboard(_: str = Depends(verify)):
    return FileResponse(SEGMENTS_HTML, media_type="text/html")


@app.get("/babyshop-inventory-dashboard.html")
def inventory_dashboard(_: str = Depends(verify)):
    return FileResponse(INVENTORY_HTML, media_type="text/html")


@app.get("/babyshop-stoy-dashboard.html")
def stoy_dashboard(_: str = Depends(verify)):
    return FileResponse(STOY_HTML, media_type="text/html")


@app.get("/babyshop-roas-impact.html")
def roas_impact_dashboard(_: str = Depends(verify)):
    return FileResponse(ROAS_HTML, media_type="text/html")


@app.get("/babyshop-roas-simulations.html")
def roas_simulations_dashboard(_: str = Depends(verify)):
    return FileResponse(SIM_HTML, media_type="text/html")


@app.get("/babyshop-voyado-dashboard.html")
def voyado_dashboard(_: str = Depends(verify)):
    return FileResponse(VOYADO_HTML, media_type="text/html")


@app.get("/babyshop-bundles-dashboard.html")
def bundles_dashboard(_: str = Depends(verify)):
    return FileResponse(BUNDLES_HTML, media_type="text/html")


@app.get("/babyshop-sos-dashboard.html")
def sos_dashboard(_: str = Depends(verify)):
    return FileResponse(SOS_HTML, media_type="text/html")


@app.get("/babyshop-meta-dashboard.html")
def meta_dashboard(_: str = Depends(verify)):
    return FileResponse(META_HTML, media_type="text/html")


# Shared table filtering + export module, used by <script src="/table-tools.js">
# on the dashboard pages. Same `verify` dependency as the HTML routes: the pages
# are fetched with Basic credentials, so the browser replays them for this
# sub-resource — an unauthenticated route here would be a hole, not a fix.
@app.get("/table-tools.js")
def table_tools_js(_: str = Depends(verify)):
    return FileResponse(TABLE_TOOLS_JS, media_type="application/javascript")


# Chart.js v4.4.1 UMD, vendored. Served same-origin because cdn.jsdelivr.net is
# unreachable from some networks — a failed CDN load threw "Chart is not
# defined", blanking every chart and leaving the KPI cards on embedded data.
@app.get("/chart.umd.js")
def chart_js(_: str = Depends(verify)):
    return FileResponse(CHART_JS, media_type="application/javascript")


# Shared design system: tokens, @font-face, header/nav chrome. Linked from every
# page's <head>. Same `verify` dependency as every other sub-resource.
@app.get("/brand.css")
def brand_css(_: str = Depends(verify)):
    return FileResponse(BRAND_CSS, media_type="text/css")


# Config-driven header/nav. Every page renders its header from this file's PAGES
# registry, so a new dashboard page is one line there plus a route above.
@app.get("/nav.js")
def nav_js(_: str = Depends(verify)):
    return FileResponse(NAV_JS, media_type="application/javascript")


@app.get("/babyshop-logo.svg")
def logo_svg(_: str = Depends(verify)):
    return FileResponse(LOGO_SVG, media_type="image/svg+xml")


# The teddy-bear mark alone (wordmark cropped out) — the full logo is
# illegible at favicon size.
@app.get("/babyshop-favicon.svg")
def favicon_svg(_: str = Depends(verify)):
    return FileResponse(FAVICON_SVG, media_type="image/svg+xml")


# Jost, vendored for the same reason Chart.js is: fonts.gstatic.com is not
# reachable from every network this dashboard is opened on.
@app.get("/fonts/{name}")
def font_file(name: str, _: str = Depends(verify)):
    if name not in FONT_FILES:
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(FONTS_DIR / name, media_type="font/woff2")


# ── Health (no auth — for Cloud Run probes) ──────────────────────────────────
# NOT `/healthz`: Google Frontend reserves `/health*`-with-suffix paths
# (`/healthz`, `/livez`, `/readyz`) and answers them itself with a Google-branded
# 404 — the request never reaches this container. `/health` passes through.
@app.get("/health", include_in_schema=False)
def health():
    return {"status": "ok"}


# ── API namespace ─────────────────────────────────────────────────────────────
@app.get("/api/kv-data")
def api_kv_data(_: str = Depends(verify)):
    from funnel_client import fetch_kv_overview, FunnelNotConfigured

    try:
        return fetch_kv_overview()
    except FunnelNotConfigured as e:
        return JSONResponse({"error": str(e)}, status_code=503)


@app.get("/api/product-data")
def api_product_data(_: str = Depends(verify)):
    from funnel_client import fetch_product_overview, FunnelNotConfigured

    try:
        return fetch_product_overview()
    except FunnelNotConfigured as e:
        return JSONResponse({"error": str(e)}, status_code=503)


@app.get("/api/stoy-data")
def api_stoy_data(_: str = Depends(verify)):
    """Stoy funnel-shift test snapshot (written to Firestore by the BigQuery job)."""
    from funnel_client import get_cache
    data = get_cache().get("stoy-test")
    if data is None:
        return JSONResponse({"error": "no stoy snapshot yet"}, status_code=503)
    return data


@app.get("/api/customer-insights")
def api_customer_insights(_: str = Depends(verify)):
    """Customer Insights snapshot (written to Firestore by refresh_customer_insights.py).

    Same Firestore-cache read as /api/stoy-data and /api/roas-impact — one document,
    `funnel_cache/{workspace}__customer-insights`, whose TTL the cache layer enforces.

    Deliberately 200-with-an-empty-payload instead of the 503 those two return: the
    Customer Insights page is shipped BEFORE its refresher has ever run, and half the
    contract (`norce`) stays null for longer still. Handing the page a well-formed
    skeleton lets it render its own pending state — KPIs as "awaiting data", empty
    tables with an explanation — instead of falling back to a generic fetch failure.
    `generated_at: null` is the page's signal that no snapshot exists yet."""
    from funnel_client import get_cache

    try:
        data = get_cache().get("customer-insights")
    except Exception as e:
        # A Firestore hiccup must not blank the tab — log and serve the skeleton.
        print(f"ERROR /api/customer-insights: {type(e).__name__}: {e}", flush=True)
        data = None
    if data is not None:
        return data
    return {
        "generated_at": None,
        "sources": {
            "funnel": {"from": None, "to": None},
            "norce": {"available": False, "last_sync": None,
                      "note": "awaiting NORCE_* secrets"},
        },
        "kpis": {},
        "funnel": {"monthly": [], "cac_matrix": [], "trend": []},
        "norce": None,
        "caveats": ["no customer-insights snapshot yet — the refresh job has not run"],
    }


@app.get("/api/segments")
def api_segments(_: str = Depends(verify)):
    """Customer Segments snapshot (written to Firestore by refresh_segments.py).

    Same 200-with-a-skeleton contract as /api/customer-insights: the tab ships
    before its refresher has run, and `generated_at: null` tells the page to
    render its pending state. The skeleton's shape mirrors the page's embedded
    SNAP constant — keep the two in sync."""
    from funnel_client import get_cache

    try:
        data = get_cache().get("segments")
    except Exception as e:
        # A Firestore hiccup must not blank the tab — log and serve the skeleton.
        print(f"ERROR /api/segments: {type(e).__name__}: {e}", flush=True)
        data = None
    if data is not None:
        return data
    return {
        "generated_at": None,
        "sources": {"norce": {"dataset": None, "coverage": {}},
                    "history_start": "2025-06-11", "identity": ""},
        "kpis": {},
        "lifecycle": [], "cohorts": [], "value_deciles": [],
        "lifestage": {"distribution": [], "sized_customers": 0,
                      "ltv_by_entry": [], "aging_out": {}},
        "discount": [], "cross_brand": [], "markets": [],
        "caveats": ["no Segments snapshot yet — refresh_segments.py has not run"],
    }


@app.get("/api/share-of-search")
def api_share_of_search(_: str = Depends(verify)):
    """Share of Search payload (merged Keyword Planner history per market, written by
    refresh_share_of_search.py — monthly via /internal/refresh-sos).

    Same 200-with-a-skeleton contract as /api/segments: `generated_at: null` tells
    the page to render its pending state, and config always rides along so the tab
    can show the keyword set before the first collection has run."""
    import refresh_share_of_search as sos

    try:
        return sos.read_payload()
    except Exception as e:
        # A Firestore hiccup must not blank the tab — log and serve the skeleton.
        print(f"ERROR /api/share-of-search: {type(e).__name__}: {e}", flush=True)
        return {
            "generated_at": None,
            "config": {"markets": sos.DEFAULT_CONFIG["markets"],
                       "keywords": sos.DEFAULT_CONFIG["keywords"]},
            "markets": {},
        }


@app.get("/api/voyado-email")
def api_voyado_email(_: str = Depends(verify)):
    """Voyado email snapshot (written to Firestore by refresh_voyado.py).

    Same Firestore-cache read as /api/customer-insights, and the same
    200-with-a-skeleton contract rather than the 503 that /api/stoy-data and
    /api/roas-impact return: the tab ships before its refresher has run, and
    the event tables backfill over hours, so the page must be able to render its
    own pending state. `generated_at: null` is that signal.

    The skeleton carries the caveats even when empty — this dataset has no
    bounce data and its revenue is last-click, and a reader should meet those
    facts on the empty state too, not only once numbers appear."""
    from funnel_client import get_cache

    try:
        data = get_cache().get("voyado-email")
    except Exception as e:
        # A Firestore hiccup must not blank the tab — log and serve the skeleton.
        print(f"ERROR /api/voyado-email: {type(e).__name__}: {e}", flush=True)
        data = None
    if data is not None:
        return data
    return {
        "generated_at": None,
        "sources": {"voyado": {"dataset": None, "coverage": {}},
                    "window": {"from": None, "to": None, "days": 0},
                    "attribution": {"model": "last click", "window_days": 7,
                                    "key": "voyado contactId"}},
        "kpis": {}, "trend": [], "markets": [], "campaigns": [],
        "automations": [], "revenue_share": [],
        "caveats": ["no Voyado snapshot yet — refresh_voyado.py has not run"],
    }


@app.get("/api/bundles")
def api_bundles(_: str = Depends(verify)):
    """Bundles snapshot (written to Firestore by refresh_bundles.py).

    Same 200-with-a-skeleton contract as /api/segments: the tab ships before
    its refresher has run, and `generated_at: null` tells the page to render
    its pending state. Shape mirrors refresh_bundles.build_payload()."""
    from funnel_client import get_cache

    try:
        data = get_cache().get("bundles")
    except Exception as e:
        # A Firestore hiccup must not blank the tab — log and serve the skeleton.
        print(f"ERROR /api/bundles: {type(e).__name__}: {e}", flush=True)
        data = None
    if data is not None:
        return data
    return {
        "generated_at": None,
        "sources": {"norce": {"dataset": None}, "window_days": 365,
                    "min_together": 30, "min_lift_strong": 5,
                    "gross_margin": 0.44, "images": ""},
        "shops": {}, "baskets": [], "category_pairs": [], "pairs": [],
        "caveats": ["no Bundles snapshot yet — refresh_bundles.py has not run"],
    }


# The Meta tab's filter grid. Every PRESET combination is a separate Firestore
# document written by refresh_meta.py, so a preset switch is one document read
# and the response shape is identical whichever combination is asked for.
#
# The default (all, 7 days) lives at the bare `meta` key it has had since v1,
# so an old cached page that asks for no parameters still gets a real snapshot.
#
# A CUSTOM range (?from=&to=) is the one thing no snapshot can cover — there are
# too many of them to precompute — so it is answered live from BigQuery by
# refresh_meta.build_custom(), the same function the nightly job's presets are
# built with. See _api_meta_live below.
META_MARKETS = ("all", "SE", "NO")
META_DAYS = (7, 14, 28, 90)
META_DEFAULT_MARKET = "all"
META_DEFAULT_DAYS = 7

# ── Live custom ranges ───────────────────────────────────────────────────────
# One BigQuery scan per distinct (market, from, to) per TTL. The cache is small
# and in-process on purpose: a Cloud Run instance serves a handful of readers,
# the payload is ~350 kB, and the point is only to stop a re-render, a reload
# or a second reader from paying for the same scan twice. A per-key lock means
# two simultaneous requests for the same range run ONE query and both get its
# result, rather than racing and billing twice.
META_LIVE_TTL = int(os.environ.get("META_LIVE_TTL", "600"))          # 10 minutes
META_LIVE_CACHE_MAX = int(os.environ.get("META_LIVE_CACHE_MAX", "12"))
_meta_live: "OrderedDict[tuple, tuple[float, dict]]" = OrderedDict()
_meta_live_guard = threading.Lock()      # guards the two dicts below, never a query
_meta_live_locks: dict = {}


def _meta_live_get(key: tuple):
    """The cached payload for one range, or None. Expiry is checked on read."""
    with _meta_live_guard:
        hit = _meta_live.get(key)
        if hit is None:
            return None
        born, payload = hit
        if time.time() - born > META_LIVE_TTL:
            _meta_live.pop(key, None)
            return None
        _meta_live.move_to_end(key)       # LRU: a range still being read stays
        return payload, round(time.time() - born)


def _meta_live_put(key: tuple, payload: dict) -> None:
    with _meta_live_guard:
        _meta_live[key] = (time.time(), payload)
        _meta_live.move_to_end(key)
        while len(_meta_live) > META_LIVE_CACHE_MAX:
            _meta_live.popitem(last=False)
        # Locks outlive their cache entry by design (a request can be in flight
        # for a key that is not cached yet), but they must not accumulate.
        if len(_meta_live_locks) > 4 * META_LIVE_CACHE_MAX:
            for k in [k for k in _meta_live_locks
                      if k not in _meta_live and not _meta_live_locks[k].locked()]:
                _meta_live_locks.pop(k, None)


def _meta_live_lock(key: tuple):
    with _meta_live_guard:
        lk = _meta_live_locks.get(key)
        if lk is None:
            lk = _meta_live_locks[key] = threading.Lock()
        return lk


def _meta_range(date_from: str, date_to: str):
    """Validate a custom range. Returns (start, end) or raises ValueError.

    The bounds live in refresh_meta so the job and the API cannot state
    different limits; the messages are written for the reader, because the page
    prints them verbatim next to the date inputs."""
    import datetime as _dt
    import refresh_meta as rm

    if not (date_from and date_to):
        raise ValueError("a custom range needs both from and to")
    try:
        start = _dt.date.fromisoformat(date_from)
        end = _dt.date.fromisoformat(date_to)
    except ValueError:
        raise ValueError("dates must be YYYY-MM-DD")
    if start > end:
        raise ValueError("the start date is after the end date")
    # The same settle offset the presets use: the last two days of spend have
    # not finished attributing, and showing them would read as a collapse.
    last = _dt.date.today() - _dt.timedelta(days=rm.SETTLE_DAYS)
    if end > last:
        raise ValueError(f"the last settled day is {last.isoformat()} — Meta "
                         f"attribution needs {rm.SETTLE_DAYS} days to settle")
    first = _dt.date.fromisoformat(rm.DATA_START)
    if start < first:
        raise ValueError(f"the Meta feed starts on {rm.DATA_START}")
    span = (end - start).days + 1
    if span > rm.MAX_SPAN_DAYS:
        raise ValueError(f"{span} days is over the {rm.MAX_SPAN_DAYS}-day limit "
                         "(the query also scans an equally long comparison period)")
    return start, end


def _api_meta_live(market: str, date_from: str, date_to: str):
    """One custom range, computed live from BigQuery.

    Credentials come from refresh_meta._credentials(): ADC in production, i.e.
    the service's own runtime SA — which already holds bigquery.jobUser here and
    dataViewer on the warehouse datasets because the nightly job runs in this
    same image with the same identity. No new IAM, no new secret. Locally,
    META_LIVE_AUTH=gcloud + META_LIVE_ACCOUNT pick a user account instead."""
    import refresh_meta as rm

    try:
        start, end = _meta_range(date_from, date_to)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    key = (market, start.isoformat(), end.isoformat())
    hit = _meta_live_get(key)
    if hit is not None:
        payload, age = hit
        print(f"/api/meta live {market} {key[1]}..{key[2]} — cache hit "
              f"({age}s old), no BigQuery", flush=True)
        return payload

    lock = _meta_live_lock(key)
    with lock:
        # Re-checked inside the lock: a request that queued behind an identical
        # one in flight finds its result here instead of running the scan again.
        hit = _meta_live_get(key)
        if hit is not None:
            payload, age = hit
            print(f"/api/meta live {market} {key[1]}..{key[2]} — cache hit after "
                  f"wait ({age}s old), no BigQuery", flush=True)
            return payload
        t0 = time.time()
        try:
            # The Firestore client is the app's own singleton, used ONLY to read
            # the image mirror's index by id — the service never writes there
            # and never fetches from Meta's CDN; mirroring stays the job's work.
            from funnel_client import get_firestore_db
            payload, st = rm.build_custom(market, start, end, db=get_firestore_db())
        except Exception as e:
            print(f"ERROR /api/meta live {market} {key[1]}..{key[2]}: "
                  f"{type(e).__name__}: {e}", flush=True)
            return JSONResponse(
                {"error": f"the BigQuery query failed ({type(e).__name__}). "
                          "Try a shorter range, or a preset."}, status_code=502)
        payload["filters"]["available_markets"] = list(META_MARKETS)
        payload["filters"]["available_days"] = list(META_DAYS)
        _meta_live_put(key, payload)
        print(f"/api/meta live {market} {key[1]}..{key[2]} ({st['span']} d) — "
              f"{time.time() - t0:.1f}s total, {st['elapsed']}s in BigQuery, "
              f"{st['bytes_processed']:,} B processed, scan from {st['scan_from']}, "
              f"{st['ads']} ads / {st['ad_days']:,} ad-days, "
              f"{st['images']} mirrored images indexed in {st['images_elapsed']}s",
              flush=True)
        return payload


def _meta_cache_key(market: str, days: int) -> str:
    if market == META_DEFAULT_MARKET and days == META_DEFAULT_DAYS:
        return "meta"
    return f"meta__{market}_{days}"


@app.get("/api/meta")
def api_meta(market: str = META_DEFAULT_MARKET, days: str = str(META_DEFAULT_DAYS),
             date_from: str = Query("", alias="from"),
             date_to: str = Query("", alias="to"),
             _: str = Depends(verify)):
    """Meta creatives snapshot (written to Firestore by refresh_meta.py).

    Same 200-with-a-skeleton contract as /api/bundles. Shape mirrors
    refresh_meta.build_combo() — keep the two in step.

    `market` and `days` select one precomputed combination. An unknown value
    falls back to the default rather than 400ing: this is a dashboard read, a
    hand-edited URL should show the default view instead of an error page, and
    the `filters` block in the response always states which combination was
    actually served.

    `from` / `to` (either one present) switch to the live path instead: an
    arbitrary range, queried from BigQuery, answering with the same payload
    shape and `filters.custom = true`. Bad input is a 400 and a failed query a
    502, both `{"error": "…"}` — the page prints the message next to the date
    inputs, which is more use than a default view the reader did not ask for.
    `from`/`to` are aliased because `from` is a Python keyword."""
    if market not in META_MARKETS:
        market = META_DEFAULT_MARKET
    if date_from or date_to:
        return _api_meta_live(market, date_from.strip(), date_to.strip())

    from funnel_client import get_cache

    # `days` is typed as a string on purpose: FastAPI would answer ?days=abc
    # with a 422 validation page, and a dashboard URL someone hand-edited
    # should show the default view, not an error.
    try:
        days_n = int(days)
    except (TypeError, ValueError):
        days_n = META_DEFAULT_DAYS
    if days_n not in META_DAYS:
        days_n = META_DEFAULT_DAYS
    days = days_n

    cache = None
    try:
        cache = get_cache()
        data = cache.get(_meta_cache_key(market, days))
    except Exception as e:
        print(f"ERROR /api/meta: {type(e).__name__}: {e}", flush=True)
        data = None
    # A combination whose document has not been written yet (an older refresh
    # run, or a TTL expiry mid-rollout) falls back to the default document
    # rather than to an empty skeleton — the wrong window is far better than a
    # blank page, and the echoed filters say which one is on screen.
    if data is None and (market, days) != (META_DEFAULT_MARKET, META_DEFAULT_DAYS):
        try:
            data = cache.get("meta") if cache else None
        except Exception as e:
            print(f"ERROR /api/meta fallback: {type(e).__name__}: {e}", flush=True)
            data = None
    if data is not None:
        # Old snapshots predate the filter block; state the default rather than
        # letting the page think its request was honoured.
        data.setdefault("filters", {"market": META_DEFAULT_MARKET,
                                    "days": META_DEFAULT_DAYS})
        data["filters"]["available_markets"] = list(META_MARKETS)
        data["filters"]["available_days"] = list(META_DAYS)
        return data
    return {
        "generated_at": None,
        "sources": {"insights": None, "ads": None, "creatives": None,
                    "marts": None, "account_id": None,
                    "account_label": None, "attribution": "7d_click,1d_view",
                    "markets": ["SE", "NO"], "all_markets": ["SE", "NO"],
                    "min_spend": 300,
                    "min_spend_l28": 1000, "images": "",
                    "instagram_coverage": {}},
        "filters": {"market": market, "days": days,
                    "available_markets": list(META_MARKETS),
                    "available_days": list(META_DAYS)},
        "window": {"from": None, "to": None, "prev_from": None,
                   "prev_to": None, "l28_from": None, "l28_to": None,
                   "days": days, "long_days": 28 if days == 7 else days},
        "kpis": {}, "media_types": [], "media_formats": [],
        "fatigue": {"rows": [], "by_status": [], "total_spend": None,
                    "at_risk_spend": None, "at_risk_pct": None,
                    "thresholds": {}},
        "leaderboards": {}, "rollups": {"by_tag": [], "by_media_type": [],
                                        "by_promo": [], "by_tag_media": [],
                                        "averages": {}, "index_metrics": []},
        "top_creatives": {"SE": [], "NO": []}, "recent": [],
        "caveats": ["no Meta snapshot yet — refresh_meta.py has not run"],
    }


# Mirrored creative images, one Firestore document per ad, written by
# refresh_meta.mirror_images(). The tab used to hotlink Meta's signed CDN URL,
# which is 64x64 for most creatives and dies about four days after minting;
# this serves our own 640px/192px JPEGs instead.
#
# Read STRICTLY by document id. `meta_creative_images` holds base64 image bytes,
# so a list/query over it would pull megabytes per request — there is no route
# anywhere that enumerates this collection.
META_IMAGE_COLLECTION = "meta_creative_images"
META_IMAGE_SIZES = {"thumb": "thumb_b64", "large": "large_b64"}


@app.get("/api/meta/image/{ad_id}")
def api_meta_image(ad_id: str, size: str = "thumb", _: str = Depends(verify)):
    """One mirrored creative image as image/jpeg. 404 when it was never mirrored.

    404 is a normal, expected answer: an ad whose only source URL was already
    dead, or one added since the last refresh, simply has no document. The page
    treats it as "fall back to the hotlink, then to a placeholder"."""
    import base64

    field = META_IMAGE_SIZES.get(size)
    if field is None:
        raise HTTPException(status_code=400, detail="size must be thumb or large")
    # The ad id lands in a Firestore document path; Meta ids are digits, and
    # anything else would either miss or (with a '/') address a different path.
    if not ad_id.isdigit():
        raise HTTPException(status_code=404, detail="Not found")

    try:
        from funnel_client import get_firestore_db, FUNNEL_WORKSPACE
        db = get_firestore_db()
        if db is None:
            raise HTTPException(status_code=404, detail="Not found")
        snap = (db.collection(META_IMAGE_COLLECTION)
                  .document(f"{FUNNEL_WORKSPACE}__{ad_id}").get())
    except HTTPException:
        raise
    except Exception as e:
        print(f"ERROR /api/meta/image/{ad_id}: {type(e).__name__}: {e}", flush=True)
        raise HTTPException(status_code=404, detail="Not found")

    if not snap.exists:
        raise HTTPException(status_code=404, detail="Not found")
    d = snap.to_dict() or {}
    b64 = d.get(field) or d.get("thumb_b64")
    if not b64:
        raise HTTPException(status_code=404, detail="Not found")
    try:
        raw = base64.b64decode(b64)
    except Exception:
        raise HTTPException(status_code=404, detail="Not found")

    return Response(
        content=raw,
        media_type="image/jpeg",
        headers={
            # Private: the whole service sits behind Basic auth and these are a
            # client's ad creatives, not public assets. A day is safe because a
            # mirrored image is immutable for the life of its document — the
            # nightly job only rewrites it when a BETTER source appears.
            "Cache-Control": "private, max-age=86400",
            "X-Image-Source": str(d.get("source") or ""),
        },
    )


@app.get("/api/roas-impact")
def api_roas_impact(_: str = Depends(verify)):
    """ROAS Impact monitor snapshot (written to Firestore by refresh_roas_impact.py)."""
    from funnel_client import get_cache
    data = get_cache().get("roas-impact")
    if data is None:
        return JSONResponse({"error": "no roas-impact snapshot yet"}, status_code=503)
    return data


@app.get("/api/roas-sims")
def api_roas_sims(request: Request, runs: int = 0, account: str = "", token: str = "",
                  _: str = Depends(verify)):
    """ROAS Simulations payload, assembled from the Firestore snapshots that
    refresh_roas_sims.py writes straight off the Google Ads API.

    Serves EXACTLY the shape pipeline/webapp.gs served, so the dashboard's normalize
    path runs unchanged:
      { generatedAt, source, spreadsheet, config, columns, rows, rowCount, runDates,
        truncated, shares: {...}|null, actuals: {...}|null }

    Query params mirror the Apps Script endpoint:
      runs=N    keep only the N most recent run dates (0 = every snapshot retained).
                Without it `shares` still carries the LATEST run date only — that
                asymmetry is deliberate and matches webapp.gs.
      account=  exact Customer Name filter, applied to all three grids.

    The access key is read from the `X-Roas-Sims-Key` HEADER first, falling back to a
    `token=` query param. The header is what the dashboard now sends: Cloud Run logs the
    full request URL, so a key in the query string ends up in Cloud Logging on every page
    load. The query param stays supported because the legacy Apps Script endpoint could
    only ever accept it, and dropping it would break a rollback.

    Always 200, like the Apps Script did: the dashboard distinguishes states by the
    payload's `error` / `status` fields, and a non-2xx would read to it as a transient
    network failure instead of a rejected key."""
    supplied = request.headers.get("X-Roas-Sims-Key") or token
    err = _verify_sims_token(supplied)
    if err is not None:
        return err
    import refresh_roas_sims as rs
    try:
        return rs.build_payload(runs=max(0, min(runs, 400)), account=account)
    except Exception as e:
        # Never a hard failure: the page keeps its cached/demo snapshot and says why.
        # The reason is logged server-side but NOT echoed — the browser-visible string
        # would otherwise carry Firestore paths, project ids and stack detail to anyone
        # who can load the page.
        print(f"ERROR /api/roas-sims: {type(e).__name__}: {e}", flush=True)
        return {"error": "roas-sims snapshot read failed — see the service logs.",
                "status": "read-error",
                "columns": list(rs.RAW_COLUMNS), "rows": [], "rowCount": 0, "runDates": [],
                "truncated": False, "droppedDatasets": [], "shares": None, "actuals": None,
                "config": rs._clean_config(None)}


@app.get("/api/budget")
def api_budget(_: str = Depends(verify)):
    """2026 forecast / budget plan (written to Firestore by budget_source.push_budget,
    which the daily BigQuery job calls). Falls back to the committed budget_2026.json
    so the Budget tab works on a fresh deploy before the first refresh has run."""
    from funnel_client import get_cache
    data = get_cache().get("budget-2026")
    if data is not None:
        return data
    try:
        import budget_source
        return budget_source.load_budget_json()
    except Exception as e:
        return JSONResponse({"error": f"no budget snapshot yet: {e}"}, status_code=503)


@app.get("/api/daily-targets")
def api_daily_targets(month: str = "", _: str = Depends(verify)):
    """Daily target curve for one month, plus that month's daily actuals.

    The budget is monthly; "are we on track today" needs a per-day target. The
    model (daily_targets.py) spreads the monthly target with a day-of-week /
    day-of-month / calendar-event index learned from the export's daily history,
    normalised so the daily targets sum exactly to the monthly target.

    `month` is YYYY-MM and defaults to the current one. The targets are GLOBAL —
    budget_2026.json carries no market/shop/channel split, so this endpoint
    takes no filter arguments (see the Budget view's own note).
    """
    import datetime as _dt
    try:
        if month:
            y, m = (int(x) for x in month.split("-", 1))
            _dt.date(y, m, 1)                       # validates the month
        else:
            t = _dt.date.today()
            y, m = t.year, t.month
    except Exception as e:
        return JSONResponse({"error": f"bad month (expected YYYY-MM): {e}"}, status_code=400)
    try:
        import daily_targets
        return daily_targets.build_payload(y, m)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/breakdown")
def api_breakdown(dataset: str, start: str, end: str, cstart: str, cend: str,
                  market: str = "", shop: str = "", channel: str = "",
                  _: str = Depends(verify)):
    """Per-period breakdown tables computed live from BigQuery for ANY date range +
    comparison window, so the brand/category (product) and market/shop/channel (kv)
    tables reflect the selected period — not a fixed 30-day snapshot.

    start/end = current range, cstart/cend = comparison range (computed client-side
    from the pop / yoy_date / yoy_wday selector).

    market/shop/channel (kv only) scope every table to that AND-combination, so an
    active report filter reaches the breakdowns too instead of leaving them showing
    all markets while the KPI cards show one."""
    import datetime as _dt
    try:
        import bq_source as bs
        cs, ce = _dt.date.fromisoformat(start), _dt.date.fromisoformat(end)
        ps, pe = _dt.date.fromisoformat(cstart), _dt.date.fromisoformat(cend)
    except Exception as e:
        return JSONResponse({"error": f"bad params: {e}"}, status_code=400)
    try:
        if dataset == "product":
            return {
                # LOWER(kv_brand): unify the casing split (revenue on 'kuling',
                # some ad cost on 'Kuling') so each brand is one complete row.
                "brands":     bs._prod_dim("LOWER(kv_brand)", cs, ce, ps, pe),
                "categories": bs._prod_dim("Product_type_2", cs, ce, ps, pe),
            }
        if dataset == "kv":
            f = {"market": market, "shop": shop, "channel": channel}
            return {
                "markets":  bs._kv_dim("market_level_1_kv", cs, ce, ps, pe, filters=f),
                "shops":    bs._kv_dim("shop_new", cs, ce, ps, pe, rev_prev_key="rev_prev", filters=f),
                "channels": bs._kv_dim("Channel_Type_Level_2", cs, ce, ps, pe, filters=f),
            }
        return JSONResponse({"error": "dataset must be 'product' or 'kv'"}, status_code=400)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/filtered")
def api_filtered(start: str, end: str, cstart: str, cend: str,
                 market: str = "", shop: str = "", channel: str = "",
                 series: str = "", sstart: str = "", send: str = "",
                 _: str = Depends(verify)):
    """KV totals for an AND-combination of market/shop/channel filters, computed
    live from BigQuery. Lets the KV Overview cards reflect more than one filter
    at once (e.g. market=SE AND channel=Google) — the single-dimension breakdown
    snapshots can't express a cross-filter, so this queries the combination.

    Optional `series=daily` additionally returns `days` — the filtered daily KV
    series over [sstart, send] (defaults to [start, end]) — so the Daily Trend
    and Weekly Waterfall charts can describe the filter too, instead of staying
    on all-markets totals. Without `series` the response is unchanged."""
    import datetime as _dt
    try:
        import bq_source as bs
        cs, ce = _dt.date.fromisoformat(start), _dt.date.fromisoformat(end)
        ps, pe = _dt.date.fromisoformat(cstart), _dt.date.fromisoformat(cend)
        # Series window — independent of the KPI range (the charts show a fixed
        # ~30-day window), so it is requested separately and falls back to it.
        ss = _dt.date.fromisoformat(sstart) if sstart else cs
        se = _dt.date.fromisoformat(send) if send else ce
    except Exception as e:
        return JSONResponse({"error": f"bad params: {e}"}, status_code=400)
    filters = {"market": market, "shop": shop, "channel": channel}
    try:
        out = bs._kv_filtered(filters, cs, ce, ps, pe)
        if series == "daily":
            out["days"] = bs.filtered_daily(filters, ss, se)
        return out
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/refresh-status")
def api_refresh_status(_: str = Depends(verify)):
    """Surface the most recent refresh run + recent errors so the dashboards
    can show a status pill.

    Reads two Firestore locations written by refresh_funnel.py:
      • refresh_status/latest   — single doc with ok / last_run / last_stage / last_message
      • refresh_errors/*        — per-error docs, doc-id is the UTC timestamp

    Returns a stable shape even when Firestore is unreachable so the pill
    can gracefully fall back to "unknown" instead of breaking the dashboard.
    """
    import time as _t

    try:
        from google.cloud import firestore  # type: ignore
    except Exception as e:
        return JSONResponse(
            {"ok": None, "reason": f"firestore-unavailable: {e!r}"},
            status_code=200,
        )

    try:
        db = firestore.Client()
        latest = db.collection("refresh_status").document("latest").get()
        latest_data = latest.to_dict() if latest.exists else None

        # Pull errors from the last 24 h so an old failure doesn't haunt the pill.
        cutoff = int(_t.time()) - 24 * 3600
        errs_q = (
            db.collection("refresh_errors")
            .where("timestamp", ">=", cutoff)
            .order_by("timestamp", direction=firestore.Query.DESCENDING)
            .limit(20)
        )
        errors = []
        for doc in errs_q.stream():
            d = doc.to_dict() or {}
            errors.append({
                "id":        doc.id,
                "stage":     d.get("stage", ""),
                "error":     d.get("error", ""),
                "timestamp": d.get("timestamp", 0),
            })

        return {
            "ok":            (latest_data or {}).get("ok"),
            "last_run":      (latest_data or {}).get("last_run"),
            "last_stage":    (latest_data or {}).get("last_stage", ""),
            "last_message":  (latest_data or {}).get("last_message", ""),
            "next_due":      (latest_data or {}).get("next_due"),
            "error_count":   len(errors),
            "errors":        errors,
            "now":           int(_t.time()),
        }
    except Exception as e:
        return JSONResponse(
            {"ok": None, "reason": f"firestore-query-failed: {e!r}"},
            status_code=200,
        )


@app.get("/api/inventory")
def api_inventory(_: str = Depends(verify)):
    """Read inventory snapshot from Firestore cache.
    Refreshed hourly by Cloud Scheduler hitting /internal/refresh."""
    from inventory_client import fetch_inventory, InventoryNotConfigured
    try:
        return fetch_inventory()
    except InventoryNotConfigured as e:
        return JSONResponse({"error": str(e)}, status_code=503)


@app.get("/api/inventory-trends")
def api_inventory_trends(_: str = Depends(verify), days: int = 30):
    """Return last N daily snapshots for the trend chart.
    Pads missing days with synthesized values around the most recent
    real snapshot; the response marks each row as real or synthetic."""
    from inventory_client import fetch_inventory_trends
    try:
        return fetch_inventory_trends(days=max(7, min(days, 365)))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ── Internal endpoints (called by Cloud Scheduler) ────────────────────────────
INTERNAL_TOKEN = os.environ.get("INTERNAL_TOKEN")


def _verify_internal(request: Request) -> None:
    """Shared secret auth for Cloud-Scheduler-triggered refresh.

    DEV_MODE does NOT bypass this when INTERNAL_TOKEN is set. The live service runs with
    DEV_MODE=true on an --allow-unauthenticated Cloud Run service, so a blanket bypass
    would leave every /internal/* endpoint open to anyone who guessed the path — including
    the Google Ads collection, which spends metered API quota across eight accounts.
    Presence of the token is the switch; DEV_MODE only covers the case where no token has
    been configured at all (genuine local development)."""
    if not INTERNAL_TOKEN:
        if DEV_MODE:
            return
        raise HTTPException(status_code=500, detail="INTERNAL_TOKEN not configured")
    token = request.headers.get("X-Internal-Token", "")
    if not _const_eq(token, INTERNAL_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid internal token")


@app.post("/internal/refresh")
def internal_refresh(request: Request):
    """Force-refresh all data sources. Called hourly by Cloud Scheduler.
    Returns per-source status — partial failures are logged but the
    job is considered successful as long as at least one source updated."""
    _verify_internal(request)

    results = {}

    # Inventory (Channable XML)
    try:
        from inventory_client import fetch_inventory
        snap = fetch_inventory(force=True)
        results["inventory"] = {
            "status": "ok",
            "skus": snap.get("feed_skus_total"),
            "generated_at": snap.get("generated_at"),
        }
    except Exception as e:
        results["inventory"] = {"status": "error", "error": str(e)}

    # Funnel KV
    try:
        from funnel_client import fetch_kv_overview
        fetch_kv_overview()
        results["kv"] = {"status": "ok"}
    except Exception as e:
        results["kv"] = {"status": "error", "error": str(e)}

    # Funnel Product
    try:
        from funnel_client import fetch_product_overview
        fetch_product_overview()
        results["product"] = {"status": "ok"}
    except Exception as e:
        results["product"] = {"status": "error", "error": str(e)}

    any_ok = any(r.get("status") == "ok" for r in results.values())
    return JSONResponse(results, status_code=200 if any_ok else 502)


@app.post("/internal/refresh-roas-sims")
def internal_refresh_roas_sims(request: Request, run_date: str = ""):
    """Pull Target-ROAS bid simulations, impression shares and measured actuals from the
    Google Ads API and write one daily snapshot to Firestore (roas_sim_snapshots).

    Cloud Scheduler target — daily. Same X-Internal-Token gate as /internal/refresh.

    POST ONLY, deliberately. This endpoint spends metered Google Ads quota across eight
    accounts, and the service is --allow-unauthenticated: a GET route is reachable by any
    crawler, link prefetcher or browser address bar that learns the path. Smoke-test it
    with `curl -X POST` instead.

    `run_date=YYYY-MM-DD` overrides the snapshot's document id (backfill / replay); the
    write is idempotent per day either way, so a re-run replaces that day's rows.

    Degrades loudly, never silently:
      400 when run_date is not a valid YYYY-MM-DD
      409 when another collection already holds the run lock (Scheduler retry overlap)
      503 + the exact env vars still unset when the credentials are missing
      502 when every account failed (the body names each one)
      200 when at least one account returned — per-account errors ride in `accounts`."""
    _verify_internal(request)

    import refresh_roas_sims as rs
    from refresh_roas_sims import MissingCredentials, RefreshInProgress, refresh

    # Validate at the boundary, before anything is collected or written. build_snapshot()
    # checks too, but only after the client is built — and the doc id is derived from this
    # value, so a malformed one would otherwise be caught by prune_snapshots() AFTER the
    # snapshot had already been written under a malformed id.
    if run_date and not rs.RUN_DATE_RE.match(run_date):
        return JSONResponse({"status": "bad-request",
                             "error": "run_date must be YYYY-MM-DD"}, status_code=400)
    try:
        out = refresh(run_date=run_date or None)
    except MissingCredentials as e:
        return JSONResponse({"status": "not-configured", "error": str(e)}, status_code=503)
    except RefreshInProgress as e:
        # Not an error: overlapping Scheduler attempts are expected, and skipping the
        # second one is the whole point of the lock.
        return JSONResponse({"status": "already-running", "error": str(e)}, status_code=409)
    except ValueError as e:
        return JSONResponse({"status": "bad-request", "error": str(e)}, status_code=400)
    except Exception as e:
        print(f"ERROR /internal/refresh-roas-sims: {type(e).__name__}: {e}", flush=True)
        return JSONResponse({"status": "error", "error": f"{type(e).__name__}: {e}"},
                            status_code=500)
    return JSONResponse(out, status_code=200 if out["status"] == "ok" else 502)


@app.post("/internal/refresh-sos")
def internal_refresh_sos(request: Request, run_date: str = ""):
    """Pull Keyword Planner monthly search volumes for the curated Share of Search
    keyword set (one call per market) and merge them into sos_history.

    Cloud Scheduler target — monthly on the 15th, when Google has finalised the
    previous month's volumes. Same X-Internal-Token gate and POST-only reasoning as
    /internal/refresh-roas-sims (metered quota + --allow-unauthenticated service).

    Degrades loudly, never silently:
      400 when run_date is not a valid YYYY-MM-DD
      409 when another collection already holds the run lock (Scheduler retry overlap)
      503 + the exact env vars still unset when the credentials are missing
      502 when every market failed (the body names each one)
      200 when at least one market was collected and merged"""
    _verify_internal(request)

    import refresh_share_of_search as sos

    if run_date and not sos.RUN_DATE_RE.match(run_date):
        return JSONResponse({"status": "bad-request",
                             "error": "run_date must be YYYY-MM-DD"}, status_code=400)
    try:
        out = sos.refresh(run_date=run_date or None)
    except sos.MissingCredentials as e:
        return JSONResponse({"status": "not-configured", "error": str(e)}, status_code=503)
    except sos.RefreshInProgress as e:
        return JSONResponse({"status": "already-running", "error": str(e)}, status_code=409)
    except ValueError as e:
        return JSONResponse({"status": "bad-request", "error": str(e)}, status_code=400)
    except Exception as e:
        print(f"ERROR /internal/refresh-sos: {type(e).__name__}: {e}", flush=True)
        return JSONResponse({"status": "error", "error": f"{type(e).__name__}: {e}"},
                            status_code=500)
    return JSONResponse(out, status_code=200 if out["status"] == "ok" else 502)
