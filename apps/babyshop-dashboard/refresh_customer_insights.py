#!/usr/bin/env python3
"""
Customer Insights snapshot — BigQuery → Firestore.

Writes `funnel_cache/<workspace>__customer-insights`, the single document the
Customer Insights tab reads. Same client/auth wiring and the same
{data, fetched_at, expires_at, ttl_seconds, workspace} wrapper as bq_source.py.

TWO SOURCES, ONE DOCUMENT
  funnel  `babyshop-funnel-data.bs_funnel_export.funnel_data` — LIVE today.
          Spend, sessions and the pre-aggregated new/old customer counts.
  norce   `project-a7ade44e-e7e3-4871-a83.norce` marts — built by norce_sync.py,
          which cannot run until the NORCE_* secrets exist. Until the dataset is
          there AND has rows, `data.norce` is null and the UI renders its Norce
          sections as awaiting-data. Nothing here fails because Norce is absent.

FUNNEL RULES BAKED IN (from the data audit — do not "simplify" these away)
  • Market is `market_level_1_kv`. NEVER `market_new` — it is NULL on 98.8% of
    cost rows.
  • The export is a UNION OF DISJOINT FEEDS (cost / product / session /
    customer): no row carries both Cost and a customer count. Each feed is
    therefore aggregated in its OWN CTE and the results are joined on
    month x market x channel. Joining at row level would multiply nothing
    against nothing and silently produce zeros.
  • Customer columns (`newCustomers__File_Import`, `oldCustomers__File_Import`,
    `Orders_count__File_Import`) are meaningful only from 2025-09 — before that
    newCustomers is flat zero. FUNNEL_START enforces it.
  • Customer counts exist only for shop 'Babyshop'; Lekmer's are broken, so the
    whole tab is Babyshop-shop-only and says so in `caveats`.
  • Per-market new-share is suspect (SE ~3% vs DE ~71%); channel-level shares
    are plausible. The caveat travels with the payload so the UI can show it.
  • Gross values throughout — returns and cancellations are ignored (user
    decision), so kv_returns is never netted out.

Run locally:  python3 refresh_customer_insights.py
              SKIP_FIRESTORE=1 CUSTOMER_INSIGHTS_OUT=/tmp/ci.json python3 refresh_customer_insights.py
"""
from __future__ import annotations
import datetime, json, os, time
from google.cloud import bigquery

BQ_PROJECT = os.environ.get("BQ_PROJECT", "babyshop-funnel-data")
BQ_TABLE   = os.environ.get("BQ_TABLE", "babyshop-funnel-data.bs_funnel_export.funnel_data")
WORKSPACE  = os.environ.get("FUNNEL_WORKSPACE", "-Ln87GcdqU9CMJV6zMBY")
COLLECTION = "funnel_cache"
DOC_KEY    = "customer-insights"
TTL        = 30 * 24 * 3600
SOURCE     = "bigquery-export"

FIRESTORE_PROJECT = os.environ.get("FIRESTORE_PROJECT", "project-a7ade44e-e7e3-4871-a83")

# Norce marts (norce_sync.py). Absent until the NORCE_* secrets exist.
NORCE_PROJECT = os.environ.get("NORCE_BQ_PROJECT", "project-a7ade44e-e7e3-4871-a83")
NORCE_DATASET = os.environ.get("NORCE_BQ_DATASET", "norce")

# The customer feed is flat zero before this; anything earlier is noise.
FUNNEL_START = os.environ.get("CUSTOMER_INSIGHTS_START", "2025-09-01")
# Customer counts only exist for this shop — Lekmer's are broken in the export.
CUSTOMER_SHOP = os.environ.get("CUSTOMER_INSIGHTS_SHOP", "Babyshop")
WINDOW_DAYS = 90

# Cohort triangle bounds — see the cohorts query for why each exists.
COHORT_MAX_OFFSET = int(os.environ.get("CUSTOMER_INSIGHTS_COHORT_MAX_OFFSET", "12"))
COHORT_MIN_SIZE   = int(os.environ.get("CUSTOMER_INSIGHTS_COHORT_MIN_SIZE", "50"))

