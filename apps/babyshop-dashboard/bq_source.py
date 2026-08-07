#!/usr/bin/env python3
"""
BigQuery-backed refresh for the Babyshop dashboard.

Reads the Funnel→BigQuery export and writes the kv-overview / product-overview
snapshots straight into Firestore (the same docs the dashboard reads). No MCP,
no Funnel connector, no source_overrides — Funnel's export is pre-aggregated so
plain SUM ... GROUP BY reproduces every dashboard number (validated to the krona).

Run as a Cloud Run Job (entrypoint) or locally:  python3 bq_source.py
"""
from __future__ import annotations
import datetime, json, time, os
from google.cloud import bigquery

BQ_PROJECT = os.environ.get("BQ_PROJECT", "babyshop-funnel-data")
BQ_TABLE   = os.environ.get("BQ_TABLE", "babyshop-funnel-data.bs_funnel_export.funnel_data")
WORKSPACE  = os.environ.get("FUNNEL_WORKSPACE", "-Ln87GcdqU9CMJV6zMBY")
COLLECTION = "funnel_cache"
TTL        = 30 * 24 * 3600
SOURCE     = "bigquery-export"

SV_MON = ["jan","feb","mar","apr","maj","jun","jul","aug","sep","okt","nov","dec"]
def _lbl(s: datetime.date, e: datetime.date) -> str:
    return (f"{s.day} {SV_MON[s.month-1]} – {e.day} {SV_MON[e.month-1]} {e.year}"
            if s.year == e.year else
            f"{s.day} {SV_MON[s.month-1]} {s.year} – {e.day} {SV_MON[e.month-1]} {e.year}")
def _dm(s: str) -> str:
    y,m,d = map(int, s.split("-")); return f"{d}/{m}"
def I(v): return int(round(float(v or 0)))

FIRESTORE_PROJECT = os.environ.get("FIRESTORE_PROJECT", "project-a7ade44e-e7e3-4871-a83")

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
        _client = bigquery.Client(project=BQ_PROJECT, credentials=_credentials())
    return _client

def _rows(sql, params):
    job = bq().query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params))
    return list(job.result())

def _p(cs, ce, ps, pe):
    return [bigquery.ScalarQueryParameter(n, "DATE", v) for n, v in
            [("cs", cs), ("ce", ce), ("ps", ps), ("pe", pe)]]

KV = ("SUM(kv_revenue) rev, SUM(kv_gp1) gp1, SUM(kv_gp2) gp2, SUM(kv_gp2)-COALESCE(SUM(Cost),0) gp3, "
      "CAST(SUM(kv_transactions) AS INT64) txns, SUM(Cost) cost")
PR = ("SUM(kv_revenue_product) rev, SUM(kv_product_cogs) cogs, SUM(kv_gp1_product) gp1, "
      "SUM(kv_gp2_product) gp2, SUM(kv_gp2_product)-COALESCE(SUM(shopping_cost),0) gp3")

def _merge_kv(cur, prev, rev_prev_key="revenue_prev"):
    pm = {r["name"]: r for r in prev}
    out = []
    for r in cur:
        p = pm.get(r["name"])
        out.append({"name": r["name"], "revenue": I(r["rev"]), "gp2": I(r["gp2"]),
                    "gp3": I(r["gp3"]), "cost": I(r["cost"]),
                    rev_prev_key: I(p["rev"]) if p else None,
                    "gp2_prev": I(p["gp2"]) if p else None,
                    "gp3_prev": I(p["gp3"]) if p else None,
                    "cost_prev": I(p["cost"]) if p else None})
    return out

def _kv_dim(col, cs, ce, ps, pe, rev_prev_key="revenue_prev"):
    def q(s, e):
        sql = (f"SELECT {col} name, {KV} FROM `{BQ_TABLE}` "
               f"WHERE Date BETWEEN @cs AND @ce AND {col} IS NOT NULL "
               f"GROUP BY name HAVING SUM(kv_revenue) > 0 ORDER BY rev DESC")
        return _rows(sql, _p(s, e, s, e))
    return _merge_kv(q(cs, ce), q(ps, pe), rev_prev_key)

