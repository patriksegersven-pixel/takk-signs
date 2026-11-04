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
# target); `fgross` is Funnel gross, carried only so COS% stays Funnel/Funnel.
METRICS = ["gross", "net", "gp1", "gp2", "cost", "gp3", "orders", "units", "freight", "fgross"]
NORCE_FIELDS = ("gross", "freight", "orders", "units")
BASIS = {k: ("norce" if k in NORCE_FIELDS else "funnel") for k in METRICS}
# pacing metric -> budget line / daily_targets key
BUDGET_KEY = {"gross": "gross_sales", "net": "net_sales", "gp1": "gp1",
              "gp2": "gp2", "cost": "marketing"}
KVD_FIELD = {"fgross": "rev", "net": "net_rev", "gp1": "gp1", "gp2": "gp2",
             "cost": "cost", "gp3": "gp3"}
MARKET_SQL = ("IF(market_level_1_kv IN ('" + "','".join(bm.MARKETS) + "'), "
              "market_level_1_kv, 'ROW')")


def _f(v) -> float:
    return float(v or 0)


def _daily_by_market(start: datetime.date, end: datetime.date) -> dict:
    """{market: {iso: {metric: value}}} including 'all', from kv_rows."""
    rows = bs._rows(
        f"WITH {bs._kv_cte('Date BETWEEN @s AND @e')} "
        f"SELECT CAST(Date AS STRING) d, {MARKET_SQL} m, {bs.KVD} "
        f"FROM kv_rows GROUP BY d, m",
        [bigquery.ScalarQueryParameter("s", "DATE", start),
         bigquery.ScalarQueryParameter("e", "DATE", end)])
    out: dict = {m: {} for m in MKTS}
    for r in rows:
        vals = {k: _f(r[f]) for k, f in KVD_FIELD.items()}
        for m in (r["m"], "all"):
            day = out[m].setdefault(r["d"], {k: 0.0 for k in KVD_FIELD})
            for k, v in vals.items():
                day[k] += v
    return out


def _norce_market(app_key: str) -> str:
    mk = (app_key or "").split("-")[-1].upper()
    return mk if mk in bm.MARKETS else "ROW"


