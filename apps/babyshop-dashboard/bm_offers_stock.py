#!/usr/bin/env python3
"""
bm_offers_stock.py: hero products, stock and Shopping spend for the Black Month
offers section (bm_offers.py calls build_stock()).

THE QUESTION THIS ANSWERS
  "Are we still paying for clicks on products we cannot sell?" Spend is only
  waste for the days a SKU was actually out of stock. The first version summed
  7 days of spend and classified SKUs by TODAY's stock, which counted the spend
  that sold the last units as waste (checked 2026-11-04: 117 of 135 "sold-out"
  SKUs with spend on 3 Nov sold 192 units on 2–3 Nov). So now, per SKU per day:

    out of stock on day d  =  a stock snapshot exists for d (bm_stock_daily,
                              written by every build) showing the SKU out of
                              stock at the day's FIRST snapshot, and no Norce
                              sale that day
                         or   no snapshot for d: the SKU is out of stock now and
                              d is after its last Norce sale (inferred)

  Snapshots start 2026-11-04, so older days are inferred. Inference misses a
  SKU that sold out and was restocked since (spend on those days is not
  counted: the safe direction).

STOCK
  One shared pool for Babyshop and Lekmer: Lekmer sells the same Norce PartNos
  (89 % of its sold SKUs and 92 % of its advertised item ids are in the
  Babyshop Channable feed), so there is no separate Lekmer feed to read. A SKU
  that sold recently but is missing from the feed has sold out (Channable drops
  out-of-stock items).

SPEND
  Google Ads shopping_performance_view per product item id per day (read-only
  GAQL via refresh_roas_sims' client), Babyshop SE/NO/FI/DK and Lekmer SE/NO/DK
  (Lekmer has no FI account). Converted to SEK with norce.currency_rates.
"""
from __future__ import annotations

import datetime
from collections import defaultdict

from google.cloud import bigquery

import bm_common as bm

D = datetime.timedelta
BRANDS = ["babyshop", "lekmer"]
ADS_ACCOUNTS = [("babyshop", "SE", "4851485396"), ("babyshop", "NO", "8623945183"),
                ("babyshop", "FI", "6161399704"), ("babyshop", "DK", "2054294342"),
                ("lekmer", "SE", "7780114635"), ("lekmer", "NO", "8308232278"),
                ("lekmer", "DK", "2756397225")]
SNAP_TABLE = "bm_stock_daily"
HERO_N = 50
SPEND_DAYS = 14


def _ctx():
    """Late import: bm_offers imports this module."""
    import bm_offers as o
    return o


# ── Google Ads spend per item per day ────────────────────────────────────────
def ads_daily(today: datetime.date) -> list[dict]:
    """[{b, m, p, d, c}] cost in SEK per brand × market × item × day, last 14
    days through today (today is partial)."""
    o = _ctx()
    import refresh_roas_sims as rs
    fx = {r["c"]: r["s"] for r in o._q(f"SELECT currency_code c, to_sek s FROM `{o.NORCE}.currency_rates`")}
    svc = rs.ads_client().get_service("GoogleAdsService")
    a = (today - D(SPEND_DAYS)).isoformat()
    q = ("SELECT segments.date, segments.product_item_id, customer.currency_code, metrics.cost_micros "
         f"FROM shopping_performance_view WHERE segments.date BETWEEN '{a}' AND '{today.isoformat()}' "
         "AND metrics.cost_micros > 0")
    out = []
    for b, m, cid in ADS_ACCOUNTS:
        acc = defaultdict(float)
        for row in rs._search(svc, cid, q):
            acc[(row.segments.product_item_id.upper(), row.segments.date)] += (
                row.metrics.cost_micros / 1e6 * fx.get(row.customer.currency_code, 1.0))
        out += [{"b": b, "m": m, "p": p, "d": d, "c": c} for (p, d), c in acc.items()]
    return out


