"""
Funnel.io API client — server-side data fetcher for the GCP-hosted dashboard.

Auth: OAuth 2.0 refresh-token flow.
  • Register an OAuth application in your Funnel workspace
    (Workspace settings → Integrations → API/OAuth → Create application).
    You'll get a `client_id` and `client_secret`.
  • Run `bootstrap_oauth.py` once locally to capture a long-lived
    `refresh_token` for your account.
  • Store all three in Google Secret Manager and inject as env vars on
    Cloud Run (see DEPLOY.md):
        FUNNEL_CLIENT_ID
        FUNNEL_CLIENT_SECRET
        FUNNEL_REFRESH_TOKEN

Cache: Every Funnel response is cached in **Firestore** so:
  • Cold-start Cloud Run instances see warm data immediately
  • All instances share one cache (no per-instance redundancy)
  • Funnel sees roughly one call per (cache key × TTL window), not one
    per user request

When Firestore isn't reachable (e.g. local dev with no GCP creds), the
module silently falls back to an in-process dict so the app still works.
Set DISABLE_FIRESTORE_CACHE=1 to force the in-process path.

This module is NOT yet wired into the dashboard HTML — the dashboards still
render from their baked-in JS constants. Implement the TODOs in
`_fetch_kv_overview_uncached` / `_fetch_product_overview_uncached` to flip
the switch.

Reference: https://funnel.io/help/integrations/api
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any, Optional

import httpx


# ── Config ───────────────────────────────────────────────────────────────────
FUNNEL_CLIENT_ID     = os.environ.get("FUNNEL_CLIENT_ID")
FUNNEL_CLIENT_SECRET = os.environ.get("FUNNEL_CLIENT_SECRET")
FUNNEL_REFRESH_TOKEN = os.environ.get("FUNNEL_REFRESH_TOKEN")
FUNNEL_WORKSPACE     = os.environ.get("FUNNEL_WORKSPACE", "-Ln87GcdqU9CMJV6zMBY")

# Endpoint defaults — override via env var if Funnel changes them.
FUNNEL_BASE_URL  = os.environ.get("FUNNEL_BASE_URL",  "https://api.funnel.io")
FUNNEL_TOKEN_URL = os.environ.get("FUNNEL_TOKEN_URL", "https://api.funnel.io/oauth/token")

# Default cache TTL (per cache key). Override per-call with the `ttl` arg
# to `_cached()`.
CACHE_TTL_SECONDS = int(os.environ.get("CACHE_TTL_SECONDS", "1800"))   # 30 min

# Firestore collection where cache entries live.
FIRESTORE_COLLECTION = os.environ.get("FIRESTORE_COLLECTION", "funnel_cache")

# Token cache: (access_token, expires_at_epoch). Protected by a lock so
# concurrent requests don't trigger multiple refresh round-trips.
_token_lock = threading.Lock()
_token: Optional[tuple[str, float]] = None


# ── Exceptions ───────────────────────────────────────────────────────────────
class FunnelNotConfigured(Exception):
    """Missing OAuth credentials — /api/* routes are unusable until set."""


class FunnelAuthError(Exception):
    """Token exchange failed — refresh_token is likely expired or revoked."""


# ── OAuth ────────────────────────────────────────────────────────────────────
def _require_oauth() -> None:
    missing = [
        name for name, val in [
            ("FUNNEL_CLIENT_ID",     FUNNEL_CLIENT_ID),
            ("FUNNEL_CLIENT_SECRET", FUNNEL_CLIENT_SECRET),
            ("FUNNEL_REFRESH_TOKEN", FUNNEL_REFRESH_TOKEN),
        ] if not val
    ]
    if missing:
        raise FunnelNotConfigured(
            "OAuth not configured. Missing env var(s): "
            + ", ".join(missing)
            + ". Bootstrap with bootstrap_oauth.py then store in Secret Manager."
        )


def _fetch_access_token() -> tuple[str, float]:
    """Exchange the refresh token for a fresh access token."""
    _require_oauth()
    with httpx.Client(timeout=15.0) as c:
        r = c.post(
            FUNNEL_TOKEN_URL,
            data={
                "grant_type":    "refresh_token",
                "refresh_token": FUNNEL_REFRESH_TOKEN,
                "client_id":     FUNNEL_CLIENT_ID,
                "client_secret": FUNNEL_CLIENT_SECRET,
            },
            headers={"Accept": "application/json"},
        )
    if r.status_code != 200:
        raise FunnelAuthError(
            f"Token exchange failed: {r.status_code} {r.text[:200]}"
        )
    body = r.json()
    access_token = body["access_token"]
    # Refresh ~60 s before actual expiry to avoid mid-request expiry.
    expires_in   = int(body.get("expires_in", 3600))
    expires_at   = time.time() + max(expires_in - 60, 60)
    return access_token, expires_at


def _access_token() -> str:
    """Return a valid access token, refreshing if needed."""
    global _token
    with _token_lock:
        if _token is None or _token[1] <= time.time():
            _token = _fetch_access_token()
        return _token[0]


# ── HTTP client ──────────────────────────────────────────────────────────────
def _client() -> httpx.Client:
    return httpx.Client(
        base_url=FUNNEL_BASE_URL,
        headers={
            "Authorization": f"Bearer {_access_token()}",
            "Accept":        "application/json",
        },
        timeout=30.0,
    )


# ── Cache backends ───────────────────────────────────────────────────────────
class _Cache:
    """Minimal cache interface."""
    name = "abstract"

    def get(self, key: str) -> Optional[dict]:
        raise NotImplementedError

    def set(self, key: str, value: dict, ttl_seconds: int) -> None:
        raise NotImplementedError


class _InProcessCache(_Cache):
    """Per-instance dict cache. Used for local dev or as a Firestore fallback."""
    name = "in-process"

    def __init__(self) -> None:
        self._store: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[dict]:
        with self._lock:
            entry = self._store.get(key)
            if not entry:
                return None
            expires_at, data = entry
            if time.time() > expires_at:
                self._store.pop(key, None)
                return None
            return data

    def set(self, key: str, value: dict, ttl_seconds: int) -> None:
        with self._lock:
            self._store[key] = (time.time() + ttl_seconds, value)


class _FirestoreCache(_Cache):
    """Shared cache backed by Firestore Native.

    Document layout:
        funnel_cache/{key}:
            data:         dict (the response payload)
            fetched_at:   server timestamp (Firestore-native)
            expires_at:   epoch seconds (we set this so reads need no math
                          against a possibly-skewed local clock)
            ttl_seconds:  echo of the TTL used (for ops visibility)
            workspace:    FUNNEL_WORKSPACE (so a workspace switch invalidates
                          stale entries naturally — different key)
    """
    name = "firestore"

    def __init__(self) -> None:
        # Imported lazily so local dev without google-cloud-firestore still works
        from google.cloud import firestore  # type: ignore

        self._firestore_module = firestore
        self._db = firestore.Client()
        self._collection = self._db.collection(FIRESTORE_COLLECTION)

    def _doc_id(self, key: str) -> str:
        # Workspace baked into the doc id keeps caches per-workspace
        return f"{FUNNEL_WORKSPACE}__{key}"

    def get(self, key: str) -> Optional[dict]:
        snap = self._collection.document(self._doc_id(key)).get()
        if not snap.exists:
            return None
        d = snap.to_dict() or {}
        expires_at = d.get("expires_at", 0)
        if time.time() > float(expires_at):
            return None
        return d.get("data")

    def set(self, key: str, value: dict, ttl_seconds: int) -> None:
        self._collection.document(self._doc_id(key)).set({
            "data":        value,
            "fetched_at":  self._firestore_module.SERVER_TIMESTAMP,
            "expires_at":  time.time() + ttl_seconds,
            "ttl_seconds": ttl_seconds,
            "workspace":   FUNNEL_WORKSPACE,
        })


_cache_singleton: Optional[_Cache] = None
_cache_lock = threading.Lock()


def get_cache() -> _Cache:
    """Return a process-wide cache singleton. Prefers Firestore, falls back."""
    global _cache_singleton
    with _cache_lock:
        if _cache_singleton is not None:
            return _cache_singleton

        if os.environ.get("DISABLE_FIRESTORE_CACHE"):
            _cache_singleton = _InProcessCache()
            print("Funnel cache: in-process (DISABLE_FIRESTORE_CACHE=1)")
            return _cache_singleton

        try:
            _cache_singleton = _FirestoreCache()
            print(f"Funnel cache: Firestore (collection={FIRESTORE_COLLECTION})")
        except Exception as e:
            # Don't fail boot — local dev without GCP creds should still work.
            print(f"Funnel cache: Firestore unavailable ({e!r}); using in-process")
            _cache_singleton = _InProcessCache()
        return _cache_singleton


def _cached(key: str, fn, ttl: int = CACHE_TTL_SECONDS):
    """Read-through cache: check, fetch on miss, store on success."""
    cache = get_cache()
    existing = cache.get(key)
    if existing is not None:
        return existing
    value = fn()
    try:
        cache.set(key, value, ttl)
    except Exception as e:
        # A cache write failure must never break the request — log and serve.
        print(f"Funnel cache: write failed for {key!r}: {e!r}")
    return value


# ── Field IDs (from prior Funnel exploration) ────────────────────────────────
# Cross-platform measures
M_REVENUE      = "cf1ijsm2qp2r9v3_kv_revenue"
M_GP2          = "cf1ijsmd6ra2jmj_kv_gp2"
M_GP3          = "cf1ijsubsqmhepf_kv_gp3"
M_TRANSACTIONS = "cf1ijsmm7d814r4_kv_transactions"
M_COGS         = "cf1ijsm5jefks7q_kv_cogs"
M_COMMON_COST  = "common-cost"

# Cross-platform dimensions
D_DATE      = "date"
D_MARKET_KV = "cf1j1qh36fictd7_market_level_1_kv"
D_SHOP_NEW  = "dim-1i7g88h71q5jq_shop_new"
D_CHANNEL_2 = "cf1i7dnbhv3imth_Google__Bing_New"

# Product measures
M_PROD_REVENUE   = "cf1j2rvf6cstma8_kv_revenue_new"
M_PROD_COGS      = "cf1j4pbcoib0bom_kv_product_cogs"
M_PROD_GP1       = "cf1j4pb7q0974md_kv_gp1_product"
M_PROD_GP2       = "cf1j4pbasj1d5lc_kv_gp2_product"
M_GP3_SHOPPING   = "cf1j4pau1ki6bas_kv_gp3_shopping"
D_PRODUCT_TYPE_2 = "cf1j4pcmic8ucum_Product_type_2"


# ── Public API ───────────────────────────────────────────────────────────────
def fetch_kv_overview() -> dict:
    """Return the dataset the KV Overview dashboard needs.

    Shape mirrors the inline JS constants in babyshop-dashboard.html so you
    can swap to live data with minimal HTML changes.
    """
    return _cached("kv-overview", _fetch_kv_overview_uncached)


def fetch_product_overview() -> dict:
    return _cached("product-overview", _fetch_product_overview_uncached)


# ── Implementation stubs ─────────────────────────────────────────────────────
def _fetch_kv_overview_uncached() -> dict:
    """
    TODO: Call Funnel's data-export API and aggregate into:
      - totals (current + prior period)
      - by date (daily trend)
      - by market   (dimension: D_MARKET_KV)
      - by shop     (dimension: D_SHOP_NEW)
      - by channel  (dimension: D_CHANNEL_2)

    Use `_client()` which already injects a valid Bearer token, e.g.:

        with _client() as c:
            r = c.post("/data-export/v1/queries", json={
                "workspace_id": FUNNEL_WORKSPACE,
                "dimensions":   [D_DATE, D_MARKET_KV, D_SHOP_NEW, D_CHANNEL_2],
                "measures":     [M_REVENUE, M_GP2, M_GP3, M_TRANSACTIONS, M_COMMON_COST],
                "currency":     "SEK",
                "date_start":   "...",
                "date_end":     "...",
            })
            rows = r.json()["rows"]

    Then aggregate the rows in-process and return the dashboard shape.
    """
    _require_oauth()
    raise NotImplementedError("Wire up Funnel KV overview fetch.")


def _fetch_product_overview_uncached() -> dict:
    """TODO: Same pattern with product fields:
        measures:   [M_PROD_REVENUE, M_PROD_COGS, M_PROD_GP1, M_PROD_GP2, M_GP3_SHOPPING]
        dimensions: [D_PRODUCT_TYPE_2, D_MARKET_KV, D_SHOP_NEW]
    """
    _require_oauth()
    raise NotImplementedError("Wire up Funnel product overview fetch.")
