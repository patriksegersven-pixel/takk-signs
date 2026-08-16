#!/usr/bin/env python3
"""
Norce Commerce (OData v4 Query API) → BigQuery extraction for Customer Insights.

Lands the raw order history in `project-a7ade44e-e7e3-4871-a83.norce` (EU, so it
can be joined against the Funnel export, which is EU too) and then applies the
mart SQL in `norce_marts.sql`. `refresh_customer_insights.py` reads the
marts; nothing else in the app touches this dataset.

Mirrors refresh_roas_sims.py: module-level config, a collect step, an explicit
MissingCredentials error naming the env vars that are still unset, and a
`__main__` block that runs locally. Auth/BigQuery client wiring follows
bq_source.py (ADC in production, `gcloud auth print-access-token` locally).

WHY THIS EXISTS
  The Funnel export knows spend and channel but has no customer identity — its
  new/old customer columns are a pre-aggregated file import. Norce has the order
  history, so cohorts, repeat rate and 1-year CLV can only be built here.

IDENTITY AND PII (read this before changing anything)
  Norce has NO stable customer id on orders: BuyerCustomerId is null on 100% of
  them. The only identity is `Buyer.EmailAddress` (100% populated), so the
  customer key is

      customer_hash = SHA256(LOWER(TRIM(email)))   hex, computed AT EXTRACT TIME

  The raw email NEVER reaches BigQuery, a log line or a local file — it exists
  only inside _order_row() for the length of one hash call. The request itself
  asks for the minimum: `$expand=Buyer($select=EmailAddress,CountryId,ZipCode)`,
  so no name, phone or street address is even transferred (validated live —
  nested $select inside $expand works on this tenant).

  `IsForgotten` is Norce's GDPR erasure flag. Every run re-reads the forgotten
  orders and DELETEs their rows from `orders`/`order_items`, so an erasure
  propagates on the next sync rather than lingering in the warehouse.

DATA-MODEL FACTS (audited against the live prod tenant, 2026-08-16)
  • History starts 2025-06-11 (platform cutover). ~416k orders / ~1.43M lines.
  • Orders are served PER APPLICATION (market × shop). See APPLICATIONS.
  • There is NO order-total field. Order value = SUM(Items.LineAmount) ex-VAT,
    EXCLUDING the shipping line PartNo='1000014'. The header VatRate=0 is an
    artifact; real VAT sits per line.
  • OrderItems and ProductCategories are reachable ONLY via $expand from their
    parent set — the standalone sets 404.
  • Reporting/OrderSummaryByMonth and Core/KpiOrders both HTTP 500. Everything
    is built from raw Orders.
  • Statuses: 4 = completed (the bulk), 2 = in flight, 5 = likely cancel/return.
    NO status filtering anywhere — user decision: returns/cancellations are
    ignored and all values are gross.
  • Application/* and Core/* lookups have no ApplicationId property; they are
    tenant-wide and must NOT be filtered per application.

CREDENTIALS (env vars, wired from Secret Manager — see pipeline/CUSTOMER-INSIGHTS.md)
  NORCE_CLIENT_ID       Norce Admin → Settings → Users → OAUTH
  NORCE_CLIENT_SECRET

  Neither secret exists yet. Without them this job prints exactly which env vars
  are missing and exits 0 WITHOUT creating the dataset — a scheduled run must not
  page anyone, and refresh_customer_insights.py already serves `"norce": null`
  when the dataset is absent.

Run locally:
    python3 norce_sync.py                    # incremental (since the watermark)
    python3 norce_sync.py --backfill         # full history from 2025-06-11
    python3 norce_sync.py --backfill --from 2026-01-01
    python3 norce_sync.py --marts-only       # re-apply norce_marts.sql
    python3 norce_sync.py --apps babyshop-se,babyshop-no
"""
from __future__ import annotations
import argparse, datetime, hashlib, os, sys, time
from typing import Any, Iterator

import httpx
from google.cloud import bigquery

# ── Norce API ────────────────────────────────────────────────────────────────
# Prod has no environment segment in the host, and `scope` is the environment
# name (not a per-API permission) — API access comes from the resources enabled
# on the integration user. Both documented at docs.norce.io.
NORCE_BASE_URL  = os.environ.get("NORCE_BASE_URL", "https://babyshop.api-se.norce.tech")
NORCE_TOKEN_URL = os.environ.get("NORCE_TOKEN_URL", f"{NORCE_BASE_URL}/identity/1.0/connect/token")
NORCE_QUERY_URL = os.environ.get("NORCE_QUERY_URL", f"{NORCE_BASE_URL}/commerce/query/2.0")
NORCE_SCOPE     = os.environ.get("NORCE_SCOPE", "prod")

NORCE_CLIENT_ID     = os.environ.get("NORCE_CLIENT_ID")
NORCE_CLIENT_SECRET = os.environ.get("NORCE_CLIENT_SECRET")

