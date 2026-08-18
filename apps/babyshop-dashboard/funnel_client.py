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
        try:
            self._db = firestore.Client()
        except Exception:
            # ADC in production (Cloud Run SA); gcloud user token as a local
            # fallback — same pattern as bq_source._credentials(). The fallback
            # refreshes: a bare token lasts about an hour, and this cache client
            # is held for the life of the process, so a long local run would
            # otherwise die with a RefreshError partway through. See
            # voyado_sync._credentials for the full write-up.
            import datetime
            import subprocess
            import google.oauth2.credentials  # type: ignore

            class _GcloudToken(google.oauth2.credentials.Credentials):
                def refresh(self, request):  # noqa: ARG002 - signature fixed by google-auth
                    self.token = subprocess.check_output(
                        ["gcloud", "auth", "print-access-token"]).decode().strip()
                    # google-auth compares expiry against a NAIVE utcnow().
                    self.expiry = (datetime.datetime.utcnow()
                                   + datetime.timedelta(minutes=45))

            tok = subprocess.check_output(
                ["gcloud", "auth", "print-access-token"]).decode().strip()
            creds = _GcloudToken(tok)
            # Must be set here too: expiry=None reads as "never expires", so
            # google-auth would never call refresh() at all.
            creds.expiry = datetime.datetime.utcnow() + datetime.timedelta(minutes=45)
            self._db = firestore.Client(
                project=os.environ.get("FIRESTORE_PROJECT",
                                       "project-a7ade44e-e7e3-4871-a83"),
                credentials=creds)
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
    return _with_seasons(_cached("product-overview", _fetch_product_overview_uncached))


# ── Season (Norce "Product Collection") overlay ──────────────────────────────
# The products tab's category/brand tables come from the Funnel→BigQuery export
# (written to funnel_cache/<ws>__product-overview by bq_source.py). That export
# has no SKU column, so it cannot carry a season of its own. The season lives in
# Norce, is synced to BigQuery by norce_sync.py and snapshotted to
# funnel_cache/<ws>__product-seasons by refresh_product_seasons.py.
#
# Merging happens HERE, on the read path, rather than in either producer:
#   • bq_source.py owns the Funnel side and knows nothing about Norce
#   • refresh_product_seasons.py owns the Norce side and must not rewrite a
#     document another job owns — two writers on one doc is how you get a race
#   • the merge is two dict lookups over ~275 rows, in front of a cached read,
#     so it costs nothing per request. NO Norce or BigQuery call is made here.
#
# A missing/stale seasons document is a no-op: rows come back exactly as
# bq_source wrote them and the tab renders "—" in the Season column.
SEASONS_CACHE_KEY = "product-seasons"


def _season_index(payload: dict, dim: str) -> dict:
    """{lowercased name -> season record} for 'brands' or 'categories'.

    Keys are re-normalised on read: the snapshot writes them lowercased and
    trimmed already, but the Funnel row names are normalised the same way here
    so one side drifting cannot silently break every lookup.
    """
    src = (payload or {}).get(dim) or {}
    if not isinstance(src, dict):
        return {}
    return {str(k).strip().lower(): v for k, v in src.items() if isinstance(v, dict)}


def _with_seasons(overview: dict) -> dict:
    """Attach `season` (+ share/coverage/mix) to each category and brand row.

    Never raises and never mutates the cached document: the rows are shallow-
    copied, so a merge failure can only ever mean "no Season column", never a
    corrupted product-overview cache entry.
    """
    if not isinstance(overview, dict):
        return overview
    try:
        seasons = get_cache().get(SEASONS_CACHE_KEY)
    except Exception as e:
        print(f"Funnel: season overlay skipped — cache read failed: {e!r}")
        return overview
    if not isinstance(seasons, dict):
        return overview

    out = dict(overview)
    matched = 0
    for rows_key, dim in (("categories", "categories"), ("brands", "brands")):
        rows = overview.get(rows_key)
        if not isinstance(rows, list):
            continue
        idx = _season_index(seasons, dim)
        merged = []
        for r in rows:
            if not isinstance(r, dict):
                merged.append(r)
                continue
            r = dict(r)
            s = idx.get(str(r.get("name", "")).strip().lower())
            if s:
                matched += 1
                r["season"] = s.get("season")
                r["season_share"] = s.get("share")
                r["season_coverage"] = s.get("coverage")
                r["season_mix"] = s.get("mix")
            merged.append(r)
        out[rows_key] = merged

    # Provenance for the tab's tooltip plus the raw by-name lookups. Kept as its
    # own key so the existing payload contract is strictly additive.
    #
    # The lookups are deliberately redundant with the per-row `season` above:
    # the products tab REPLACES its brand/category arrays whenever the period
    # selector changes, from /api/breakdown, whose rows come straight out of
    # BigQuery and carry no season. Shipping the maps lets the page re-attach
    # the label on every render instead of blanking the column the first time
    # somebody picks 90D. ~275 small records, a few tens of KB.
    out["seasons"] = {
        "window":       seasons.get("window"),
        "generated_at": seasons.get("generated_at"),
        "source":       seasons.get("source"),
        "vocabulary":   seasons.get("seasons"),
        "coverage":     seasons.get("coverage"),
        "notes":        seasons.get("notes"),
        "brands":       seasons.get("brands") or {},
        "categories":   seasons.get("categories") or {},
        "rows_matched": matched,
    }
    return out


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