# ── stock snapshots ──────────────────────────────────────────────────────────
def write_snapshot(feed: dict, extra_skus: set[str], now: datetime.datetime) -> int:
    """Upsert today's stock state per SKU. The first snapshot of a day is kept
    (first_*); later runs update last_*. SKUs that sold or had spend but are
    missing from the feed are written as out of stock."""
    import norce_sync as ns
    S = bigquery.SchemaField
    schema = [S("snap_date", "DATE"), S("PartNo", "STRING"), S("in_feed", "BOOL"),
              S("first_stock", "INT64"), S("first_oos", "BOOL"), S("first_ts", "TIMESTAMP"),
              S("last_stock", "INT64"), S("last_oos", "BOOL"), S("last_ts", "TIMESTAMP")]
    t = bigquery.Table(ns.T(SNAP_TABLE), schema=schema)
    t.time_partitioning = bigquery.TimePartitioning(field="snap_date")
    ns.bq().create_table(t, exists_ok=True)
    d, ts = now.date().isoformat(), now.astimezone(datetime.timezone.utc).isoformat()
    rows = []
    for p in set(feed) | extra_skus:
        f = feed.get(p)
        st = f["stock"] if f else 0
        oos = (f is None) or st <= 0 or f["avail"] == "out_of_stock"
        rows.append({"snap_date": d, "PartNo": p, "in_feed": f is not None,
                     "first_stock": st, "first_oos": oos, "first_ts": ts,
                     "last_stock": st, "last_oos": oos, "last_ts": ts})
    stg = ns._load(rows, SNAP_TABLE, schema)
    ns.bq().query(
        f"MERGE `{ns.T(SNAP_TABLE)}` t USING `{stg}` s "
        f"ON t.snap_date = s.snap_date AND t.PartNo = s.PartNo "
        f"WHEN MATCHED THEN UPDATE SET last_stock = s.last_stock, last_oos = s.last_oos, "
        f"last_ts = s.last_ts, in_feed = s.in_feed "
        f"WHEN NOT MATCHED THEN INSERT ROW").result()
    ns.bq().delete_table(stg, not_found_ok=True)
    return len(rows)


def _snapshots(a: datetime.date) -> dict:
    o = _ctx()
    try:
        rows = o._q(f"SELECT CAST(snap_date AS STRING) d, PartNo p, first_oos FROM "
                    f"`{o.NORCE}.{SNAP_TABLE}` WHERE snap_date >= @a", a=a)
    except Exception:                        # table not created yet
        return {}
    return {(r["p"], r["d"]): r["first_oos"] for r in rows}


# ── sales: velocity, last sale, sold-on-day ──────────────────────────────────
def _sales(today: datetime.date) -> tuple[list, dict, set]:
    """velocity rows (14 d, all Babyshop + Lekmer applications), last sale date
    per PartNo (60 d) and the set of (PartNo, day) with a sale (15 d). Nightly
    order_items through yesterday, plus bm_offer_lines (intraday, SE/NO/FI/DK)."""
    o = _ctx()
    vel = o._q(f"""
      SELECT li.PartNo p, SPLIT(o.app_key, '-')[OFFSET(0)] b, UPPER(SPLIT(o.app_key, '-')[OFFSET(1)]) m,
             SUM(li.QtyOrdered) u, SUM(li.LineAmount * fx.to_sek) rev
      FROM `{o.NORCE}.order_items` li JOIN `{o.NORCE}.orders` o ON o.Id = li.OrderId
      LEFT JOIN `{o.NORCE}.currency_rates` fx ON fx.currency_id = o.CurrencyId
      WHERE (o.app_key LIKE 'babyshop-%' OR o.app_key LIKE 'lekmer-%')
        AND li.PartNo != '{o.SHIPPING_PART_NO}' AND li.LineAmount > 0
        AND DATE(o.OrderDate) BETWEEN @a AND @b
      GROUP BY p, b, m""", a=today - D(14), b=today - D(1))
    days = o._q(f"""
      SELECT p, CAST(d AS STRING) d FROM (
        SELECT li.PartNo p, DATE(o.OrderDate, 'Europe/Stockholm') d
        FROM `{o.NORCE}.order_items` li JOIN `{o.NORCE}.orders` o ON o.Id = li.OrderId
        WHERE (o.app_key LIKE 'babyshop-%' OR o.app_key LIKE 'lekmer-%') AND li.LineAmount > 0
          AND DATE(o.OrderDate) >= @a
        UNION ALL
        SELECT PartNo, DATE(OrderDate, 'Europe/Stockholm') FROM `{o.NORCE}.bm_offer_lines`
        WHERE LineAmount > 0 AND DATE(OrderDate) >= @a)
      WHERE p != '{o.SHIPPING_PART_NO}' GROUP BY p, d""", a=today - D(60))
    last, sold = {}, set()
    recent = (today - D(SPEND_DAYS + 1)).isoformat()
    for r in days:
        if r["d"] > last.get(r["p"], ""):
            last[r["p"]] = r["d"]
        if r["d"] >= recent:
            sold.add((r["p"], r["d"]))
    return vel, last, sold