# Server-side page cap is 500; asking for more is silently clamped.
PAGE_SIZE   = int(os.environ.get("NORCE_PAGE_SIZE", "500"))
HTTP_TIMEOUT = float(os.environ.get("NORCE_HTTP_TIMEOUT", "120"))
# Token endpoint allows 3 req/min per IP (burst 20) — one token per run is plenty
# at a 1 h lifetime, but refresh 60 s early so a long backfill never 401s mid-page.
TOKEN_SKEW_S = 60

# Applications = market × shop. Orders are served per application; the id is both
# the `applicationId` header and an explicit $filter (belt and braces: the header
# alone does not restrict Orders).
APPLICATIONS: dict[str, int] = {
    "babyshop-se": 1244, "babyshop-no": 1264, "babyshop-fi": 1265, "babyshop-dk": 1266,
    "babyshop-eu": 1267, "babyshop-na": 1268, "babyshop-uk": 1269, "babyshop-row": 1270,
    "babyshop-asia": 1271,
    "lekmer-se": 1272, "lekmer-no": 1273, "lekmer-fi": 1274, "lekmer-dk": 1275,
}

# Tenant-wide queries (Products/*, Application/*, Core/*) still need an
# applicationId CONTEXT header on this tenant: without one, Products/Products
# 500s on every request (three backfills died on this before the pattern was
# clear — orders always send the header and never failed). The header sets
# context only; it does not filter these client-wide sets.
CONTEXT_APP_ID = int(os.environ.get("NORCE_CONTEXT_APP_ID", "1244"))  # babyshop-se

# Platform cutover — nothing exists before this date, so a backfill starts here.
HISTORY_START = os.environ.get("NORCE_HISTORY_START", "2025-06-11")
# Shipping is booked as a normal order line; it is NOT merchandise revenue.
# The exclusion itself lives in norce_marts.sql (the literal is hard-coded
# there too) — this constant exists so a grep for the PartNo finds both halves.
SHIPPING_PART_NO = "1000014"

# ── BigQuery ─────────────────────────────────────────────────────────────────
BQ_PROJECT  = os.environ.get("NORCE_BQ_PROJECT", "project-a7ade44e-e7e3-4871-a83")
BQ_DATASET  = os.environ.get("NORCE_BQ_DATASET", "norce")
# EU (multi-region), matching babyshop-funnel-data.bs_funnel_export so the marts
# can be joined to the Funnel export without a cross-region copy.
BQ_LOCATION = os.environ.get("NORCE_BQ_LOCATION", "EU")

# Lives at the app root, NOT under pipeline/ — .dockerignore excludes `pipeline/`,
# so anything in there is missing from the container image at runtime.
MARTS_SQL = os.path.join(os.path.dirname(os.path.abspath(__file__)), "norce_marts.sql")

# Re-read a safety lap of already-seen updates on every incremental run. Norce
# writes `Updated` from its own clock and orders land out of order under load, so
# a watermark taken at exactly max(Updated) would skip the stragglers.
WATERMARK_LAP = datetime.timedelta(hours=int(os.environ.get("NORCE_WATERMARK_LAP_H", "6")))


class MissingCredentials(RuntimeError):
    """No Norce OAuth credentials in the environment — the sync cannot run."""


def missing_credentials() -> list[str]:
    return [n for n, v in (("NORCE_CLIENT_ID", NORCE_CLIENT_ID),
                           ("NORCE_CLIENT_SECRET", NORCE_CLIENT_SECRET)) if not v]


# ════════════════════════════════════════════════════════════════════════════
#  Norce client
# ════════════════════════════════════════════════════════════════════════════
_token: tuple[str, float] | None = None


def _access_token() -> str:
    """Client-credentials token, cached for its lifetime (docs: 3600 s)."""
    global _token
    if _token and _token[1] > time.time():
        return _token[0]
    missing = missing_credentials()
    if missing:
        raise MissingCredentials(
            "Norce OAuth not configured. Missing env var(s): " + ", ".join(missing)
            + ". Create them in Secret Manager and wire them onto the job — see "
              "pipeline/CUSTOMER-INSIGHTS.md.")
    with httpx.Client(timeout=30.0) as c:
        r = c.post(NORCE_TOKEN_URL,
                   data={"grant_type": "client_credentials",
                         "client_id": NORCE_CLIENT_ID,
                         "client_secret": NORCE_CLIENT_SECRET,
                         "scope": NORCE_SCOPE},
                   headers={"Content-Type": "application/x-www-form-urlencoded",
                            "Accept": "application/json"})
    if r.status_code != 200:
        raise RuntimeError(f"Norce token exchange failed: {r.status_code} {r.text[:200]}")
    body = r.json()
    _token = (body["access_token"], time.time() + max(int(body.get("expires_in", 3600)) - TOKEN_SKEW_S, 60))
    return _token[0]