def _norce_daily(start: datetime.date, end: datetime.date, out: dict) -> str | None:
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
                "orders": _f(r["orders"]), "units": _f(r["units"])}
        for m in (_norce_market(r["market"]), "all"):
            day = out[m].setdefault(iso, {})
            for k, v in vals.items():
                day[k] = day.get(k, 0.0) + v
    return last


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
      SELECT c.market, DATE(o.OrderDate, '{rdp.TZ}') order_date, c.order_value,
             c.status_id,
             ROW_NUMBER() OVER (PARTITION BY c.customer_hash, c.shop
                                ORDER BY o.OrderDate, c.order_id) = 1 AS is_new
      FROM `{rci.D()}.customer_orders` c
      JOIN `{rci.D()}.orders` o ON o.Id = c.order_id
      WHERE DATE(o.OrderDate, '{rdp.TZ}') <= @e)
    SELECT IF(order_date >= @s, 'cur', 'ly') per,
           IF(market IN ('{"','".join(bm.MARKETS)}'), market, 'ROW') m, is_new,
           COUNT(*) orders, SUM(order_value) rev
    FROM ranked
    WHERE status_id != 6
      AND (order_date BETWEEN @s AND @e OR order_date BETWEEN @ls AND @le)
    GROUP BY 1, 2, 3"""
    rows = bs._rows(sql, [bigquery.ScalarQueryParameter("s", "DATE", s),
                          bigquery.ScalarQueryParameter("e", "DATE", e),
                          bigquery.ScalarQueryParameter("ls", "DATE", ly_s),
                          bigquery.ScalarQueryParameter("le", "DATE", ly_e)])
    blank = lambda: {"new": {"orders": 0, "revenue": 0.0}, "ret": {"orders": 0, "revenue": 0.0}}
    out = {m: {"cur": blank(), "ly": blank()} for m in MKTS}
    for r in rows:
        k = "new" if r["is_new"] else "ret"
        for m in (r["m"], "all"):
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


def build(ctx: dict) -> dict:
    today = ctx["today"]
    days = bm.month_days()
    isos = [d.isoformat() for d in days]
    ly_days = [d - LY_SHIFT for d in days]
    budget = ctx["budget"]

    # ── Actuals: Nov 2026 MTD + event-aligned LY window + October for the shape
    fetch_to = min(today - datetime.timedelta(days=1), bm.MONTH_END)
    data = _daily_by_market(ly_days[0], fetch_to)
    norce_last = _norce_daily(ly_days[0], fetch_to, data)
    # A day counts once BOTH sources hold it, so every KPI shares one as-of.
    complete = [i for i, iso in enumerate(isos) if iso <= fetch_to.isoformat()
                and data["all"].get(iso, {}).get("gross", 0) > 0
                and data["all"].get(iso, {}).get("fgross", 0) > 0]
    asof_i = complete[-1] if complete else -1
    asof = isos[asof_i] if asof_i >= 0 else None

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

    # ── Per-market split: Nov 2025 (event-aligned) market mix, per metric
    ly_isos = [d.isoformat() for d in ly_days]
    ly_tot = {k: sum(data["all"].get(i, {}).get(k, 0) for i in ly_isos)
              for k in METRICS}
    rnd = lambda d: {k: [None if v is None else round(v) for v in vs] for k, vs in d.items()}

    markets = {}
    for m in MKTS:
        ser = data[m]
        # GP3's own mix is unstable (GP2 minus cost can be ~0 in a market), so
        # market GP3 is derived from the market's GP2 and cost targets instead.
        share = {k: (1.0 if m == "all" else
                     (sum(ser.get(i, {}).get(k, 0) for i in ly_isos) / ly_tot[k]
                      if ly_tot[k] else 0.0)) for k in BUDGET_KEY}
        act = {k: [(ser[i][k] if (j <= asof_i and i in ser) else None)
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

    # ── New vs returning (Norce), same MTD window and its LY twin
    cust = None
    if asof_i >= 0:
        s, e = days[0], days[asof_i]
        cust = _customers(s, e, s - LY_SHIFT, e - LY_SHIFT)

    events = {d.isoformat(): n for d, n in dt.calendar_events(bm.YEAR).items()
              if d.year == bm.YEAR and d.month == bm.MONTH}
    events[dt.swedish_payday(bm.YEAR, bm.MONTH).isoformat()] = "payday"

    return {
        "asof": asof,
        "today": today.isoformat(),
        "days": isos,
        "ly_days": ly_isos,
        "events": events,
        "markets": markets,
        "customers": cust,
        "basis": BASIS,
        "basis_label": {"norce": "Norce ordered", "funnel": "Funnel export"},
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
            "Net sales, GP1, GP2, ad cost, GP3 and COS%: Funnel export, the KV Overview's "
            "de-duplicated numbers. Norce has no cost (CostUnit is empty) and no returns, "
            "so these cannot be built on the ordered basis. Funnel days can still grow for "
            "~2 days.",
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
            "Market targets: the forecast is global only, so each market's target is the "
            "global target × that market's share of the same metric in Nov 2025 "
            "(event-aligned; Norce mix for gross sales, Funnel mix for the rest). Norce "
            "market = the ordering application (SE/NO/FI/DK incl. Lekmer; EU, UK, NA, Asia, "
            "ROW apps = ROW). Market GP3 = market GP2 − market ad cost. The mix has moved "
            "a lot since then (NO and DK roughly doubled in Oct 2026 YoY; international "
            "revenue moved from 'ROW EUR' to named countries), so per-market targets are "
            "a rough guide: NO/DK read ahead, ROW reads behind.",
            "1 Nov: All Saints' Day fell on 1 Nov 2025 but on 31 Oct 2026, so 1 Nov 2026 has "
            "no last-year twin and the model gives it an average-day target × the Sunday "
            "factor (~1.9 M). It reads behind on that day for that reason.",
            "Ad cost target = Finance's whole marketing line (5.54 M), which also holds "
            "non-ad channels; actual ad cost is de-duplicated Funnel ad spend.",
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