_KV_FILTER_COLS = {
    "market":  "market_level_1_kv",
    "shop":    "shop_new",
    "channel": "Channel_Type_Level_2",
}

def _kv_filtered(filters, cs, ce, ps, pe):
    """KV totals for an arbitrary AND-combination of market/shop/channel.

    `filters` is a dict like {"market": "SE", "channel": "Google"} — only the
    keys present are constrained, so any subset (including all three) works.
    Returns {"cur": {...}, "prev": {...}} with the same metric shape the
    dashboard's KPI cards consume (revenue/gp2/gp3/txns/cost)."""
    conds, params = ["Date BETWEEN @cs AND @ce"], []
    for i, (dim, val) in enumerate(
            (d, filters[d]) for d in _KV_FILTER_COLS if filters.get(d)):
        pname = f"f{i}"
        conds.append(f"{_KV_FILTER_COLS[dim]} = @{pname}")
        params.append(bigquery.ScalarQueryParameter(pname, "STRING", val))

    def q(s, e):
        p = [bigquery.ScalarQueryParameter("cs", "DATE", s),
             bigquery.ScalarQueryParameter("ce", "DATE", e)] + params
        rows = _rows(f"SELECT {KV} FROM `{BQ_TABLE}` WHERE {' AND '.join(conds)}", p)
        r = rows[0] if rows else None
        return {"revenue": I(r["rev"]) if r else 0, "gp2": I(r["gp2"]) if r else 0,
                "gp3": I(r["gp3"]) if r else 0, "txns": I(r["txns"]) if r else 0,
                "cost": I(r["cost"]) if r else 0}
    return {"cur": q(cs, ce), "prev": q(ps, pe)}

def filtered_daily(filters, start: datetime.date, end: datetime.date):
    """Daily KV series for an arbitrary AND-combination of market/shop/channel.

    Same `filters` shape as _kv_filtered ({"market": "SE", "channel": "Google"} —
    only the keys present are constrained). Returns one row per day that has
    data, ordered by date:

        [{"d": "2026-05-24", "rev": …, "gp2": …, "gp3": …, "cost": …}, …]

    Same metric aliases as build_payloads' daily query, but `d` stays the ISO
    date (the caller buckets it) instead of the dd/m display label. Fully
    parameterised — no user-supplied value is interpolated into the SQL."""
    conds = ["Date BETWEEN @start AND @end"]
    params = [bigquery.ScalarQueryParameter("start", "DATE", start),
              bigquery.ScalarQueryParameter("end", "DATE", end)]
    for i, dim in enumerate(d for d in _KV_FILTER_COLS if filters.get(d)):
        pname = f"f{i}"
        conds.append(f"{_KV_FILTER_COLS[dim]} = @{pname}")
        params.append(bigquery.ScalarQueryParameter(pname, "STRING", filters[dim]))
    rows = _rows(f"SELECT CAST(Date AS STRING) d, {KV} FROM `{BQ_TABLE}` "
                 f"WHERE {' AND '.join(conds)} GROUP BY d ORDER BY d", params)
    return [{"d": r["d"], "rev": I(r["rev"]), "gp2": I(r["gp2"]),
             "gp3": I(r["gp3"]), "cost": I(r["cost"])} for r in rows]

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

def _prod_dim(col, cs, ce, ps, pe, limit=25):
    def q(s, e):
        sql = (f"SELECT COALESCE(NULLIF({col}, ''), 'Uncategorised') name, {PR} FROM `{BQ_TABLE}` "
               f"WHERE Date BETWEEN @cs AND @ce "
               f"GROUP BY name HAVING SUM(kv_revenue_product) > 0 ORDER BY rev DESC LIMIT {limit}")
        return _rows(sql, _p(s, e, s, e))
    return _merge_prod(q(cs, ce), q(ps, pe))

