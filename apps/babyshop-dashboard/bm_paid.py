#!/usr/bin/env python3
"""
bm_paid.py: the `paid` section of the Black Month tab (Google + Meta levers).

What it answers, every morning in November: where is paid spend against the
plan, which Google campaigns sit off their tROAS or are capped by budget, can
a tROAS be touched today (14-day cooldown), is the Meta money going where the
approved plan says, which channel brings new customers at what cost, and where
the next krona earns the most GP2.

Sources (all read-only):
  Google Ads API     Babyshop SE/NO/FI/DK/ROW: per-campaign daily cost + profit
                     (conversion value = PROFIT in these accounts), tROAS, budget,
                     impression share lost to budget/rank, seasonality adjustments,
                     platform new-vs-returning conversions. Client + credentials
                     from refresh_roas_sims (GOOGLE_ADS_* env).
  roas_sims (BQ)     target_changes: the tROAS change log behind the cooldown.
  Funnel export      de-duplicated SEK cost + attributed GP2 per day × market ×
                     channel (bq_source._kv_cte) → Google spend vs plan, GP3 and the
                     marginal-ROAS estimate. Babyshop shop only.
  Warehouse (x-proj) claude-private-499703 via refresh_meta's client:
                     agg_daily_kpis_by_ad (Meta spend/ROAS by campaign/ad set),
                     agg_daily_kpis_by_meta_product (catalog product breakdown),
                     fct_order_ga4_match (order-level new vs returning by channel).
  norce (BQ)         SKU → brand / category for the Meta product breakdown; FX.
"""
from __future__ import annotations

import datetime
import math
import re
from collections import defaultdict

import bm_common as bm

GOOGLE_ACCOUNTS = {          # Babyshop accounts only; Lekmer is outside the plan
    "4851485396": "SE", "8623945183": "NO", "6161399704": "FI",
    "2054294342": "DK", "5541487401": "ROW",
}
MKTS = bm.MARKETS + ["ROW"]
HIST_DAYS = 35               # history window: marginal ROAS + L7/L28 views
COOLDOWN_DAYS = 14
FUNNEL_CH = {"Google Ads": "google", "Paid Social (Meta)": "meta"}
WH = "`claude-private-499703.babyshop_marts.{}`"
LEGACY = bm.GCP_PROJECT


def _mk(m: str | None) -> str:
    return m if m in bm.MARKETS else "ROW"


def _r(v, nd=0):
    if v is None:
        return None
    v = float(v)
    if math.isnan(v) or math.isinf(v):
        return None
    return round(v, nd) if nd else int(round(v))


def _div(a, b, nd=2):
    return None if not b else round(float(a or 0) / float(b), nd)


# ════════════════════════════════════════════════════════════════════════════
#  Plan curves
# ════════════════════════════════════════════════════════════════════════════

def _plan_index() -> tuple[list[float], str]:
    """The marketing-cost daily index for November (sums to 1)."""
    try:
        import daily_targets
        return daily_targets.build_index(bm.YEAR, bm.MONTH, "cost")["index"], \
            "daily_targets marketing-cost index"
    except Exception as e:                       # flat fallback keeps the page alive
        n = len(bm.month_days())
        return [1.0 / n] * n, f"flat (index failed: {type(e).__name__})"


# ════════════════════════════════════════════════════════════════════════════
#  Google Ads API
# ════════════════════════════════════════════════════════════════════════════

def _fx() -> dict[str, float]:
    from google.cloud import bigquery
    c = bigquery.Client(project=LEGACY)
    rows = c.query(f"SELECT currency_code c, to_sek r FROM `{LEGACY}.norce.currency_rates` "
                   "WHERE currency_code IN ('SEK','NOK','EUR','DKK','USD')").result()
    fx = {r["c"]: float(r["r"]) for r in rows if r["r"]}
    fx.setdefault("SEK", 1.0)
    return fx


def _google(today: datetime.date, d0: datetime.date) -> dict:
    import refresh_roas_sims as R
    service = R.ads_client().get_service("GoogleAdsService")
    out = {"accounts": [], "daily": [], "campaigns": [], "seasonality": [],
           "platform_nvr": {}, "errors": []}
    for cid, mk in GOOGLE_ACCOUNTS.items():
        try:
            info = R._account_info(service, cid, {"label": f"Babyshop {mk}", "currency": "SEK"})
            out["accounts"].append({"cid": cid, "market": mk, "name": info["name"],
                                    "currency": info["currency"]})
            cmap = R._campaign_map(service, cid, R._strategy_map(service, cid))

            for row in R._search(service, cid, (
                    "SELECT segments.date, campaign.id, metrics.cost_micros, "
                    "metrics.conversions_value, metrics.conversions FROM campaign "
                    f"WHERE segments.date BETWEEN '{d0}' AND '{today}' "
                    "AND metrics.cost_micros > 0")):
                out["daily"].append({
                    "market": mk, "cid": cid, "id": str(row.campaign.id),
                    "d": row.segments.date, "cost": row.metrics.cost_micros / 1e6,
                    "value": row.metrics.conversions_value or 0.0,
                    "conv": row.metrics.conversions or 0.0})

            for row in R._search(service, cid, (
                    "SELECT campaign.id, campaign.name, campaign.advertising_channel_type, "
                    "campaign.bidding_strategy_system_status, campaign.primary_status, "
                    "campaign.primary_status_reasons, campaign_budget.amount_micros, "
                    "campaign_budget.explicitly_shared, campaign_budget.name, "
                    "metrics.cost_micros, metrics.conversions_value, "
                    "metrics.search_impression_share, "
                    "metrics.search_budget_lost_impression_share, "
                    "metrics.search_rank_lost_impression_share "
                    "FROM campaign WHERE segments.date DURING LAST_7_DAYS "
                    "AND campaign.status = 'ENABLED'")):
                c, m, b = row.campaign, row.metrics, row.campaign_budget
                reasons = [x.name for x in c.primary_status_reasons]
                sys_status = c.bidding_strategy_system_status.name
                meta = cmap.get(str(c.id), {})
                out["campaigns"].append({
                    "market": mk, "cid": cid, "currency": info["currency"],
                    "id": str(c.id), "name": R._safe_name(c.name),
                    "channel": c.advertising_channel_type.name,
                    "bidding": meta.get("type", ""), "troas": meta.get("target"),
                    "status": sys_status, "primary_status": c.primary_status.name,
                    "reasons": reasons,
                    "budget": b.amount_micros / 1e6 if b.amount_micros else None,
                    "budget_shared": bool(b.explicitly_shared),
                    "budget_name": R._safe_name(b.name) if b.explicitly_shared else None,
                    "l7_cost_api": m.cost_micros / 1e6,
                    "is": R._opt(m, "search_impression_share"),
                    "lost_budget": R._opt(m, "search_budget_lost_impression_share"),
                    "lost_rank": R._opt(m, "search_rank_lost_impression_share"),
                })

            for row in R._search(service, cid, (
                    "SELECT bidding_seasonality_adjustment.name, "
                    "bidding_seasonality_adjustment.start_date_time, "
                    "bidding_seasonality_adjustment.end_date_time, "
                    "bidding_seasonality_adjustment.conversion_rate_modifier, "
                    "bidding_seasonality_adjustment.status, "
                    "bidding_seasonality_adjustment.scope, "
                    "bidding_seasonality_adjustment.advertising_channel_types "
                    "FROM bidding_seasonality_adjustment")):
                a = row.bidding_seasonality_adjustment
                if a.end_date_time[:10] < bm.MONTH_START.isoformat():
                    continue                       # old years' adjustments
                out["seasonality"].append({
                    "market": mk, "name": a.name, "start": a.start_date_time,
                    "end": a.end_date_time, "modifier": a.conversion_rate_modifier,
                    "status": a.status.name, "scope": a.scope.name,
                    "channels": [x.name for x in a.advertising_channel_types]})

            nvr = {}
            for row in R._search(service, cid, (
                    "SELECT segments.new_versus_returning_customers, metrics.conversions, "
                    "metrics.conversions_value FROM customer "
                    f"WHERE segments.date BETWEEN '{today - datetime.timedelta(days=28)}' "
                    f"AND '{today - datetime.timedelta(days=1)}'")):
                k = row.segments.new_versus_returning_customers.name
                nvr[k] = {"conv": row.metrics.conversions, "value": row.metrics.conversions_value}
            out["platform_nvr"][mk] = nvr
        except Exception as e:
            out["errors"].append(f"Google {mk} ({cid}): {type(e).__name__}: {str(e)[:200]}")
    return out