def _get(url: str, params: dict[str, Any] | None, app_id: int | None) -> dict:
    """One GET with auth + the applicationId header, retrying 429/5xx."""
    headers = {"Authorization": f"Bearer {_access_token()}", "Accept": "application/json"}
    if app_id is not None:
        # Documented as `applicationId` (or `application-id`); it sets the
        # application CONTEXT. It does not filter Orders — the $filter does.
        headers["applicationId"] = str(app_id)
    delay = 2.0
    for attempt in range(6):
        with httpx.Client(timeout=HTTP_TIMEOUT) as c:
            r = c.get(url, params=params, headers=headers)
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 500, 502, 503, 504) and attempt < 5:
            # 429 is the documented rate-limit code; the 5xx set covers the
            # transient gateway errors seen during the audit.
            time.sleep(delay)
            delay *= 2
            headers["Authorization"] = f"Bearer {_access_token()}"
            continue
        raise RuntimeError(f"Norce {r.status_code} on {url}: {r.text[:300]}")
    raise RuntimeError(f"Norce request gave up after retries: {url}")


def query(entity_path: str, app_id: int | None = None,
          page_size: int = PAGE_SIZE, **opts: Any) -> Iterator[dict]:
    """Page through an OData entity set, yielding rows.

    `entity_path` is Namespace/EntitySet, e.g. "Orders/Orders". `opts` are OData
    options without the `$` (select/expand/filter/orderby). Paging follows
    @odata.nextLink when the server sends one and falls back to $skip — the
    tenant honours both, but only nextLink is guaranteed stable across versions.
    `page_size` exists because $top=500 with a double $expand makes the tenant
    500 on Products/Products; heavy-expand callers pass something smaller.
    """
    params = {f"${k}": v for k, v in opts.items() if v is not None}
    params["$top"] = page_size
    url, seen = f"{NORCE_QUERY_URL}/{entity_path}", 0
    while True:
        body = _get(url, params, app_id)
        rows = body.get("value") or []
        for row in rows:
            yield row
        seen += len(rows)
        nxt = body.get("@odata.nextLink")
        if nxt:
            url, params = nxt, None       # nextLink already carries every option
            continue
        if len(rows) < page_size:
            return
        # No nextLink but a full page — fall back to $skip. It must be the
        # CUMULATIVE count, not this page's size: a run that started on
        # nextLink and lost it mid-stream would otherwise rewind to page two.
        url = f"{NORCE_QUERY_URL}/{entity_path}"
        params = {f"${k}": v for k, v in opts.items() if v is not None}
        params["$top"], params["$skip"] = page_size, seen


def _odata_ts(d: datetime.datetime | datetime.date) -> str:
    """OData v4 datetime literal — unquoted, always UTC."""
    if isinstance(d, datetime.datetime):
        return d.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{d.isoformat()}T00:00:00Z"


# ════════════════════════════════════════════════════════════════════════════
#  Row shaping — the only place raw PII is ever touched
# ════════════════════════════════════════════════════════════════════════════
def customer_hash(email: str | None) -> str | None:
    """SHA256 of the lower/trimmed email, hex. Returns None for a blank email.

    Lower+trim before hashing so the same person always lands on one hash; the
    same normalisation is baked into the marts' documentation so nobody
    re-derives it differently later.
    """
    if not email:
        return None
    e = email.strip().lower()
    return hashlib.sha256(e.encode("utf-8")).hexdigest() if e else None


# Only these order fields are transferred. Note what is NOT here: every
# Delivery* name/address field except the zip, and the whole Payer/ShipTo tree.
ORDER_SELECT = ("Id,ApplicationId,OrderNo,OrderDate,Created,Updated,StatusId,CurrencyId,"
                "FreightCost,OrderFee,CashDiscount,LineDiscount,PaymentMethodId,"
                "DeliveryCountryId,DeliveryZipCode,IsForgotten")
ORDER_EXPAND = ("Items($select=Id,OrderId,PartNo,ProductName,QtyOrdered,PriceSale,"
                "DiscountAmount,LineAmount,VatRate,StatusId),"
                "Buyer($select=EmailAddress,CountryId,ZipCode)")


def _f(v: Any) -> float:
    return float(v) if v is not None else 0.0


def _order_row(o: dict, app_key: str) -> dict:
    """Norce order → the `orders` BigQuery row. Hashes the email and drops the rest."""
    buyer = o.get("Buyer") or {}
    return {
        "Id": o["Id"], "ApplicationId": o.get("ApplicationId"), "app_key": app_key,
        "OrderNo": o.get("OrderNo"), "OrderDate": o.get("OrderDate"),
        "Created": o.get("Created"), "Updated": o.get("Updated") or o.get("Created"),
        "StatusId": o.get("StatusId"), "CurrencyId": o.get("CurrencyId"),
        "FreightCost": _f(o.get("FreightCost")), "OrderFee": _f(o.get("OrderFee")),
        "CashDiscount": _f(o.get("CashDiscount")), "LineDiscount": _f(o.get("LineDiscount")),
        "PaymentMethodId": o.get("PaymentMethodId"),
        "DeliveryCountryId": o.get("DeliveryCountryId"),
        "DeliveryZipCode": o.get("DeliveryZipCode"),
        "IsForgotten": bool(o.get("IsForgotten")),
        # ── the only derived-from-PII field that is persisted ──
        "customer_hash": customer_hash(buyer.get("EmailAddress")),
        "buyer_country_id": buyer.get("CountryId"), "buyer_zip": buyer.get("ZipCode"),
    }