def build_payloads(cur_end: datetime.date | None = None):
    ce = cur_end or (datetime.date.today() - datetime.timedelta(days=1))
    cs = ce - datetime.timedelta(days=29)
    pe = cs - datetime.timedelta(days=1)
    ps = pe - datetime.timedelta(days=29)

    # ── KV daily (both periods in one query, split in Python) ──
    drows = _rows(
        f"SELECT CAST(Date AS STRING) d, {KV} FROM `{BQ_TABLE}` "
        f"WHERE Date BETWEEN @ps AND @ce GROUP BY d ORDER BY d", _p(cs, ce, ps, pe))
    def in_range(ds, a, b): return a.isoformat() <= ds <= b.isoformat()
    dcur = [r for r in drows if in_range(r["d"], cs, ce)]
    dprev = [r for r in drows if in_range(r["d"], ps, pe)]
    kv_tot = lambda rs: {"revenue": I(sum(r["rev"] or 0 for r in rs)),
                         "gp1": I(sum(r["gp1"] or 0 for r in rs)),
                         "gp2": I(sum(r["gp2"] or 0 for r in rs)),
                         "gp3": I(sum(r["gp3"] or 0 for r in rs)),
                         "txns": I(sum(r["txns"] or 0 for r in rs)),
                         "adCost": I(sum(r["cost"] or 0 for r in rs))}
    daily = [{"d": _dm(r["d"]), "rev": I(r["rev"]), "gp1": I(r["gp1"]), "gp2": I(r["gp2"]),
              "gp3": I(r["gp3"]), "cost": I(r["cost"])} for r in dcur]
    weeks = []
    for wi in range(0, len(dcur), 7):
        c = dcur[wi:wi+7]
        weeks.append({"label": f"V{wi//7+1} · {_dm(c[0]['d'])}",
                      "revenue": I(sum(r["rev"] or 0 for r in c)),
                      "gp2": I(sum(r["gp2"] or 0 for r in c)),
                      "gp3": I(sum(r["gp3"] or 0 for r in c)),
                      "cost": I(sum(r["cost"] or 0 for r in c))})

    # ── KV monthly totals for the long-term chart (real, 2025 → current year) ──
    # Always include the current month, even though it's incomplete (data runs
    # through `ce`, i.e. yesterday), so the latest month-to-date is always shown.
    LONG_START = datetime.date(2025, 1, 1)
    monthly = {}
    for r in _rows(
            f"SELECT FORMAT_DATE('%Y-%m', Date) ym, {KV} FROM `{BQ_TABLE}` "
            f"WHERE Date BETWEEN DATE '{LONG_START}' AND DATE '{ce}' GROUP BY ym ORDER BY ym", []):
        y, m = r["ym"].split("-"); yi, mi = int(y), int(m)
        yd = monthly.setdefault(y, {k: [None] * 12 for k in ("revenue", "gp1", "gp2", "gp3", "cost")})
        yd["revenue"][mi-1] = I(r["rev"]); yd["gp1"][mi-1] = I(r["gp1"]); yd["gp2"][mi-1] = I(r["gp2"])
        yd["gp3"][mi-1] = I(r["gp3"]); yd["cost"][mi-1] = I(r["cost"])

    # ── KV daily totals, long history (2025 → current) — drives real per-period
    #    comparisons on the KV Overview (any period + pop/yoy window). ──
    kv_daily_long = [{"iso": r["d"], "revenue": I(r["rev"]), "gp1": I(r["gp1"]), "gp2": I(r["gp2"]),
                      "gp3": I(r["gp3"]), "txns": I(r["txns"]), "cost": I(r["cost"])}
                     for r in _rows(
            f"SELECT CAST(Date AS STRING) d, {KV} FROM `{BQ_TABLE}` "
            f"WHERE Date BETWEEN DATE '{LONG_START}' AND DATE '{ce}' GROUP BY d ORDER BY d", [])]

    kv_overview = {
        "period": {"start": cs.isoformat(), "end": ce.isoformat(), "label": _lbl(cs, ce)},
        "prev_period": {"start": ps.isoformat(), "end": pe.isoformat(), "label": _lbl(ps, pe)},
        "totals": {"cur": kv_tot(dcur), "prev": kv_tot(dprev)},
        "markets":  _kv_dim("market_level_1_kv", cs, ce, ps, pe),
        "shops":    _kv_dim("shop_new", cs, ce, ps, pe, rev_prev_key="rev_prev"),
        "channels": _kv_dim("Channel_Type_Level_2", cs, ce, ps, pe),
        "daily": daily, "weeks": weeks, "monthly": monthly,
        "daily_long": kv_daily_long, "data_end": ce.isoformat(), "source": SOURCE,
    }

    # ── Product totals (per-day → sum) ──
    def prod_tot(s, e):
        r = _rows(f"SELECT {PR} FROM `{BQ_TABLE}` WHERE Date BETWEEN @cs AND @ce", _p(s, e, s, e))[0]
        return {"revenue": I(r["rev"]), "cogs": I(r["cogs"]), "gp1": I(r["gp1"]),
                "gp2": I(r["gp2"]), "gp3": I(r["gp3"])}
    # ── Product daily totals, long history (2025 → current) ──
    # The dashboard sums these client-side for ANY period + comparison window
    # (7D/30D/90D/custom · pop/yoy), so deltas reflect the real range — not a
    # linear scale of the 30-day numbers.
    daily_long = [{"iso": r["d"], "revenue": I(r["rev"]), "cogs": I(r["cogs"]),
                   "gp1": I(r["gp1"]), "gp2": I(r["gp2"]), "gp3": I(r["gp3"])}
                  for r in _rows(
            f"SELECT CAST(Date AS STRING) d, {PR} FROM `{BQ_TABLE}` "
            f"WHERE Date BETWEEN DATE '{LONG_START}' AND DATE '{ce}' GROUP BY d ORDER BY d", [])]
    product_overview = {
        "period": kv_overview["period"], "prev_period": kv_overview["prev_period"],
        "totals": {"cur": prod_tot(cs, ce), "prev": prod_tot(ps, pe)},
        "categories": _prod_dim("Product_type_2", cs, ce, ps, pe),
        # LOWER(kv_brand): revenue rows are lowercase ('kuling') but some ad-cost rows
        # are capitalised ('Kuling'); unify so each brand is one row with its full cost.
        "brands":     _prod_dim("LOWER(kv_brand)", cs, ce, ps, pe),
        "daily_long": daily_long, "data_end": ce.isoformat(), "source": SOURCE,
    }
    return kv_overview, product_overview, (cs, ce)

