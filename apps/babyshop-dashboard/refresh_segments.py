#!/usr/bin/env python3
"""
Customer Segments snapshot — BigQuery (norce marts) → Firestore.

Writes `funnel_cache/<workspace>__segments`, the single document the Segments
tab reads. Same client/auth wiring and the same
{data, fetched_at, expires_at, ttl_seconds, workspace} wrapper as
refresh_customer_insights.py and refresh_voyado.py.

SOURCE
  `project-a7ade44e-e7e3-4871-a83.norce` — the landing tables and the marts
  from norce_sync.py + norce_marts.sql. Nothing else: no Funnel, no cost.

WHAT THIS ADDS OVER THE CUSTOMER INSIGHTS TAB
  Segment-level views of the same order history: lifecycle states over time,
  value deciles, discount dependence, cross-brand overlap, and — the
  Babyshop-native one — life stage inferred from the SIZE on each order line
  (EU kids' clothing sizes are the child's height in cm, which maps to age).

RULES INHERITED FROM THE MARTS (norce_marts.sql header — do not "fix" here)
  • identity = customer_hash (SHA256 of lowercased email), scoped PER SHOP —
    babyshop and lekmer are separate businesses; the cross-brand section is the
    one deliberate exception and says so.
  • order_value is SEK at today's rate, ex-VAT, excluding the shipping line.
  • NO status filtering; gross values.
  • No 2- or 3-year CLV: 1-year only (revenue_365d_from_first, matured only).

LIFE-STAGE INFERENCE (all approximations are one-directional: a line that
does not parse contributes nothing — it never guesses)
  • "86 cm" / "98/104 cm"      → height, midpoint of a split size
  • "18-24 Months"             → age in months, midpoint
  • "2-4 Y"                    → age in years, midpoint
  • "…-25 EU"                  → EU kids' shoe size, mapped via a standard table
  Height → age uses the WHO/CDC growth-curve midpoints (the same table printed
  in the tab's explainer). Stages: newborn ≤6m, baby ≤18m, toddler ≤4y,
  kids ≤8y, big_kids ≤12y, teen >12y.
  DiscountAmount ratios are computed within a customer's own currency and never
  cross-summed, so the discount section needs no FX conversion.

Run locally:  python3 refresh_segments.py
              SKIP_FIRESTORE=1 SEGMENTS_OUT=/tmp/seg.json python3 refresh_segments.py
"""
from __future__ import annotations
import datetime, json, os, time
from google.cloud import bigquery

BQ_PROJECT  = os.environ.get("NORCE_BQ_PROJECT", "project-a7ade44e-e7e3-4871-a83")
BQ_DATASET  = os.environ.get("NORCE_BQ_DATASET", "norce")
BQ_LOCATION = os.environ.get("BQ_LOCATION", "EU")

WORKSPACE  = os.environ.get("FUNNEL_WORKSPACE", "-Ln87GcdqU9CMJV6zMBY")
COLLECTION = "funnel_cache"
DOC_KEY    = "segments"
TTL        = 30 * 24 * 3600
FIRESTORE_PROJECT = os.environ.get("FIRESTORE_PROJECT", "project-a7ade44e-e7e3-4871-a83")

HISTORY_START = "2025-06-11"   # platform cutover; norce_marts.sql header
DOC_BUDGET_BYTES = 900_000

CAVEATS = [
    "identity is the hashed buyer email, scoped per shop — babyshop and lekmer "
    "are counted as separate customers everywhere except the cross-brand "
    "section, which exists precisely to show the overlap",
    "history starts 2025-06-11 (platform cutover), so 'lapsed' cannot yet mean "
    "more than ~14 months inactive, and customers first seen before 2025-09-11 "
    "may be pre-cutover regulars (migration window)",
    "all money is SEK at today's rate for all history (constant-currency), "
    "ex-VAT, gross — returns and cancellations are not netted off",
    "life stage is inferred from sizes on order lines (cm = child height); "
    "lines without a parseable size contribute nothing, multi-child households "
    "show as the most recent child, and gifts skew a small share of signals",
    "discount share is computed within each customer's own currency (no FX "
    "needed for a ratio); 'discounted' means the line carried DiscountAmount > 0",
    "the 'mixed' discount bucket self-selects multi-order customers (one order "
    "rarely spans both price points), so its high repeat rate is composition, "
    "not causation — compare full_price vs promo_only, which are mostly "
    "single-basket-defined, and read AOV/revenue rather than repeat rate",
    "1-year CLV averages only customers whose own 365 days have fully elapsed — "
    "younger customers are excluded from that column, not averaged in low",
]