def _target_changes(since: datetime.date) -> list[dict]:
    from google.cloud import bigquery
    c = bigquery.Client(project=LEGACY)
    ids = "','".join(GOOGLE_ACCOUNTS)
    rows = c.query(f"""
        SELECT CAST(change_date AS STRING) d, customer_id, customer_name, campaign_id,
               campaign_name, field, old_target, new_target, source,
               predicted_cost_pct, predicted_gp3_pct
        FROM `{LEGACY}.roas_sims.target_changes`
        WHERE change_date >= '{since}' AND REGEXP_REPLACE(customer_id, r'\\D', '') IN ('{ids}')
        ORDER BY change_date DESC, applied_at DESC""").result()
    return [dict(r) for r in rows]


# ════════════════════════════════════════════════════════════════════════════
#  Funnel export (legacy de-duplicated cost + attributed GP2)
# ════════════════════════════════════════════════════════════════════════════

def _funnel(d0: datetime.date, d1: datetime.date) -> list[dict]:
    import bq_source as bs
    from google.cloud import bigquery
    rows = bs._rows(
        f"WITH {bs._kv_cte('Date BETWEEN @s AND @e')} "
        "SELECT CAST(Date AS STRING) d, market_level_1_kv mk, Channel_Type_Level_1 ch, "
        f"{bs.KVD} FROM kv_rows WHERE shop_new = 'Babyshop' "
        "AND Channel_Type_Level_1 IN ('Google Ads', 'Paid Social (Meta)') "
        "GROUP BY d, mk, ch",
        [bigquery.ScalarQueryParameter("s", "DATE", d0),
         bigquery.ScalarQueryParameter("e", "DATE", d1)])
    agg = defaultdict(lambda: {"cost": 0.0, "gp2": 0.0, "rev": 0.0, "txns": 0})
    for r in rows:
        k = (r["d"], _mk(r["mk"]), FUNNEL_CH[r["ch"]])
        a = agg[k]
        a["cost"] += float(r["cost"] or 0)
        a["gp2"] += float(r["gp2"] or 0)
        a["rev"] += float(r["net_rev"] or 0)
        a["txns"] += int(r["txns"] or 0)
    return [{"d": k[0], "market": k[1], "channel": k[2], **v} for k, v in agg.items()]


def _norce(d0: datetime.date, d1: datetime.date) -> list[dict]:
    """Norce ordered revenue per Stockholm day × market: refresh_daily_perf's
    DAILY_SQL (ex VAT, gross of returns, every status except 6), Babyshop only."""
    import refresh_daily_perf as rdp
    out = []
    for r in rdp._rows(rdp.DAILY_SQL, [rdp._p("from_day", d0), rdp._p("to_day", d1)]):
        key = r["market"] or ""
        if not key.startswith("babyshop-"):
            continue
        code = key.split("-", 1)[1].upper()
        out.append({"d": r["d"].isoformat(), "market": _mk(code), "orders": int(r["orders"] or 0),
                    "merch": float(r["merch_sek"] or 0), "freight": float(r["freight_sek"] or 0)})
    return out


# ════════════════════════════════════════════════════════════════════════════
#  Warehouse (Meta + order-level new vs returning)
# ════════════════════════════════════════════════════════════════════════════

TYPE_RULES = [  # first match wins, on the lower-cased campaign name
    ("brand_funded", r"mktcontrib|marketing.?contrib"),
    ("kuling", r"kuling"),
    ("retargeting", r"catalog|dpa|retarget|remarket"),
    ("prospecting", r"prospect|evergreen|offers|broad|advantage|asc"),
]
CAMPAIGN_MKT = [(m, re.compile(rf"(^|[ \-|+]){m}([ \-+|]|$)", re.I)) for m in bm.MARKETS]


def classify(campaign: str) -> str:
    n = (campaign or "").lower()
    for t, rx in TYPE_RULES:
        if re.search(rx, n):
            return t
    return "unclassified"


def _meta_market(mart_market: str, campaign: str) -> str:
    if mart_market in bm.MARKETS:
        return mart_market
    for m, rx in CAMPAIGN_MKT:
        if rx.search(campaign or ""):
            return m
    return "ROW"


def _meta(d0: datetime.date, d1: datetime.date) -> list[dict]:
    import refresh_meta as rm
    rows = rm.q(f"""
        SELECT CAST(date AS STRING) d, market, campaign_name, adset_name,
               SUM(ad_spend_reporting) spend, SUM(purchases) purchases,
               SUM(purchase_value_reporting) value, SUM(link_clicks) clicks,
               SUM(impressions) impressions
        FROM {WH.format('agg_daily_kpis_by_ad')}
        WHERE date BETWEEN @s AND @e AND client_site = 'babyshop'
        GROUP BY d, market, campaign_name, adset_name""", s=d0, e=d1)
    return [{"d": r["d"], "market": _meta_market(r["market"], r["campaign_name"]),
             "campaign": r["campaign_name"] or "(none)", "adset": r["adset_name"] or "(none)",
             "type": classify(r["campaign_name"]),
             "spend": float(r["spend"] or 0), "purchases": float(r["purchases"] or 0),
             "value": float(r["value"] or 0), "clicks": float(r["clicks"] or 0),
             "impressions": int(r["impressions"] or 0)} for r in rows]