def write_firestore(kv, product, stoy=None):
    from google.cloud import firestore
    db = firestore.Client(project=FIRESTORE_PROJECT, credentials=_credentials())
    now = time.time()
    fields = lambda payload: {"data": payload, "fetched_at": firestore.SERVER_TIMESTAMP,
                              "expires_at": now + TTL, "ttl_seconds": TTL, "workspace": WORKSPACE}
    db.collection(COLLECTION).document(f"{WORKSPACE}__kv-overview").set(fields(kv))
    db.collection(COLLECTION).document(f"{WORKSPACE}__product-overview").set(fields(product))
    if stoy is not None:
        db.collection(COLLECTION).document(f"{WORKSPACE}__stoy-test").set(fields(stoy))
    db.collection("refresh_status").document("latest").set({
        "ok": True, "last_run": int(now), "last_stage": "complete",
        "last_message": "BigQuery refresh complete", "next_due": int(now + 24*3600)})

# ── Per-campaign Google daily actuals (ROAS Simulations calibration/backtest) ─
CAMPAIGN_ACTUALS_KEY = "campaign-actuals"
# Columnar rows (arrays, not objects) keep the doc well under Firestore's 1 MiB
# limit; if 120 days still serialises past the guard, shorten the lookback.
CAMPAIGN_ACTUALS_LOOKBACKS = (120, 90, 60)
CAMPAIGN_ACTUALS_MAX_BYTES = 800_000
CAMPAIGN_ACTUALS_COLUMNS = ["date", "campaign", "shop", "market", "cost", "gp2"]