DEFINITIONS = {
    "lifecycle_states": "at each month-end: repeat_active = 2+ orders & last "
                        "order ≤90d; new = single order ≤90d old; at_risk = "
                        "last order 91–180d; lapsed = >180d",
    "value_deciles": "customers ranked by lifetime revenue (SEK), decile 1 = top 10%",
    "discount_buckets": "full_price = 0% of line value discounted; mixed <50%; "
                        "promo_only ≥50%",
    "life_stages": "newborn ≤6m · baby ≤18m · toddler ≤4y · kids ≤8y · "
                   "big_kids ≤12y · teen >12y — from the latest sized purchase",
    "entry_stage": "the life stage implied by the FIRST sized purchase — the "
                   "runway a customer was acquired with",
    "aging_out": "latest sized purchase implies age ≥11y (~size 152+): "
                 "predictable churn as the child outgrows the assortment",
}


def I(v):
    return int(round(float(v or 0)))


def F(v, nd=4):
    return None if v is None else round(float(v), nd)


def _credentials():
    """ADC in production (Cloud Run SA); gcloud user token as a local fallback.
    Same shape as refresh_voyado._credentials — every module wires its own."""
    try:
        import google.auth
        creds, _ = google.auth.default()
        return creds
    except Exception:
        import subprocess, google.oauth2.credentials

        class _GcloudToken(google.oauth2.credentials.Credentials):
            def refresh(self, request):  # noqa: ARG002 - signature fixed by google-auth
                self.token = subprocess.check_output(
                    ["gcloud", "auth", "print-access-token"]).decode().strip()
                self.expiry = datetime.datetime.utcnow() + datetime.timedelta(minutes=45)

        tok = subprocess.check_output(["gcloud", "auth", "print-access-token"]).decode().strip()
        c = _GcloudToken(tok)
        c.expiry = datetime.datetime.utcnow() + datetime.timedelta(minutes=45)
        return c


_client = None
def bq():
    global _client
    if _client is None:
        _client = bigquery.Client(project=BQ_PROJECT, credentials=_credentials(),
                                  location=BQ_LOCATION)
    return _client


def D(name: str) -> str:
    return f"`{BQ_PROJECT}.{BQ_DATASET}.{name}`"


def _rows(sql, params=None):
    job = bq().query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params or []))
    return list(job.result())


