#!/usr/bin/env python3
"""
Bluebird-warehouse data layer for the Babyshop dashboard (parallel run).

Replaces bq_source.py: same payload shapes, same function surface consumed by
app.py (_kv_dim / _kv_filtered / filtered_daily / _prod_dim), but every number
comes from the clean dbt marts in `claude-private-499703.babyshop_marts`
instead of the Funnel export.

DELIBERATELY ABSENT — do not reintroduce:
  • The Funnel Cost de-duplication (COST_GRAIN/COST_FIXED). The marts carry
    per-platform spend facts with no double counting; running the dedup here
    would subtract real money.
  • The ROW-bucket market resolution (KV_MARKET). The ladder carries real
    market codes plus explicit ROW/EU buckets.

CONVENTION MAPPING (validated against the old pipeline, May–Jul 2026, ≤0.5%):
  ladder gp1 is GROSS of returns (revenue − cogs); returns sit in ops_cost as
  cost_returns. The dashboard's Finance-net convention is derived per query:
      net_rev = revenue − cost_returns
      gp1     = gp1     − cost_returns
  gp2 needs no correction. gp3 = gp2 − ad_spend, computed here (not the
  ladder's gp3 column) so the formula is identical to the old dashboard's.

AD SPEND lives on the ladder rows (`ad_spend`, allocated by the dbt build and
conservation-tested upstream). Until the Google Ads backfill + dbt run land,
it under-reports (Meta only) — the payload's `source` field flags this page as
the parallel run either way.
"""
from __future__ import annotations
import datetime, os
from google.cloud import bigquery

MART_PROJECT = os.environ.get("MART_PROJECT", "claude-private-499703")
MART_DATASET = os.environ.get("MART_DATASET", "babyshop_marts")
LADDER   = f"`{MART_PROJECT}.{MART_DATASET}.agg_daily_profit_ladder`"
PRODUCTS = f"`{MART_PROJECT}.{MART_DATASET}.agg_daily_kuvio_products`"
GADS_PRODUCTS = f"`{MART_PROJECT}.{MART_DATASET}.agg_daily_kpis_by_gads_product`"
SOURCE = "bluebird-marts"
LONG_START = datetime.date(2025, 1, 1)

SV_MON = ["jan","feb","mar","apr","maj","jun","jul","aug","sep","okt","nov","dec"]
def _lbl(s: datetime.date, e: datetime.date) -> str:
    return (f"{s.day} {SV_MON[s.month-1]} – {e.day} {SV_MON[e.month-1]} {e.year}"
            if s.year == e.year else
            f"{s.day} {SV_MON[s.month-1]} {s.year} – {e.day} {SV_MON[e.month-1]} {e.year}")
def _dm(s: str) -> str:
    y, m, d = map(int, s.split("-")); return f"{d}/{m}"
def I(v): return int(round(float(v or 0)))


def _credentials():
    """ADC in production; gcloud user token as a local fallback (see bq_source)."""
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
        _client = bigquery.Client(project=MART_PROJECT, credentials=_credentials())
    return _client


def _rows(sql, params=()):
    job = bq().query(sql, job_config=bigquery.QueryJobConfig(query_parameters=list(params)))
    return list(job.result())


def _p(cs, ce, ps, pe):
    return [bigquery.ScalarQueryParameter(n, "DATE", v) for n, v in
            [("cs", cs), ("ce", ce), ("ps", ps), ("pe", pe)]]


# The old dashboard passes bq_source's legacy column tokens straight through
# app.py; map them onto the ladder's columns so app.py needs only the import
# swap. Values are (SQL expression, filter column) pairs — dims group by a
# display-friendly expression but filters must hit the same one.
_KV_COLS = {
    "market_level_1_kv": "market",
    "shop_new":          "COALESCE(NULLIF(shop_display, ''), shop)",
    "Channel_Type_Level_2": "COALESCE(NULLIF(channel_display, ''), channel)",
    # canonical short names, so new code doesn't have to use the legacy tokens
    "market":  "market",
    "shop":    "COALESCE(NULLIF(shop_display, ''), shop)",
    "channel": "COALESCE(NULLIF(channel_display, ''), channel)",
}

# Net-of-returns KV aggregate — the single place the metric formulas live.
KVD = ("SUM(revenue) rev, "
       "SUM(revenue) - COALESCE(SUM(cost_returns), 0) net_rev, "
       "SUM(gp1) - COALESCE(SUM(cost_returns), 0) gp1, "
       "SUM(gp2) gp2, "
       "SUM(gp2) - COALESCE(SUM(ad_spend), 0) gp3, "
       "CAST(SUM(orders) AS INT64) txns, "
       "SUM(ad_spend) cost")