# Firestore's hard document limit is 1,048,576 bytes. Refuse to write above this
# and say what is big, rather than let the write fail at the API and leave the
# tab serving a stale snapshot with nobody the wiser.
#
# The check measures COMPACT JSON, which deliberately over-states the real cost:
# Firestore charges string=bytes+1, number=8, bool/null=1, plus each map key per
# entry. Measured 2026-08-16 on the untrimmed payload — compact JSON 690,779 B
# vs Firestore-accounted 608,252 B (58% of the limit). So this budget trips
# before Firestore would reject the write, which is the safe direction.
#
# funnel.monthly and norce.cohorts both grow every month. When this trips, the
# fix is a rolling window on them, not a bigger number here.
DOC_BUDGET_BYTES = 900_000

CAVEATS = [
    "market-level new-share unreliable (feed definition)",
    "funnel history from 2025-09",
    "norce history from 2025-06-11",
    "gross values; returns ignored (user decision)",
    f"customer counts are shop '{CUSTOMER_SHOP}' only — Lekmer's are broken in the export",
]


def I(v):
    return int(round(float(v or 0)))


def F(v, nd=2):
    return None if v is None else round(float(v), nd)


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


# Both datasets this reads — the Funnel export and the Norce marts — are EU.
# Pinned explicitly rather than left to inference: when a referenced dataset is
# missing, BigQuery falls back to the client default (US) and reports a
# location mismatch instead of a plain "not found", which is a confusing error
# to debug on the day the Norce dataset is one typo away from existing.
BQ_LOCATION = os.environ.get("BQ_LOCATION", "EU")

_client = None
def bq():
    global _client
    if _client is None:
        _client = bigquery.Client(project=BQ_PROJECT, credentials=_credentials(),
                                  location=BQ_LOCATION)
    return _client


def _rows(sql, params=None):
    job = bq().query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params or []))
    return list(job.result())


def _d(name, value):
    return bigquery.ScalarQueryParameter(name, "DATE", value)


# ── Funnel feed predicates ───────────────────────────────────────────────────
# A row belongs to the customer feed if ANY of the three customer columns is
# populated — checking only newCustomers would drop months where a market had
# returning customers but no acquisitions.
CUST_FEED = ("(newCustomers__File_Import IS NOT NULL "
             "OR oldCustomers__File_Import IS NOT NULL "
             "OR Orders_count__File_Import IS NOT NULL)")
SPEND_FEED = "(Cost IS NOT NULL OR Sessions IS NOT NULL)"
SHOP_FILTER = "shop_new = @shop"


def _shop_param():
    return bigquery.ScalarQueryParameter("shop", "STRING", CUSTOMER_SHOP)


def data_end() -> datetime.date:
    """Last date the customer feed actually reports — the tab's 'as of'."""
    r = _rows(f"SELECT MAX(Date) d FROM `{BQ_TABLE}` WHERE {CUST_FEED} AND {SHOP_FILTER}",
              [_shop_param()])
    return r[0]["d"] if r and r[0]["d"] else datetime.date.today() - datetime.timedelta(days=1)


def funnel_monthly(start: datetime.date, end: datetime.date) -> list[dict]:
    """month x market x channel_l1, customer feed and spend feed joined AFTER aggregation.

    Rows that carry neither customers nor spend are dropped: a session-only
    market/channel cell is not actionable and there are ~7k of them, which would
    push the document past Firestore's 1 MiB limit for no benefit.
    """
    sql = f"""
    WITH cust AS (
      SELECT FORMAT_DATE('%Y-%m', Date) month, market_level_1_kv market,
             Channel_Type_Level_1 channel_l1,
             SUM(newCustomers__File_Import) new_customers,
             SUM(oldCustomers__File_Import) old_customers,
             SUM(Orders_count__File_Import) orders
      FROM `{BQ_TABLE}`
      WHERE Date BETWEEN @s AND @e AND {SHOP_FILTER} AND {CUST_FEED}
      GROUP BY 1, 2, 3),
    spend AS (
      SELECT FORMAT_DATE('%Y-%m', Date) month, market_level_1_kv market,
             Channel_Type_Level_1 channel_l1,
             SUM(Cost) cost, SUM(Sessions) sessions
      FROM `{BQ_TABLE}`
      WHERE Date BETWEEN @s AND @e AND {SHOP_FILTER} AND {SPEND_FEED}
      GROUP BY 1, 2, 3)
    SELECT COALESCE(c.month, s.month) month, COALESCE(c.market, s.market) market,
           COALESCE(c.channel_l1, s.channel_l1) channel_l1,
           c.new_customers, c.old_customers, c.orders, s.cost, s.sessions
    FROM cust c FULL OUTER JOIN spend s USING (month, market, channel_l1)
    WHERE COALESCE(c.new_customers, 0) + COALESCE(c.old_customers, 0) > 0
       OR COALESCE(s.cost, 0) > 0
    ORDER BY month, market, channel_l1
    """
    return [{"month": r["month"], "market": r["market"], "shop": CUSTOMER_SHOP,
             "channel_l1": r["channel_l1"], "new_customers": I(r["new_customers"]),
             "old_customers": I(r["old_customers"]), "orders": I(r["orders"]),
             "cost": I(r["cost"]), "sessions": I(r["sessions"])}
            for r in _rows(sql, [_d("s", start), _d("e", end), _shop_param()])]