# ── The size→age parser, shared by every life-stage query ────────────────────
# One CTE string interpolated into each query that needs it. Produces one row
# per order line that carries a parseable size: (customer_hash, shop, app_key,
# order_date, age_months).
#
# Order of precedence per line: explicit months → explicit years → height in cm
# → EU shoe size. Explicit beats inferred; height beats shoe because the shoe
# table is the roughest.
def _sized_lines_cte() -> str:
    return f"""
sized_lines AS (
  SELECT
    o.customer_hash, o.shop, o.app_key, o.order_date,
    CASE
      WHEN mo1 IS NOT NULL AND mo1 <= 36
        THEN DIV(mo1 + COALESCE(mo2, mo1), 2)
      WHEN yr1 IS NOT NULL AND yr1 <= 18
        THEN DIV((yr1 + COALESCE(yr2, yr1)) * 12, 2)
      WHEN h BETWEEN 44 AND 188 THEN
        CASE  -- height (cm) → age (months): growth-curve midpoints
          WHEN h <= 56  THEN 1   WHEN h <= 62  THEN 3   WHEN h <= 68  THEN 6
          WHEN h <= 74  THEN 9   WHEN h <= 80  THEN 12  WHEN h <= 86  THEN 18
          WHEN h <= 92  THEN 24  WHEN h <= 98  THEN 36  WHEN h <= 104 THEN 48
          WHEN h <= 110 THEN 60  WHEN h <= 116 THEN 72  WHEN h <= 122 THEN 84
          WHEN h <= 128 THEN 96  WHEN h <= 134 THEN 108 WHEN h <= 140 THEN 120
          WHEN h <= 146 THEN 132 WHEN h <= 152 THEN 144 WHEN h <= 158 THEN 156
          WHEN h <= 164 THEN 168 WHEN h <= 170 THEN 180 ELSE 192
        END
      WHEN eu BETWEEN 16 AND 40 THEN
        CASE  -- EU kids' shoe size → age (months), standard fitting table
          WHEN eu <= 18 THEN 9   WHEN eu <= 20 THEN 15  WHEN eu <= 22 THEN 21
          WHEN eu <= 24 THEN 27  WHEN eu <= 26 THEN 39  WHEN eu <= 28 THEN 54
          WHEN eu <= 30 THEN 66  WHEN eu <= 32 THEN 84  WHEN eu <= 34 THEN 102
          WHEN eu <= 36 THEN 120 ELSE 144
        END
    END AS age_months
  FROM (
    SELECT
      li.OrderId,
      -- "98/104 cm" → midpoint; "86 cm" → itself
      DIV(COALESCE(SAFE_CAST(REGEXP_EXTRACT(li.ProductName, r'(\\d{{2,3}})\\s*/\\s*\\d{{2,3}}\\s*[cC][mM]\\b') AS INT64),
                   SAFE_CAST(REGEXP_EXTRACT(li.ProductName, r'(\\d{{2,3}})\\s*[cC][mM]\\b') AS INT64), 0)
        + COALESCE(SAFE_CAST(REGEXP_EXTRACT(li.ProductName, r'\\d{{2,3}}\\s*/\\s*(\\d{{2,3}})\\s*[cC][mM]\\b') AS INT64),
                   SAFE_CAST(REGEXP_EXTRACT(li.ProductName, r'(\\d{{2,3}})\\s*[cC][mM]\\b') AS INT64), 0), 2) AS h,
      SAFE_CAST(REGEXP_EXTRACT(li.ProductName, r'(?i)\\b(\\d{{1,2}})\\s*-\\s*\\d{{1,2}}\\s*m(?:onths|ths|o)?\\b') AS INT64) AS mo1,
      SAFE_CAST(REGEXP_EXTRACT(li.ProductName, r'(?i)\\b\\d{{1,2}}\\s*-\\s*(\\d{{1,2}})\\s*m(?:onths|ths|o)?\\b') AS INT64) AS mo2,
      SAFE_CAST(REGEXP_EXTRACT(li.ProductName, r'(?i)\\b(\\d{{1,2}})\\s*-\\s*\\d{{1,2}}\\s*y(?:rs|ears|r)?\\b') AS INT64) AS yr1,
      SAFE_CAST(REGEXP_EXTRACT(li.ProductName, r'(?i)\\b\\d{{1,2}}\\s*-\\s*(\\d{{1,2}})\\s*y(?:rs|ears|r)?\\b') AS INT64) AS yr2,
      SAFE_CAST(REGEXP_EXTRACT(li.ProductName, r'(?i)[-\\s](\\d{{2}})\\s*EU\\b') AS INT64) AS eu
    FROM {D('order_items')} li
    WHERE li.PartNo != '1000014'
  ) p
  JOIN {D('customer_orders')} o ON o.order_id = p.OrderId
  WHERE (p.h BETWEEN 44 AND 188) OR p.mo1 IS NOT NULL
     OR (p.yr1 IS NOT NULL AND p.yr1 <= 18) OR (p.eu BETWEEN 16 AND 40)
),
customer_stage AS (
  SELECT
    customer_hash, shop,
    ARRAY_AGG(age_months IGNORE NULLS ORDER BY order_date DESC, age_months LIMIT 1)[SAFE_OFFSET(0)] AS latest_age_months,
    ARRAY_AGG(age_months IGNORE NULLS ORDER BY order_date ASC,  age_months LIMIT 1)[SAFE_OFFSET(0)] AS entry_age_months
  FROM sized_lines
  WHERE age_months IS NOT NULL
  GROUP BY 1, 2
)"""


STAGE_CASE = """CASE
      WHEN {col} <= 6   THEN 'newborn'
      WHEN {col} <= 18  THEN 'baby'
      WHEN {col} <= 48  THEN 'toddler'
      WHEN {col} <= 96  THEN 'kids'
      WHEN {col} <= 144 THEN 'big_kids'
      ELSE 'teen' END"""

