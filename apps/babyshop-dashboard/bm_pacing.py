#!/usr/bin/env python3
"""
bm_pacing.py: the Black Month "Pacing & forecast" section.

Answers: how are we tracking against Finance's November forecast, month to date
and per day, and where does the month land if the recent run-rate holds?

Sources (all read-only), and which metric comes from where (BASIS):
  • Gross sales (= merchandise), orders, units, AOV: Norce ORDERED data in
    BigQuery, on refresh_daily_perf's exact definition (its DAILY_SQL: Stockholm
    trading day, every status except 6, merchandise excluding the shipping
    pseudo-SKU, freight carried separately). Merchandise is what the forecast's
    Gross Sales line measures, so plan vs actual is like for like.
  • Net sales, GP1, GP2, ad cost, GP3: the Funnel export through bq_source's
    de-duplicated `kv_rows` CTE and KVD aggregate (the KV Overview's numbers).
    Norce carries no cost (CostUnit is empty) and no returns, so none of these
    can be built on the ordered basis.
  • Daily targets: daily_targets.month_targets(), the KV Overview's own model.
    It spreads budget_2026.json's November figure by an index that lines
    November 2025 up with November 2026 by EVENT (Black Friday, payday,
    weekday). A second, October-shaped curve is built here for comparison.
  • New vs returning: the Norce `customer_orders` mart (order date), first
    order per customer x shop = new.

Every series is per market ('all', SE, NO, FI, DK, ROW) so the page can filter
without another fetch. The page reads `markets[m].summary` for the KPI cards;
`insights` can read the same summary from the stored snapshot.
"""
from __future__ import annotations

import datetime

from google.cloud import bigquery

import bm_common as bm
import bq_source as bs
import daily_targets as dt
import refresh_daily_perf as rdp

MKTS = ["all"] + bm.MARKETS + ["ROW"]
# Brand = shop. The forecast, Finance's P&L (BC) and both data sources cover
# Babyshop + Lekmer together; 'all' is that combined scope.
BRANDS = ["all", "babyshop", "lekmer"]
SEGS = [f"{b}|{m}" for b in BRANDS for m in MKTS]


def _keys(brand: str, market: str) -> tuple:
    """The four segment keys one (brand, market) row rolls up into."""
    return (f"{brand}|{market}", f"{brand}|all", f"all|{market}", "all|all")
# Event-aligned last year: -364 days keeps the weekday AND lines Black Friday up
# with Black Friday (27 Nov 2026 -> 28 Nov 2025) and Cyber Monday with Cyber
# Monday (30 Nov 2026 -> 1 Dec 2025).
LY_SHIFT = datetime.timedelta(days=364)
RUN_RATE_DAYS = 7
# Daily shapes of the same monthly forecast (all sum to Finance's figure):
#   event  Nov 2025 lined up by event (BF, CM, payday, weekday), taken in full.
#          daily_targets' default shrinks that shape 70% toward a flat month
#          (PRIOR_SHRINK, tuned on Feb-Jul, which have no Black Friday); in
#          November that puts ~10% of the month in 1-3 Nov against 5% last year
#          and flattens BF, so pacing reads far behind early and the landing
#          collapses. Primary curve for this tab.
#   kv     daily_targets as the KV Overview uses it (shrunk), for reconciliation.
#   oct    October 2026's day-of-month profile (the "last month" shape).
SHAPES = ("event", "kv", "oct")
# Metrics carried per day. orders/units/freight have no budget line (no
# target). The P&L ladder is built per day by _derive():
#   net  = Norce merchandise - BC returns
#   gp1  = net - COGS (BC cost-of-goods rate on the Norce base)
#   gp2  = gp1 - direct variable cost (dv): Norce freight income, shipping-model
#          outbound cost, shipping-tab pick & pack + packaging, and the
#          forecast's remaining direct-variable lines at forecast
#   gp3  = gp2 - de-duplicated Funnel ad cost
# fgross/fnet/fgp1/fgp2 are the Funnel export's own figures: fgross keeps COS%
# Funnel ÷ Funnel (the KV Overview definition); the rest are for comparison.
METRICS = ["gross", "net", "gp1", "gp2", "cost", "gp3", "orders", "units", "freight",
           "returns", "cogs", "dv", "fgross", "fnet", "fgp1", "fgp2"]
BASIS = {"gross": "norce", "orders": "norce", "units": "norce", "freight": "norce",
         "net": "norce_bc", "returns": "norce_bc", "gp1": "norce_bc", "cogs": "norce_bc",
         "gp2": "model", "dv": "model", "gp3": "model",
         "cost": "funnel", "fgross": "funnel", "fnet": "funnel", "fgp1": "funnel", "fgp2": "funnel"}
BASIS_LABEL = {"norce": "Norce ordered", "norce_bc": "Norce − BC returns − Norce COGS",
               "model": "Norce + BC + shipping model", "funnel": "Funnel export"}
# pacing metric -> budget line / daily_targets key
BUDGET_KEY = {"gross": "gross_sales", "net": "net_sales", "gp1": "gp1",
              "gp2": "gp2", "cost": "marketing"}
KVD_FIELD = {"fgross": "rev", "fnet": "net_rev", "fgp1": "gp1", "fgp2": "gp2", "cost": "cost"}
# Forecast direct-variable lines (budget_2026.json's direct_var = GP1 - GP2,
# Exec P&L forecast block) that the shipping model does NOT measure. They are
# carried AT FORECAST in actuals, so they add no variance: warehousing, inbound
# VAS, returns handling, return shipping, other shipping, outbound VAS and
# transaction fees (a net credit). Nov 2026 values, SEK, cost positive.
OTHER_DV_FC_LINES = ("Fulfillment 3PL - warehousing", "Fulfillment VAS - inbound",
                     "Fulfillment 3PL - returns", "Fulfillment VAS - returns",
                     "Shipping costs - returns", "Other shipping costs",
                     "Fulfillment VAS - outbound", "Transaction fees")