def funnel_cac_matrix(start: datetime.date, end: datetime.date) -> list[dict]:
    """market x channel_l1 over the trailing window: spend, acquisitions, CAC.

    CAC here is last-touch-free: the export gives no channel attribution for the
    customer counts, so this divides a channel's spend by the new customers the
    same market x channel cell reports. It is a directional cost-per-acquisition,
    which is exactly how the tab labels it.
    """
    sql = f"""
    WITH cust AS (
      SELECT market_level_1_kv market, Channel_Type_Level_1 channel_l1,
             SUM(newCustomers__File_Import) new_customers,
             SUM(oldCustomers__File_Import) old_customers
      FROM `{BQ_TABLE}`
      WHERE Date BETWEEN @s AND @e AND {SHOP_FILTER} AND {CUST_FEED}
      GROUP BY 1, 2),
    spend AS (
      SELECT market_level_1_kv market, Channel_Type_Level_1 channel_l1, SUM(Cost) cost
      FROM `{BQ_TABLE}`
      WHERE Date BETWEEN @s AND @e AND {SHOP_FILTER} AND Cost IS NOT NULL
      GROUP BY 1, 2)
    SELECT COALESCE(c.market, s.market) market,
           COALESCE(c.channel_l1, s.channel_l1) channel_l1,
           s.cost, c.new_customers, c.old_customers
    FROM cust c FULL OUTER JOIN spend s USING (market, channel_l1)
    WHERE COALESCE(s.cost, 0) > 0 OR COALESCE(c.new_customers, 0) > 0
    ORDER BY s.cost DESC NULLS LAST
    """
    out = []
    for r in _rows(sql, [_d("s", start), _d("e", end), _shop_param()]):
        cost, new, old = I(r["cost"]), I(r["new_customers"]), I(r["old_customers"])
        out.append({"market": r["market"], "channel_l1": r["channel_l1"],
                    "cost_90d": cost, "new_customers_90d": new,
                    "old_customers_90d": old,
                    # new + old is the feed's own definition of total customers
                    # in the window. It is NOT the order count — new+old covers
                    # ~68% of Orders_count__File_Import.
                    "total_customers_90d": new + old,
                    "cac": F(cost / new) if new else None})
    return out


def funnel_trend(start: datetime.date, end: datetime.date) -> list[dict]:
    """One row per month: totals across every market and channel."""
    sql = f"""
    WITH cust AS (
      SELECT FORMAT_DATE('%Y-%m', Date) month,
             SUM(newCustomers__File_Import) new_customers,
             SUM(oldCustomers__File_Import) old_customers,
             SUM(Orders_count__File_Import) orders
      FROM `{BQ_TABLE}`
      WHERE Date BETWEEN @s AND @e AND {SHOP_FILTER} AND {CUST_FEED}
      GROUP BY 1),
    spend AS (
      SELECT FORMAT_DATE('%Y-%m', Date) month, SUM(Cost) cost, SUM(Sessions) sessions
      FROM `{BQ_TABLE}`
      WHERE Date BETWEEN @s AND @e AND {SHOP_FILTER} AND {SPEND_FEED}
      GROUP BY 1)
    SELECT COALESCE(c.month, s.month) month, c.new_customers, c.old_customers,
           c.orders, s.cost, s.sessions
    FROM cust c FULL OUTER JOIN spend s USING (month)
    ORDER BY month
    """
    out = []
    for r in _rows(sql, [_d("s", start), _d("e", end), _shop_param()]):
        new, old, cost = I(r["new_customers"]), I(r["old_customers"]), I(r["cost"])
        out.append({"month": r["month"], "new_customers": new, "old_customers": old,
                    "orders": I(r["orders"]), "cost": cost, "sessions": I(r["sessions"]),
                    "new_share": F(new / (new + old), 4) if (new + old) else None,
                    "cac": F(cost / new) if new else None})
    return out