STAGES = ["newborn", "baby", "toddler", "kids", "big_kids", "teen"]


# ── Sections ─────────────────────────────────────────────────────────────────
def coverage() -> dict:
    r = _rows(f"""
      SELECT
        (SELECT COUNT(*) FROM {D('customer_orders')})                          AS orders,
        (SELECT MIN(order_date) FROM {D('customer_orders')})                   AS lo,
        (SELECT MAX(order_date) FROM {D('customer_orders')})                   AS hi,
        (SELECT COUNT(*) FROM {D('customer_facts')})                           AS customers,
        (SELECT COUNTIF(PartNo != '1000014') FROM {D('order_items')})          AS lines
    """)[0]
    parsed = _rows(f"""
      WITH {_sized_lines_cte()}
      SELECT COUNT(*) AS n FROM sized_lines WHERE age_months IS NOT NULL
    """)[0]
    return {"orders": I(r["orders"]), "customers": I(r["customers"]),
            "from": str(r["lo"]) if r["lo"] else None,
            "to": str(r["hi"]) if r["hi"] else None,
            "lines": I(r["lines"]), "sized_lines": I(parsed["n"]),
            "size_coverage": F(parsed["n"] / r["lines"] if r["lines"] else None)}


def kpis() -> dict:
    r = _rows(f"""
      SELECT
        COUNT(*)                                   AS customers,
        COUNTIF(is_repeat)                         AS repeaters,
        AVG(orders_cnt)                            AS orders_per_customer
      FROM {D('customer_facts')}
    """)[0]
    xb = _rows(f"""
      SELECT COUNT(*) AS hashes, COUNTIF(shops = 2) AS both
      FROM (SELECT customer_hash, COUNT(DISTINCT shop) AS shops
            FROM {D('customer_facts')} GROUP BY 1)
    """)[0]
    return {
        "customers": I(r["customers"]),
        "repeat_rate": F(r["repeaters"] / r["customers"] if r["customers"] else None),
        "orders_per_customer": F(r["orders_per_customer"], 2),
        "cross_brand_share": F(xb["both"] / xb["hashes"] if xb["hashes"] else None),
    }


def lifecycle() -> list[dict]:
    """Month-end lifecycle state counts, per shop — the triangle join is ~15
    months × 417k orders, small for BigQuery."""
    rows = _rows(f"""
      WITH months AS (
        SELECT m FROM UNNEST(GENERATE_DATE_ARRAY(
          DATE_TRUNC(DATE '{HISTORY_START}', MONTH),
          DATE_TRUNC(CURRENT_DATE(), MONTH), INTERVAL 1 MONTH)) AS m
      ),
      snap AS (
        SELECT
          m.m AS month, o.shop, o.customer_hash,
          MIN(o.order_date) AS first_d, MAX(o.order_date) AS last_d, COUNT(*) AS cnt,
          LEAST(LAST_DAY(m.m), CURRENT_DATE()) AS me
        FROM months m
        JOIN {D('customer_orders')} o ON o.order_date <= LEAST(LAST_DAY(m.m), CURRENT_DATE())
        GROUP BY 1, 2, 3, LEAST(LAST_DAY(m.m), CURRENT_DATE())
      )
      SELECT month, shop,
        COUNTIF(state = 'repeat_active') AS repeat_active,
        COUNTIF(state = 'new')           AS new_cnt,
        COUNTIF(state = 'at_risk')       AS at_risk,
        COUNTIF(state = 'lapsed')        AS lapsed
      FROM (
        SELECT month, shop, CASE
          WHEN cnt >= 2 AND DATE_DIFF(me, last_d, DAY) <= 90  THEN 'repeat_active'
          WHEN cnt  = 1 AND DATE_DIFF(me, first_d, DAY) <= 90 THEN 'new'
          WHEN DATE_DIFF(me, last_d, DAY) <= 180              THEN 'at_risk'
          ELSE 'lapsed' END AS state
        FROM snap)
      GROUP BY 1, 2
      ORDER BY 1, 2
    """)
    return [{"month": str(r["month"])[:7], "shop": r["shop"],
             "new": I(r["new_cnt"]), "repeat_active": I(r["repeat_active"]),
             "at_risk": I(r["at_risk"]), "lapsed": I(r["lapsed"])} for r in rows]