def _item_rows(o: dict) -> list[dict]:
    return [{"Id": it["Id"], "OrderId": o["Id"], "PartNo": it.get("PartNo"),
             "ProductName": it.get("ProductName"), "QtyOrdered": _f(it.get("QtyOrdered")),
             "PriceSale": _f(it.get("PriceSale")), "DiscountAmount": _f(it.get("DiscountAmount")),
             "LineAmount": _f(it.get("LineAmount")), "VatRate": _f(it.get("VatRate")),
             "StatusId": it.get("StatusId")}
            for it in (o.get("Items") or [])]


# ════════════════════════════════════════════════════════════════════════════
#  BigQuery
# ════════════════════════════════════════════════════════════════════════════
def _credentials():
    """ADC in production (Cloud Run SA); gcloud user token as a local fallback."""
    try:
        import google.auth
        creds, _ = google.auth.default()
        return creds
    except Exception:
        import subprocess, google.oauth2.credentials
        tok = subprocess.check_output(["gcloud", "auth", "print-access-token"]).decode().strip()
        return google.oauth2.credentials.Credentials(tok)


_client = None
def bq() -> bigquery.Client:
    global _client
    if _client is None:
        _client = bigquery.Client(project=BQ_PROJECT, credentials=_credentials(),
                                  location=BQ_LOCATION)
    return _client


def T(name: str) -> str:
    return f"{BQ_PROJECT}.{BQ_DATASET}.{name}"


S = bigquery.SchemaField
SCHEMAS: dict[str, list[bigquery.SchemaField]] = {
    "orders": [
        S("Id", "INT64", mode="REQUIRED"), S("ApplicationId", "INT64"), S("app_key", "STRING"),
        S("OrderNo", "STRING"), S("OrderDate", "TIMESTAMP"), S("Created", "TIMESTAMP"),
        S("Updated", "TIMESTAMP"), S("StatusId", "INT64"), S("CurrencyId", "INT64"),
        S("FreightCost", "FLOAT64"), S("OrderFee", "FLOAT64"), S("CashDiscount", "FLOAT64"),
        S("LineDiscount", "FLOAT64"), S("PaymentMethodId", "INT64"),
        S("DeliveryCountryId", "INT64"), S("DeliveryZipCode", "STRING"),
        S("IsForgotten", "BOOL"), S("customer_hash", "STRING"),
        S("buyer_country_id", "INT64"), S("buyer_zip", "STRING"),
    ],
    "order_items": [
        S("Id", "INT64", mode="REQUIRED"), S("OrderId", "INT64", mode="REQUIRED"),
        S("PartNo", "STRING"), S("ProductName", "STRING"), S("QtyOrdered", "FLOAT64"),
        S("PriceSale", "FLOAT64"), S("DiscountAmount", "FLOAT64"), S("LineAmount", "FLOAT64"),
        S("VatRate", "FLOAT64"), S("StatusId", "INT64"),
    ],
    "product_skus":       [S("PartNo", "STRING"), S("ProductId", "INT64"), S("EanCode", "STRING")],
    "products":           [S("Id", "INT64"), S("ManufacturerId", "INT64"),
                           S("DefaultName", "STRING"), S("IsActive", "BOOL")],
    "product_categories": [S("ProductId", "INT64"), S("CategoryId", "INT64"), S("IsPrimary", "BOOL")],
    "dim_manufacturers":  [S("ManufacturerId", "INT64"), S("Name", "STRING"), S("IsActive", "BOOL")],
    "dim_categories":     [S("Id", "INT64"), S("Code", "STRING"), S("DefaultName", "STRING"),
                           S("DefaultFullName", "STRING"), S("IsActive", "BOOL")],
    # Real product titles by SKU, from the Channable feed. Norce's
    # Product.DefaultName is VARIANT-grain ("2-4 Y", "One Size"), so the product
    # dimension of first_purchase_products is unreadable without this.
    "sku_titles":         [S("PartNo", "STRING"), S("title", "STRING"), S("brand", "STRING")],
    "dim_payment_methods": [S("Id", "INT64"), S("DefaultName", "STRING")],
    "dim_currencies":      [S("Id", "INT64"), S("Code", "STRING"), S("DefaultName", "STRING")],
    "dim_countries":       [S("Id", "INT64"), S("Code", "STRING"), S("DefaultName", "STRING")],
    # One row per sync step; the incremental watermark is MAX(watermark) here.
    "sync_state":          [S("step", "STRING"), S("watermark", "TIMESTAMP"),
                            S("run_at", "TIMESTAMP"), S("rows", "INT64")],
}

# Partition/cluster only where the volume justifies it (~416k orders / 1.43M lines).
_PARTITION = {"orders": "OrderDate"}
_CLUSTER   = {"orders": ["app_key", "customer_hash"], "order_items": ["OrderId", "PartNo"]}