# ── Norce side (optional until the sync has run) ─────────────────────────────
def norce_available() -> bool:
    """True only if the marts exist AND customer_orders actually has rows.

    Dataset-exists is not enough: norce_sync creates the tables before it loads
    anything, so an interrupted first run would otherwise look 'available' and
    serve an all-zero tab.
    """
    try:
        r = _rows(f"SELECT COUNT(*) n FROM `{NORCE_PROJECT}.{NORCE_DATASET}.customer_orders`")
        return bool(r and r[0]["n"])
    except Exception as e:
        # Expected while the dataset does not exist. Printed anyway, so a
        # permissions regression after the backfill is not silently mistaken
        # for "Norce has not run yet".
        print(f"   norce marts unavailable: {type(e).__name__}: {str(e)[:160]}")
        return False


def norce_payload() -> dict:
    """The Norce half of the contract, straight off the marts."""
    D = f"{NORCE_PROJECT}.{NORCE_DATASET}"
    # total_customers is the mart's active_customers under the name the UI
    # wants; by construction new + returning = active, so it is carried rather
    # than recomputed and the two can never disagree.
    monthly = [{"month": str(r["month"]), "market": r["market"], "shop": r["shop"],
                "new_customers": I(r["new_customers"]),
                "returning_customers": I(r["returning_customers"]),
                "total_customers": I(r["active_customers"]),
                "active_customers": I(r["active_customers"]), "orders": I(r["orders"]),
                "revenue": I(r["revenue"]), "aov": F(r["aov"]),
                "repeat_rate": F(r["repeat_rate"], 4)}
               for r in _rows(f"SELECT * FROM `{D}.monthly_customer_metrics` "
                              f"ORDER BY month, market, shop")]

    # Bounded on purpose — this is the one series that grows without limit
    # (every new month adds a cohort AND extends every existing cohort), and
    # the whole payload has to fit one 1 MiB Firestore document.
    #   months_since_first <= 12  1-year CLV is the value at offset 12 and the
    #                             user decision rules out 2- and 3-year CLV, so
    #                             offsets past 12 are outside the spec anyway.
    #   cohort_size >= 50         a retention percentage off 6 customers is
    #                             noise the UI would have to hide regardless.
    cohorts = [{"cohort_month": str(r["cohort_month"]), "market": r["market"], "shop": r["shop"],
                "months_since_first": I(r["months_since_first"]),
                "cohort_size": I(r["cohort_size"]),
                "active_customers": I(r["active_customers"]),
                "retention_rate": F(r["retention_rate"], 4),
                "cumulative_revenue_per_customer": F(r["cumulative_revenue_per_customer"])}
               for r in _rows(f"SELECT * FROM `{D}.cohort_retention` "
                              f"WHERE months_since_first <= {COHORT_MAX_OFFSET} "
                              f"AND cohort_size >= {COHORT_MIN_SIZE} "
                              f"ORDER BY cohort_month, market, shop, months_since_first")]

    # 1-year CLV = mean revenue_365d_from_first over customers whose own 365
    # days have elapsed (the mart NULLs the rest), per first market.
    # returning_customers is COUNTIF(is_repeat) over THIS row's own `customers`
    # base (the matured cohort), not over all customers — so it is always a
    # subset of the total sitting next to it.
    clv = [{"market": r["market"], "shop": r["shop"], "customers": I(r["customers"]),
            "returning_customers": I(r["returning"]),
            "clv_1y": F(r["clv_1y"]), "aov": F(r["aov"]), "orders_per_customer": F(r["opc"])}
           for r in _rows(f"""
        SELECT first_market market, shop, COUNT(*) customers,
               COUNTIF(is_repeat) returning,
               AVG(revenue_365d_from_first) clv_1y, AVG(aov) aov, AVG(orders_cnt) opc
        FROM `{D}.customer_facts`
        WHERE revenue_365d_from_first IS NOT NULL
        GROUP BY 1, 2 ORDER BY customers DESC""")]

    # Churn proxy: of the customers who could have come back, how many did not
    # buy in the last 365 days. Norce history is short, so this is a floor.
    # repeat_rate is the share who ever placed a second order — a different
    # question from churn, and the one the LTV story actually rests on.
    churn = [{"market": r["market"], "shop": r["shop"], "customers": I(r["customers"]),
              "returning_customers": I(r["returning"]),
              "lapsed_12m": I(r["lapsed"]), "churn_12m": F(r["churn"], 4),
              "repeat_rate": F(r["repeat_rate"], 4),
              "orders_per_customer": F(r["opc"])}
             for r in _rows(f"""
        SELECT first_market market, shop, COUNT(*) customers,
               COUNTIF(is_repeat) returning,
               COUNTIF(days_since_last_order > 365) lapsed,
               SAFE_DIVIDE(COUNTIF(days_since_last_order > 365), COUNT(*)) churn,
               SAFE_DIVIDE(COUNTIF(is_repeat), COUNT(*)) repeat_rate,
               AVG(orders_cnt) opc
        FROM `{D}.customer_facts`
        WHERE first_order_date <= DATE_SUB(CURRENT_DATE(), INTERVAL 365 DAY)
        GROUP BY 1, 2 ORDER BY customers DESC""")]

    def fpp(dim_type, limit=200):
        # Key names are the UI contract: name / new_customers /
        # share_of_first_orders. revenue + revenue_share ride along as extras.
        return [{"month": str(r["month"]), "market": r["market"], "shop": r["shop"],
                 "name": r["dim_value"], "new_customers": I(r["new_customer_orders"]),
                 "share_of_first_orders": F(r["order_share"], 4),
                 "revenue": I(r["revenue"]), "revenue_share": F(r["revenue_share"], 4)}
                for r in _rows(f"""
            SELECT * FROM `{D}.first_purchase_products`
            WHERE dim_type = @t ORDER BY new_customer_orders DESC LIMIT {limit}""",
                               [bigquery.ScalarQueryParameter("t", "STRING", dim_type)])]

    last_sync = _rows(f"SELECT CAST(MAX(run_at) AS STRING) t FROM `{D}.sync_state`")
    return {
        "monthly_customer_metrics": monthly,
        "cohorts": cohorts,
        "clv_1y_by_market": clv,
        "churn": churn,
        "first_purchase_products": {"brands": fpp("brand"), "categories": fpp("category_l1"),
                                    "products": fpp("product")},
        "last_sync": last_sync[0]["t"] if last_sync else None,
    }