def cohorts() -> list[dict]:
    """Retention triangle per shop, offsets 1..12, aggregated over markets."""
    rows = _rows(f"""
      SELECT cohort_month, shop, months_since_first,
             SUM(cohort_size) AS cohort_size, SUM(active_customers) AS active
      FROM {D('cohort_retention')}
      WHERE months_since_first BETWEEN 1 AND 12
      GROUP BY 1, 2, 3
      ORDER BY 1, 2, 3
    """)
    return [{"cohort": str(r["cohort_month"])[:7], "shop": r["shop"],
             "offset": I(r["months_since_first"]), "size": I(r["cohort_size"]),
             "retention": F(r["active"] / r["cohort_size"] if r["cohort_size"] else None)}
            for r in rows]


def value_deciles() -> list[dict]:
    rows = _rows(f"""
      WITH t AS (
        SELECT revenue_total, orders_cnt,
               NTILE(10) OVER (ORDER BY revenue_total DESC) AS decile
        FROM {D('customer_facts')}
      )
      SELECT decile, COUNT(*) AS customers, SUM(revenue_total) AS revenue,
             AVG(revenue_total) AS avg_revenue, AVG(orders_cnt) AS avg_orders
      FROM t GROUP BY 1 ORDER BY 1
    """)
    total = sum(float(r["revenue"] or 0) for r in rows) or None
    return [{"decile": I(r["decile"]), "customers": I(r["customers"]),
             "revenue_sek": I(r["revenue"]),
             "revenue_share": F(float(r["revenue"] or 0) / total if total else None),
             "avg_revenue_sek": I(r["avg_revenue"]), "avg_orders": F(r["avg_orders"], 2)}
            for r in rows]


def lifestage_distribution() -> list[dict]:
    rows = _rows(f"""
      WITH {_sized_lines_cte()}
      SELECT
        {STAGE_CASE.format(col='cs.latest_age_months')} AS stage,
        COUNT(*) AS customers,
        COUNTIF(f.days_since_last_order <= 180) AS active_customers
      FROM customer_stage cs
      JOIN {D('customer_facts')} f
        ON f.customer_hash = cs.customer_hash AND f.shop = cs.shop
      GROUP BY 1
    """)
    by = {r["stage"]: r for r in rows}
    total = sum(I(r["customers"]) for r in rows) or None
    return [{"stage": s, "customers": I(by[s]["customers"]) if s in by else 0,
             "active_customers": I(by[s]["active_customers"]) if s in by else 0,
             "share": F(I(by[s]["customers"]) / total if (total and s in by) else None)}
            for s in STAGES]


def ltv_by_entry_stage() -> list[dict]:
    rows = _rows(f"""
      WITH {_sized_lines_cte()}
      SELECT
        {STAGE_CASE.format(col='cs.entry_age_months')} AS stage,
        COUNT(*)                                       AS customers,
        AVG(f.revenue_total)                           AS avg_revenue,
        AVG(f.orders_cnt)                              AS avg_orders,
        SAFE_DIVIDE(SUM(f.revenue_total), SUM(f.orders_cnt)) AS aov,
        AVG(IF(f.is_repeat, 1, 0))                     AS repeat_rate,
        AVG(f.revenue_365d_from_first)                 AS clv_1y,
        COUNTIF(f.revenue_365d_from_first IS NOT NULL) AS mature_customers
      FROM customer_stage cs
      JOIN {D('customer_facts')} f
        ON f.customer_hash = cs.customer_hash AND f.shop = cs.shop
      GROUP BY 1
    """)
    by = {r["stage"]: r for r in rows}
    out = []
    for s in STAGES:
        r = by.get(s)
        out.append({"stage": s,
                    "customers": I(r["customers"]) if r else 0,
                    "avg_revenue_sek": I(r["avg_revenue"]) if r else None,
                    "avg_orders": F(r["avg_orders"], 2) if r else None,
                    "aov_sek": I(r["aov"]) if r else None,
                    "repeat_rate": F(r["repeat_rate"]) if r else None,
                    "clv_1y_sek": I(r["clv_1y"]) if (r and r["clv_1y"] is not None) else None,
                    "mature_customers": I(r["mature_customers"]) if r else 0})
    return out