def ensure_dataset() -> None:
    """Create the dataset + every table if absent. Idempotent; safe to re-run.

    Deliberately called only AFTER the credential check, so a run with no secrets
    leaves the project untouched rather than parking an empty dataset.
    """
    ds = bigquery.Dataset(f"{BQ_PROJECT}.{BQ_DATASET}")
    ds.location = BQ_LOCATION
    ds.description = "Norce Commerce order history for Customer Insights (hashed identity only)."
    bq().create_dataset(ds, exists_ok=True)
    for name, schema in SCHEMAS.items():
        t = bigquery.Table(T(name), schema=schema)
        if name in _PARTITION:
            t.time_partitioning = bigquery.TimePartitioning(field=_PARTITION[name])
        if name in _CLUSTER:
            t.clustering_fields = _CLUSTER[name]
        bq().create_table(t, exists_ok=True)


def _load(rows: list[dict], table: str, schema: list[bigquery.SchemaField]) -> str:
    """Load rows into a fresh staging table and return its full name."""
    stg = T(f"_stg_{table}_{int(time.time()*1000)}")
    cfg = bigquery.LoadJobConfig(schema=schema, write_disposition="WRITE_TRUNCATE")
    bq().load_table_from_json(rows, stg, job_config=cfg).result()
    # Staging is scratch — expire it so a crashed run cannot leave litter behind.
    t = bq().get_table(stg)
    t.expires = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=6)
    bq().update_table(t, ["expires"])
    return stg


def merge(rows: list[dict], table: str, keys: list[str]) -> int:
    """Upsert `rows` into `table` on `keys`. Empty input is a no-op.

    Rows are de-duplicated on the key first, last one winning. BigQuery's MERGE
    aborts the whole statement if one target row matches two source rows, so a
    single repeated PartNo in an expanded product batch would otherwise fail the
    entire sync rather than one record.
    """
    if not rows:
        return 0
    seen: dict[tuple, dict] = {}
    for r in rows:
        seen[tuple(r.get(k) for k in keys)] = r
    rows = list(seen.values())
    schema = SCHEMAS[table]
    stg = _load(rows, table, schema)
    cols = [f.name for f in schema]
    on = " AND ".join(f"t.{k} = s.{k}" for k in keys)
    upd = ", ".join(f"{c} = s.{c}" for c in cols if c not in keys)
    sql = (f"MERGE `{T(table)}` t USING `{stg}` s ON {on} "
           f"WHEN MATCHED THEN UPDATE SET {upd} "
           f"WHEN NOT MATCHED THEN INSERT ({', '.join(cols)}) "
           f"VALUES ({', '.join('s.' + c for c in cols)})")
    bq().query(sql).result()
    bq().delete_table(stg, not_found_ok=True)
    return len(rows)


def replace(rows: list[dict], table: str) -> int:
    """Full reload — used for the small lookup dimensions."""
    if not rows:
        return 0
    cfg = bigquery.LoadJobConfig(schema=SCHEMAS[table], write_disposition="WRITE_TRUNCATE")
    bq().load_table_from_json(rows, T(table), job_config=cfg).result()
    return len(rows)


def watermark(step: str) -> datetime.datetime | None:
    """Last successful high-water mark for a step, less the safety lap."""
    try:
        rows = list(bq().query(
            f"SELECT MAX(watermark) w FROM `{T('sync_state')}` WHERE step = @s",
            job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("s", "STRING", step)])).result())
    except Exception:
        return None
    w = rows[0]["w"] if rows else None
    return (w - WATERMARK_LAP) if w else None


def set_watermark(step: str, w: datetime.datetime, n: int) -> None:
    bq().query(
        f"INSERT INTO `{T('sync_state')}` (step, watermark, run_at, `rows`) "
        f"VALUES (@s, @w, CURRENT_TIMESTAMP(), @n)",
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("s", "STRING", step),
            bigquery.ScalarQueryParameter("w", "TIMESTAMP", w),
            bigquery.ScalarQueryParameter("n", "INT64", n)])).result()


# ════════════════════════════════════════════════════════════════════════════
#  Sync steps
# ════════════════════════════════════════════════════════════════════════════
def sync_orders(app_keys: list[str], since: datetime.datetime | None,
                start: datetime.date | None) -> tuple[int, int]:
    """Pull orders (+ their items) for each application and upsert them.

    Two modes, one code path:
      • backfill    `start` set  → filter on OrderDate ge start
      • incremental `since` set  → filter on Updated (Updated is nullable, so
                                   fall back to Created for never-updated rows)
    """
    n_orders = n_items = 0
    for key in app_keys:
        app_id = APPLICATIONS[key]
        if since is not None:
            ts = _odata_ts(since)
            clause = f"(Updated ge {ts} or (Updated eq null and Created ge {ts}))"
        else:
            clause = f"OrderDate ge {_odata_ts(start or datetime.date.fromisoformat(HISTORY_START))}"
        flt = f"ApplicationId eq {app_id} and {clause}"

        orders, items, t0, a_ord, a_lin = [], [], time.time(), 0, 0
        for o in query("Orders/Orders", app_id, select=ORDER_SELECT, expand=ORDER_EXPAND,
                       filter=flt, orderby="Id"):
            orders.append(_order_row(o, key))
            items.extend(_item_rows(o))
            # Flush in slabs so a long backfill checkpoints instead of holding
            # ~400k orders in memory and losing everything on one bad page.
            if len(orders) >= 5000:
                a_ord += merge(orders, "orders", ["Id"])
                a_lin += merge(items, "order_items", ["Id"])
                orders, items = [], []
        a_ord += merge(orders, "orders", ["Id"])
        a_lin += merge(items, "order_items", ["Id"])
        n_orders, n_items = n_orders + a_ord, n_items + a_lin
        print(f"   {key:<14} orders→{a_ord:>7,}  lines→{a_lin:>8,}  {time.time()-t0:.1f}s")
    return n_orders, n_items