# ── Payload ──────────────────────────────────────────────────────────────────
def build_payload() -> dict:
    end = data_end()
    start = datetime.date.fromisoformat(FUNNEL_START)
    win_start = end - datetime.timedelta(days=WINDOW_DAYS - 1)

    monthly = funnel_monthly(start, end)
    cac = funnel_cac_matrix(win_start, end)
    trend = funnel_trend(start, end)

    # ── KPIs ──
    by_month = {t["month"]: t for t in trend}
    months = sorted(by_month)
    mtd = by_month.get(end.strftime("%Y-%m"), {})
    prev_key = (end.replace(day=1) - datetime.timedelta(days=1)).strftime("%Y-%m")
    prev = by_month.get(prev_key, {})

    new_90 = sum(c["new_customers_90d"] for c in cac)
    old_90 = sum(c["old_customers_90d"] for c in cac)
    cost_90 = sum(c["cost_90d"] for c in cac)

    norce = norce_payload() if norce_available() else None
    # The headline CLV is the volume-weighted mean across markets, so a tiny
    # market with a freak average cannot swing it.
    clv_1y = None
    if norce and norce["clv_1y_by_market"]:
        n = sum(r["customers"] for r in norce["clv_1y_by_market"] if r["clv_1y"] is not None)
        if n:
            clv_1y = round(sum(r["clv_1y"] * r["customers"]
                               for r in norce["clv_1y_by_market"] if r["clv_1y"] is not None) / n, 2)
    blended_cac = round(cost_90 / new_90, 2) if new_90 else None
    # Both are volume-weighted across markets, over the same population: the
    # customers whose first order is at least 365 days old.
    repeat_12m = churn_12m = None
    if norce and norce["churn"]:
        cn = sum(r["customers"] for r in norce["churn"])
        if cn:
            churn_12m = round(sum(r["lapsed_12m"] for r in norce["churn"]) / cn, 4)
            repeat_12m = round(sum((r["repeat_rate"] or 0) * r["customers"]
                                   for r in norce["churn"]) / cn, 4)

    return {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "sources": {
            "funnel": {"from": start.isoformat(), "to": end.isoformat(), "source": SOURCE},
            "norce": ({"available": True, "last_sync": norce["last_sync"],
                       "note": f"{NORCE_PROJECT}.{NORCE_DATASET} marts"} if norce else
                      {"available": False, "last_sync": None,
                       "note": "awaiting NORCE_* secrets"}),
        },
        "kpis": {
            "new_customers_mtd": mtd.get("new_customers", 0),
            "new_customers_prev_month": prev.get("new_customers", 0),
            "new_share_90d": round(new_90 / (new_90 + old_90), 4) if (new_90 + old_90) else None,
            "blended_cac_90d": blended_cac,
            "clv_1y": clv_1y,
            "ltv_cac_ratio": round(clv_1y / blended_cac, 2) if (clv_1y and blended_cac) else None,
            "repeat_rate_12m": repeat_12m,
            "churn_12m": churn_12m,
        },
        "funnel": {"monthly": monthly, "cac_matrix": cac, "trend": trend,
                   "window_days": WINDOW_DAYS,
                   "window": {"start": win_start.isoformat(), "end": end.isoformat()},
                   "months": months},
        "norce": norce,
        "caveats": CAVEATS,
    }