def _kv_conds(filters):
    conds, params = [], []
    if not filters:
        return conds, params
    for i, dim in enumerate(d for d in ("market", "shop", "channel") if (filters or {}).get(d)):
        pname = f"f{i}"
        conds.append(f"{_KV_COLS[dim]} = @{pname}")
        params.append(bigquery.ScalarQueryParameter(pname, "STRING", filters[dim]))
    return conds, params


def _merge_kv(cur, prev, rev_prev_key="revenue_prev"):
    pm = {r["name"]: r for r in prev}
    out = []
    for r in cur:
        p = pm.get(r["name"])
        out.append({"name": r["name"], "revenue": I(r["rev"]), "net_revenue": I(r["net_rev"]),
                    "gp1": I(r["gp1"]), "gp2": I(r["gp2"]), "gp3": I(r["gp3"]),
                    "cost": I(r["cost"]),
                    rev_prev_key: I(p["rev"]) if p else None,
                    "net_revenue_prev": I(p["net_rev"]) if p else None,
                    "gp1_prev": I(p["gp1"]) if p else None,
                    "gp2_prev": I(p["gp2"]) if p else None,
                    "gp3_prev": I(p["gp3"]) if p else None,
                    "cost_prev": I(p["cost"]) if p else None})
    return out


def _kv_dim(col, cs, ce, ps, pe, rev_prev_key="revenue_prev", filters=None):
    """Per-value KV totals for one dimension (legacy or canonical column token)."""
    expr = _KV_COLS[col]
    fconds, fparams = _kv_conds(filters)
    where_extra = ("" if not fconds else " AND " + " AND ".join(fconds))
    def q(s, e):
        sql = (f"SELECT {expr} AS name, {KVD} FROM {LADDER} "
               f"WHERE date BETWEEN @cs AND @ce AND {expr} IS NOT NULL{where_extra} "
               f"GROUP BY name HAVING rev > 0 OR cost <> 0 ORDER BY rev DESC")
        return _rows(sql, _p(s, e, s, e) + fparams)
    return _merge_kv(q(cs, ce), q(ps, pe), rev_prev_key)


def _kv_filtered(filters, cs, ce, ps, pe):
    fconds, params = _kv_conds(filters)
    where_extra = ("" if not fconds else " AND " + " AND ".join(fconds))
    def q(s, e):
        p = [bigquery.ScalarQueryParameter("cs", "DATE", s),
             bigquery.ScalarQueryParameter("ce", "DATE", e)] + params
        rows = _rows(f"SELECT {KVD} FROM {LADDER} "
                     f"WHERE date BETWEEN @cs AND @ce{where_extra}", p)
        r = rows[0] if rows else None
        z = lambda k: I(r[k]) if r and r[k] is not None else 0
        return {"revenue": z("rev"), "net_revenue": z("net_rev"), "gp1": z("gp1"),
                "gp2": z("gp2"), "gp3": z("gp3"), "txns": z("txns"), "cost": z("cost")}
    return {"cur": q(cs, ce), "prev": q(ps, pe)}


def filtered_daily(filters, start: datetime.date, end: datetime.date):
    fconds, fparams = _kv_conds(filters)
    where_extra = ("" if not fconds else " AND " + " AND ".join(fconds))
    params = [bigquery.ScalarQueryParameter("start", "DATE", start),
              bigquery.ScalarQueryParameter("end", "DATE", end)] + fparams
    rows = _rows(f"SELECT CAST(date AS STRING) d, {KVD} FROM {LADDER} "
                 f"WHERE date BETWEEN @start AND @end{where_extra} "
                 f"GROUP BY d ORDER BY d", params)
    return [{"d": r["d"], "rev": I(r["rev"]), "net_rev": I(r["net_rev"]),
             "gp1": I(r["gp1"]), "gp2": I(r["gp2"]),
             "gp3": I(r["gp3"]), "cost": I(r["cost"])} for r in rows]


# ── Product marts ────────────────────────────────────────────────────────────
# Product P&L comes from the Kuvio order feed (per-brand/category COGS & GP),
# and product GP3 subtracts Google Shopping spend from the gads product mart,
# joined on lowercased brand. Categories carry no clean spend attribution in
# the warehouse yet, so category gp3 = gp2 (flagged via the payload source).

def _merge_prod(cur, prev):
    pm = {r["name"]: r for r in prev}
    out = []
    for r in cur:
        p = pm.get(r["name"])
        out.append({"name": r["name"], "revenue": I(r["rev"]), "cogs": I(r["cogs"]),
                    "gp1": I(r["gp1"]), "gp2": I(r["gp2"]), "gp3": I(r["gp3"]),
                    "revenue_prev": I(p["rev"]) if p else None,
                    "gp1_prev": I(p["gp1"]) if p else None,
                    "gp2_prev": I(p["gp2"]) if p else None,
                    "gp3_prev": I(p["gp3"]) if p else None})
    return out