def purge_forgotten() -> int:
    """Honour Norce's GDPR erasure flag.

    Runs on EVERY sync, not just when a forgotten order happens to be in the
    delta: an order can be flagged long after its last update, and the flag is
    the only signal we get. The whole order goes — header and lines — because a
    header with customer_hash blanked would still leave the basket linkable.
    """
    forgotten = [o["Id"] for key in APPLICATIONS
                 for o in query("Orders/Orders", APPLICATIONS[key], select="Id",
                                filter=f"ApplicationId eq {APPLICATIONS[key]} and IsForgotten eq true")]
    if not forgotten:
        return 0
    p = [bigquery.ArrayQueryParameter("ids", "INT64", forgotten)]
    for sql in (f"DELETE FROM `{T('order_items')}` WHERE OrderId IN UNNEST(@ids)",
                f"DELETE FROM `{T('orders')}` WHERE Id IN UNNEST(@ids)"):
        bq().query(sql, job_config=bigquery.QueryJobConfig(query_parameters=p)).result()
    return len(forgotten)


def sync_products(since: datetime.datetime | None) -> tuple[int, int, int]:
    """Products + their SKUs and category links, in one expanded pass.

    ProductCategories has no standalone set (404s), so the only way to get the
    links is $expand from Products — same for the SKU list.

    Paged by KEYSET (Id gt last, orderby Id), not $skip/nextLink: the tenant
    500s non-deterministically at deep offsets on this entity, which killed
    two backfills mid-stream. Keyset holds at any depth, and a page that
    still 500s after retries is shrunk (100 -> 25 -> 5 -> 1); a single row
    that 500s is skipped with a warning rather than sinking the whole run.
    """
    clause = None
    if since is not None:
        ts = _odata_ts(since)
        clause = f"(Updated ge {ts} or (Updated eq null and Created ge {ts}))"
    prods, skus, cats = [], [], []
    last, page, skips = 0, 100, 0
    while True:
        f = f"Id gt {last}" + (f" and {clause}" if clause else "")
        params = {"$select": "Id,ManufacturerId,DefaultName,IsActive",
                  "$expand": "Skus($select=PartNo,ProductId,EanCode),"
                             "Categories($select=ProductId,CategoryId,IsPrimary)",
                  "$filter": f, "$orderby": "Id", "$top": page}
        try:
            rows = _get(f"{NORCE_QUERY_URL}/Products/Products", params, CONTEXT_APP_ID).get("value") or []
        except RuntimeError as e:
            if page > 1:
                page = max(1, page // 4)
                continue
            # A single poison row — skip past it. Ids are dense enough that +1
            # converges; the row is logged so it can be chased upstream.
            skips += 1
            if skips > 25:
                # 25 consecutive single-row failures is not poison data, it is
                # the endpoint being down (observed 2026-08-16: every query
                # 500ed for a stretch, then recovered). Fail loudly; the
                # nightly run picks up where the watermark left off.
                raise RuntimeError(
                    "Products/Products failing on every row — endpoint outage, "
                    f"aborting rather than crawling the Id space. Last error: {e}")
            print(f"   !! skipping product Id>{last} after persistent error: {e}", flush=True)
            last += 1
            continue
        skips = 0
        time.sleep(0.05)              # be polite — three backfills in one day
                                      # visibly degraded this endpoint
        for p in rows:
            prods.append({"Id": p["Id"], "ManufacturerId": p.get("ManufacturerId"),
                          "DefaultName": p.get("DefaultName"), "IsActive": bool(p.get("IsActive"))})
            skus.extend({"PartNo": s.get("PartNo"), "ProductId": p["Id"], "EanCode": s.get("EanCode")}
                        for s in (p.get("Skus") or []))
            cats.extend({"ProductId": p["Id"], "CategoryId": c.get("CategoryId"),
                         "IsPrimary": bool(c.get("IsPrimary"))} for c in (p.get("Categories") or []))
        if rows:
            last = rows[-1]["Id"]
            if len(prods) % 10_000 < page:
                print(f"   products… {len(prods):,} (Id {last})", flush=True)
        done = len(rows) < page
        if page < 100:                # a shrunk page succeeded — resume full pages
            page = 100
            if rows:                  # only genuinely done when a FULL page comes up short
                continue
        if done:
            break
    return (merge(prods, "products", ["Id"]),
            merge(skus, "product_skus", ["PartNo"]),
            merge(cats, "product_categories", ["ProductId", "CategoryId"]))


def sync_sku_titles() -> int:
    """Load real product titles by SKU from the Channable Google Shopping feed.

    WHY: Norce's `Product.DefaultName` is variant-grain, so the product
    dimension of first_purchase_products reads "2-4 Y" / "One Size" / "86/92 cm"
    instead of a product. The feed's `g:id` IS `OrderItem.PartNo` (verified in
    the original audit), so it keys straight onto the order lines.

    Config and the streaming approach are reused from inventory_client — same
    feed, same env var, same iterparse + elem.clear() pattern that keeps a 96 MB
    document at ~30 MB of peak memory. Only three fields are pulled here, so the
    aggregation in that module is deliberately not duplicated.

    COVERAGE IS PARTIAL BY DESIGN. The feed is the CURRENT catalogue and skews
    Babyshop SE, so discontinued SKUs and much of Lekmer simply are not in it.
    The mart COALESCEs back to the Norce name, which is why this returning 0 —
    or the secret being absent entirely — degrades to exactly today's behaviour
    rather than blanking the dimension.

    Requires CHANNABLE_FEED_URL. It is mounted on the dashboard SERVICE but not
    (yet) on the sync job; without it this logs and returns 0.
    """
    import xml.etree.ElementTree as ET
    try:
        import inventory_client
    except Exception as e:
        print(f"   !! sku_titles skipped — inventory_client unavailable: {e!r}")
        return 0
    feed_url = os.environ.get("CHANNABLE_FEED_URL") or inventory_client.CHANNABLE_FEED_URL
    if not feed_url:
        print("   !! sku_titles skipped — CHANNABLE_FEED_URL is not set "
              "(mounted on the dashboard service, not on this job). "
              "first_purchase_products falls back to the Norce variant names.")
        return 0

    tmp_path = f"/tmp/channable-{int(time.time())}.xml"
    with httpx.stream("GET", feed_url, timeout=300.0, follow_redirects=True) as r:
        r.raise_for_status()
        with open(tmp_path, "wb") as fh:
            for chunk in r.iter_bytes(64 * 1024):
                fh.write(chunk)

    rows: dict[str, dict] = {}
    try:
        for _event, elem in ET.iterparse(tmp_path, events=("end",)):
            if elem.tag != "item":
                continue

            def t(tag: str, ns: bool = False) -> str:
                el = elem.find("g:" + tag, inventory_client.NS) if ns else elem.find(tag)
                return el.text.strip() if (el is not None and el.text) else ""

            sku = t("id", ns=True)
            # short_title is the product-level name; `title` carries the variant
            # suffix. Prefer short_title for exactly that reason.
            title = t("short_title", ns=True) or t("title")
            if sku and title:
                # Keyed by SKU so the load is inherently de-duplicated.
                rows[sku] = {"PartNo": sku, "title": title,
                             "brand": t("brand", ns=True) or None}
            elem.clear()
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    return replace(list(rows.values()), "sku_titles")


def sync_dimensions() -> dict[str, int]:
    """Full reload of the lookup tables — a few thousand rows in total.

    All of these are TENANT-WIDE: Application/* and Core/* have no ApplicationId
    property, so passing an application filter makes the request 400.
    """
    out = {}
    out["dim_manufacturers"] = replace(
        [{"ManufacturerId": r.get("ManufacturerId"), "Name": r.get("Name"),
          "IsActive": bool(r.get("IsActive"))}
         for r in query("Application/ClientManufacturers", CONTEXT_APP_ID,
                        select="ManufacturerId,Name,IsActive")], "dim_manufacturers")
    out["dim_categories"] = replace(
        [{"Id": r.get("Id"), "Code": r.get("Code"), "DefaultName": r.get("DefaultName"),
          "DefaultFullName": r.get("DefaultFullName"), "IsActive": bool(r.get("IsActive"))}
         for r in query("Application/Categories", CONTEXT_APP_ID,
                        select="Id,Code,DefaultName,DefaultFullName,IsActive")], "dim_categories")
    out["dim_payment_methods"] = replace(
        [{"Id": r.get("Id"), "DefaultName": r.get("DefaultName")}
         for r in query("Core/PaymentMethods", CONTEXT_APP_ID, select="Id,DefaultName")], "dim_payment_methods")
    out["dim_currencies"] = replace(
        [{"Id": r.get("Id"), "Code": r.get("Code"), "DefaultName": r.get("DefaultName")}
         for r in query("Core/Currencies", CONTEXT_APP_ID, select="Id,Code,DefaultName")], "dim_currencies")
    out["dim_countries"] = replace(
        [{"Id": r.get("Id"), "Code": r.get("Code"), "DefaultName": r.get("DefaultName")}
         for r in query("Core/Countries", CONTEXT_APP_ID, select="Id,Code,DefaultName")], "dim_countries")
    return out


def apply_marts() -> int:
    """(Re)create the mart views from norce_marts.sql.

    The file is the single source of truth for the mart definitions; keeping the
    SQL out of Python means it can be diffed, and pasted straight into the BQ
    console when something needs debugging. Statements are separated by the
    `-- @@` sentinel rather than `;` so a semicolon inside a string or comment
    can never split a statement in half.
    """
    with open(MARTS_SQL, encoding="utf-8") as fh:
        raw = fh.read()
    n = 0
    for chunk in raw.split("-- @@"):
        # A chunk is a statement only if it has executable SQL — the file's
        # header block and any stray commentary are skipped rather than sent to
        # BigQuery as a syntax error.
        body = "\n".join(l for l in chunk.splitlines() if not l.strip().startswith("--")).strip()
        if not body.upper().startswith("CREATE"):
            continue
        bq().query(chunk.replace("${DATASET}", f"{BQ_PROJECT}.{BQ_DATASET}")).result()
        n += 1
    return n


# ════════════════════════════════════════════════════════════════════════════
def main() -> int:
    ap = argparse.ArgumentParser(description="Norce → BigQuery sync for Customer Insights")
    ap.add_argument("--backfill", action="store_true", help="full history instead of the delta")
    ap.add_argument("--from", dest="from_date", help="backfill start date (default 2025-06-11)")
    ap.add_argument("--apps", help="comma-separated application keys (default: all)")
    ap.add_argument("--marts-only", action="store_true", help="only re-apply norce_marts.sql")
    ap.add_argument("--titles-only", action="store_true",
                    help="only reload norce.sku_titles from the Channable feed")
    ap.add_argument("--skip-products", action="store_true", help="skip the product/dimension pass")
    args = ap.parse_args()

    t0 = time.time()

    # These two modes touch BigQuery and the Channable feed but NEVER the Norce
    # API, so they run without Norce credentials — which is the difference
    # between being able to re-apply a mart fix from a laptop and not.
    if args.marts_only or args.titles_only:
        ensure_dataset()
        if args.titles_only:
            print(f"✓ sku_titles reloaded · {sync_sku_titles():,} SKUs · {time.time()-t0:.1f}s")
        if args.marts_only:
            print(f"✓ Norce marts applied · {apply_marts()} statements · {time.time()-t0:.1f}s")
        return 0

    missing = missing_credentials()
    if missing:
        # Graceful, not a crash: the secrets genuinely do not exist yet, a
        # scheduled run must not page anyone, and refresh_customer_insights.py
        # serves `"norce": null` until this job has produced rows. Nothing is
        # created in BigQuery on this path.
        print("⚠️  Norce sync skipped — missing env var(s): " + ", ".join(missing))
        print("    Create the secrets and wire them on, then re-run with --backfill:")
        print("      ./pipeline/setup-customer-insights.sh      (see pipeline/CUSTOMER-INSIGHTS.md)")
        return 0

    ensure_dataset()

    app_keys = [k.strip() for k in args.apps.split(",")] if args.apps else list(APPLICATIONS)
    unknown = [k for k in app_keys if k not in APPLICATIONS]
    if unknown:
        print(f"✗ Unknown application key(s): {', '.join(unknown)}. "
              f"Known: {', '.join(APPLICATIONS)}")
        return 1

    since = None if args.backfill else watermark("orders")
    start = datetime.date.fromisoformat(args.from_date) if args.from_date else None
    mode = (f"backfill from {start or HISTORY_START}" if since is None
            else f"incremental since {since:%Y-%m-%d %H:%M}Z")
    print(f"Norce sync · {mode} · {len(app_keys)} application(s)")

    run_started = datetime.datetime.now(datetime.timezone.utc)
    n_orders, n_items = sync_orders(app_keys, since, start)
    # Watermark = when the run STARTED, never max(Updated): an order updated
    # mid-run would otherwise be skipped forever. WATERMARK_LAP re-reads the
    # overlap on the next run, and the MERGE makes that idempotent.
    set_watermark("orders", run_started, n_orders)

    n_forgotten = purge_forgotten()
    prods = skus = cats = titles = 0
    dims: dict[str, int] = {}
    if not args.skip_products:
        prods, skus, cats = sync_products(None if args.backfill else watermark("products"))
        set_watermark("products", run_started, prods)
        dims = sync_dimensions()
        # Non-fatal: the Channable feed is a nice-to-have that makes the product
        # dimension readable. If it is down or unconfigured the marts COALESCE
        # back to the Norce variant names, which is strictly today's behaviour.
        try:
            titles = sync_sku_titles()
        except Exception as e:
            print(f"   !! sku_titles refresh failed (non-fatal): {e!r}")

    n_marts = apply_marts()
    print(f"✓ Norce sync · orders {n_orders:,} · lines {n_items:,} · products {prods:,} "
          f"· skus {skus:,} · category links {cats:,} · dims {sum(dims.values()):,} "
          f"· sku titles {titles:,} · forgotten purged {n_forgotten:,} "
          f"· marts {n_marts} · {time.time()-t0:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