def _campaign_actuals_rows(cs: datetime.date, ce: datetime.date):
    # GROUP BY / HAVING / ORDER BY name the underlying columns, never a SELECT alias:
    # BigQuery resolves those clauses against the FROM clause first, so an alias like
    # `cost` would bind to the ungrouped `Cost` column instead. `Campaign` is left
    # unaliased for the same reason (it is the only grouped column also selected).
    rows = _rows(
        f"SELECT CAST(Date AS STRING) d, Campaign, shop_new shop, "
        f"market_level_1_kv market, COALESCE(SUM(Cost), 0) cost, COALESCE(SUM(kv_gp2), 0) gp2 "
        f"FROM `{BQ_TABLE}` WHERE Date BETWEEN @cs AND @ce "
        f"AND Channel_Type_Level_2 = 'Google' AND Campaign IS NOT NULL "
        f"GROUP BY d, Campaign, shop_new, market_level_1_kv "
        f"HAVING SUM(Cost) > 0 ORDER BY d, Campaign",
        [bigquery.ScalarQueryParameter("cs", "DATE", cs),
         bigquery.ScalarQueryParameter("ce", "DATE", ce)])
    return [[r["d"], r["Campaign"], r["shop"] or "", r["market"] or "",
             round(float(r["cost"] or 0), 2), round(float(r["gp2"] or 0), 2)] for r in rows]

