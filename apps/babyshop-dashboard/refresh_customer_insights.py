#!/usr/bin/env python3
"""
Customer Insights snapshot — BigQuery → Firestore.

Writes `funnel_cache/<workspace>__customer-insights`, the single document the
Customer Insights tab reads. Same client/auth wiring and the same
{data, fetched_at, expires_at, ttl_seconds, workspace} wrapper as bq_source.py.

TWO SOURCES, STRICTLY SPLIT BY ROLE
  norce   `project-a7ade44e-e7e3-4871-a83.norce` marts (norce_sync.py) — the ONLY
          source of customer counts, orders, revenue, cohorts and CLV. History
          from 2025-06-11.
  funnel  `babyshop-funnel-data.bs_funnel_export.funnel_data` — COST ONLY
          (plus sessions). Nothing else from this table reaches the payload.

WHY THE FUNNEL CUSTOMER COLUMNS ARE BANNED
  `newCustomers__File_Import` / `oldCustomers__File_Import` /
  `Orders_count__File_Import` are a pre-aggregated file import and they are
  WRONG: measured against Norce first-orders they undercount DK by ~80x and SE
  by ~9x. They produced a DK CAC of ~17,000 kr where the truth is ~185 kr. They
  must not appear in any metric. Norce is the customer source of record.
  This module reads those columns nowhere — if you are about to add them back,
  don't.

WHY CHANNEL-LEVEL CAC DOES NOT EXIST
  Norce carries no channel/UTM data (Source='WEB' on every order) and the Funnel
  export carries no order identity, so there is no key that links an acquisition
  to the channel that paid for it. `channel_spend` therefore reports SPEND ONLY.
  Order-level channel linkage is expected later via a Kuvio API — that is the
  seam: add the join in `channel_spend()` and fill in its `cac` field then.

COST GRAIN — MARKET, NOT SHOP (verified 2026-08-16)
  Funnel cost rows do carry `shop_new`, but not reliably: FI x Lekmer reports
  77 kr of spend over 90 days against 205 Norce first-orders, i.e. a CAC of
  0.38 kr. So all CAC is computed at MARKET grain. Rows that are market x shop
  (clv_1y_by_market, churn) carry the market CAC on the babyshop row only and
  NULL for lekmer, because ~93% of the market's spend is Babyshop's — applying
  it to Lekmer would be a fabrication.

FUNNEL RULES STILL IN FORCE FOR COST
  • Market is `market_level_1_kv`. NEVER `market_new` — NULL on 98.8% of cost rows.
  • The export is a union of DISJOINT feeds, so cost is aggregated in its own
    query at its own grain and joined to Norce on month x market. Never at row
    level.
  • Gross values throughout — returns and cancellations are ignored (user
    decision).

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

FIRESTORE_PROJECT = os.environ.get("FIRESTORE_PROJECT", "project-a7ade44e-e7e3-4871-a83")

NORCE_PROJECT = os.environ.get("NORCE_BQ_PROJECT", "project-a7ade44e-e7e3-4871-a83")
NORCE_DATASET = os.environ.get("NORCE_BQ_DATASET", "norce")

# Norce platform cutover — nothing exists before this, so it bounds every series.
NORCE_START = os.environ.get("NORCE_HISTORY_START", "2025-06-11")
WINDOW_DAYS = 90
LOOKBACK_365 = 365

# Cohort triangle bounds — see the cohorts query for why each exists.
COHORT_MAX_OFFSET = int(os.environ.get("CUSTOMER_INSIGHTS_COHORT_MAX_OFFSET", "12"))
COHORT_MIN_SIZE   = int(os.environ.get("CUSTOMER_INSIGHTS_COHORT_MIN_SIZE", "50"))

# Shop that owns the overwhelming majority of paid spend. Market-grain CAC is
# attached to this shop's rows only (see the module docstring).
PRIMARY_SHOP = os.environ.get("CUSTOMER_INSIGHTS_PRIMARY_SHOP", "babyshop")

# Both datasets are EU. Pinned explicitly rather than left to inference: when a
# referenced dataset is missing, BigQuery falls back to the client default (US)
# and reports a location mismatch instead of a plain "not found".
BQ_LOCATION = os.environ.get("BQ_LOCATION", "EU")

# Firestore's hard document limit is 1,048,576 bytes. The check measures COMPACT
# JSON, which deliberately over-states the real cost: Firestore charges
# string=bytes+1, number=8, bool/null=1, plus each map key per entry. Measured
# 2026-08-16 — compact JSON 690,779 B vs Firestore-accounted 608,252 B. So this
# budget trips before Firestore would reject the write, the safe direction.
# `trend` grows ~26 rows/month and `cohorts` ~170; when this trips, the fix is a
# rolling window on them, not a bigger number here.
DOC_BUDGET_BYTES = 900_000

# Billing currency per Google Ads account, read live from the API on 2026-08-16
# (customer.currency_code). Kept here as documentation of WHY no per-account
# conversion is applied to funnel Cost: the export has already done it. Note DK
# is split — Babyshop DK bills SEK while Lekmer DK bills DKK, which is what made
# the raw numbers look like a currency bug.
AD_ACCOUNT_CURRENCIES = {
    "Babyshop SE (4851485396)": "SEK", "Babyshop NO (8623945183)": "NOK",
    "Babyshop FI (6161399704)": "EUR", "Babyshop DK (2054294342)": "SEK",
    "Babyshop ROW (5541487401)": "SEK", "Lekmer SE (7780114635)": "SEK",
    "Lekmer NO (8308232278)": "NOK", "Lekmer DK (2756397225)": "DKK",
}

CAVEATS = [
    "all money is SEK — Norce order values are converted from local currency; "
    "funnel Cost is already SEK at source (verified against the Google Ads API)",
    "FX uses the latest spot rate applied to all history (constant-currency), "
    "so period comparisons are not distorted by rate moves; Norce's stored rates "
    "were last updated 2023-06-16 and NOK is ~4% low",
    "funnel Google Shopping cost is de-duplicated (the export carries the same "
    "spend as both a campaign total and a brand split in DK/FI/Lekmer slices)",
    "customer counts, orders and revenue are Norce — funnel customer columns "
    "deliberately unused (they undercount DK ~80x, SE ~9x); funnel contributes cost only",
    "channel-level CAC awaiting order-level channel data (Kuvio API) — "
    "channel_spend reports spend only",
    "funnel cost is market-grain; shop-level CAC is not available "
    "(FI x Lekmer cost implies a 0.38 kr CAC), so lekmer rows carry no CAC",
    "trend[].cost and trend[].cac are MARKET-level and repeat across the shop rows "
    "of a market — do not sum them across shops",
    "norce history from 2025-06-11 (platform cutover)",
    "gross values; returns ignored (user decision)",
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


# ════════════════════════════════════════════════════════════════════════════
#  Funnel Cost de-duplication — READ THIS BEFORE CHANGING ANY COST QUERY
#
#  `SUM(Cost)` over the export DOUBLE-COUNTS Google Shopping spend in some
#  slices. The export carries, for the same campaign and day, BOTH:
#     • a campaign-total row with kv_brand blank, AND
#     • brand-attributed rows that split exactly that same money
#  ...but only for some shop x market slices (DK, FI, and all of Lekmer). For
#  SE/NO Babyshop the brand split goes to `shopping_cost` instead and `Cost`
#  is already correct, which is why the bug hid for so long.
#
#  Verified against the Google Ads API for July 2026 (funnel value vs the
#  account's own cost, in the account's currency):
#     SE Babyshop  761,356.49 vs 761,356.49 SEK   ratio 1.0000  (exact, to the öre)
#     DK Babyshop   94,346.90 vs  94,346.90 SEK   ratio 1.0000  (exact)
#     SE Lekmer     26,549.08 vs  26,549.08 SEK   ratio 1.0000  (exact)
#     NO Babyshop  403,715.79 vs 412,160.31 NOK   ratio 0.9795
#     NO Lekmer     10,332.21 vs  10,548.32 NOK   ratio 0.9795  (same rate)
#     DK Lekmer      8,388.71 vs   5,657.73 DKK   ratio 1.4827
#     FI Babyshop   98,078.42 vs   8,849.91 EUR   ratio 11.083
#  Every ratio is that account's currency->SEK rate, and 11.083/1.4827 = 7.475,
#  the EUR/DKK peg. So Funnel Cost is ALREADY NORMALISED TO SEK — there is no
#  mixed-currency problem on the cost side, only this duplication.
#
#  THE RULE: aggregate to date x campaign x channel x market x shop, then if the
#  brand-split total equals the brand-blank total (within 1%), they are the same
#  money at two grains -> keep one. Otherwise they are genuinely different money
#  -> add them. The second branch matters: Meta cost is almost entirely
#  brand-attributed with no blank counterpart, and a naive "blank rows only"
#  filter would have deleted 96.5% of it.
#  Applying this leaves Affiliate / Naver / Price Comparison / Meta at 100% of
#  their naive totals and cuts Google Ads by 13.8%.
# ════════════════════════════════════════════════════════════════════════════
COST_GRAIN = ("Date, Campaign, Channel_Type_Level_1, Channel_Type_Level_2, "
              "market_level_1_kv, shop_new")
COST_BLANK = "SUM(IF(kv_brand IS NULL OR kv_brand = '', Cost, 0))"
COST_SPLIT = "SUM(IF(kv_brand IS NOT NULL AND kv_brand <> '', Cost, 0))"
COST_FIXED = ("IF(split > 0 AND blank > 0 "
              "AND ABS(split - blank) <= 0.01 * GREATEST(split, blank), "
              "blank, blank + split)")


def _cost_cte(where: str) -> str:
    """CTE `cost_rows` with a de-duplicated `cost` per COST_GRAIN slice."""
    return f"""
    cost_grain AS (
      SELECT {COST_GRAIN}, {COST_BLANK} blank, {COST_SPLIT} split
      FROM `{BQ_TABLE}` WHERE {where} AND Cost IS NOT NULL
      GROUP BY Date, Campaign, Channel_Type_Level_1, Channel_Type_Level_2,
               market_level_1_kv, shop_new),
    cost_rows AS (
      SELECT Date, Channel_Type_Level_1, Channel_Type_Level_2,
             market_level_1_kv, shop_new, {COST_FIXED} AS cost
      FROM cost_grain)
    """


def D() -> str:
    return f"{NORCE_PROJECT}.{NORCE_DATASET}"


# ── Window ───────────────────────────────────────────────────────────────────
def data_end() -> datetime.date:
    """Last date BOTH sources cover.

    Taking the earlier of the two maxima keeps the 90-day window honest: a
    Norce day with no matching funnel cost would inflate CAC's denominator,
    and a funnel day with no Norce orders would inflate its numerator.
    """
    f = _rows(f"SELECT MAX(Date) d FROM `{BQ_TABLE}` WHERE Cost IS NOT NULL AND Cost > 0")
    n = _rows(f"SELECT MAX(order_date) d FROM `{D()}.customer_orders`")
    fd = f[0]["d"] if f and f[0]["d"] else None
    nd = n[0]["d"] if n and n[0]["d"] else None
    if fd and nd:
        return min(fd, nd)
    return fd or nd or (datetime.date.today() - datetime.timedelta(days=1))


# ── Funnel: COST ONLY ────────────────────────────────────────────────────────
def cost_by_market_month(start: datetime.date, end: datetime.date) -> dict:
    """{(month, market): {cost, sessions}} — the only funnel read for the trend."""
    # Sessions come from their own feed rows and are NOT duplicated, so they are
    # summed separately and joined on month x market — never mixed into the
    # de-duplication, which would change what "blank vs split" means.
    sql = f"""
    WITH {_cost_cte("Date BETWEEN @s AND @e AND market_level_1_kv IS NOT NULL")},
    c AS (
      SELECT FORMAT_DATE('%Y-%m', Date) month, market_level_1_kv market,
             SUM(cost) cost
      FROM cost_rows GROUP BY 1, 2),
    s AS (
      SELECT FORMAT_DATE('%Y-%m', Date) month, market_level_1_kv market,
             SUM(Sessions) sessions
      FROM `{BQ_TABLE}`
      WHERE Date BETWEEN @s AND @e AND market_level_1_kv IS NOT NULL
        AND Sessions IS NOT NULL
      GROUP BY 1, 2)
    SELECT COALESCE(c.month, s.month) month, COALESCE(c.market, s.market) market,
           c.cost, s.sessions
    FROM c FULL OUTER JOIN s USING (month, market)
    """
    # None, not 0, when a feed reports nothing for that cell: "no session rows"
    # and "zero sessions" are different claims and the UI renders them differently.
    return {(r["month"], r["market"]): {
                "cost": I(r["cost"]) if r["cost"] is not None else None,
                "sessions": I(r["sessions"]) if r["sessions"] is not None else None}
            for r in _rows(sql, [_d("s", start), _d("e", end)])}


def cost_by_market(start: datetime.date, end: datetime.date) -> dict:
    sql = f"""
    WITH {_cost_cte("Date BETWEEN @s AND @e AND market_level_1_kv IS NOT NULL")}
    SELECT market_level_1_kv market, SUM(cost) cost FROM cost_rows GROUP BY 1
    """
    return {r["market"]: I(r["cost"]) for r in _rows(sql, [_d("s", start), _d("e", end)])}


def channel_spend(start: datetime.date, end: datetime.date) -> list[dict]:
    """market x channel_l1 SPEND. No customer counts and no CAC, by design.

    THE KUVIO SEAM: when order-level channel data lands, join acquisitions here
    on market x channel and populate `cac`. Until then it is explicitly null so
    the UI can render "awaiting order-level channel data" rather than a number
    nobody can defend.
    """
    sql = f"""
    WITH {_cost_cte("Date BETWEEN @s AND @e AND market_level_1_kv IS NOT NULL")},
    ch AS (
      -- The filter lives OUTSIDE this CTE: `HAVING SUM(cost) > 0` would resolve
      -- `cost` to the SELECT alias, i.e. SUM(SUM(cost)), which BigQuery rejects
      -- as an aggregation of an aggregation.
      SELECT market_level_1_kv market, Channel_Type_Level_1 channel_l1, SUM(cost) cost
      FROM cost_rows GROUP BY 1, 2)
    SELECT market, channel_l1, cost,
           SAFE_DIVIDE(cost, SUM(cost) OVER (PARTITION BY market)) share_of_market_spend
    FROM ch WHERE cost > 0 ORDER BY market, cost DESC
    """
    return [{"market": r["market"], "channel_l1": r["channel_l1"],
             "cost_90d": I(r["cost"]),
             "share_of_market_spend": F(r["share_of_market_spend"], 4),
             "cac": None,
             "note": "awaiting order-level channel data (Kuvio API)"}
            for r in _rows(sql, [_d("s", start), _d("e", end)])]


# ── Norce: every customer number ─────────────────────────────────────────────
def norce_available() -> bool:
    """True only if the marts exist AND customer_orders actually has rows."""
    try:
        r = _rows(f"SELECT COUNT(*) n FROM `{D()}.customer_orders`")
        return bool(r and r[0]["n"])
    except Exception as e:
        print(f"   norce marts unavailable: {type(e).__name__}: {str(e)[:160]}")
        return False


def norce_window(start: datetime.date, end: datetime.date) -> list[dict]:
    """market x shop customer counts over an arbitrary window.

    new / returning split exactly as monthly_customer_metrics does it — by
    whether the customer's first-ever order predates the window — so total is
    always new + returning with no double counting.
    """
    sql = f"""
    SELECT o.market, o.shop,
           COUNT(DISTINCT o.customer_hash) total_customers,
           COUNT(DISTINCT IF(f.first_order_date >= @s, o.customer_hash, NULL)) new_customers,
           COUNT(DISTINCT IF(f.first_order_date <  @s, o.customer_hash, NULL)) returning_customers,
           COUNT(*) orders, SUM(o.order_value) revenue
    FROM `{D()}.customer_orders` o
    JOIN `{D()}.customer_facts` f USING (customer_hash, shop)
    WHERE o.order_date BETWEEN @s AND @e
    GROUP BY 1, 2
    """
    return [{"market": r["market"], "shop": r["shop"],
             "total_customers": I(r["total_customers"]),
             "new_customers": I(r["new_customers"]),
             "returning_customers": I(r["returning_customers"]),
             "orders": I(r["orders"]), "revenue": I(r["revenue"])}
            for r in _rows(sql, [_d("s", start), _d("e", end)])]


def orders_per_customer_12m(end: datetime.date) -> tuple[float | None, dict]:
    """Trailing-365d purchases per ACTIVE customer.

    Denominator is customers with >= 1 order in the window — NOT the all-time
    base and NOT the matured-cohort base that `orders_per_customer` uses. The
    per-row variant is keyed on first_market so it describes the same population
    as the clv/churn row it is attached to.
    """
    start = end - datetime.timedelta(days=LOOKBACK_365 - 1)
    g = _rows(f"""
        SELECT COUNT(*) orders, COUNT(DISTINCT customer_hash) custs
        FROM `{D()}.customer_orders` WHERE order_date BETWEEN @s AND @e""",
              [_d("s", start), _d("e", end)])
    overall = F(g[0]["orders"] / g[0]["custs"]) if g and g[0]["custs"] else None
    per = {}
    for r in _rows(f"""
        SELECT f.first_market market, o.shop,
               COUNT(*) orders, COUNT(DISTINCT o.customer_hash) custs
        FROM `{D()}.customer_orders` o
        JOIN `{D()}.customer_facts` f USING (customer_hash, shop)
        WHERE o.order_date BETWEEN @s AND @e
        GROUP BY 1, 2""", [_d("s", start), _d("e", end)]):
        if r["custs"]:
            per[(r["market"], r["shop"])] = F(r["orders"] / r["custs"])
    return overall, per


def build_trend(cost_month: dict) -> list[dict]:
    """month x market x shop from Norce, with that month's MARKET funnel cost.

    Cost and CAC are MARKET-level: the same value appears on every shop row of
    a market, because funnel cost cannot be split by shop reliably. Summing
    `cost` across shops would double it — the caveat says so, and `meta` flags
    it. The customer columns are pure Norce and DO sum correctly.
    """
    rows = [{"month": str(r["month"])[:7], "market": r["market"], "shop": r["shop"],
             "new_customers": I(r["new_customers"]),
             "returning_customers": I(r["returning_customers"]),
             "total_customers": I(r["active_customers"]),
             "orders": I(r["orders"]), "revenue": I(r["revenue"]),
             "aov": F(r["aov"]), "repeat_rate": F(r["repeat_rate"], 4),
             "orders_per_customer_month": F(r["orders"] / r["active_customers"])
                                          if r["active_customers"] else None}
            for r in _rows(f"SELECT * FROM `{D()}.monthly_customer_metrics` "
                           f"ORDER BY month, market, shop")]
    # Market-level new totals drive the market CAC, so a market's CAC is the
    # same regardless of which shop row it is read from.
    new_by_mm: dict[tuple, int] = {}
    for r in rows:
        k = (r["month"], r["market"])
        new_by_mm[k] = new_by_mm.get(k, 0) + r["new_customers"]
    for r in rows:
        k = (r["month"], r["market"])
        c = cost_month.get(k) or {}
        cost, new = c.get("cost"), new_by_mm.get(k, 0)
        r["cost"] = cost
        r["sessions"] = c.get("sessions")
        r["cac"] = F(cost / new) if (cost and new) else None
    return rows


def fx_rates() -> dict:
    """The rates the marts actually used, for meta.fx. Never fails the run.

    Single rate source: the `currency_rates` view, built from Norce's own
    Core/Currencies.ExchangeRate. Read here rather than recomputed so the
    snapshot can never disagree with the marts about what a number means.
    """
    try:
        return {r["currency_code"]: F(r["to_sek"], 6)
                for r in _rows(f"SELECT currency_code, to_sek FROM `{D()}.currency_rates` "
                               f"WHERE to_sek IS NOT NULL ORDER BY currency_code")}
    except Exception as e:
        print(f"   fx rates unavailable: {type(e).__name__}: {str(e)[:120]}")
        return {}


def norce_payload(win_start: datetime.date, end: datetime.date,
                  market_cac: dict, opc_per: dict) -> dict:
    cohorts = [{"cohort_month": str(r["cohort_month"]), "market": r["market"], "shop": r["shop"],
                "months_since_first": I(r["months_since_first"]),
                "cohort_size": I(r["cohort_size"]),
                "active_customers": I(r["active_customers"]),
                "retention_rate": F(r["retention_rate"], 4),
                "cumulative_revenue_per_customer": F(r["cumulative_revenue_per_customer"])}
               for r in _rows(f"SELECT * FROM `{D()}.cohort_retention` "
                              f"WHERE months_since_first <= {COHORT_MAX_OFFSET} "
                              f"AND cohort_size >= {COHORT_MIN_SIZE} "
                              f"ORDER BY cohort_month, market, shop, months_since_first")]

    def cac_for(market, shop):
        # Market CAC belongs to the shop that spent the money. Attaching a
        # Babyshop-dominated CAC to a Lekmer row would invent a number.
        return market_cac.get(market) if shop == PRIMARY_SHOP else None

    clv = []
    for r in _rows(f"""
        SELECT first_market market, shop, COUNT(*) customers,
               COUNTIF(is_repeat) returning,
               AVG(revenue_365d_from_first) clv_1y, AVG(aov) aov, AVG(orders_cnt) opc
        FROM `{D()}.customer_facts`
        WHERE revenue_365d_from_first IS NOT NULL
        GROUP BY 1, 2 ORDER BY customers DESC"""):
        cac = cac_for(r["market"], r["shop"])
        clv_1y = F(r["clv_1y"])
        clv.append({"market": r["market"], "shop": r["shop"], "customers": I(r["customers"]),
                    "returning_customers": I(r["returning"]), "clv_1y": clv_1y,
                    "aov": F(r["aov"]), "orders_per_customer": F(r["opc"]),
                    "orders_per_customer_12m": opc_per.get((r["market"], r["shop"])),
                    "cac": cac,
                    "ltv_cac_ratio": F(clv_1y / cac) if (clv_1y and cac) else None})

    churn = []
    for r in _rows(f"""
        SELECT first_market market, shop, COUNT(*) customers,
               COUNTIF(is_repeat) returning,
               COUNTIF(days_since_last_order > 365) lapsed,
               SAFE_DIVIDE(COUNTIF(days_since_last_order > 365), COUNT(*)) churn,
               SAFE_DIVIDE(COUNTIF(is_repeat), COUNT(*)) repeat_rate,
               AVG(orders_cnt) opc
        FROM `{D()}.customer_facts`
        WHERE first_order_date <= DATE_SUB(CURRENT_DATE(), INTERVAL 365 DAY)
        GROUP BY 1, 2 ORDER BY customers DESC"""):
        churn.append({"market": r["market"], "shop": r["shop"], "customers": I(r["customers"]),
                      "returning_customers": I(r["returning"]), "lapsed_12m": I(r["lapsed"]),
                      "churn_12m": F(r["churn"], 4), "repeat_rate": F(r["repeat_rate"], 4),
                      "orders_per_customer": F(r["opc"]),
                      "orders_per_customer_12m": opc_per.get((r["market"], r["shop"])),
                      "cac": cac_for(r["market"], r["shop"])})

    def fpp(dim_type, limit=200):
        return [{"month": str(r["month"]), "market": r["market"], "shop": r["shop"],
                 "name": r["dim_value"], "new_customers": I(r["new_customer_orders"]),
                 "share_of_first_orders": F(r["order_share"], 4),
                 "revenue": I(r["revenue"]), "revenue_share": F(r["revenue_share"], 4)}
                for r in _rows(f"""
            SELECT * FROM `{D()}.first_purchase_products`
            WHERE dim_type = @t ORDER BY new_customer_orders DESC LIMIT {limit}""",
                               [bigquery.ScalarQueryParameter("t", "STRING", dim_type)])]

    last_sync = _rows(f"SELECT CAST(MAX(run_at) AS STRING) t FROM `{D()}.sync_state`")
    return {
        "cohorts": cohorts,
        "clv_1y_by_market": clv,
        "churn": churn,
        "first_purchase_products": {"brands": fpp("brand"), "categories": fpp("category_l1"),
                                    "products": fpp("product")},
        "last_sync": last_sync[0]["t"] if last_sync else None,
    }


# ── Payload ──────────────────────────────────────────────────────────────────
def build_payload() -> dict:
    if not norce_available():
        raise RuntimeError(
            "Norce marts are empty or missing — every customer metric now comes "
            "from Norce, so there is nothing to publish. Run norce_sync.py first.")

    end = data_end()
    start = datetime.date.fromisoformat(NORCE_START)
    win_start = end - datetime.timedelta(days=WINDOW_DAYS - 1)

    cost_month = cost_by_market_month(start, end)
    market_cost = cost_by_market(win_start, end)
    trend = build_trend(cost_month)
    win = norce_window(win_start, end)
    opc_12m, opc_per = orders_per_customer_12m(end)

    # ── cac_matrix: MARKET grain, the authoritative CAC surface ──
    by_market: dict[str, dict] = {}
    for r in win:
        m = by_market.setdefault(r["market"], {"new": 0, "ret": 0, "tot": 0})
        m["new"] += r["new_customers"]
        m["ret"] += r["returning_customers"]
        m["tot"] += r["total_customers"]
    cac_matrix = []
    for market in sorted(set(by_market) | set(market_cost),
                         key=lambda m: -market_cost.get(m, 0)):
        v = by_market.get(market, {"new": 0, "ret": 0, "tot": 0})
        cost = market_cost.get(market, 0)
        cac_matrix.append({"market": market, "cost_90d": cost,
                           "new_customers_90d": v["new"],
                           "returning_customers_90d": v["ret"],
                           "total_customers_90d": v["tot"],
                           # A market with no recorded spend (EU/ASIA/NA carry
                           # none) has an UNKNOWN CAC, not a zero one — 0.0
                           # would claim acquisition there is free.
                           "cac": F(cost / v["new"]) if (cost and v["new"]) else None})
    market_cac = {r["market"]: r["cac"] for r in cac_matrix if r["cac"] is not None}

    norce = norce_payload(win_start, end, market_cac, opc_per)
    fx = fx_rates()

    # ── KPIs ──
    months = sorted({r["month"] for r in trend})
    mtd_key = end.strftime("%Y-%m")
    prev_key = (end.replace(day=1) - datetime.timedelta(days=1)).strftime("%Y-%m")
    sum_new = lambda mk: sum(r["new_customers"] for r in trend if r["month"] == mk)

    new_90 = sum(v["new"] for v in by_market.values())
    ret_90 = sum(v["ret"] for v in by_market.values())
    cost_90 = sum(market_cost.values())

    clv_1y = None
    if norce["clv_1y_by_market"]:
        n = sum(r["customers"] for r in norce["clv_1y_by_market"] if r["clv_1y"] is not None)
        if n:
            clv_1y = round(sum(r["clv_1y"] * r["customers"]
                               for r in norce["clv_1y_by_market"] if r["clv_1y"] is not None) / n, 2)
    blended_cac = round(cost_90 / new_90, 2) if new_90 else None
    repeat_12m = churn_12m = None
    if norce["churn"]:
        cn = sum(r["customers"] for r in norce["churn"])
        if cn:
            churn_12m = round(sum(r["lapsed_12m"] for r in norce["churn"]) / cn, 4)
            repeat_12m = round(sum((r["repeat_rate"] or 0) * r["customers"]
                                   for r in norce["churn"]) / cn, 4)

    return {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "sources": {
            "norce": {"role": "customers, orders, revenue, cohorts, CLV",
                      "from": NORCE_START, "to": end.isoformat(),
                      "available": True, "last_sync": norce["last_sync"]},
            "funnel": {"role": "cost only (and sessions)",
                       "from": NORCE_START, "to": end.isoformat(),
                       "note": "customer columns deliberately unused — they undercount "
                               "DK ~80x and SE ~9x versus Norce first-orders"},
        },
        "meta": {
            "customer_source": "norce",
            "cost_source": "funnel",
            "cost_grain": "market",
            "shop_grain_cost_reliable": False,
            "shop_grain_cost_evidence": "FI x Lekmer = 77 kr / 90d against 205 first-orders "
                                        "(CAC 0.38 kr)",
            "cac_shop_scope": f"market CAC attached to '{PRIMARY_SHOP}' rows only; null for others",
            "kpi_shop_scope": "all shops",
            "trend_cost_is_market_level": True,
            "channel_cac_available": False,
            "channel_cac_blocker": "no order-level channel data; awaiting Kuvio API",
            "window_days": WINDOW_DAYS,
            "window": {"start": win_start.isoformat(), "end": end.isoformat()},
            "months": months,
            "currency": "SEK",
            "fx": {
                "reporting_currency": "SEK",
                "rates_to_sek": fx,
                "rate_source": "norce Core/Currencies.ExchangeRate via the currency_rates "
                               "view — the single rate source; the marts convert with it too",
                "rate_staleness": "Norce's non-EUR rows were last updated 2023-06-16; vs rates "
                                  "derived from live Google Ads spend on 2026-08-16, NOK is 4.0% "
                                  "low, EUR 0.7%, DKK 0.6%. Refresh Core/Currencies upstream to "
                                  "fix every consumer at once",
                "norce_revenue": "converted — Norce order values are LOCAL per "
                                 "storefront (NOK/DKK/EUR/USD/GBP); marts multiply by "
                                 "currency_rates.to_sek",
                "funnel_cost": "NOT converted — already SEK at source. Verified against the "
                               "Google Ads API: for the three SEK-billing accounts the funnel "
                               "figure equals the account's own cost EXACTLY (SE Babyshop and "
                               "SE Lekmer and DK Babyshop, to the öre), and for every non-SEK "
                               "account the ratio is that currency's SEK rate (NOK 0.9795, "
                               "DKK 1.4827, EUR 11.083; 11.083/1.4827 = 7.475 = the EUR/DKK peg). "
                               "Converting it again would double-convert",
                "funnel_cost_non_google": "Meta / Affiliate / Naver / Price Comparison (~10% of "
                                          "spend) carry no account identifier. Assumed SEK like "
                                          "Google: their magnitudes are consistent with SEK and "
                                          "the export normalises cost centrally rather than "
                                          "per-connector",
                "funnel_cost_dedup": "Google Shopping spend is duplicated in `Cost` "
                                     "(campaign-total row + brand-split rows) for DK, FI "
                                     "and all Lekmer slices; de-duplicated per "
                                     "date x campaign x channel x market x shop",
                "rate_basis": "latest spot rate applied to all history (constant-currency), "
                              "so a market's trend is not partly an FX chart",
                "ad_account_currencies": AD_ACCOUNT_CURRENCIES,
            },
            "definitions": {
                "orders_per_customer_12m": "orders in the trailing 365d / distinct customers "
                                           "with >=1 order in that window (active base)",
                "orders_per_customer": "lifetime orders per customer over that row's cohort "
                                       "base (matured cohorts only)",
                "orders_per_customer_month": "that month's orders / that month's active customers",
                "new_share_90d": "norce new / (new + returning) over the 90d window",
            },
        },
        "kpis": {
            "new_customers_mtd": sum_new(mtd_key),
            "new_customers_prev_month": sum_new(prev_key),
            "new_share_90d": round(new_90 / (new_90 + ret_90), 4) if (new_90 + ret_90) else None,
            "blended_cac_90d": blended_cac,
            "orders_per_customer_12m": opc_12m,
            "clv_1y": clv_1y,
            "ltv_cac_ratio": round(clv_1y / blended_cac, 2) if (clv_1y and blended_cac) else None,
            "repeat_rate_12m": repeat_12m,
            "churn_12m": churn_12m,
        },
        "trend": trend,
        "cac_matrix": cac_matrix,
        "channel_spend": channel_spend(win_start, end),
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
          f"{p['sources']['norce']['from']}→{p['sources']['norce']['to']} · "
          f"new MTD {k['new_customers_mtd']:,} (prev {k['new_customers_prev_month']:,}) · "
          f"new-share 90d {k['new_share_90d']} · blended CAC {k['blended_cac_90d']} · "
          f"orders/cust 12m {k['orders_per_customer_12m']} · "
          f"{len(p['trend'])} trend rows · {len(p['cac_matrix'])} markets · "
          f"{len(p['channel_spend'])} channel-spend rows · {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