def aging_out() -> dict:
    r = _rows(f"""
      WITH {_sized_lines_cte()}
      SELECT
        COUNT(*) AS customers,
        COUNTIF(f.days_since_last_order <= 180) AS active_customers,
        (SELECT COUNT(*) FROM customer_stage) AS sized_customers,
        SUM(rev12.revenue_12m) AS revenue_12m
      FROM customer_stage cs
      JOIN {D('customer_facts')} f
        ON f.customer_hash = cs.customer_hash AND f.shop = cs.shop
      LEFT JOIN (
        SELECT customer_hash, shop, SUM(order_value) AS revenue_12m
        FROM {D('customer_orders')}
        WHERE order_date >= DATE_SUB(CURRENT_DATE(), INTERVAL 365 DAY)
        GROUP BY 1, 2
      ) rev12 ON rev12.customer_hash = cs.customer_hash AND rev12.shop = cs.shop
      WHERE cs.latest_age_months >= 132
    """)[0]
    return {"customers": I(r["customers"]),
            "active_customers": I(r["active_customers"]),
            "share_of_sized": F(r["customers"] / r["sized_customers"]
                                if r["sized_customers"] else None),
            "revenue_12m_sek": I(r["revenue_12m"])}


def discount_buckets() -> list[dict]:
    rows = _rows(f"""
      WITH per_cust AS (
        SELECT o.customer_hash, o.shop,
               SAFE_DIVIDE(SUM(IF(li.DiscountAmount > 0, li.LineAmount, 0)),
                           NULLIF(SUM(li.LineAmount), 0)) AS disc_share
        FROM {D('order_items')} li
        JOIN {D('customer_orders')} o ON o.order_id = li.OrderId
        WHERE li.PartNo != '1000014' AND li.LineAmount > 0
        GROUP BY 1, 2
      )
      SELECT
        CASE WHEN p.disc_share IS NULL OR p.disc_share = 0 THEN 'full_price'
             WHEN p.disc_share < 0.5 THEN 'mixed'
             ELSE 'promo_only' END AS bucket,
        COUNT(*)                    AS customers,
        AVG(IF(f.is_repeat, 1, 0))  AS repeat_rate,
        AVG(f.revenue_total)        AS avg_revenue,
        SAFE_DIVIDE(SUM(f.revenue_total), SUM(f.orders_cnt)) AS aov
      FROM per_cust p
      JOIN {D('customer_facts')} f
        ON f.customer_hash = p.customer_hash AND f.shop = p.shop
      GROUP BY 1
    """)
    order = {"full_price": 0, "mixed": 1, "promo_only": 2}
    total = sum(I(r["customers"]) for r in rows) or None
    return sorted(({"bucket": r["bucket"], "customers": I(r["customers"]),
                    "share": F(I(r["customers"]) / total if total else None),
                    "repeat_rate": F(r["repeat_rate"]),
                    "avg_revenue_sek": I(r["avg_revenue"]), "aov_sek": I(r["aov"])}
                   for r in rows), key=lambda x: order.get(x["bucket"], 9))


def cross_brand() -> list[dict]:
    rows = _rows(f"""
      WITH per_hash AS (
        SELECT customer_hash,
               COUNT(DISTINCT shop)  AS shops,
               MIN(shop)             AS only_shop,
               SUM(revenue_total)    AS revenue,
               SUM(orders_cnt)       AS orders
        FROM {D('customer_facts')}
        GROUP BY 1
      )
      SELECT
        CASE WHEN shops = 2 THEN 'both'
             WHEN only_shop = 'babyshop' THEN 'babyshop_only'
             ELSE 'lekmer_only' END AS grp,
        COUNT(*)          AS customers,
        AVG(revenue)      AS avg_revenue,
        AVG(orders)       AS avg_orders,
        SAFE_DIVIDE(SUM(revenue), SUM(orders)) AS aov
      FROM per_hash
      GROUP BY 1
    """)
    order = {"babyshop_only": 0, "lekmer_only": 1, "both": 2}
    return sorted(({"group": r["grp"], "customers": I(r["customers"]),
                    "avg_revenue_sek": I(r["avg_revenue"]),
                    "avg_orders": F(r["avg_orders"], 2), "aov_sek": I(r["aov"])}
                   for r in rows), key=lambda x: order.get(x["group"], 9))


