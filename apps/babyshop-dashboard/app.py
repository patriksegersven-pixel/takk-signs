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
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
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

    user_ok = secrets.compare_digest(credentials.username, DASH_USER)
    pass_ok = secrets.compare_digest(credentials.password, DASH_PASS)
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": 'Basic realm="Babyshop Dashboard"'},
        )
    return credentials.username


# ── Static dashboard routes ──────────────────────────────────────────────────
STATIC_DIR     = Path(__file__).parent
KV_HTML        = STATIC_DIR / "babyshop-dashboard.html"
PRODUCT_HTML   = STATIC_DIR / "babyshop-product-dashboard.html"
INVENTORY_HTML = STATIC_DIR / "babyshop-inventory-dashboard.html"
STOY_HTML      = STATIC_DIR / "babyshop-stoy-dashboard.html"
ROAS_HTML      = STATIC_DIR / "babyshop-roas-impact.html"
SIM_HTML       = STATIC_DIR / "babyshop-roas-simulations.html"


@app.get("/")
def root(_: str = Depends(verify)):
    return RedirectResponse(url="/babyshop-dashboard.html", status_code=302)


@app.get("/babyshop-dashboard.html")
def kv_dashboard(_: str = Depends(verify)):
    return FileResponse(KV_HTML, media_type="text/html")


@app.get("/babyshop-product-dashboard.html")
def product_dashboard(_: str = Depends(verify)):
    return FileResponse(PRODUCT_HTML, media_type="text/html")


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


# ── Health (no auth — for Cloud Run probes) ──────────────────────────────────
@app.get("/healthz", include_in_schema=False)
def healthz():
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


@app.get("/api/roas-impact")
def api_roas_impact(_: str = Depends(verify)):
    """ROAS Impact monitor snapshot (written to Firestore by refresh_roas_impact.py)."""
    from funnel_client import get_cache
    data = get_cache().get("roas-impact")
    if data is None:
        return JSONResponse({"error": "no roas-impact snapshot yet"}, status_code=503)
    return data


@app.get("/api/campaign-actuals")
def api_campaign_actuals(_: str = Depends(verify)):
    """Daily per-campaign Google actuals — cost + KV GP2 (written to Firestore by
    bq_source.build_campaign_actuals). Feeds the ROAS Simulations value-calibration
    factor and backtest."""
    from funnel_client import get_cache
    data = get_cache().get("campaign-actuals")
    if data is None:
        return JSONResponse({"error": "no campaign-actuals snapshot yet"}, status_code=503)
    return data


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


@app.get("/api/breakdown")
def api_breakdown(dataset: str, start: str, end: str, cstart: str, cend: str,
                  _: str = Depends(verify)):
    """Per-period breakdown tables computed live from BigQuery for ANY date range +
    comparison window, so the brand/category (product) and market/shop/channel (kv)
    tables reflect the selected period — not a fixed 30-day snapshot.

    start/end = current range, cstart/cend = comparison range (computed client-side
    from the pop / yoy_date / yoy_wday selector)."""
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
            return {
                "markets":  bs._kv_dim("market_level_1_kv", cs, ce, ps, pe),
                "shops":    bs._kv_dim("shop_new", cs, ce, ps, pe, rev_prev_key="rev_prev"),
                "channels": bs._kv_dim("Channel_Type_Level_2", cs, ce, ps, pe),
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
    """Shared secret auth for Cloud-Scheduler-triggered refresh."""
    if DEV_MODE:
        return
    if not INTERNAL_TOKEN:
        raise HTTPException(status_code=500, detail="INTERNAL_TOKEN not configured")
    token = request.headers.get("X-Internal-Token", "")
    if not secrets.compare_digest(token, INTERNAL_TOKEN):
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