def _meta_products(d0: datetime.date, d1: datetime.date) -> list[dict]:
    import refresh_meta as rm
    rows = rm.q(f"""
        SELECT market, product_retailer_id pid, ANY_VALUE(product_name) name,
               SUM(ad_spend) spend, SUM(purchases) purchases, SUM(purchase_value) value
        FROM {WH.format('agg_daily_kpis_by_meta_product')}
        WHERE date BETWEEN @s AND @e
        GROUP BY market, pid HAVING spend > 0""", s=d0, e=d1)
    out = [{"market": _mk(r["market"]), "pid": r["pid"], "name": r["name"],
            "spend": float(r["spend"] or 0), "purchases": float(r["purchases"] or 0),
            "value": float(r["value"] or 0)} for r in rows]
    # SKU (PartNo) → brand + primary category from the Norce sync.
    pids = sorted({r["pid"] for r in out if r["pid"]})
    if pids:
        from google.cloud import bigquery
        c = bigquery.Client(project=LEGACY)
        n = f"`{LEGACY}.norce`"
        job = c.query(f"""
            SELECT s.PartNo pid, ANY_VALUE(m.Name) brand, ANY_VALUE(dc.DefaultFullName) cat
            FROM {n}.product_skus s
            LEFT JOIN {n}.products p ON p.Id = s.ProductId
            LEFT JOIN {n}.dim_manufacturers m ON m.ManufacturerId = p.ManufacturerId
            LEFT JOIN {n}.product_categories pc ON pc.ProductId = s.ProductId AND pc.IsPrimary
            LEFT JOIN {n}.dim_categories dc ON dc.Id = pc.CategoryId
            WHERE s.PartNo IN UNNEST(@p) GROUP BY pid""",
            job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ArrayQueryParameter("p", "STRING", pids)]))
        lk = {r["pid"]: r for r in job.result()}
        for r in out:
            hit = lk.get(r["pid"])
            parts = [p.strip() for p in ((hit and hit["cat"]) or "").split(" - ")
                     if p.strip() and p.strip() != "Ecom"]
            r["brand"] = (hit and hit["brand"]) or "Unknown"
            r["category"] = parts[0] if parts else "Unknown"
            r["subcategory"] = " › ".join(parts[:2]) if parts else "Unknown"
    return out


GA4_CHANNEL_SQL = r"""
  CASE
    WHEN REGEXP_CONTAINS(LOWER(session_source), r'google') AND
         REGEXP_CONTAINS(LOWER(session_medium), r'^(cpc|ppc|paid)') THEN 'google'
    WHEN REGEXP_CONTAINS(LOWER(session_source), r'^(facebook|instagram|meta|fb|ig)$') AND
         REGEXP_CONTAINS(LOWER(session_medium), r'^(cpc|ppc|paid|paid_social|paidsocial)') THEN 'meta'
    WHEN session_medium IS NULL OR session_source IS NULL THEN 'unknown'
    ELSE 'other' END"""


def _nvr(windows: dict[str, tuple[datetime.date, datetime.date]]) -> dict:
    import refresh_meta as rm
    out = {}
    for key, (s, e) in windows.items():
        rows = rm.q(f"""
            SELECT market, {GA4_CHANNEL_SQL} ch,
                   COUNTIF(match_status = 'matched') orders,
                   COUNTIF(match_status = 'matched' AND is_new_customer) new_orders,
                   SUM(IF(match_status = 'matched', order_revenue_net, 0)) rev,
                   COUNTIF(match_status IN ('matched', 'order_only', 'settling')) all_orders
            FROM {WH.format('fct_order_ga4_match')}
            WHERE date BETWEEN @s AND @e AND client_site = 'babyshop'
            GROUP BY market, ch""", s=s, e=e)
        out[key] = [{"market": _mk(r["market"]), "channel": r["ch"],
                     "orders": int(r["orders"] or 0), "new": int(r["new_orders"] or 0),
                     "rev": float(r["rev"] or 0), "all_orders": int(r["all_orders"] or 0)}
                    for r in rows]
    return out


# ════════════════════════════════════════════════════════════════════════════
#  Derived views
# ════════════════════════════════════════════════════════════════════════════

def _marginal(pairs: list[tuple[float, float]]) -> dict:
    """Rough marginal GP2 per krona from daily (cost, gp2) pairs.

    Fits gp2 = a·cost^b on log-log; the marginal return at the window's average
    spend is b × (Σgp2 / Σcost). Days with no spend or non-positive GP2 drop out.
    `confidence` is low when spend barely moved (the slope is then mostly noise)
    or the fit explains little. Demand drives spend AND return on the same days,
    which biases b upward: read it as a ranking, not a forecast."""
    pts = [(math.log(c), math.log(g)) for c, g in pairs if c > 50 and g > 0]
    tot_c = sum(c for c, _ in pairs)
    tot_g = sum(g for _, g in pairs)
    avg = _div(tot_g, tot_c)
    if len(pts) < 10 or not tot_c:
        return {"avg": avg, "marginal": None, "b": None, "r2": None, "n": len(pts),
                "confidence": "none"}
    n = len(pts)
    mx = sum(x for x, _ in pts) / n
    my = sum(y for _, y in pts) / n
    sxx = sum((x - mx) ** 2 for x, _ in pts)
    sxy = sum((x - mx) * (y - my) for x, y in pts)
    syy = sum((y - my) ** 2 for _, y in pts)
    if sxx <= 0:
        return {"avg": avg, "marginal": None, "b": None, "r2": None, "n": n, "confidence": "none"}
    b = sxy / sxx
    r2 = (sxy * sxy / (sxx * syy)) if syy > 0 else 0.0
    cv = math.sqrt(sxx / n)                      # spread of log-spend ≈ CV of spend
    bc = min(max(b, 0.0), 1.5)
    conf = "low" if (cv < 0.15 or r2 < 0.2) else ("medium" if r2 < 0.5 else "high")
    return {"avg": avg, "marginal": round(bc * (tot_g / tot_c), 2), "b": round(b, 2),
            "r2": round(r2, 2), "n": n, "spend_cv": round(cv, 2), "confidence": conf}


BRAND_RX = re.compile(r"(^|[-_ |])brand([-_ |]|$)", re.I)
STEP = 0.10                  # the tROAS move we suggest: ±10 %
NOISE = 0.10                 # |incremental marginal − 1| below this → hold


def google_type(name: str, channel: str) -> tuple[str, str]:
    """(segment, INCREMENTALITY key). Brand = 'brand' as a name token; any brand
    campaign (search or shopping) takes the brand-search rate."""
    if BRAND_RX.search(name or ""):
        return "brand", "google_brand_search"
    if channel == "SEARCH":
        return "non-brand", "google_generic_search"
    return "non-brand", "google_shopping_pmax"