SHIP_RATE_DAYS = 28
MARKET_SQL = ("IF(market_level_1_kv IN ('" + "','".join(bm.MARKETS) + "'), "
              "market_level_1_kv, 'ROW')")


def _f(v) -> float:
    return float(v or 0)


def _daily_by_market(start: datetime.date, end: datetime.date) -> dict:
    """{"brand|market": {iso: {metric: value}}} with roll-ups, from kv_rows.
    Brand from shop_new (only 'Babyshop' and 'Lekmer' carry revenue or cost)."""
    rows = bs._rows(
        f"WITH {bs._kv_cte('Date BETWEEN @s AND @e')} "
        f"SELECT CAST(Date AS STRING) d, {MARKET_SQL} m, "
        f"IF(shop_new = 'Lekmer', 'lekmer', 'babyshop') b, {bs.KVD} "
        f"FROM kv_rows GROUP BY d, m, b",
        [bigquery.ScalarQueryParameter("s", "DATE", start),
         bigquery.ScalarQueryParameter("e", "DATE", end)])
    out: dict = {k: {} for k in SEGS}
    for r in rows:
        vals = {k: _f(r[f]) for k, f in KVD_FIELD.items()}
        for m in _keys(r["b"], r["m"]):
            day = out[m].setdefault(r["d"], {k: 0.0 for k in KVD_FIELD})
            for k, v in vals.items():
                day[k] += v
    return out


def _norce_market(app_key: str) -> str:
    mk = (app_key or "").split("-")[-1].upper()
    return mk if mk in bm.MARKETS else "ROW"


def _norce_daily(start: datetime.date, end: datetime.date, out: dict,
                 ship_rate: dict, ship_avg: float) -> str | None:
    """Merge Norce ordered metrics into `out` ({market: {iso: {...}}}).

    Market = the Norce application's market (Babyshop + Lekmer per country;
    EU/UK/NA/Asia/ROW applications fold into ROW). Returns the last date with
    rows, so the caller can tell a stalled norce-sync from a quiet day."""
    rows = rdp._rows(rdp.DAILY_SQL, [rdp._p("from_day", start), rdp._p("to_day", end)])
    last = None
    for r in rows:
        iso = r["d"].isoformat()
        last = iso if last is None or iso > last else last
        vals = {"gross": _f(r["merch_sek"]), "freight": _f(r["freight_sek"]),
                "orders": _f(r["orders"]), "units": _f(r["units"]),
                # Shipping model's outbound cost per order for this application.
                "ship_model": ship_rate.get(r["market"], ship_avg) * _f(r["orders"])}
        brand = "lekmer" if (r["market"] or "").startswith("lekmer") else "babyshop"
        for m in _keys(brand, _norce_market(r["market"])):
            day = out[m].setdefault(iso, {})
            for k, v in vals.items():
                day[k] = day.get(k, 0.0) + v
    return last


def _marketing_channels(s: datetime.date, e: datetime.date, ly_s: datetime.date,
                        ly_e: datetime.date) -> dict:
    """All marketing cost in the Funnel export by channel, MTD and LY MTD, per
    brand|market segment. Same de-duplicated kv_rows `cost` as every other ad
    cost figure here: every channel with cost (Google, Meta, affiliate, Naver,
    price comparison, ...), nothing filtered out."""
    rows = bs._rows(
        f"WITH {bs._kv_cte('(Date BETWEEN @s AND @e) OR (Date BETWEEN @ls AND @le)')} "
        f"SELECT IF(Date >= @s, 'cur', 'ly') per, {MARKET_SQL} m, "
        f"IF(shop_new = 'Lekmer', 'lekmer', 'babyshop') b, "
        f"COALESCE(Channel_Type_Level_1, 'Other') ch, SUM(cost) cost "
        f"FROM kv_rows GROUP BY 1, 2, 3, 4 HAVING ABS(cost) > 0",
        [bigquery.ScalarQueryParameter(k, "DATE", v) for k, v in
         (("s", s), ("e", e), ("ls", ly_s), ("le", ly_e))])
    acc: dict = {}
    for r in rows:
        for k in _keys(r["b"], r["m"]):
            c = acc.setdefault(k, {}).setdefault(r["ch"], {"channel": r["ch"], "mtd": 0.0, "ly_mtd": 0.0})
            c["mtd" if r["per"] == "cur" else "ly_mtd"] += _f(r["cost"])
    return {k: sorted(({"channel": c["channel"], "mtd": round(c["mtd"]), "ly_mtd": round(c["ly_mtd"])}
                       for c in v.values()), key=lambda c: -c["mtd"])
            for k, v in acc.items()}


def _bc_market(country: str) -> str:
    return country if country in bm.MARKETS else "ROW"