# ── build ────────────────────────────────────────────────────────────────────
def build_stock(today: datetime.date, feed: dict, ads: list[dict] | None, uplift: dict,
                sku_econ: dict, now: datetime.datetime) -> dict:
    o = _ctx()
    MARKETS = o.MARKETS
    vel, last_sale, sold = _sales(today)
    pid, titles = o._sku_dims()
    ads = ads or []

    sku_u = defaultdict(float)                                    # all brands/markets, 14 d units
    sku_rev = defaultdict(lambda: defaultdict(lambda: defaultdict(float)))   # p → b → m → rev
    for r in vel:
        sku_u[r["p"]] += r["u"] or 0
        if r["m"] in MARKETS and r["b"] in BRANDS:
            sku_rev[r["p"]][r["b"]][r["m"]] += r["rev"] or 0

    def oos_now(p):
        f = feed.get(p)
        return f is None or f["stock"] <= 0 or f["avail"] == "out_of_stock"

    # ── snapshot first, so today's state is on file even if the rest fails
    snap_n = None
    try:
        snap_n = write_snapshot(feed, set(sku_u) | {a["p"] for a in ads}, now)
    except Exception as e:                                        # noqa: BLE001
        print(f"[bm offers] stock snapshot failed: {e}", flush=True)
    snaps = _snapshots(today - D(SPEND_DAYS))
    t_iso = today.isoformat()

    oos_cache = {}
    def oos_on(p, d):
        k = (p, d)
        if k not in oos_cache:
            if (p, d) in sold:
                v = False
            elif d == t_iso:
                v = oos_now(p)
            elif k in snaps:
                v = bool(snaps[k])
            else:
                ls = last_sale.get(p)
                v = oos_now(p) and (ls is None or d > ls)
            oos_cache[k] = v
        return oos_cache[k]

    pre_days = max((bm.BLACK_WEEK[0] - today).days, 0)
    bw_days = max((bm.CYBER_MONDAY - max(today, bm.BLACK_WEEK[0])).days + 1, 0)
    upl = uplift.get("all") or 1.0

    def cover(p):
        v = sku_u.get(p, 0) / 14
        return (feed[p]["stock"] / v) if (v > 0 and p in feed) else None

    def prebw(p):                      # in stock, but sells out before Black Week at current pace
        c = cover(p)
        return not oos_now(p) and c is not None and c < pre_days

    # ── spend by day: total, out-of-stock on the day; per SKU
    yday = (today - D(1)).isoformat()
    w7 = (today - D(7)).isoformat()                               # 7 full days: today-7 .. today-1
    daily = defaultdict(lambda: defaultdict(float))               # (b,m,d) → {total, oos}
    sku_ad = defaultdict(lambda: defaultdict(float))              # p → "b|m|kind" → SEK
    for a in ads:
        p, d, c = a["p"], a["d"], a["c"]
        o_ = oos_on(p, d)
        for b in (a["b"], "all"):
            for m in (a["m"], "all"):
                k = daily[(b, m, d)]
                k["total"] += c
                if o_:
                    k["oos"] += c
        key = a["b"] + "|" + a["m"]
        if w7 <= d <= yday:
            sku_ad[p][key + "|7d"] += c
            if o_:
                sku_ad[p][key + "|oos7"] += c
            if prebw(p):
                sku_ad[p][key + "|prebw7"] += c
        if d == t_iso:
            sku_ad[p][key + "|today"] += c
            if o_:
                sku_ad[p][key + "|oostoday"] += c
        if d == yday and o_:
            sku_ad[p][key + "|oosyday"] += c

    def agg_spend(b, m):
        def s(d0, d1, k):
            return sum(v[k] for (bb, mm, d), v in daily.items() if bb == b and mm == m and d0 <= d <= d1)
        return {"total_7d": o._r(s(w7, yday, "total")), "oos_7d": o._r(s(w7, yday, "oos")),
                "oos_yday": o._r(s(yday, yday, "oos")), "total_today": o._r(s(t_iso, t_iso, "total")),
                "oos_today": o._r(s(t_iso, t_iso, "oos")),
                "prebw_7d": o._r(sum(v for p, kv in sku_ad.items() for k, v in kv.items()
                                     if k.endswith("|prebw7") and (b == "all" or k.startswith(b + "|"))
                                     and (m == "all" or k.split("|")[1] == m)))}

    acc_markets = {"all": ["all"] + MARKETS, "babyshop": ["all"] + MARKETS,
                   "lekmer": ["all", "SE", "NO", "DK"]}
    spend = {b: {m: agg_spend(b, m) for m in acc_markets[b]} for b in ["all"] + BRANDS} if ads else None
    days14 = [(today - D(i)).isoformat() for i in range(SPEND_DAYS, -1, -1)]
    oos_daily = ({b: {m: [{"d": d, "total": o._r(daily[(b, m, d)]["total"]),
                           "oos": o._r(daily[(b, m, d)]["oos"])} for d in days14]
                      for m in acc_markets[b]} for b in ["all"] + BRANDS} if ads else None)

    # SKUs still receiving spend while out of stock (today, or yesterday's full day)
    now_rows = []
    for p, kv in sku_ad.items():
        for bm_ in {k.rsplit("|", 1)[0] for k in kv}:
            t_, y_ = kv.get(bm_ + "|oostoday", 0), kv.get(bm_ + "|oosyday", 0)
            if t_ + y_ <= 0:
                continue
            b, m = bm_.split("|")
            f = feed.get(p)
            tt = titles.get(p, (None, None))
            ls = last_sale.get(p)
            now_rows.append({
                "part": p, "brand": b, "m": m,
                "title": (f["title"] if f else tt[0]) or p, "maker": (f["brand"] if f else tt[1]) or "",
                "spend_today": o._r(t_), "spend_yday": o._r(y_),
                "in_feed": f is not None, "stock": f["stock"] if f else 0,
                "last_sale": ls, "still_oos": oos_now(p)})
    now_rows.sort(key=lambda r: -(r["spend_today"] + r["spend_yday"]))

    # ── products (Norce VariantId = product/colour; sizes grouped)
    groups = defaultdict(list)
    for p in set(feed) | set(sku_u) | set(sku_ad):
        groups[pid.get(p) or (feed[p]["g"] if p in feed else p)].append(p)

    rows = []
    for gid, skus in groups.items():
        rev_b = {b: {m: sum(sku_rev[p][b][m] for p in skus) for m in MARKETS} for b in BRANDS}
        for b in BRANDS:
            rev_b[b]["all"] = sum(rev_b[b].values())
        rev = {m: sum(rev_b[b][m] for b in BRANDS) for m in MARKETS + ["all"]}

        def adsum(kind, b=None, skus=skus):
            return {m: sum(v for p in skus for k, v in sku_ad[p].items()
                           if k.endswith("|" + kind) and k.split("|")[1] == m
                           and (b is None or k.startswith(b + "|"))) for m in MARKETS}
        ad7 = adsum("7d")
        if rev["all"] <= 0 and not any(ad7.values()):
            continue
        in_feed = [p for p in skus if p in feed]
        f0 = feed[in_feed[0]] if in_feed else None
        t0 = next((titles[p] for p in skus if p in titles), (None, None))
        units = sum(sku_u.get(p, 0) for p in skus)
        stock = sum(feed[p]["stock"] for p in in_feed)
        selling = [p for p in skus if sku_u.get(p, 0) > 0]
        oos_selling = [p for p in selling if oos_now(p)]
        v = units / 14
        cov = stock / v if v > 0 else None
        need = v * (pre_days + upl * bw_days)
        price = max((feed[p]["price"] for p in in_feed), default=None)
        sale = min((feed[p]["sale"] for p in in_feed), default=None)
        e = [sku_econ[p] for p in skus if p in sku_econ]
        u_e, g_e = sum(x["units"] for x in e), sum(x["gp2"] for x in e)
        flag = ("oos" if stock <= 0 else
                "prebw" if cov is not None and cov < pre_days else
                "sizes" if selling and len(oos_selling) / len(selling) >= 0.34 else
                "bw" if stock < need else "ok")
        rows.append({
            "id": gid, "brand": "all",          # stock is shared: one row for both brands
            "title": (f0["title"] if f0 else t0[0]) or skus[0],
            "maker": (f0["brand"] if f0 else t0[1]) or "", "cat": f0["cat"] if f0 else "",
            "url": f0["url"] if f0 else None, "price": o._r(price), "sale": o._r(sale),
            "depth": o._r(1 - sale / price, 3) if price and sale and sale < price * 0.99 else 0,
            "rev": {k: o._r(x) for k, x in rev.items()},
            "rev_b": {b: {k: o._r(x) for k, x in rev_b[b].items()} for b in BRANDS},
            "units14": o._r(units, 1), "stock": stock,
            "skus_selling": len(selling), "oos_selling": len(oos_selling),
            "cover": o._r(cov, 1), "need_to_cm": o._r(need), "short_to_cm": o._r(max(need - stock, 0)),
            "gp2_unit": o._r(g_e / u_e) if u_e else None,
            "flag": flag,
            # flat dicts = both brands; *_b = per brand. ad7_oos / ad_oos_today
            # count only spend on days the SKU was out of stock.
            "ad7": {m: o._r(x) for m, x in ad7.items()},
            "ad7_oos": {m: o._r(x) for m, x in adsum("oos7").items()},
            "ad_oos_today": {m: o._r(x) for m, x in adsum("oostoday").items()},
            "ad7_prebw": {m: o._r(x) for m, x in adsum("prebw7").items()},
            "ad_b": {b: {"ad7": {m: o._r(x) for m, x in adsum("7d", b).items()},
                         "ad7_oos": {m: o._r(x) for m, x in adsum("oos7", b).items()},
                         "ad_oos_today": {m: o._r(x) for m, x in adsum("oostoday", b).items()}}
                     for b in BRANDS}})

    keep = set()
    for m in ["all"] + MARKETS:
        keep |= {r["id"] for r in sorted(rows, key=lambda r: -(r["rev"][m] or 0))[:HERO_N]}
        for b in BRANDS:
            keep |= {r["id"] for r in sorted(rows, key=lambda r: -(r["rev_b"][b][m] or 0))[:HERO_N // 2]}
    top300 = sorted(rows, key=lambda r: -(r["rev"]["all"] or 0))[:300]
    keep |= {r["id"] for r in top300 if r["depth"] and r["depth"] >= 0.05}
    waste = lambda r: sum(r["ad7_oos"].values()) + sum(r["ad_oos_today"].values())
    keep |= {r["id"] for r in sorted(rows, key=lambda r: -waste(r))[:40] if waste(r) > 0}
    heroes = sorted((r for r in rows if r["id"] in keep), key=lambda r: -(r["rev"]["all"] or 0))

    sold_gone = [p for p in sku_u if p not in feed]
    return {"velocity_window": [(today - D(14)).isoformat(), yday], "bw_uplift": uplift,
            "need_basis": {"pre_bw_days": pre_days, "bw_days": bw_days},
            "products": heroes, "ad_spend": spend, "oos_daily": oos_daily,
            "oos_now": now_rows[:60],
            "snapshot": {"rows": snap_n, "first_day": "2026-11-04",
                         "days_from_snapshots": sorted({d for _, d in snaps})},
            "gone_from_feed": {"skus": len(sold_gone),
                               "rev14": o._r(sum(sum(sum(x.values()) for x in sku_rev[p].values())
                                                 for p in sold_gone))}}