def _curves() -> dict[str, dict]:
    """Latest ROAS-Simulations curve per strategy, κ-calibrated (v_kappa_calibrated),
    anchor point excluded. strategy_id = campaign id for campaign-level strategies."""
    from google.cloud import bigquery
    c = bigquery.Client(project=LEGACY)
    ds = f"`{LEGACY}.roas_sims"
    rows = c.query(f"""
        SELECT s.strategy_id, s.customer_name, s.run_date, s.current_target,
               s.target_roas, s.cost, s.conversions_value, k.k_cost, k.k_value, k.inc_class
        FROM {ds}.sim_points` s
        LEFT JOIN {ds}.v_kappa_calibrated` k USING (customer_name, strategy_id)
        WHERE s.run_date = (SELECT MAX(run_date) FROM {ds}.sim_points`)
          AND NOT s.is_anchor AND s.target_roas > 0""").result()
    out: dict[str, dict] = {}
    for r in rows:
        cv = out.setdefault(str(r["strategy_id"]), {
            "run_date": r["run_date"].isoformat(), "current_target": r["current_target"],
            "k_cost": r["k_cost"], "k_value": r["k_value"], "inc_class": r["inc_class"],
            "points": []})
        cv["points"].append((float(r["target_roas"]), float(r["cost"] or 0),
                             float(r["conversions_value"] or 0)))
    for cv in out.values():
        cv["points"].sort()
    return out


def curve_marginal(cv: dict, target: float) -> dict | None:
    """Marginal profit per krona around `target` on a calibrated curve.

    down: what the EXTRA spend from a 10 % lower target returns (Δvalue ÷ Δcost);
    up:   what the spend we would SAVE with a 10 % higher target was returning.
    Both axes deflated by κ (k_cost/k_value). None when the step falls outside the
    simulated range (Google's simulator does not extrapolate, neither do we)."""
    from roas_sims_bq import _interp
    pts = cv["points"]
    if len(pts) < 3 or not target:
        return None
    kc, kv = cv.get("k_cost") or 1.0, cv.get("k_value") or 1.0
    c0, v0 = _interp(pts, target)
    cd, vd = _interp(pts, target * (1 - STEP))
    cu, vu = _interp(pts, target * (1 + STEP))
    c0, v0, cd, vd, cu, vu = c0 * kc, v0 * kv, cd * kc, vd * kv, cu * kc, vu * kv
    floor = 0.01 * max(c0, 1.0)
    down = (vd - v0) / (cd - c0) if cd - c0 > floor else None
    up = (v0 - vu) / (c0 - cu) if c0 - cu > floor else None
    if down is None and up is None:
        return None
    lo, hi = pts[0][0], pts[-1][0]
    return {"down": None if down is None else round(down, 2),
            "up": None if up is None else round(up, 2),
            "extra_cost_pct": round((cd - c0) / c0, 3) if c0 else None,
            "in_range": lo <= target <= hi, "kappa": [round(kc, 2), round(kv, 2)]}


def suggest(inc_down, inc_up, headroom: bool, troas, cooldown_until, fallback: bool) -> dict:
    """The tROAS move. Spend more (lower tROAS) where the extra krona still earns
    > 1 incremental profit and there is demand left to buy; spend less (raise tROAS)
    where the krona we would cut earns < 1. Otherwise hold."""
    if not troas:
        return {"action": "none", "cls": "muted", "text": "No tROAS (other bidding)"}
    act = None
    if inc_down is not None and inc_down > 1 + NOISE and headroom:
        act, new = "lower", troas * (1 - STEP)
    elif inc_up is not None and inc_up < 1 - NOISE:
        act, new = "raise", troas * (1 + STEP)
    if not act:
        why = ("no headroom" if inc_down is not None and inc_down > 1 + NOISE and not headroom
               else "near break-even" if (inc_down or inc_up) is not None else "no estimate")
        return {"action": "hold", "cls": "muted", "text": f"Hold ({why})"}
    txt = f"{'Lower' if act == 'lower' else 'Raise'} tROAS {troas:.2f} → {new:.2f} via apply_troas.py"
    if fallback:
        txt += " (weak estimate: verify)"
    if cooldown_until:
        txt += f", after cooldown {cooldown_until}"
    cls = ("muted" if cooldown_until else "up" if act == "lower" else "down")
    return {"action": act, "cls": cls, "text": txt, "new_troas": round(new, 2),
            "blocked_until": cooldown_until}


def _market_margins(d0: datetime.date, d1: datetime.date) -> dict[str, float]:
    """GP2 ÷ net revenue per market, Babyshop shop, all channels (Funnel export:
    the only place a margin exists; Norce has no cost)."""
    import bq_source as bs
    from google.cloud import bigquery
    rows = bs._rows(
        f"WITH {bs._kv_cte('Date BETWEEN @s AND @e')} "
        f"SELECT market_level_1_kv mk, {bs.KVD} FROM kv_rows WHERE shop_new = 'Babyshop' GROUP BY mk",
        [bigquery.ScalarQueryParameter("s", "DATE", d0),
         bigquery.ScalarQueryParameter("e", "DATE", d1)])
    acc = defaultdict(lambda: [0.0, 0.0])
    for r in rows:
        a = acc[_mk(r["mk"])]
        a[0] += float(r["gp2"] or 0); a[1] += float(r["net_rev"] or 0)
    return {m: round(g / v, 3) for m, (g, v) in acc.items() if v > 0}


def find_headroom(p) -> tuple[str | None, object]:
    """Pacing's spend-headroom block, wherever it sits (key containing 'headroom')."""
    stack = [("", p)]
    while stack:
        path, node = stack.pop(0)
        if isinstance(node, dict):
            for k, v in node.items():
                if "headroom" in str(k).lower():
                    return (path + "." + k).lstrip("."), v
                stack.append((path + "." + str(k), v))
    return None, None