_PROD_DIMS = {
    # legacy tokens from the old bq_source call sites, plus canonical names
    "LOWER(kv_brand)": "brand",
    "Product_type_2":  "category",
    "brand":           "brand",
    "category":        "category",
}


def _prod_dim(col, cs, ce, ps, pe, limit=1000):
    kind = _PROD_DIMS[col]
    if kind == "brand":
        def q(s, e):
            sql = f"""
            WITH p AS (
              SELECT LOWER(NULLIF(brand, '')) b, SUM(revenue) rev, SUM(cogs) cogs,
                     SUM(gp1) gp1, SUM(gp2) gp2
              FROM {PRODUCTS} WHERE date BETWEEN @cs AND @ce GROUP BY b),
            sp AS (
              SELECT LOWER(NULLIF(product_brand, '')) b, SUM(ad_spend) spend
              FROM {GADS_PRODUCTS} WHERE date BETWEEN @cs AND @ce GROUP BY b)
            SELECT COALESCE(p.b, 'Uncategorised') name, p.rev, p.cogs, p.gp1, p.gp2,
                   p.gp2 - COALESCE(sp.spend, 0) gp3
            FROM p LEFT JOIN sp USING (b)
            WHERE p.rev > 0 ORDER BY p.rev DESC LIMIT {limit}"""
            return _rows(sql, _p(s, e, s, e))
    else:
        def q(s, e):
            sql = f"""
            SELECT COALESCE(NULLIF(category, ''), 'Uncategorised') name,
                   SUM(revenue) rev, SUM(cogs) cogs, SUM(gp1) gp1, SUM(gp2) gp2,
                   SUM(gp2) gp3
            FROM {PRODUCTS} WHERE date BETWEEN @cs AND @ce
            GROUP BY name HAVING rev > 0 ORDER BY rev DESC LIMIT {limit}"""
            return _rows(sql, _p(s, e, s, e))
    return _merge_prod(q(cs, ce), q(ps, pe))


def _prod_daily(start: datetime.date, end: datetime.date):
    sql = f"""
    WITH p AS (
      SELECT date, SUM(revenue) rev, SUM(cogs) cogs, SUM(gp1) gp1, SUM(gp2) gp2
      FROM {PRODUCTS} WHERE date BETWEEN @cs AND @ce GROUP BY date),
    sp AS (
      SELECT date, SUM(ad_spend) spend FROM {GADS_PRODUCTS}
      WHERE date BETWEEN @cs AND @ce GROUP BY date)
    SELECT CAST(p.date AS STRING) d, p.rev, p.cogs, p.gp1, p.gp2,
           p.gp2 - COALESCE(sp.spend, 0) gp3
    FROM p LEFT JOIN sp ON p.date = sp.date ORDER BY d"""
    return _rows(sql, _p(start, end, start, end))