def _check_size(payload: dict) -> int:
    """Refuse to write a document that is about to hit Firestore's 1 MiB cap."""
    n = len(json.dumps(payload, ensure_ascii=False))
    if n > DOC_BUDGET_BYTES:
        big = sorted(((len(json.dumps(v, ensure_ascii=False)), k)
                      for k, v in payload.items()), reverse=True)[:3]
        raise RuntimeError(
            f"Customer Insights payload is {n:,} B, over the {DOC_BUDGET_BYTES:,} B budget "
            f"(Firestore's hard limit is 1,048,576). Biggest sections: "
            + ", ".join(f"{k} {s:,} B" for s, k in big)
            + ". Put a rolling window on the growing series — do not just raise the budget.")
    return n


def write_firestore(payload: dict) -> str:
    from google.cloud import firestore
    _check_size(payload)
    db = firestore.Client(project=FIRESTORE_PROJECT, credentials=_credentials())
    doc_id = f"{WORKSPACE}__{DOC_KEY}"
    db.collection(COLLECTION).document(doc_id).set({
        "data": payload, "fetched_at": firestore.SERVER_TIMESTAMP,
        "expires_at": time.time() + TTL, "ttl_seconds": TTL, "workspace": WORKSPACE})
    return f"{COLLECTION}/{doc_id}"


def main():
    t0 = time.time()
    p = build_payload()
    out = os.environ.get("CUSTOMER_INSIGHTS_OUT")
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(p, fh, ensure_ascii=False)
        print(f"   wrote {out} ({os.path.getsize(out):,} bytes)")
    where = "(skipped)" if os.environ.get("SKIP_FIRESTORE") else write_firestore(p)
    k = p["kpis"]
    print(f"✓ Customer Insights refresh · {where} · "
          f"funnel {p['sources']['funnel']['from']}→{p['sources']['funnel']['to']} · "
          f"new MTD {k['new_customers_mtd']:,} (prev {k['new_customers_prev_month']:,}) · "
          f"new-share 90d {k['new_share_90d']} · blended CAC {k['blended_cac_90d']} · "
          f"norce {'yes' if p['norce'] else 'null'} · "
          f"{len(p['funnel']['monthly'])} monthly rows · {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