def _cost_inputs() -> dict:
    """Everything the P&L ladder needs beyond Norce and Funnel, from snapshots
    this project already maintains:

      • Exec P&L (funnel_cache/<ws>__exec-pl, refresh_exec_pl.py): Business
        Central per country and month — gross, returns (credit memos, by
        POSTING date) and item-ledger COGS, the GL COGS total, the posted
        logistics lines, and the forecast's direct-variable lines. The open
        month is month-to-date through `sources.bc.max_posting_date`.
      • Shipping cost tab (funnel_cache/<ws>__shipping + shipping_cost.order_costs):
        modelled outbound carrier cost per order, and pick & pack + packaging
        per order.
    """
    from google.cloud import firestore
    db = firestore.Client(project=bm.GCP_PROJECT)
    doc = lambda k: ((db.collection(bm.COLLECTION).document(f"{bm.WORKSPACE}__{k}").get().to_dict() or {})
                     .get("data") or {})
    ex, sh = doc("exec-pl"), doc("shipping")
    if not ex.get("markets") or not sh.get("fulfilment"):
        raise RuntimeError("exec-pl or shipping snapshot missing: cannot build the P&L ladder")

    def gl_scale(ym: str) -> float | None:
        mk = ex["markets"].get(ym, {})
        il = sum(_f(v.get("cogs_item_ledger")) for v in mk.values())
        gl = _f(ex["ladder"].get(ym, {}).get("cogs"))
        return gl / il if il and gl else None

    def month_rates(ym: str, scale: float | None = None) -> dict:
        """Per KV market: returns, gross, item-ledger COGS, scaled so the
        market COGS add up to the GL total (the ladder's COGS basis). The open
        month passes `scale` from closed months: its own GL-to-item-ledger
        ratio is lumpy early on (1.20 on 6 Nov 2026 vs 0.93-1.03 in closed
        months), which moved GM1 ~9 pp and flipped the headroom."""
        mk = ex["markets"].get(ym, {})
        agg: dict = {}
        for c, v in mk.items():
            a = agg.setdefault(_bc_market(c), {"gross": 0.0, "returns": 0.0, "cogs": 0.0, "orders": 0})
            a["gross"] += _f(v.get("gross")); a["returns"] += _f(v.get("returns"))
            a["cogs"] += _f(v.get("cogs_item_ledger")); a["orders"] += int(v.get("orders") or 0)
        if scale is None:
            scale = gl_scale(ym) or 1.0
        for a in agg.values():
            a["cogs"] *= scale
            a["returns_rate"] = a["returns"] / a["gross"] if a["gross"] else 0.0
            a["cogs_rate"] = a["cogs"] / a["gross"] if a["gross"] else 0.0
        return {"markets": agg, "cogs_gl_scale": round(scale, 4)}

    ym_cur = f"{bm.YEAR:04d}-{bm.MONTH:02d}"
    ym_ly = f"{bm.YEAR - 1:04d}-{bm.MONTH:02d}"
    # Open month: median GL/item-ledger ratio of the last three closed months.
    closed = sorted(ym for ym in ex["markets"] if ym < ym_cur and gl_scale(ym))[-3:]
    cur_scale = sorted(gl_scale(ym) for ym in closed)[len(closed) // 2] if closed else 1.0
    lad_ly = ex["ladder"].get(ym_ly, {})
    ly_orders = sum(int(v.get("orders") or 0) for v in ex["markets"].get(ym_ly, {}).values())
    ly_dv = _f(lad_ly.get("nship")) + _f(lad_ly.get("ful")) + _f(lad_ly.get("tf"))

    fl = ex.get("forecast", {}).get("lines", {})
    mon = bm.MONTH_START.strftime("%b").lower()
    other_fc = -sum(_f(fl.get(k, {}).get(mon)) for k in OTHER_DV_FC_LINES)   # cost positive

    f = sh["fulfilment"]
    used = [r for r in f.get("monthly", []) if r.get("month") in f.get("months", [])]
    orders = sum(int(r.get("orders") or 0) for r in used)
    pick = (sum(_f(r.get("pick_contract")) for r in used) / orders) if orders else 0.0
    pack = _f(f.get("packaging_per_order"))

    thru = sh.get("orders_through")
    rows = bs._rows(f"""
      SELECT project, COUNT(*) n, SUM(cost_sek) c
      FROM `{bm.GCP_PROJECT}.shipping_cost.order_costs`
      WHERE order_date > DATE_SUB(@t, INTERVAL {SHIP_RATE_DAYS} DAY) AND order_date <= @t
      GROUP BY project""", [bigquery.ScalarQueryParameter("t", "DATE", thru)])
    n_all = sum(int(r["n"]) for r in rows)
    ship = {r["project"]: _f(r["c"]) / int(r["n"]) for r in rows if r["n"]}
    ship_avg = sum(_f(r["c"]) for r in rows) / n_all if n_all else 0.0
    return {
        "cur": month_rates(ym_cur, scale=cur_scale), "ly": month_rates(ym_ly),
        "bc_through": ex.get("sources", {}).get("bc", {}).get("max_posting_date"),
        "exec_pl_generated": ex.get("generated_at"),
        "ly_dv_per_order": ly_dv / ly_orders if ly_orders else 0.0,
        "ly_dv_total": ly_dv, "ly_dv_orders": ly_orders,
        "other_dv_fc": other_fc,
        "pick_per_order": pick, "pack_per_order": pack,
        "fulfilment_label": f.get("label"),
        "ship_rate": ship, "ship_rate_avg": ship_avg, "ship_through": thru,
    }


def _norce_cogs(start: datetime.date, end: datetime.date) -> dict:
    """COGS ÷ sales per brand x market (and 'all|<market>') for the open month,
    from Norce's own unit costs: the customer_orders mart prices every order
    line at the CURRENT CostUnit on the application's primary price list
    (order-line CostUnit is empty on this tenant). Current cost is right for
    the current month; coverage is ~95 % of November's merchandise, and the
    rate is taken over costed lines only (cost ÷ costed value)."""
    rows = bs._rows(f"""
      SELECT shop, app_key, SUM(order_cost) cost, SUM(costed_value) costed,
             SUM(order_value - IFNULL(shipping_value, 0)) merch
      FROM `{bm.GCP_PROJECT}.norce.customer_orders`
      WHERE status_id != 6 AND order_date BETWEEN @s AND @e
      GROUP BY shop, app_key""",
        [bigquery.ScalarQueryParameter("s", "DATE", start),
         bigquery.ScalarQueryParameter("e", "DATE", end)])
    agg: dict = {}
    for r in rows:
        m = _norce_market(r["app_key"])
        for k in (f"{r['shop']}|{m}", f"all|{m}"):
            a = agg.setdefault(k, [0.0, 0.0, 0.0])
            a[0] += _f(r["cost"]); a[1] += _f(r["costed"]); a[2] += _f(r["merch"])
    return {k: {"rate": c / v, "coverage": v / g if g else None}
            for k, (c, v, g) in agg.items() if v}


def _derive(data: dict, ci: dict, isos: list[str], asof_i: int, ly_isos: list[str],
            other_rate: float) -> None:
    """Build net / GP1 / GP2 / GP3 (+ returns, cogs, dv) per day on the leaf
    segments (brand x market), then roll the leaves up. See METRICS."""
    cur_days = set(isos[:asof_i + 1])
    ly_pos = {iso: j for j, iso in enumerate(ly_isos)}
    leaves = [f"{b}|{m}" for b in ("babyshop", "lekmer") for m in MKTS if m != "all"]
    # BC returns MTD are absolute amounts per market; spread over the market's
    # days and brands in proportion to Norce gross (BC has no shop dimension).
    g_mtd = {m: sum(data[f"all|{m}"].get(i, {}).get("gross", 0.0) for i in cur_days)
             for m in MKTS if m != "all"}
    pp = ci["pick_per_order"] + ci["pack_per_order"]
    for leaf in leaves:
        m = leaf.split("|")[1]
        cur_m = ci["cur"]["markets"].get(m, {})
        ly_m = ci["ly"]["markets"].get(m, {})
        ret_factor = cur_m.get("returns", 0.0) / g_mtd[m] if g_mtd[m] else 0.0
        for iso, day in data[leaf].items():
            g, orders = day.get("gross", 0.0), day.get("orders", 0.0)
            if iso in cur_days:
                ret = g * ret_factor
                # Norce cost on the goods kept: returned units go back to stock.
                nc = ci["norce_cogs"].get(leaf) or ci["norce_cogs"].get(f"all|{m}")
                cogs = (g - ret) * nc["rate"] if nc else g * cur_m.get("cogs_rate", 0.0)
                # Unmeasured lines at the forecast's rate per krona of sales.
                other = other_rate * g
                dv = day.get("ship_model", 0.0) + pp * orders - day.get("freight", 0.0) + other
            elif iso in ly_pos:
                ret = g * ly_m.get("returns_rate", 0.0)
                cogs = g * ly_m.get("cogs_rate", 0.0)
                dv = ci["ly_dv_per_order"] * orders
            else:
                continue
            day.update(returns=ret, cogs=cogs, dv=dv, net=g - ret, gp1=g - ret - cogs,
                       gp2=g - ret - cogs - dv)
    # Roll the derived fields up from the leaves (raw fields were rolled at load).
    derived = ("returns", "cogs", "dv", "net", "gp1", "gp2")
    for key in SEGS:
        if key in leaves:
            continue
        b, m = key.split("|")
        kids = [lf for lf in leaves if (b == "all" or lf.startswith(b + "|"))
                and (m == "all" or lf.endswith("|" + m))]
        for iso, day in data[key].items():
            for k in derived:
                day[k] = sum(data[lf].get(iso, {}).get(k, 0.0) for lf in kids)
    for key in SEGS:
        for day in data[key].values():
            if "gp2" in day:
                day["gp3"] = day["gp2"] - day.get("cost", 0.0)


def _october_index(field: str, hist: list[dict]) -> list[float]:
    """November index shaped like October 2026 instead of November 2025.

    Each November day takes the October day with the same day-of-month (so the
    25th payday lines up), with October's weekday effect divided out and
    November's multiplied back in, using daily_targets' own weekday factors.
    No Black Friday peak: October has none, which is exactly why the
    event-aligned November 2025 shape is the primary target.
    """
    series = {datetime.date.fromisoformat(r["iso"]): float(r[field] or 0) for r in hist}
    years = sorted({d.year for d in series})
    dow = dt._dow_factors(series, {y: dt.calendar_events(y) for y in years})
    raw = []
    for nd in bm.month_days():
        od = datetime.date(2026, 10, nd.day)
        v = max(series.get(od, 0.0), 0.0) / (dow[od.weekday()] or 1.0)
        raw.append(v * dow[nd.weekday()])
    tot = sum(raw)
    return [x / tot for x in raw] if tot > 0 else [1.0 / len(raw)] * len(raw)


def _spread(total: float, index: list[float]) -> list[float]:
    return [total * w for w in index]


def _sum(xs) -> float:
    return sum(x for x in xs if x is not None)


def _customers(s: datetime.date, e: datetime.date, ly_s: datetime.date,
               ly_e: datetime.date) -> dict:
    """New vs returning orders per market, MTD and event-aligned LY.

    An order is NEW when it is the customer's first-ever order in that shop
    (first by order date, then order id): the same customer x shop grain the
    Customer tab's customer_facts uses. Same day and status rules as the gross
    sales actuals (Stockholm day, status 6 excluded); revenue is merchandise, so
    new + returning ties to Norce gross sales MTD.
    """
    import refresh_customer_insights as rci
    sql = f"""
    WITH ranked AS (
      SELECT c.market, c.shop, DATE(o.OrderDate, '{rdp.TZ}') order_date, c.order_value,
             c.status_id,
             ROW_NUMBER() OVER (PARTITION BY c.customer_hash, c.shop
                                ORDER BY o.OrderDate, c.order_id) = 1 AS is_new
      FROM `{rci.D()}.customer_orders` c
      JOIN `{rci.D()}.orders` o ON o.Id = c.order_id
      WHERE DATE(o.OrderDate, '{rdp.TZ}') <= @e)
    SELECT IF(order_date >= @s, 'cur', 'ly') per, shop,
           IF(market IN ('{"','".join(bm.MARKETS)}'), market, 'ROW') m, is_new,
           COUNT(*) orders, SUM(order_value) rev
    FROM ranked
    WHERE status_id != 6
      AND (order_date BETWEEN @s AND @e OR order_date BETWEEN @ls AND @le)
    GROUP BY 1, 2, 3, 4"""
    rows = bs._rows(sql, [bigquery.ScalarQueryParameter("s", "DATE", s),
                          bigquery.ScalarQueryParameter("e", "DATE", e),
                          bigquery.ScalarQueryParameter("ls", "DATE", ly_s),
                          bigquery.ScalarQueryParameter("le", "DATE", ly_e)])
    blank = lambda: {"new": {"orders": 0, "revenue": 0.0}, "ret": {"orders": 0, "revenue": 0.0}}
    out = {m: {"cur": blank(), "ly": blank()} for m in SEGS}
    for r in rows:
        k = "new" if r["is_new"] else "ret"
        brand = "lekmer" if r["shop"] == "lekmer" else "babyshop"
        for m in _keys(brand, r["m"]):
            b = out[m][r["per"]][k]
            b["orders"] += int(r["orders"] or 0)
            b["revenue"] += _f(r["rev"])
    for m in out:
        for per in ("cur", "ly"):
            for k in ("new", "ret"):
                b = out[m][per][k]
                b["revenue"] = round(b["revenue"])
    return out


def _summary(days: list[str], asof_i: int, tgt: dict, act: dict, ly: dict) -> dict:
    """MTD vs target / LY, run-rate, landing forecast, required run-rate.

    asof_i: index of the last complete day (-1 = nothing yet)."""
    n = asof_i + 1
    rr_lo = max(0, n - RUN_RATE_DAYS)
    out = {}
    for k in METRICS:
        a_mtd = _sum(act[k][:n])
        l_mtd = _sum(ly[k][:n])
        row = {"mtd": round(a_mtd), "ly_mtd": round(l_mtd), "ly_month": round(_sum(ly[k]))}
        if k in tgt:
            t_mtd = _sum(tgt[k][:n])
            t_month = _sum(tgt[k])
            t_rem = t_month - t_mtd
            a_rr, t_rr = _sum(act[k][rr_lo:n]), _sum(tgt[k][rr_lo:n])
            ratio = (a_rr / t_rr) if t_rr else None
            landing = a_mtd + t_rem * ratio if ratio is not None else None
            gap = t_month - a_mtd
            row.update({
                "target_mtd": round(t_mtd), "target_month": round(t_month),
                "run_rate_ratio": round(ratio, 4) if ratio is not None else None,
                "landing": round(landing) if landing is not None else None,
                # Multiple of the remaining daily targets needed to still land
                # exactly on the month: 1.10 = every remaining day 10% over.
                "required_ratio": round(gap / t_rem, 4) if t_rem > 0 else None,
                "required_per_day": round(gap / (len(days) - n)) if len(days) > n else None,
            })
        out[k] = row
    return out


def _headroom(S: dict, t: dict, n: int, budget: dict) -> dict:
    """Spend headroom against the forecast's GP3 (the binding target).

    The forecast's 10.5% COS is derived from assumed GM1/GM2. What is fixed is
    GP3 = GP2 - marketing, so the spend GP2 can carry while still landing GP3 is

        allowed spend = GP2 - GP3 target

    MTD against the daily GP3 target curve, and for the month on landing GP2
    (run-rate). Headroom is allowed minus actual (MTD) and allowed minus
    expected spend at the current run-rate (month), spread over the remaining
    days by the marketing index. It only holds if the extra spend adds at least
    as much incremental GP2 as it costs, which this cannot see.

    Everything is on the pacing ladder: sales = Norce merchandise, GP1 = minus
    BC returns and COGS, GP2 = minus the shipping model's direct variable cost.
    """
    r = lambda v: None if v is None else round(v)
    rate = lambda a, b: round(a / b, 4) if b else None
    g, gp1, gp2, gp3, c = (S[k] for k in ("gross", "gp1", "gp2", "gp3", "cost"))
    gm1_fc = budget["gp1"] / budget["gross_sales"]
    gm2_fc = budget["gp2"] / budget["gross_sales"]
    gm1_act = gp1["mtd"] / g["mtd"] if g["mtd"] else None
    gm2_act = gp2["mtd"] / g["mtd"] if g["mtd"] else None

    allowed_mtd = gp2["mtd"] - gp3["target_mtd"]
    allowed_month = (gp2["landing"] - gp3["target_month"]) if gp2["landing"] is not None else None
    expected = c["landing"]
    rem_days = len(t["cost"]) - n
    plan_rem = sum(t["cost"][n:])
    allowed_rem = allowed_month - c["mtd"] if allowed_month is not None else None
    head_rem = (allowed_month - expected) if allowed_month is not None and expected is not None else None
    w = [v / plan_rem for v in t["cost"][n:]] if plan_rem > 0 else []

    bridge = None
    if gm1_act is not None:
        sales, sales_t = g["mtd"], g["target_mtd"]
        volume = (sales - sales_t) * gm2_fc
        gm1_eff = sales * (gm1_act - gm1_fc)
        # GM1 -> GM2 is direct variable cost (net shipping, fulfilment, payment).
        dvc_eff = sales * ((gm2_act - gm1_act) - (gm2_fc - gm1_fc))
        resid = gp2["mtd"] - gp2["target_mtd"] - volume - gm1_eff - dvc_eff
        bridge = {"target_gp2": r(gp2["target_mtd"]), "volume": r(volume),
                  "gm1_margin": r(gm1_eff), "variable_cost": r(dvc_eff),
                  "market_mix": r(resid), "actual_gp2": r(gp2["mtd"])}
    return {
        "allowed_spend_mtd": r(allowed_mtd),
        "actual_spend_mtd": r(c["mtd"]),
        "headroom_mtd": r(allowed_mtd - c["mtd"]),
        "planned_spend_mtd": r(c["target_mtd"]),
        "allowed_spend_month": r(allowed_month),
        "expected_spend_month": r(expected),
        "planned_spend_month": r(c["target_month"]),
        "allowed_spend_remaining": r(allowed_rem),
        "headroom_remaining": r(head_rem),
        "headroom_per_day": r(head_rem / rem_days) if head_rem is not None and rem_days > 0 else None,
        "remaining_days": rem_days,
        "allowed_spend_daily": [r(allowed_rem * x) for x in w] if allowed_rem is not None else [],
        "headroom_daily": [r(head_rem * x) for x in w] if head_rem is not None else [],
        "earned_cos_mtd": rate(allowed_mtd, g["mtd"]),
        "earned_cos": rate(allowed_month, g["landing"]) if allowed_month is not None else None,
        "cos_target": bm.COS_TARGET,
        "gm1_actual": round(gm1_act, 4) if gm1_act is not None else None,
        "gm1_fc": round(gm1_fc, 4),
        "gm2_actual": round(gm2_act, 4) if gm2_act is not None else None,
        "gm2_fc": round(gm2_fc, 4),
        "gp3_target_mtd": r(gp3["target_mtd"]), "gp3_mtd": r(gp3["mtd"]),
        "gp3_target_month": r(gp3["target_month"]), "gp3_landing": r(gp3["landing"]),
        "bridge": bridge,
    }


def build(ctx: dict) -> dict:
    today = ctx["today"]
    days = bm.month_days()
    isos = [d.isoformat() for d in days]
    ly_days = [d - LY_SHIFT for d in days]
    budget = ctx["budget"]

    # ── Actuals: Nov 2026 MTD + event-aligned LY window + October for the shape
    fetch_to = min(today - datetime.timedelta(days=1), bm.MONTH_END)
    data = _daily_by_market(ly_days[0], fetch_to)
    ci = _cost_inputs()
    norce_last = _norce_daily(ly_days[0], fetch_to, data, ci["ship_rate"], ci["ship_rate_avg"])
    # A day counts once BOTH sources hold it, so every KPI shares one as-of.
    complete = [i for i, iso in enumerate(isos) if iso <= fetch_to.isoformat()
                and data["all|all"].get(iso, {}).get("gross", 0) > 0
                and data["all|all"].get(iso, {}).get("fgross", 0) > 0]
    asof_i = complete[-1] if complete else -1
    asof = isos[asof_i] if asof_i >= 0 else None
    ci["norce_cogs"] = _norce_cogs(days[0], days[asof_i]) if asof_i >= 0 else {}

    # ── Global targets, three daily shapes of the same monthly forecast
    hist = dt.daily_history()
    shapes = {}
    for sh in SHAPES:
        if sh == "event":
            # daily_targets' event-aligned Nov 2025 index with the prior-year
            # shape taken in full (shrink=1.0). See SHAPES for why.
            ix = {f: dt.build_index(bm.YEAR, bm.MONTH, f, hist, shrink=1.0)["index"]
                  for f in ("revenue", "cost")}
        elif sh == "kv":
            ix = {f: dt.build_index(bm.YEAR, bm.MONTH, f, hist)["index"]
                  for f in ("revenue", "cost")}
        else:
            ix = {f: _october_index(f, hist) for f in ("revenue", "cost")}
        t = {k: _spread(budget[BUDGET_KEY[k]], ix["cost" if k == "cost" else "revenue"])
             for k in BUDGET_KEY}
        # GP3 = GP2 - ad cost per day, so the daily lines reconcile (Σ = budget gp3).
        t["gp3"] = [g - c for g, c in zip(t["gp2"], t["cost"])]
        shapes[sh] = t

    # ── Net / GP1 / GP2 / GP3 per day from Norce + BC + shipping model
    ly_isos = [d.isoformat() for d in ly_days]
    _derive(data, ci, isos, asof_i, ly_isos, ci["other_dv_fc"] / budget["gross_sales"])

    # ── Per-market split: Nov 2025 (event-aligned) market mix, per metric
    ly_tot = {k: sum(data["all|all"].get(i, {}).get(k, 0) for i in ly_isos)
              for k in METRICS}
    rnd = lambda d: {k: [None if v is None else round(v) for v in vs] for k, vs in d.items()}

    markets, headroom, margins = {}, {}, {}
    for m in SEGS:
        ser = data[m]
        # GP3's own mix is unstable (GP2 minus cost can be ~0 in a market), so
        # market GP3 is derived from the market's GP2 and cost targets instead.
        share = {k: (1.0 if m == "all|all" else
                     (sum(ser.get(i, {}).get(k, 0) for i in ly_isos) / ly_tot[k]
                      if ly_tot[k] else 0.0)) for k in BUDGET_KEY}
        act = {k: [(ser[i].get(k, 0.0) if (j <= asof_i and i in ser) else None)
                   for j, i in enumerate(isos)] for k in METRICS}
        ly = {k: [ser.get(i, {}).get(k, 0.0) for i in ly_isos] for k in METRICS}
        targets, summaries = {}, {}
        for sh, t_all in shapes.items():
            t = {k: [v * share[k] for v in t_all[k]] for k in BUDGET_KEY}
            t["gp3"] = [g - c for g, c in zip(t["gp2"], t["cost"])]
            targets[sh] = rnd(t)
            summaries[sh] = _summary(isos, asof_i, t, act, ly)
        markets[m] = {
            "share_of_ly": {k: round(v, 4) for k, v in share.items()},
            "targets": targets, "actual": rnd(act), "ly": rnd(ly),
            # `summary` = the primary (event) shape; what `insights` should read.
            "summary": summaries["event"], "summaries": summaries,
        }
        t_ev = {k: [v * share[k] for v in shapes["event"][k]] for k in BUDGET_KEY}
        if asof_i >= 0:
            headroom[m] = _headroom(summaries["event"], t_ev, asof_i + 1, budget)
            S = summaries["event"]
            gm = lambda k, per="mtd": round(S[k][per] / S["gross"][per], 4) if S["gross"][per] else None
            margins[m] = {
                "basis": "Norce merchandise − BC returns − Norce COGS (current cost) − shipping-model direct variable cost",
                "gm1": gm("gp1"), "gm2": gm("gp2"), "gm3": gm("gp3"),
                "gm1_ly": gm("gp1", "ly_mtd"), "gm2_ly": gm("gp2", "ly_mtd"),
                "gm1_fc": round(budget["gp1"] / budget["gross_sales"], 4),
                "gm2_fc": round(budget["gp2"] / budget["gross_sales"], 4),
                "returns_rate": gm("returns"), "cogs_rate": gm("cogs"), "dv_rate": gm("dv"),
                "dv_per_order": round(S["dv"]["mtd"] / S["orders"]["mtd"], 1) if S["orders"]["mtd"] else None,
                "funnel_gm1": round(S["fgp1"]["mtd"] / S["fgross"]["mtd"], 4) if S["fgross"]["mtd"] else None,
                "funnel_gm2": round(S["fgp2"]["mtd"] / S["fgross"]["mtd"], 4) if S["fgross"]["mtd"] else None,
            }

    # ── New vs returning (Norce), same MTD window and its LY twin
    cust, mchan = None, None
    if asof_i >= 0:
        s, e = days[0], days[asof_i]
        cust = _customers(s, e, s - LY_SHIFT, e - LY_SHIFT)
        mchan = _marketing_channels(s, e, s - LY_SHIFT, e - LY_SHIFT)

    events = {d.isoformat(): n for d, n in dt.calendar_events(bm.YEAR).items()
              if d.year == bm.YEAR and d.month == bm.MONTH}
    events[dt.swedish_payday(bm.YEAR, bm.MONTH).isoformat()] = "payday"

    def pick(d, b):
        return {m: d[f"{b}|{m}"] for m in MKTS if d and f"{b}|{m}" in d}

    return {
        # Babyshop + Lekmer combined. `brands` carries the same blocks per shop.
        "scope": {
            "label": "Babyshop + Lekmer (all applications, all markets)",
            "forecast": ("Finance's rolling P&L forecast (budget_2026.json, 2026-08-31 vintage), "
                         "same ladder as the Executive P&L, which is built on BC. BC posted gross "
                         "Jun-Sep 2026 = 103.6 M vs Norce Babyshop + Lekmer 103.1 M (99.5%) vs "
                         "Babyshop alone 96.9 M, and the same per country (SE 50.7 / 49.9 / 45.7): "
                         "the forecast covers Babyshop + Lekmer combined."),
            "actuals": ("Norce: all 13 applications (babyshop-* and lekmer-*). Funnel export: "
                        "shop_new Babyshop + Lekmer (workspace AA/BS/LM/MJ Combo); no other shop "
                        "carries revenue or ad cost."),
            "split": ("The forecast is global only: no market or brand split. Every market and "
                      "brand target here is DERIVED from Nov 2025's mix per metric."),
        },
        "brands": {b: {"markets": pick(markets, b), "headroom": pick(headroom, b),
                       "margins": pick(margins, b),
                       "marketing_channels": pick(mchan, b) if mchan else None,
                       "customers": pick(cust, b) if cust else None}
                   for b in BRANDS if b != "all"},
        "asof": asof,
        "today": today.isoformat(),
        "days": isos,
        "ly_days": ly_isos,
        "events": events,
        "markets": pick(markets, "all"),
        "customers": pick(cust, "all") if cust else None,
        "headroom": pick(headroom, "all"),
        "margins": pick(margins, "all"),
        "marketing_channels": pick(mchan, "all") if mchan else None,
        "cost_inputs": {
            "bc_through": ci["bc_through"], "exec_pl_generated": ci["exec_pl_generated"],
            "returns_mtd_by_market": {k: round(v["returns"]) for k, v in ci["cur"]["markets"].items()},
            "cogs_rate_by_market": {k: round(v["cogs_rate"], 4) for k, v in ci["cur"]["markets"].items()},
            "cogs_rate_by_market_ly": {k: round(v["cogs_rate"], 4) for k, v in ci["ly"]["markets"].items()},
            "returns_rate_by_market_ly": {k: round(v["returns_rate"], 4) for k, v in ci["ly"]["markets"].items()},
            "cogs_gl_scale": ci["cur"]["cogs_gl_scale"],
            "cogs_rate_norce": {k: {"rate": round(v["rate"], 4), "coverage": round(v["coverage"] or 0, 3)}
                                for k, v in ci["norce_cogs"].items()},
            "ship_cost_per_order_by_app": {k: round(v, 1) for k, v in ci["ship_rate"].items()},
            "ship_rate_through": str(ci["ship_through"]),
            "pick_per_order": round(ci["pick_per_order"], 2), "pack_per_order": ci["pack_per_order"],
            "fulfilment_period": ci["fulfilment_label"],
            "other_dv_at_forecast_month": round(ci["other_dv_fc"]),
            "other_dv_rate": round(ci["other_dv_fc"] / budget["gross_sales"], 4),
            "ly_dv_per_order": round(ci["ly_dv_per_order"], 1),
        },
        "basis": BASIS,
        "basis_label": BASIS_LABEL,
        "norce_last_day": norce_last,
        "run_rate_days": RUN_RATE_DAYS,
        "budget_month": {k: round(budget[BUDGET_KEY[k]]) for k in BUDGET_KEY} | {
            "gp3": round(budget["gp2"] - budget["marketing"])},
        "shapes": {"event": "Nov 2025 by event (full shape)",
                   "kv": "KV Overview model (smoothed)",
                   "oct": "October 2026 shape"},
        "yoy_forecast": round(budget["gross_sales"] / ly_tot["gross"] - 1, 4) if ly_tot["gross"] else None,
        "notes": [
            "Gross sales, orders, units and AOV: Norce ORDERED data (order date, Stockholm "
            "day, every status except 6, all Babyshop + Lekmer applications), same "
            "definition as the Daily performance tab. Gross sales = MERCHANDISE ex VAT, "
            "excluding freight, because the forecast's Gross Sales line excludes shipping; "
            "freight is ~2% on top. Gross of returns, like the plan line. Complete days "
            "only (norce-sync closes yesterday at 03:00).",
            "Net sales = Norce merchandise − Business Central returns (credit memos incl. "
            "compensation, as in the Executive P&L). BC books a return on its POSTING date, "
            "when it is processed, not on the original order date: early-month returns are "
            "mostly October orders, and Black Week returns land in December. MTD returns are "
            "BC's actual amount per market, spread over days and brands by Norce gross (BC "
            "has no shop dimension).",
            "GP1 = net sales − COGS from Norce's own unit costs: every November order line "
            "priced at the CURRENT cost on its application's primary price list (Norce's "
            "order-line cost is empty), cost ÷ value over costed lines (~95% of November "
            "merchandise is costed), per brand and market, applied to the goods kept (net of "
            "returns). Business Central's GL-scaled rate was dropped for the open month: "
            "early-month GL postings put it at 57% on 6 Nov vs ~50% in closed months. "
            "Last year stays on BC's Nov 2025 rates (today's Norce costs would restate it).",
            "GP2 = GP1 − direct variable cost, like the forecast's GP2 line (net shipping + "
            "fulfilment + transaction fees). Measured: Norce freight income, the Shipping "
            "cost tab's modelled outbound carrier cost per order per application (last 28 "
            "days of the model), pick & pack + packaging per order from the shipping tab "
            "(contract rate; PostNord's billed rate runs ~18% lower). Carried AT FORECAST "
            "at the forecast's rate per krona of sales (no variance): warehousing, "
            "inbound/outbound/returns VAS, returns handling, return shipping, other shipping "
            "and transaction fees, 2.63 M in the November forecast = 5.0% of gross sales.",
            "GP3 = GP2 − de-duplicated Funnel ad cost. COS% stays Funnel ad cost ÷ Funnel gross.",
            "Last year (Nov 2025) on the same ladder: Norce merchandise × BC's Nov 2025 return "
            "and COGS rates per market, minus BC's posted Nov 2025 net shipping + fulfilment "
            "+ transaction fees per order. The shipping model starts Jan 2026, so LY uses the "
            "ledger instead.",
            "Targets: Finance's November forecast (budget_2026.json) spread per day by "
            "November 2025's daily shape, lined up by event (Black Friday, Cyber Monday, "
            "payday, weekday) with the KV Overview's own daily_targets model. We use that "
            "event-aligned shape rather than last month's, because October has no Black "
            "Friday peak; the October-shaped curve is a toggle for comparison.",
            "The KV Overview's version of the same model pulls last year's shape 70% "
            "toward a flat month (tuned on Feb-Jul, months without Black Friday). In "
            "November that puts ~10% of the month into 1-3 Nov instead of last year's 5% "
            "and flattens the Black Friday peak, so it reads far behind early in the month. "
            "This tab uses the full shape; the 'KV Overview model' toggle shows the other "
            "one. Monthly totals are identical.",
            "Scope: the forecast covers Babyshop + Lekmer combined (verified against BC, "
            "Finance's ledger: Jun-Sep 2026 posted gross 103.6 M vs Norce Babyshop + Lekmer "
            "103.1 M vs Babyshop alone 96.9 M). Actuals cover the same scope.",
            "Market and brand targets: the forecast is global only, so each market's and "
            "brand's target is the global target × its share of the same metric in Nov 2025 "
            "(event-aligned, on this tab's own ladder; ad cost on the Funnel mix). Norce "
            "market = the ordering application (SE/NO/FI/DK incl. Lekmer; EU, UK, NA, Asia, "
            "ROW apps = ROW). Market GP3 = market GP2 − market ad cost. The mix has moved "
            "a lot since then (NO and DK roughly doubled in Oct 2026 YoY; international "
            "revenue moved from 'ROW EUR' to named countries), so per-market targets are "
            "a rough guide: NO/DK read ahead, ROW reads behind.",
            "1 Nov: All Saints' Day fell on 1 Nov 2025 but on 31 Oct 2026, so 1 Nov 2026 has "
            "no last-year twin and the model gives it an average-day target × the Sunday "
            "factor (~1.9 M). It reads behind on that day for that reason.",
            "Spend headroom: the binding target is the forecast's GP3 (8.48 M), not its "
            "10.5% COS, which is derived from assumed GM1 38.4% / GM2 26.5%. Allowed spend "
            "= GP2 − GP3 target (MTD on the daily curve; month on landing GP2 at the "
            "current run-rate). Headroom = allowed − actual (MTD) and allowed − expected "
            "spend at run-rate (rest of month), spread by the marketing index. Extra spend "
            "only keeps GP3 if it adds at least as much incremental GP2 as it costs. "
            "GP2 and margins are on this tab's own ladder (Norce − BC − shipping model). Per market it "
            "rides the Nov 2025 mix split: the forecast itself has no market split.",
            "GM1 caveat: Business Central books returns when they are processed, so GM1/GM2 "
            "(and the headroom built on them) run high during a sales peak until the "
            "peak's returns land, largely in December.",
            "Marketing cost = ALL marketing cost in the Funnel export, every channel with cost "
            "(Google, Meta, affiliate, Naver, price comparison, ...), de-duplicated with "
            "bq_source's COST_GRAIN machinery. The forecast's marketing line (5.54 M) is "
            "entirely 'Marketing ad spend' (other marketing and marketing consultants are 0) "
            "and equals Funnel's all-channel cost for Nov 2025 (5.537 M) to 0.01%, so the "
            "scope matches. Business Central's GL booked 4.71 M marketing for Nov 2025; that "
            "gap to Funnel is not reconciled here. Funnel is the basis, as instructed.",
            "Last year = same weekday 364 days earlier, so Black Friday 27 Nov lines up "
            "with 28 Nov 2025 and Cyber Monday 30 Nov with 1 Dec 2025.",
            f"Landing = MTD actual + remaining daily target × the last {RUN_RATE_DAYS} "
            "complete days' actual/target ratio.",
            "New vs returning: Norce orders on order date; new = the customer's first-ever "
            "order in that shop. Norce history starts 11 Jun 2025, so last year's 'new' is "
            "overstated (anyone who last bought before June 2025 counts as new). Revenue "
            "there is merchandise ex shipping, so new + returning = Norce gross sales MTD.",
        ],
    }