# ── Payload builders (same shapes bq_source produced) ────────────────────────
def build_payloads(cur_end: datetime.date | None = None):
    ce = cur_end or (datetime.date.today() - datetime.timedelta(days=1))
    cs = ce - datetime.timedelta(days=29)
    pe = cs - datetime.timedelta(days=1)
    ps = pe - datetime.timedelta(days=29)

    long_rows = _rows(
        f"SELECT CAST(date AS STRING) d, {KVD} FROM {LADDER} "
        f"WHERE date BETWEEN @cs AND @ce GROUP BY d ORDER BY d",
        [bigquery.ScalarQueryParameter("cs", "DATE", LONG_START),
         bigquery.ScalarQueryParameter("ce", "DATE", ce)])

    def in_range(ds, a, b): return a.isoformat() <= ds <= b.isoformat()
    dcur  = [r for r in long_rows if in_range(r["d"], cs, ce)]
    dprev = [r for r in long_rows if in_range(r["d"], ps, pe)]
    kv_tot = lambda rs: {"revenue": I(sum(r["rev"] or 0 for r in rs)),
                         "net_revenue": I(sum(r["net_rev"] or 0 for r in rs)),
                         "gp1": I(sum(r["gp1"] or 0 for r in rs)),
                         "gp2": I(sum(r["gp2"] or 0 for r in rs)),
                         "gp3": I(sum(r["gp3"] or 0 for r in rs)),
                         "txns": I(sum(r["txns"] or 0 for r in rs)),
                         "adCost": I(sum(r["cost"] or 0 for r in rs))}
    daily = [{"d": _dm(r["d"]), "rev": I(r["rev"]), "net_rev": I(r["net_rev"]),
              "gp1": I(r["gp1"]), "gp2": I(r["gp2"]),
              "gp3": I(r["gp3"]), "cost": I(r["cost"])} for r in dcur]
    weeks = []
    for wi in range(0, len(dcur), 7):
        c = dcur[wi:wi+7]
        weeks.append({"label": f"V{wi//7+1} · {_dm(c[0]['d'])}",
                      "revenue": I(sum(r["rev"] or 0 for r in c)),
                      "net_revenue": I(sum(r["net_rev"] or 0 for r in c)),
                      "gp1": I(sum(r["gp1"] or 0 for r in c)),
                      "gp2": I(sum(r["gp2"] or 0 for r in c)),
                      "gp3": I(sum(r["gp3"] or 0 for r in c)),
                      "cost": I(sum(r["cost"] or 0 for r in c))})

    monthly = {}
    for r in long_rows:
        y, m = r["d"][:4], int(r["d"][5:7])
        yd = monthly.setdefault(y, {k: [None] * 12 for k in
                                    ("revenue", "net_revenue", "gp1", "gp2", "gp3", "cost")})
        for key, col in (("revenue", "rev"), ("net_revenue", "net_rev"), ("gp1", "gp1"),
                         ("gp2", "gp2"), ("gp3", "gp3"), ("cost", "cost")):
            yd[key][m-1] = (yd[key][m-1] or 0) + I(r[col])

    kv_daily_long = [{"iso": r["d"], "revenue": I(r["rev"]), "net_revenue": I(r["net_rev"]),
                      "gp1": I(r["gp1"]), "gp2": I(r["gp2"]), "gp3": I(r["gp3"]),
                      "txns": I(r["txns"]), "cost": I(r["cost"])} for r in long_rows]

    kv_overview = {
        "period": {"start": cs.isoformat(), "end": ce.isoformat(), "label": _lbl(cs, ce)},
        "prev_period": {"start": ps.isoformat(), "end": pe.isoformat(), "label": _lbl(ps, pe)},
        "totals": {"cur": kv_tot(dcur), "prev": kv_tot(dprev)},
        "markets":  _kv_dim("market", cs, ce, ps, pe),
        "shops":    _kv_dim("shop", cs, ce, ps, pe, rev_prev_key="rev_prev"),
        "channels": _kv_dim("channel", cs, ce, ps, pe),
        "daily": daily, "weeks": weeks, "monthly": monthly,
        "daily_long": kv_daily_long, "data_end": ce.isoformat(), "source": SOURCE,
    }

    prod_long = _prod_daily(LONG_START, ce)
    pcur  = [r for r in prod_long if in_range(r["d"], cs, ce)]
    pprev = [r for r in prod_long if in_range(r["d"], ps, pe)]
    prod_tot = lambda rs: {"revenue": I(sum(r["rev"] or 0 for r in rs)),
                           "cogs": I(sum(r["cogs"] or 0 for r in rs)),
                           "gp1": I(sum(r["gp1"] or 0 for r in rs)),
                           "gp2": I(sum(r["gp2"] or 0 for r in rs)),
                           "gp3": I(sum(r["gp3"] or 0 for r in rs))}
    product_overview = {
        "period": kv_overview["period"], "prev_period": kv_overview["prev_period"],
        "totals": {"cur": prod_tot(pcur), "prev": prod_tot(pprev)},
        "categories": _prod_dim("category", cs, ce, ps, pe),
        "brands":     _prod_dim("brand", cs, ce, ps, pe),
        "daily_long": [{"iso": r["d"], "revenue": I(r["rev"]), "cogs": I(r["cogs"]),
                        "gp1": I(r["gp1"]), "gp2": I(r["gp2"]), "gp3": I(r["gp3"])}
                       for r in prod_long],
        "data_end": ce.isoformat(), "source": SOURCE,
    }
    return kv_overview, product_overview, (cs, ce)


# ── Cached entry points for app.py ───────────────────────────────────────────
# Read-through via the existing Firestore cache layer (funnel_client._cached):
# L1-less but cross-instance, TTL 30 min. One build fills both docs, so the KV
# and Product tabs always describe the same data_end.
def _build_and_cache_both():
    from funnel_client import get_cache, CACHE_TTL_SECONDS
    kv, product, _ = build_payloads()
    cache = get_cache()
    for key, val in (("kv-overview", kv), ("product-overview", product)):
        try:
            cache.set(key, val, CACHE_TTL_SECONDS)
        except Exception as e:
            print(f"marts cache: write failed for {key!r}: {e!r}")
    return kv, product


def get_kv_payload() -> dict:
    from funnel_client import get_cache
    hit = get_cache().get("kv-overview")
    if hit is not None:
        return hit
    kv, _ = _build_and_cache_both()
    return kv


def get_product_payload() -> dict:
    from funnel_client import get_cache
    hit = get_cache().get("product-overview")
    if hit is not None:
        return hit
    _, product = _build_and_cache_both()
    return product