def build_campaign_actuals(end: datetime.date | None = None):
    """Daily Google-channel actuals per campaign — the BQ side of the ROAS
    Simulations value-calibration factor and of the backtest's realized POAS.

    Trailing complete days only (`end` defaults to yesterday, same convention as
    build_payloads), shortening the lookback until the payload fits the guard."""
    ce = end or (datetime.date.today() - datetime.timedelta(days=1))
    payload = None
    for days in CAMPAIGN_ACTUALS_LOOKBACKS:
        cs = ce - datetime.timedelta(days=days - 1)
        payload = {"start": cs.isoformat(), "end": ce.isoformat(),
                   "columns": list(CAMPAIGN_ACTUALS_COLUMNS),
                   "rows": _campaign_actuals_rows(cs, ce),
                   "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat()}
        if len(json.dumps(payload, ensure_ascii=False).encode()) <= CAMPAIGN_ACTUALS_MAX_BYTES:
            break
    return payload

def write_campaign_actuals(payload):
    from google.cloud import firestore
    db = firestore.Client(project=FIRESTORE_PROJECT, credentials=_credentials())
    db.collection(COLLECTION).document(f"{WORKSPACE}__{CAMPAIGN_ACTUALS_KEY}").set({
        "data": payload, "fetched_at": firestore.SERVER_TIMESTAMP,
        "expires_at": time.time() + TTL, "ttl_seconds": TTL, "workspace": WORKSPACE})

# ── Stoy funnel-shift test (June 2026) ───────────────────────────────────────
STOY_TEST_START = os.environ.get("STOY_TEST_START", "2026-06-10")
STOY_META_TARGET = int(os.environ.get("STOY_META_TARGET", "150000"))
STOY_MARKETS = ("SE", "NO")
# Geo-control: Stoy in its non-test retail markets (no Meta reallocation ran here).
# Same brand as the test, so it isolates the intervention from June seasonality even
# more tightly than the Kuling (different-brand) control. ROW spillover campaigns are
# excluded — near-zero revenue, so including them would only distort the control COS%.
STOY_CONTROL_MARKETS = tuple(os.environ.get("STOY_CONTROL_MARKETS", "DK,FI").split(","))

def build_stoy_payload(test_end: datetime.date | None = None):
    ts = datetime.date.fromisoformat(STOY_TEST_START)
    te = test_end or (datetime.date.today() - datetime.timedelta(days=1))
    if te < ts:
        te = ts
    days = (te - ts).days + 1
    be = ts - datetime.timedelta(days=1)          # baseline = equal-length window just before
    bs = be - datetime.timedelta(days=days - 1)
    mkts = "('" + "','".join(STOY_MARKETS) + "')"

    def params(s, e): return [bigquery.ScalarQueryParameter("s", "DATE", s),
                              bigquery.ScalarQueryParameter("e", "DATE", e)]
    PER = ("CASE WHEN Date BETWEEN DATE '%s' AND DATE '%s' THEN 'test' "
           "WHEN Date BETWEEN DATE '%s' AND DATE '%s' THEN 'base' END period"
           % (ts, te, bs, be))

    # Fully SE+NO scoped. Market per row: product/Meta rows carry market_level_1_kv;
    # Google Shopping cost rows are market-NULL but the campaign name encodes it
    # (p-shopping-se / p-shopping-no), so derive market from the campaign there.
    MKT = ("CASE WHEN market_level_1_kv IN ('SE','NO') THEN market_level_1_kv "
           "WHEN Channel_Type_Level_2 LIKE 'Google%' AND LOWER(Campaign) LIKE '%-se-%' THEN 'SE' "
           "WHEN Channel_Type_Level_2 LIKE 'Google%' AND LOWER(Campaign) LIKE '%-no-%' THEN 'NO' END")
    GCOST = "SUM(IF(Channel_Type_Level_2 LIKE 'Google%', shopping_cost, 0))"
    MCOST = "SUM(IF(Channel_Type_Level_2 LIKE 'Paid Social%', Cost, 0))"
    # Meta clicks sit on the same Paid Social rows as Meta cost → reliable at brand grain.
    # (Google shopping clicks are NOT carried at brand grain in the export — see google_click_ref.)
    MCLK = "SUM(IF(Channel_Type_Level_2 LIKE 'Paid Social%', Clicks, 0))"
    out = {b: {} for b in ("stoy", "kuling")}
    for r in _rows(
        f"SELECT LOWER(kv_brand) b, {PER}, SUM(kv_revenue_product) rev, SUM(kv_gp1_product) gp1, SUM(kv_gp2_product) gp2, "
        f"{GCOST} gcost, {MCOST} mcost, {MCLK} mclicks "
        f"FROM `{BQ_TABLE}` WHERE LOWER(kv_brand) IN ('stoy','kuling') AND {MKT} IN ('SE','NO') "
        f"AND Date BETWEEN DATE '{bs}' AND DATE '{te}' GROUP BY b, period", []):
        if r["period"] and r["b"] in out:
            p, o = r["period"], out[r["b"]]
            g, m = I(r["gcost"]), I(r["mcost"])
            o[f'rev_{p}'] = I(r["rev"]); o[f'gp1_{p}'] = I(r["gp1"]); o[f'gp2_{p}'] = I(r["gp2"])
            o[f'google_{p}'] = g; o[f'meta_{p}'] = m; o[f'spend_{p}'] = g + m
            o[f'meta_clicks_{p}'] = I(r["mclicks"])

    # Stoy geo-control: same brand, non-test markets (DK+FI). These markets carry their
    # market on every row, so attribute directly by market_level_1_kv — no campaign
    # parsing needed. Google spend lives in shopping_cost for SE/NO but in Cost for the
    # other markets, so take whichever is populated. Meta ran only in SE/NO, so the
    # control's Meta spend is ~0 by design — that's what makes it a clean counterfactual.
    cmkts = "('" + "','".join(STOY_CONTROL_MARKETS) + "')"
    CGCOST = "SUM(IF(Channel_Type_Level_2 LIKE 'Google%', IF(shopping_cost > 0, shopping_cost, Cost), 0))"
    ctrl = {}
    for r in _rows(
        f"SELECT {PER}, SUM(kv_revenue_product) rev, SUM(kv_gp1_product) gp1, SUM(kv_gp2_product) gp2, "
        f"{CGCOST} gcost, {MCOST} mcost, {MCLK} mclicks "
        f"FROM `{BQ_TABLE}` WHERE LOWER(kv_brand)='stoy' AND market_level_1_kv IN {cmkts} "
        f"AND Date BETWEEN DATE '{bs}' AND DATE '{te}' GROUP BY period", []):
        if r["period"]:
            p, g, m = r["period"], I(r["gcost"]), I(r["mcost"])
            ctrl[f'rev_{p}'] = I(r["rev"]); ctrl[f'gp1_{p}'] = I(r["gp1"]); ctrl[f'gp2_{p}'] = I(r["gp2"])
            ctrl[f'google_{p}'] = g; ctrl[f'meta_{p}'] = m; ctrl[f'spend_{p}'] = g + m
            ctrl[f'meta_clicks_{p}'] = I(r["mclicks"])
    ctrl["markets"] = list(STOY_CONTROL_MARKETS)
    out["stoy_other"] = ctrl

    # Stoy daily (SE+NO): revenue + Google Shopping + Meta spend. 90-day trailing window
    # (the dashboard toggles 30/60/90); 'spend' = google+meta for the aggregate line.
    chart_start = min(bs, te - datetime.timedelta(days=89))
    daily = [{"d": _dm(r["d"]), "iso": r["d"], "rev": I(r["rev"]),
              "google": I(r["google"]), "meta": I(r["meta"]), "spend": I(r["google"]) + I(r["meta"])}
             for r in _rows(
        f"SELECT CAST(Date AS STRING) d, SUM(kv_revenue_product) rev, {GCOST} google, {MCOST} meta "
        f"FROM `{BQ_TABLE}` WHERE LOWER(kv_brand)='stoy' AND {MKT} IN ('SE','NO') "
        f"AND Date BETWEEN DATE '{chart_start}' AND DATE '{te}' GROUP BY d ORDER BY d", [])]

    # Year-over-year (SE+NO): daily revenue + GP2, 2026 vs same calendar dates 2025.
    # Window = 6-week lead-in through the test, so the seasonal shape and the test sit side by side.
    yoy_start = ts - datetime.timedelta(days=42)
    ly = lambda d: d.replace(year=d.year - 1)
    yrows = {r["d"]: r for r in _rows(
        f"SELECT CAST(Date AS STRING) d, SUM(kv_revenue_product) rev, SUM(kv_gp1_product) gp1, SUM(kv_gp2_product) gp2 "
        f"FROM `{BQ_TABLE}` WHERE LOWER(kv_brand)='stoy' AND market_level_1_kv IN {mkts} "
        f"AND ((Date BETWEEN DATE '{yoy_start}' AND DATE '{te}') "
        f"OR (Date BETWEEN DATE '{ly(yoy_start)}' AND DATE '{ly(te)}')) GROUP BY d", [])}
    yoy, ny = [], yoy_start
    while ny <= te:
        cur, prev = yrows.get(ny.isoformat()), yrows.get(ly(ny).isoformat())
        yoy.append({"d": _dm(ny.isoformat()), "iso": ny.isoformat(),
                    "rev": I(cur["rev"]) if cur else 0, "gp1": I(cur["gp1"]) if cur else 0, "gp2": I(cur["gp2"]) if cur else 0,
                    "rev_ly": I(prev["rev"]) if prev else 0, "gp1_ly": I(prev["gp1"]) if prev else 0, "gp2_ly": I(prev["gp2"]) if prev else 0})
        ny += datetime.timedelta(days=1)
    yoy_sum = {k: sum(e[k] for e in yoy) for k in ("rev", "gp1", "gp2", "rev_ly", "gp1_ly", "gp2_ly")}
    yoy_sum["window"] = _lbl(yoy_start, te)

    # Flagship Stoy products (test window, SE+NO)
    flagship = [{"title": r["title"], "rev": I(r["rev"])} for r in _rows(
        f"SELECT Product_title__File_Import title, SUM(kv_revenue_product) rev FROM `{BQ_TABLE}` "
        f"WHERE LOWER(kv_brand)='stoy' AND market_level_1_kv IN {mkts} AND Date BETWEEN DATE '{ts}' AND DATE '{te}' "
        f"AND Product_title__File_Import IS NOT NULL GROUP BY title HAVING rev > 0 ORDER BY rev DESC LIMIT 10", [])]

    # per-market Stoy: revenue/GP + Google/Meta/total spend (SE vs NO)
    markets = {m: {} for m in STOY_MARKETS}
    for r in _rows(
        f"SELECT {MKT} m, {PER}, SUM(kv_revenue_product) rev, SUM(kv_gp1_product) gp1, SUM(kv_gp2_product) gp2, {GCOST} gcost, {MCOST} mcost, {MCLK} mclicks "
        f"FROM `{BQ_TABLE}` WHERE LOWER(kv_brand)='stoy' AND {MKT} IN ('SE','NO') "
        f"AND Date BETWEEN DATE '{bs}' AND DATE '{te}' GROUP BY m, period", []):
        if r["period"] and r["m"] in markets:
            p, g, mc = r["period"], I(r["gcost"]), I(r["mcost"])
            mk = markets[r["m"]]
            mk[f'rev_{p}'] = I(r["rev"]); mk[f'gp1_{p}'] = I(r["gp1"]); mk[f'gp2_{p}'] = I(r["gp2"])
            mk[f'google_{p}'] = g; mk[f'meta_{p}'] = mc; mk[f'spend_{p}'] = g + mc
            mk[f'meta_clicks_{p}'] = I(r["mclicks"])

    return {
        "test_period": {"start": ts.isoformat(), "end": te.isoformat(), "label": _lbl(ts, te), "days": days},
        "baseline_period": {"start": bs.isoformat(), "end": be.isoformat(), "label": _lbl(bs, be)},
        "reallocation": {"meta_spent": out["stoy"].get("meta_test", 0), "target": STOY_META_TARGET},
        "brands": out, "markets": markets, "daily": daily, "yoy": yoy, "yoy_sum": yoy_sum, "flagship": flagship,
        # CPC benchmark, fixed pre-test window (1–9 Jun 2026), SE+NO Stoy. Both channels over the
        # SAME window so they're comparable. Google brand-level clicks aren't carried in the
        # Funnel→BQ export, so Google here is from the Google Ads API; Meta is from BQ. The live
        # Meta test-period CPC (brands.stoy.meta_clicks_test) accumulates separately as the test runs.
        "cpc_ref": {"period": "1–9 Jun 2026",
                    "google": {"cost": 91092, "clicks": 13906, "cpc": 6.55},
                    "meta":   {"cost": 18374, "clicks": 5517, "cpc": 3.33},
                    "source": "Google = Google Ads API (shopping_performance_view, brand=stoy); Meta = Funnel→BQ (Paid Social). Both SE+NO."},
        "note_google": "Fully SE+NO scoped. Spend is brand-attributed: Meta via market, Google Shopping via campaign (p-shopping-se / -no). Meta clicks/CPC are live; the Google CPC is a fixed pre-test reference (brand-level Google clicks aren't in the export). The reallocation — Google Shopping ↓, Meta ↑ — is the lever; judge on revenue / GP3-after-ad-spend vs the Kuling control.",
        "source": SOURCE,
    }

def main():
    t0 = time.time()
    kv, product, (cs, ce) = build_payloads()
    stoy = build_stoy_payload(ce)
    write_firestore(kv, product, stoy)
    print(f"✓ BigQuery refresh · {kv['period']['label']} · "
          f"KV rev {kv['totals']['cur']['revenue']:,} · "
          f"product rev {product['totals']['cur']['revenue']:,} · "
          f"stoy meta {stoy['reallocation']['meta_spent']:,}/{stoy['reallocation']['target']:,} · "
          f"{time.time()-t0:.1f}s")

    # Per-campaign Google daily actuals (funnel_cache/<ws>__campaign-actuals, read
    # by /api/campaign-actuals). Non-fatal: the calibration/backtest section of the
    # ROAS Simulations page degrades gracefully when the snapshot is missing.
    try:
        ca = build_campaign_actuals(ce)
        write_campaign_actuals(ca)
        print(f"✓ Campaign actuals · {ca['start']}→{ca['end']} · {len(ca['rows'])} rows")
    except Exception as e:
        print(f"⚠️  Campaign actuals refresh failed (non-fatal): {e!r}")

    # ROAS Impact snapshot (funnel_cache/<ws>__roas-impact, read by the ROAS page).
    # Folded into the daily job so it refreshes alongside the other BigQuery data.
    # Wrapped so a ROAS failure can't abort the rest of the daily refresh.
    try:
        import refresh_roas_impact as roas
        p = roas.build_payload()
        roas.write_firestore(p)
        print(f"✓ ROAS Impact refresh · change {p['change_date']} · "
              f"data→{p['data_end']} · {p['n_campaigns']} campaigns")
    except Exception as e:
        print(f"⚠️  ROAS Impact refresh failed (non-fatal): {e!r}")

    # Budget / forecast plan (funnel_cache/<ws>__budget-2026, read by /api/budget).
    # Static plan committed as budget_2026.json; re-pushed each run so it self-heals.
    try:
        import budget_source
        budget_source.push_budget()
    except Exception as e:
        print(f"⚠️  Budget push failed (non-fatal): {e!r}")

if __name__ == "__main__":
    main()