def build(ctx: dict) -> dict:
    today: datetime.date = ctx["today"]
    yday = today - datetime.timedelta(days=1)
    d0 = min(bm.MONTH_START, today) - datetime.timedelta(days=HIST_DAYS)
    days = [d.isoformat() for d in bm.month_days()]
    notes: list[str] = []
    errors: list[str] = []

    index, index_src = _plan_index()
    m_plan_mkt = {m: [round(bm.META_PLAN_MONTH * s * w) for w in index]
                  for m, s in bm.META_PLAN_MARKET_SHARE.items()}
    m_plan_type = {t: [round(v * w) for w in index] for t, v in bm.META_PLAN_BY_TYPE.items()}

    # ── sources ──────────────────────────────────────────────────────────────
    fx = {"SEK": 1.0, "NOK": 0.93, "EUR": 11.0, "DKK": 1.47}
    try:
        fx.update(_fx())
    except Exception as e:
        errors.append(f"FX: {e}")
    try:
        g = _google(today, d0)
        errors += g.pop("errors")
    except Exception as e:
        g = {"accounts": [], "daily": [], "campaigns": [], "seasonality": [], "platform_nvr": {}}
        errors.append(f"Google Ads API: {type(e).__name__}: {str(e)[:300]}")
    try:
        changes = _target_changes(today - datetime.timedelta(days=45))
    except Exception as e:
        changes = []
        errors.append(f"target_changes: {e}")
    funnel = _funnel(d0, yday)
    try:
        curves = _curves()
    except Exception as e:
        curves = {}
        errors.append(f"ROAS-sims curves: {e}")
    l28_lo = today - datetime.timedelta(days=28)
    try:
        margins = _market_margins(l28_lo, yday)
    except Exception as e:
        margins = {}
        errors.append(f"Funnel GP2 margin: {e}")
    try:
        norce = _norce(d0, yday)
    except Exception as e:
        norce = []
        errors.append(f"Norce ordered revenue: {e}")
    meta = _meta(d0, today)
    try:
        products = _meta_products(bm.MONTH_START if today > bm.MONTH_START else d0, today)
    except Exception as e:
        products = []
        errors.append(f"Meta products: {e}")
    l28 = (today - datetime.timedelta(days=28), yday)
    mtd = (bm.MONTH_START, yday) if yday >= bm.MONTH_START else l28
    try:
        nvr_raw = _nvr({"mtd": mtd, "l28": l28})
    except Exception as e:
        nvr_raw = {}
        errors.append(f"New vs returning: {e}")

    in_month = lambda iso: bm.MONTH_START.isoformat() <= iso <= bm.MONTH_END.isoformat()
    di = {d: i for i, d in enumerate(days)}

    # ── Google pacing (Funnel SEK, + API today-so-far) ───────────────────────
    g_act = {m: [None] * len(days) for m in MKTS}
    for r in funnel:
        if r["channel"] == "google" and r["d"] in di:
            i = di[r["d"]]
            g_act[r["market"]][i] = (g_act[r["market"]][i] or 0) + r["cost"]
    acc_cur = {a["cid"]: a["currency"] for a in g["accounts"]}
    today_iso = today.isoformat()
    g_today = defaultdict(float)
    for r in g["daily"]:
        if r["d"] == today_iso:
            g_today[r["market"]] += r["cost"] * fx.get(acc_cur.get(r["cid"], "SEK"), 1.0)
    # Context only: Google has no budget, spend is judged by incremental GP3.
    google_pacing = {m: {"actual": [_r(v) for v in g_act[m]],
                         "today_so_far": _r(g_today.get(m))} for m in MKTS}

    # ── Google campaigns ─────────────────────────────────────────────────────
    last_change = {}
    for ch in changes:                                  # newest first
        last_change.setdefault(str(ch["campaign_id"]), ch)
    win = lambda n: ((today - datetime.timedelta(days=n)).isoformat(), yday.isoformat())
    w7, w14 = win(7), win(14)
    agg = defaultdict(lambda: defaultdict(float))
    for r in g["daily"]:
        a = agg[r["id"]]
        if r["d"] >= bm.MONTH_START.isoformat() and r["d"] <= yday.isoformat():
            a["mtd_cost"] += r["cost"]; a["mtd_value"] += r["value"]
        if w7[0] <= r["d"] <= w7[1]:
            a["l7_cost"] += r["cost"]; a["l7_value"] += r["value"]
        if w14[0] <= r["d"] <= w14[1]:
            a["l14_cost"] += r["cost"]; a["l14_value"] += r["value"]
        if r["d"] == today_iso:
            a["today_cost"] += r["cost"]
        if r["d"] == yday.isoformat():
            a["yday_cost"] += r["cost"]
    pairs_c = defaultdict(list)
    for r in g["daily"]:
        if l28_lo.isoformat() <= r["d"] <= yday.isoformat():
            pairs_c[r["id"]].append((r["cost"], r["value"]))
    campaigns = []
    for c in g["campaigns"]:
        a = agg.get(c["id"], {})
        f = fx.get(c["currency"], 1.0)
        lc = last_change.get(c["id"])
        dsc = (today - datetime.date.fromisoformat(lc["d"])).days if lc else None
        lb = c.get("lost_budget")
        limited = (c["status"] in ("LIMITED_BY_BUDGET", "MISCONFIGURED_SHARED_BUDGET")
                   or "BUDGET_CONSTRAINED" in " ".join(c["reasons"])
                   or (lb is not None and lb >= 0.10))
        row = {
            "market": c["market"], "id": c["id"], "name": c["name"],
            "channel": c["channel"].title().replace("_", " "),
            "bidding": c["bidding"].replace("_", " ").title(), "troas": c["troas"],
            "currency": c["currency"],
            "budget_day": _r(c["budget"]), "budget_day_sek": _r((c["budget"] or 0) * f) if c["budget"] else None,
            "budget_shared": c["budget_name"],
            "mtd_cost_sek": _r(a.get("mtd_cost", 0) * f),
            "mtd_roas": _div(a.get("mtd_value"), a.get("mtd_cost")),
            "l7_cost_sek": _r(a.get("l7_cost", 0) * f),
            "l7_roas": _div(a.get("l7_value"), a.get("l7_cost")),
            "l14_cost_sek": _r(a.get("l14_cost", 0) * f),
            "l14_roas": _div(a.get("l14_value"), a.get("l14_cost")),
            "today_cost_sek": _r(a.get("today_cost", 0) * f),
            "budget_use_today": _div(a.get("today_cost", 0), c["budget"]) if c["budget"] else None,
            "is": _r(c["is"], 3), "lost_budget": _r(lb, 3), "lost_rank": _r(c["lost_rank"], 3),
            "status": c["status"].replace("_", " ").lower(), "budget_limited": bool(limited),
            "last_change": ({"date": lc["d"], "old": lc["old_target"], "new": lc["new_target"],
                             "source": lc["source"]} if lc else None),
            "days_since_change": dsc,
            "cooldown": dsc is not None and dsc < COOLDOWN_DAYS,
            "cooldown_until": ((datetime.date.fromisoformat(lc["d"]) +
                                datetime.timedelta(days=COOLDOWN_DAYS)).isoformat()
                               if lc and dsc is not None and dsc < COOLDOWN_DAYS else None),
        }
        if not (row["l14_cost_sek"] or row["mtd_cost_sek"] or row["today_cost_sek"]):
            continue                                     # enabled but idle
        # Marginal profit ROAS: ROAS-sims curve first, recent spend/return fit second.
        seg, inc_key = google_type(c["name"], c["channel"])
        inc = bm.INCREMENTALITY.get(inc_key)
        cm = curve_marginal(curves[c["id"]], c["troas"]) if c["troas"] and c["id"] in curves else None
        basis, m_down, m_up, extra = None, None, None, None
        if cm:
            basis, m_down, m_up, extra = "curve", cm["down"], cm["up"], cm["extra_cost_pct"]
        else:
            fit = _marginal(pairs_c.get(c["id"], []))
            if fit.get("marginal") is not None and fit["confidence"] != "none":
                basis = "fit"
                m_down = m_up = fit["marginal"]
        inc_down = None if m_down is None or inc is None else round(m_down * inc, 2)
        inc_up = None if m_up is None or inc is None else round(m_up * inc, 2)
        lr = c.get("lost_rank")
        headroom = bool((lr is not None and lr >= 0.10) or (extra is not None and extra >= 0.05))
        row.update({
            "segment": seg, "inc_key": inc_key, "incrementality": inc,
            "yday_cost_sek": _r(a.get("yday_cost", 0) * f),
            "marginal_roas": m_down, "marginal_roas_up": m_up, "marginal_basis": basis,
            "curve_run": curves[c["id"]]["run_date"] if cm else None,
            "inc_marginal": inc_down, "inc_marginal_up": inc_up,
            "inc_gp3_per_10k": None if inc_down is None else _r((inc_down - 1) * 10_000),
            "headroom": headroom,
            "headroom_why": ", ".join(x for x in (
                f"lost IS rank {lr:.0%}" if lr is not None and lr >= 0.10 else "",
                f"+{extra:.0%} spend available at −10 % tROAS" if extra is not None and extra >= 0.05 else "")
                if x) or None,
        })
        sg = suggest(inc_down, inc_up, headroom, c["troas"], row["cooldown_until"], basis == "fit")
        if row["budget_limited"]:
            sg = {"action": "error", "cls": "down", "text": "ERROR: limited by budget (budgets must never bind)"}
        row["suggestion"] = sg
        row["hint_class"], row["hint"] = sg["cls"], sg["text"]     # kept for bm_insights
        campaigns.append(row)
    campaigns.sort(key=lambda r: -(r["l14_cost_sek"] or 0))

    # Post-BF seasonality check (agreed 2026-11-04: 1 Dec −45 %, 2 Dec −30 %).
    post_bf = {}
    for mk in GOOGLE_ACCOUNTS.values():
        adj = [s for s in g["seasonality"] if s["market"] == mk]
        pb = [s for s in adj if re.search(r"post.?bf", s["name"], re.I)]
        on = [s for s in pb if s["status"] == "ENABLED"]
        post_bf[mk] = {"day1": next((s for s in on if s["start"].startswith("2026-12-01")), None),
                       "day2": next((s for s in on if s["start"].startswith("2026-12-02")), None),
                       "n": len(pb)}
    seasonality = [{**s, "modifier_pct": round((s["modifier"] - 1) * 100)} for s in g["seasonality"]]

    # Platform new vs returning conversions (Google's own customer-match label).
    platform_nvr = {}
    for mk, d in g["platform_nvr"].items():
        new, ret = d.get("NEW", {}), d.get("RETURNING", {})
        tot = sum(v.get("conv", 0) for v in d.values())
        platform_nvr[mk] = {"new_conv": _r(new.get("conv")), "ret_conv": _r(ret.get("conv")),
                            "unknown_conv": _r(d.get("UNKNOWN", {}).get("conv")),
                            # only accounts with customer-acquisition data label NEW/RETURNING
                            "new_share": _div(new.get("conv"), tot, 3) if new else None}

    # ── Meta pacing + tables ─────────────────────────────────────────────────
    m_mkt = {m: [None] * len(days) for m in MKTS}
    types = list(bm.META_PLAN_BY_TYPE) + ["brand_funded", "unclassified"]
    m_type = {t: [None] * len(days) for t in types}
    m_mkt_type = defaultdict(float)          # MTD market × type, for filtered views
    for r in meta:
        if r["d"] in di and r["d"] <= today_iso:
            i = di[r["d"]]
            m_mkt[r["market"]][i] = (m_mkt[r["market"]][i] or 0) + r["spend"]
            m_type[r["type"]][i] = (m_type[r["type"]][i] or 0) + r["spend"]
            m_mkt_type[(r["market"], r["type"])] += r["spend"]
    meta_pacing = {
        "market": {m: {"plan": m_plan_mkt.get(m, [0] * len(days)),
                       "actual": [_r(v) for v in m_mkt[m]]} for m in MKTS},
        "type": {t: {"plan": m_plan_type.get(t, [0] * len(days)),
                     "actual": [_r(v) for v in m_type[t]]} for t in types},
        "market_type_mtd": [{"market": m, "type": t, "spend": _r(v)}
                            for (m, t), v in sorted(m_mkt_type.items())],
    }

    def roll(rows, keys, lo, hi):
        acc = defaultdict(lambda: defaultdict(float))
        for r in rows:
            if lo <= r["d"] <= hi:
                a = acc[tuple(r[k] for k in keys)]
                for f in ("spend", "purchases", "value", "clicks"):
                    a[f] += r[f]
        return acc

    m_lo = bm.MONTH_START.isoformat() if today > bm.MONTH_START else w7[0]
    mtd_c = roll(meta, ("market", "campaign", "type"), m_lo, today_iso)
    l7_c = roll(meta, ("market", "campaign", "type"), w7[0], w7[1])
    p7_c = roll(meta, ("market", "campaign", "type"),
                (today - datetime.timedelta(days=14)).isoformat(),
                (today - datetime.timedelta(days=8)).isoformat())
    meta_campaigns = []
    for k in set(mtd_c) | set(l7_c):
        a, b, p = mtd_c.get(k, {}), l7_c.get(k, {}), p7_c.get(k, {})
        if not (a.get("spend") or b.get("spend")):
            continue
        meta_campaigns.append({
            "market": k[0], "campaign": k[1], "type": k[2],
            "mtd_spend": _r(a.get("spend", 0)), "mtd_purchases": _r(a.get("purchases", 0)),
            "mtd_value": _r(a.get("value", 0)), "mtd_roas": _div(a.get("value"), a.get("spend")),
            "mtd_cpa": _div(a.get("spend"), a.get("purchases"), 0),
            "l7_spend": _r(b.get("spend", 0)), "l7_roas": _div(b.get("value"), b.get("spend")),
            "l7_cpa": _div(b.get("spend"), b.get("purchases"), 0),
            "p7_roas": _div(p.get("value"), p.get("spend"))})
    meta_campaigns.sort(key=lambda r: -(r["l7_spend"] or 0) - (r["mtd_spend"] or 0))

    l7_a = roll(meta, ("market", "campaign", "adset", "type"), w7[0], w7[1])
    meta_adsets = sorted(({
        "market": k[0], "campaign": k[1], "adset": k[2], "type": k[3],
        "l7_spend": _r(v["spend"]), "l7_purchases": _r(v["purchases"]),
        "l7_roas": _div(v["value"], v["spend"]), "l7_cpa": _div(v["spend"], v["purchases"], 0),
        "l7_cpc": _div(v["spend"], v["clicks"])} for k, v in l7_a.items() if v["spend"] > 0),
        key=lambda r: -r["l7_spend"])[:60]

    unclassified = sorted({r["campaign"] for r in meta if r["type"] == "unclassified"})

    # Meta lines (market × campaign type): marginal revenue ROAS from 28 daily
    # spend/revenue pairs → GP2 with the Funnel margin → × INCREMENTALITY.
    line_days = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0]))
    for r in meta:
        if l28_lo.isoformat() <= r["d"] <= yday.isoformat():
            dd = line_days[(r["market"], r["type"])][r["d"]]
            dd[0] += r["spend"]; dd[1] += r["value"]
    lc = defaultdict(lambda: [0.0, 0.0])          # Funnel last-click Meta GP2 / spend, L28
    for r in funnel:
        if r["channel"] == "meta" and r["d"] >= l28_lo.isoformat():
            lc[r["market"]][0] += r["gp2"]; lc[r["market"]][1] += r["cost"]
    meta_lines = []
    for (mk_, t), dd in sorted(line_days.items(), key=lambda kv: (MKTS.index(kv[0][0]), kv[0][1])):
        prs = list(dd.values())
        sp = sum(x for x, _ in prs)
        if sp < 500:
            continue
        fit = _marginal(prs)
        inc = bm.INCREMENTALITY.get(f"meta_{t}")
        mg = margins.get(mk_)
        if fit.get("marginal") is not None and fit["confidence"] in ("medium", "high"):
            basis, mrev = "fit", fit["marginal"]
        else:
            basis, mrev = "average (upper bound)", fit.get("avg")
        inc_m = (round(mrev * mg * inc, 2) if None not in (mrev, mg, inc) else None)
        if inc_m is None:
            act = {"action": "none", "cls": "muted",
                   "text": "Outside the plan types" if inc is None else "No estimate"}
        elif inc_m > 1 + NOISE:
            act = ({"action": "up", "cls": "up", "text": "Scale up"} if basis == "fit" else
                   {"action": "test", "cls": "warn", "text": "Test +20 % (upper-bound estimate)"})
        elif inc_m < 1 - NOISE:
            act = {"action": "down", "cls": "down", "text": "Scale down"}
        else:
            act = {"action": "hold", "cls": "muted", "text": "Hold (near break-even)"}
        mtd_sp = m_mkt_type.get((mk_, t), 0.0)
        meta_lines.append({
            "market": mk_, "type": t, "l28_spend": _r(sp), "mtd_spend": _r(mtd_sp),
            "plan_month": (_r(bm.META_PLAN_BY_TYPE[t] * bm.META_PLAN_MARKET_SHARE.get(mk_, 0))
                           if t in bm.META_PLAN_BY_TYPE else None),
            "avg_roas": fit.get("avg"), "gp2_margin": mg,
            "avg_gp2_roas_platform": None if None in (fit.get("avg"), mg) else round(fit["avg"] * mg, 2),
            "lastclick_gp2_roas_market": _div(*lc[mk_]) if lc[mk_][1] else None,
            "marginal_roas": mrev, "marginal_basis": basis, "fit": fit,
            "incrementality": inc, "inc_marginal": inc_m,
            "inc_gp3_per_10k": None if inc_m is None else _r((inc_m - 1) * 10_000),
            "suggestion": act})
    rules_text = [f"{t}: /{rx}/" for t, rx in TYPE_RULES]

    def prod_roll(key):
        acc = defaultdict(lambda: defaultdict(float))
        for p in products:
            a = acc[(p["market"], p.get(key) or "Unknown")]
            for f in ("spend", "purchases", "value"):
                a[f] += p[f]
        return [{"market": k[0], "name": k[1], "spend": _r(v["spend"]),
                 "purchases": _r(v["purchases"]), "roas": _div(v["value"], v["spend"])}
                for k, v in acc.items()]
    top_products = sorted(products, key=lambda p: -p["spend"])[:150]
    meta_products = {
        "window": [m_lo, today_iso],
        "products": [{"market": p["market"], "pid": p["pid"], "name": (p["name"] or "")[:90],
                      "brand": p.get("brand"), "category": p.get("subcategory"),
                      "spend": _r(p["spend"]), "purchases": _r(p["purchases"]),
                      "roas": _div(p["value"], p["spend"])} for p in top_products],
        "brands": prod_roll("brand"), "categories": prod_roll("category"),
        "coverage_spend": _r(sum(p["spend"] for p in products)),
    }

    # ── New vs returning (order level) ───────────────────────────────────────
    def spend_in(ch, lo, hi):
        acc = defaultdict(float)
        for r in funnel:
            if r["channel"] == ch and lo <= r["d"] <= hi:
                acc[r["market"]] += r["cost"]
        return acc
    nvr = {"windows": {}, "basis": (
        "Orders (Norce, Babyshop) matched to their GA4 purchase session; channel = the "
        "session's source/medium (GA4 session attribution). New = customer's first "
        "order (Norce first-order flag). Counts are scaled up by each market's match rate "
        "before cost per new customer = Funnel de-duplicated spend ÷ new customers. "
        "The last ~2 days are still settling (lower match rate).")}
    for key, (s, e) in {"mtd": mtd, "l28": l28}.items():
        rows = nvr_raw.get(key, [])
        sp = {"google": spend_in("google", s.isoformat(), e.isoformat()),
              "meta": spend_in("meta", s.isoformat(), e.isoformat())}
        acc = defaultdict(lambda: defaultdict(float))
        totals = defaultdict(lambda: defaultdict(float))
        for r in rows:
            a = acc[(r["market"], r["channel"])]
            for f in ("orders", "new", "rev"):
                a[f] += r[f]
            t = totals[r["market"]]
            t["matched"] += r["orders"]
            t["all"] += r["all_orders"]
        out_rows = []
        for (mk, ch), a in acc.items():
            spend = sp.get(ch, {}).get(mk)
            if not a["orders"] and not spend:
                continue
            # Only matched orders carry a channel. Scale counts up by the market's
            # match rate so cost per new customer is not inflated by the unmatched.
            t = totals[mk]
            scale = (t["all"] / t["matched"]) if t["matched"] else 1.0
            new_est = a["new"] * scale
            out_rows.append({"market": mk, "channel": ch, "orders": int(a["orders"]),
                             "new": int(a["new"]), "new_share": _div(a["new"], a["orders"], 3),
                             "new_est": _r(new_est),
                             "spend": _r(spend) if spend is not None else None,
                             "cost_per_new": _div(spend, new_est, 0) if spend and new_est else None,
                             "cost_per_order": (_div(spend, a["orders"] * scale, 0)
                                                if spend and a["orders"] else None)})
        nvr["windows"][key] = {
            "from": s.isoformat(), "to": e.isoformat(),
            "rows": sorted(out_rows, key=lambda r: (MKTS.index(r["market"]), -r["orders"])),
            "match_rate": {m: _div(t["matched"], t["all"], 3) for m, t in totals.items()}}
    nvr["google_platform"] = platform_nvr

    # ── GP3 + marginal ROAS per market × channel ─────────────────────────────
    gp3 = {}
    for key, (s, e) in {"mtd": mtd, "l14": (today - datetime.timedelta(days=14), yday),
                        "l28": l28}.items():
        acc = defaultdict(lambda: defaultdict(float))
        for r in funnel:
            if s.isoformat() <= r["d"] <= e.isoformat():
                a = acc[(r["market"], r["channel"])]
                a["cost"] += r["cost"]; a["gp2"] += r["gp2"]; a["rev"] += r["rev"]
        gp3[key] = {"from": s.isoformat(), "to": e.isoformat(), "rows": [
            {"market": k[0], "channel": k[1], "spend": _r(v["cost"]), "gp2": _r(v["gp2"]),
             "gp3": _r(v["gp2"] - v["cost"]), "gp2_roas": _div(v["gp2"], v["cost"])}
            for k, v in sorted(acc.items(), key=lambda kv: (MKTS.index(kv[0][0]), kv[0][1]))]}
        # Actual sales anchor per market: Norce ordered revenue vs total paid spend.
        mk_acc = defaultdict(lambda: defaultdict(float))
        for r in norce:
            if s.isoformat() <= r["d"] <= e.isoformat():
                a = mk_acc[r["market"]]
                a["merch"] += r["merch"]; a["rev"] += r["merch"] + r["freight"]; a["orders"] += r["orders"]
        for (m, _ch), v in acc.items():
            mk_acc[m]["paid"] += v["cost"]
        gp3[key]["markets"] = [
            {"market": m, "norce_rev": _r(v["rev"]), "norce_merch": _r(v["merch"]),
             "orders": _r(v["orders"]), "paid_spend": _r(v["paid"]),
             "paid_cos": _div(v["paid"], v["merch"], 3), "cos_target": bm.COS_TARGET}
            for m, v in sorted(mk_acc.items(), key=lambda kv: MKTS.index(kv[0]))]
    pairs = defaultdict(list)
    for r in funnel:
        if r["d"] >= l28[0].isoformat():
            pairs[(r["market"], r["channel"])].append((r["cost"], r["gp2"]))
    marginal = [{"market": k[0], "channel": k[1], **_marginal(v)}
                for k, v in sorted(pairs.items(), key=lambda kv: (MKTS.index(kv[0][0]), kv[0][1]))]

    # ── Portfolio guardrail (pacing's spend headroom) ───────────────────────
    hr_path, hr = None, None
    try:
        hr_path, hr = find_headroom(bm.read_section("pacing") or {})
    except Exception as e:
        errors.append(f"pacing snapshot: {e}")
    def _opps(rows, key):
        up = [r for r in rows if (r.get("inc_marginal") or 0) > 1 + NOISE]
        dn = [r for r in rows if r.get("inc_marginal") is not None and r["inc_marginal"] < 1 - NOISE]
        return {"more": len(up), "less": len(dn)}
    guardrail = {
        "rule": "Spend more where incremental marginal GP3 > 0 (incremental marginal ROAS > 1); "
                "spend less where it is < 0. The pacing headroom shows how much extra spend "
                "Finance's GP3 target already covers.",
        "headroom": hr, "headroom_path": hr_path,
        "google": _opps(campaigns, "inc_marginal"), "meta": _opps(meta_lines, "inc_marginal"),
        "budget_errors": [c["name"] for c in campaigns if c["budget_limited"]],
        "incrementality": bm.INCREMENTALITY, "cos_target": bm.COS_TARGET,
    }

    # ── Notes ────────────────────────────────────────────────────────────────
    notes += [
"Platform-reported: Google ROAS = profit ÷ spend (conversion value is PROFIT in the Babyshop accounts); "
        "tROAS is on the same profit basis. Last 1–3 days are still filling in (conversion lag), "
        "so judge campaigns on L14, not yesterday.",
        "Google has no budget: spend is context, judged by incremental GP3. Google spend = Funnel "
        "de-duplicated SEK cost through yesterday; today = live API spend × Norce FX. Babyshop "
        "accounts only. Any campaign limited by budget is flagged as an error.",
        "Marginal profit ROAS (Google) comes from the ROAS-Simulations curves, κ-calibrated "
        "(v_kappa_calibrated; λ slope gate not applied): the extra profit per krona from a 10 % "
        "lower tROAS ('down'), and what the krona cut by a 10 % higher tROAS earned ('up'). "
        "Campaigns without a curve fall back to a 28-day spend/profit fit, marked 'fit' (weak).",
        "INCREMENTALITY factors are ASSUMPTIONS until tested (brand search 0.30, generic search 0.80, "
        "shopping/PMax 0.70, Meta prospecting/Kuling 0.60, retargeting 0.30). Brand Shopping takes the "
        "brand rate. Incremental marginal = marginal × factor; incremental GP3 per extra 10 k = "
        "(incremental marginal − 1) × 10 k.",
        "Meta marginal: 28-day spend/revenue fit per market × type (or the average ROAS as an upper "
        "bound when the fit is weak) × Funnel GP2 margin of that market × incrementality. The "
        "approved 200 k plan is the starting point, not a cap. CAUTION: Meta pixel value runs "
        "several times above what GA4 last click credits Meta (compare the last-click GP2 ROAS "
        "column), so a 0.6 incrementality on pixel value may be generous; treat 'scale up' as "
        "a test until a lift test sets the factor.",
        "Meta = warehouse (agg_daily_kpis_by_ad, SEK). ROAS/CPA are platform-reported: Meta pixel "
        "purchase value (revenue, not profit) under Meta's own attribution.",
        "Meta campaign type is classified from the campaign name (rules shown with the table). "
        "'Kuling-catalog' counts as Kuling, not catalog retargeting; 'mktcontrib' campaigns are "
        "brand-funded and kept outside the plan.",
        "Product / brand / category view covers catalog (DPA) delivery only — Meta reports product "
        "breakdowns for catalog ads, not for single-image/video ads.",
        "GP3 = GP2 from the Funnel export (Norce has no cost data, so margins can only come from "
        "there), attributed to channel by GA4 last click, − de-duplicated cost, Babyshop shop. "
        "Last-click under-credits Meta's view-through effect. Actual sales per market are Norce "
        "ordered revenue (refresh_daily_perf definition: Stockholm day, ex VAT, gross of returns, "
        "every status except 6); paid COS = Google + Meta spend ÷ Norce merchandise revenue.",
        "Marginal ROAS is an ESTIMATE: log-log fit of daily GP2 vs spend over the last 28 days, "
        "slope × average ROAS. Demand moves spend and return together, so it flatters; use it to "
        "rank where the next krona goes, not as a forecast. Below 1.0 the last krona loses GP3.",
    ]

    return {
        "asof": yday.isoformat(), "today": today_iso, "days": days, "fx": fx,
        "guardrail": guardrail,
        "plan": {"meta_month": bm.META_PLAN_MONTH, "meta_by_type": bm.META_PLAN_BY_TYPE,
                 "meta_share": bm.META_PLAN_MARKET_SHARE, "index_source": index_src},
        "google": {"pacing": google_pacing, "campaigns": campaigns,
                   "changes": [{**c, "predicted_cost_pct": _r(c["predicted_cost_pct"], 1),
                                "predicted_gp3_pct": _r(c["predicted_gp3_pct"], 1)}
                               for c in changes[:60]],
                   "seasonality": seasonality, "post_bf": post_bf,
                   "accounts": g["accounts"], "cooldown_days": COOLDOWN_DAYS},
        "meta": {"pacing": meta_pacing, "lines": meta_lines, "margins": margins,
                 "campaigns": meta_campaigns, "adsets": meta_adsets,
                 "unclassified": unclassified, "type_rules": rules_text,
                 "products": meta_products},
        "nvr": nvr,
        "gp3": gp3, "marginal": marginal,
        "notes": notes, "errors": errors,
    }