def market_mix() -> list[dict]:
    """Current lifecycle state of each storefront's buyers. A customer who
    bought in two storefronts appears in both rows — the row reads 'of the
    people who have bought here, where are they now'."""
    rows = _rows(f"""
      WITH state AS (
        SELECT customer_hash, shop, CASE
          WHEN orders_cnt >= 2 AND days_since_last_order <= 90 THEN 'repeat_active'
          WHEN orders_cnt  = 1 AND days_since_last_order <= 90 THEN 'new'
          WHEN days_since_last_order <= 180                    THEN 'at_risk'
          ELSE 'lapsed' END AS state
        FROM {D('customer_facts')}
      ),
      buyers AS (
        SELECT DISTINCT o.app_key, o.customer_hash, o.shop
        FROM {D('customer_orders')} o
      )
      SELECT b.app_key,
        COUNT(*)                                  AS base,
        COUNTIF(s.state = 'new')                  AS new_cnt,
        COUNTIF(s.state = 'repeat_active')        AS repeat_active,
        COUNTIF(s.state = 'at_risk')              AS at_risk,
        COUNTIF(s.state = 'lapsed')               AS lapsed
      FROM buyers b
      JOIN state s ON s.customer_hash = b.customer_hash AND s.shop = b.shop
      GROUP BY 1
      ORDER BY base DESC
    """)
    return [{"app_key": r["app_key"], "base": I(r["base"]), "new": I(r["new_cnt"]),
             "repeat_active": I(r["repeat_active"]), "at_risk": I(r["at_risk"]),
             "lapsed": I(r["lapsed"])} for r in rows]


# ── Assembly ─────────────────────────────────────────────────────────────────
def build_payload() -> dict:
    cov = coverage()
    k = kpis()
    ao = aging_out()
    dist = lifestage_distribution()
    sized_total = sum(d["customers"] for d in dist)
    k["aging_out_share"] = ao["share_of_sized"]
    k["size_coverage"] = cov["size_coverage"]
    return {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "sources": {
            "norce": {"dataset": f"{BQ_PROJECT}.{BQ_DATASET}", "coverage": cov},
            "history_start": HISTORY_START,
            "identity": "customer_hash = SHA256(lower(trim(email))), per shop",
        },
        "kpis": k,
        "lifecycle": lifecycle(),
        "cohorts": cohorts(),
        "value_deciles": value_deciles(),
        "lifestage": {"distribution": dist, "sized_customers": sized_total,
                      "ltv_by_entry": ltv_by_entry_stage(), "aging_out": ao},
        "discount": discount_buckets(),
        "cross_brand": cross_brand(),
        "markets": market_mix(),
        "caveats": CAVEATS,
        "definitions": DEFINITIONS,
    }


def _check_size(payload: dict) -> int:
    n = len(json.dumps(payload, ensure_ascii=False))
    if n > DOC_BUDGET_BYTES:
        big = sorted(((len(json.dumps(v, ensure_ascii=False)), k)
                      for k, v in payload.items()), reverse=True)[:3]
        raise RuntimeError(
            f"Segments payload is {n:,} B, over the {DOC_BUDGET_BYTES:,} B budget. "
            "Biggest sections: " + ", ".join(f"{k} {s:,} B" for s, k in big)
            + ". Window the growing series — do not just raise the budget.")
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
    out = os.environ.get("SEGMENTS_OUT")
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(p, fh, ensure_ascii=False)
        print(f"   wrote {out} ({os.path.getsize(out):,} bytes)")
    where = "(skipped)" if os.environ.get("SKIP_FIRESTORE") else write_firestore(p)
    k, cov = p["kpis"], p["sources"]["norce"]["coverage"]
    print(f"✓ Segments refresh · {where} · {cov['from']}→{cov['to']} · "
          f"{k['customers']:,} customers · repeat {k['repeat_rate']} · "
          f"size coverage {k['size_coverage']} · "
          f"{len(p['lifecycle'])} lifecycle rows · {len(p['cohorts'])} cohort cells · "
          f"{p['lifestage']['sized_customers']:,} sized customers · "
          f"{len(json.dumps(p, ensure_ascii=False)):,} B · {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
