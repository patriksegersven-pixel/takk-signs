#!/usr/bin/env python3
"""
bm_offers.py: the `offers` section of the Black Month tab.

The lever this section serves is offer DEPTH (discount %) and WIDTH (how much
of the range is on offer) per market, judged on GP2. Four blocks:

  1. calendar   Live / upcoming / just-ended offers from the Airtable campaign
                calendar (base appzISMBB4Gr3LQIv, table 📣 Campaigns, the same
                one the babyshop-ads-assets routine reads), with markets
                resolved from "Markets to push in", depth parsed from the name
                and brief (the calendar has no depth field), a scope guess, the
                code, and readiness warnings for what the calendar is missing.
  2. economics  Offer vs full-price revenue, realised depth (markdown vs code),
                GP2 per order and GP2 margin, last 14 days vs the 14 before,
                per offer "during vs 14 days pre", and a daily series. Basis:
                Norce order lines with their standard price (bm_offers_norce),
                cost from norce.sku_costs, and the GP1→GP2 cost-to-serve per
                order from the Funnel export. See NOTES below.
  3. width      Per category: share of revenue sold on offer, realised depth,
                and the share of in-stock SKUs on markdown in the feed.
  4. stock      Hero products (top 50 per market by 14-day revenue + products
                on markdown among the best sellers) with shared stock, days of
                cover at 14-day pace and at last year's Black Week pace, broken
                sizes, and Google Shopping spend (last 7 days) on out-of-stock
                and low-cover SKUs from the Google Ads API (read-only).

Every block degrades on its own: a missing credential or a failed source puts
a note in the payload and leaves that block empty instead of failing the build.
"""
from __future__ import annotations

import datetime
import os
import re
import tempfile
import time
import xml.etree.ElementTree as ET
from collections import defaultdict

import httpx
import requests
from google.cloud import bigquery

import bm_common as bm

NORCE = f"{bm.GCP_PROJECT}.norce"
FUNNEL = os.environ.get("BQ_TABLE", "babyshop-funnel-data.bs_funnel_export.funnel_data")
MARKETS = bm.MARKETS
APP_ID = {"SE": 1244, "NO": 1264, "FI": 1265, "DK": 1266}
SHIPPING_PART_NO = "1000014"
OFFER_MIN_DEPTH = 0.01           # a line ≥1 % under its standard price is "on offer"
LOW_COVER_DAYS = 7
HERO_N = 50

AIRTABLE_BASE = "appzISMBB4Gr3LQIv"
AIRTABLE_TABLE = "tblqOXpefLeQ84v0T"
OFFER_TYPES = {"Main Offer", "Supporting Offer", "Weekend Treat", "Free shipping"}
# "Markets to push in" → our markets (same mapping as the ads-assets routine).
MARKET_TAGS = {"ALL": MARKETS, "NORDICS": MARKETS, "SWEDEN": ["SE"], "NORWAY": ["NO"],
               "EU": ["FI", "DK"], "excl SE": ["NO", "FI", "DK"],
               "excl UK": MARKETS, "excl SK": MARKETS}

ADS_ACCOUNTS = {"SE": "4851485396", "NO": "8623945183", "FI": "6161399704", "DK": "2054294342"}

NOTES = [
    "Full price = Norce's PriceStandard on the order line (the RRP ex VAT on ~99 % of lines). "
    "Offer line = sold ≥1 % under it after markdown and code discount; note that most of the "
    "range carries a standing markdown, so 'full price' is the minority even outside campaigns. Depth = 1 − paid ÷ standard price; markdown and code parts split by the "
    "line's DiscountAmount. Gross of returns and cancellations, like the rest of the Norce data.",
    "GP2 = Norce line revenue − current SKU cost (norce.sku_costs; uncosted lines take the "
    "costed lines' margin) − the market's GP1→GP2 cost per order from the Funnel export "
    "(last 28 days), spread over an order's lines by revenue. Returns sit inside that per-order "
    "cost, so offer and full-price lines carry the same return cost; if offer items are "
    "returned more, offer GP2 is overstated.",
    "Stock is one shared pool (Channable feed units_for_sale), so days of cover use sales in "
    "ALL markets. A SKU that sold in the last 14 days but is missing from the feed counts as "
    "sold out (Channable drops out-of-stock items). 'Short by Cyber Monday' = units needed at "
    "today's pace until Black Week, then at last year's Black Week multiple of that pace "
    "(LY Black Week units/day ÷ the same 14-day window a year earlier), minus stock.",
    "Depth and scope of calendar offers are parsed from the campaign name and brief: the "
    "calendar has no depth field. Realised depth in the tables is what customers actually got.",
]


# ── helpers ──────────────────────────────────────────────────────────────────
_bq = None
def bq() -> bigquery.Client:
    global _bq
    if _bq is None:
        _bq = bigquery.Client(project=bm.GCP_PROJECT)
    return _bq


def _q(sql: str, **params) -> list[dict]:
    qp = []
    for k, v in params.items():
        if isinstance(v, datetime.date):
            qp.append(bigquery.ScalarQueryParameter(k, "DATE", v))
        elif isinstance(v, list):
            qp.append(bigquery.ArrayQueryParameter(k, "STRING", v))
        else:
            qp.append(bigquery.ScalarQueryParameter(k, "STRING", v))
    job = bq().query(sql, job_config=bigquery.QueryJobConfig(query_parameters=qp))
    return [dict(r) for r in job.result()]


def _r(v, n=0):
    return None if v is None else round(float(v), n) if n else int(round(float(v)))


def _div(a, b):
    return a / b if b else None


D = datetime.timedelta


# ── 1. Airtable calendar ─────────────────────────────────────────────────────
def _airtable_token() -> str | None:
    raw = os.environ.get("AIRTABLE_PAT", "")
    # The secret has held two PATs glued together; take the first well-formed one.
    m = re.findall(r"pat[A-Za-z0-9]{14}\.[0-9a-f]{64}", raw)
    return m[0] if m else (raw.strip() or None)


def _airtable_records(today: datetime.date) -> list[dict]:
    tok = _airtable_token()
    if not tok:
        raise RuntimeError("AIRTABLE_PAT is not set on the job")
    lo, hi = (today - D(21)).isoformat(), (today + D(35)).isoformat()
    flt = f"AND(IS_AFTER({{End date}},'{lo}'),IS_BEFORE({{Start date}},'{hi}'))"
    out, off = [], None
    while True:
        p = {"filterByFormula": flt, "pageSize": 100}
        if off:
            p["offset"] = off
        r = requests.get(f"https://api.airtable.com/v0/{AIRTABLE_BASE}/{AIRTABLE_TABLE}",
                         params=p, headers={"Authorization": f"Bearer {tok}"}, timeout=30)
        r.raise_for_status()
        d = r.json()
        out += d.get("records", [])
        off = d.get("offset")
        if not off:
            return out


_PCT_RANGE = re.compile(r"(\d{1,2})\s?[-–]\s?(\d{1,2})\s?%")
_PCT = re.compile(r"(\d{1,2})\s?%")


def _depth(text: str) -> dict:
    """Discount levels mentioned in free text → {min, max, label}."""
    vals = []
    for a, b in _PCT_RANGE.findall(text):
        vals += [int(a), int(b)]
    vals += [int(x) for x in _PCT.findall(_PCT_RANGE.sub(" ", text))]
    vals = sorted({v for v in vals if 5 <= v <= 90})
    if not vals:
        return {"min": None, "max": None, "label": None}
    upto = re.search(r"up to|upp till|opp til|jopa|op til", text, re.I)
    if len(vals) == 1:
        lab = ("up to " if upto else "") + f"{vals[0]} %"
    else:
        lab = f"{vals[0]}–{vals[-1]} %"
    return {"min": vals[0] / 100, "max": vals[-1] / 100, "label": lab}


def _scope(f: dict, text: str, brands: set[str]) -> tuple[str, str]:
    name = (f.get("Campaign") or "").lower()
    low = text.lower()
    if f.get("Campaign Type") == "Free shipping":
        return "shipping", "Free shipping"
    if re.search(r"\bfree\b.*\bwith (the )?purchase|gift with purchase|\bgwp\b", low):
        return "gift", "Gift with purchase"
    if "hero deal" in name:
        found = sorted({b for b in brands if len(b) > 3 and re.search(rf"\b{re.escape(b)}\b", low)})
        return "products", "Hero deals" + (": " + ", ".join(found[:6]) if found else "")
    cats = f.get("Hero Categories") or []
    lp = f.get("Landing Page") or ""
    m = re.search(r"/brand/([a-z0-9-]+)", lp)
    found = sorted({b for b in brands if len(b) > 3 and re.search(rf"\b{re.escape(b)}\b", name + " " + low)})
    if re.search(r"n[äa]stan allt|almost everything|sitewide|site-wide|everything", low):
        return "sitewide", "Sitewide"
    if m or (found and f.get("Campaign Type") != "Main Offer"):
        bl = [m.group(1).replace("-", " ")] if m else found
        return "brand", "Brand: " + ", ".join(b.title() for b in bl[:6])
    if cats:
        return "category", "Category: " + ", ".join(cats[:5])
    if f.get("Campaign Type") == "Main Offer":
        return "wide", "Wide: selected range"
    return "unknown", "Not specified"


def _markets(tags: list[str]) -> list[str]:
    ms = set()
    for t in tags or []:
        ms.update(MARKET_TAGS.get(t, []))
    if "excl SE" in (tags or []):
        ms.discard("SE")
    return [m for m in MARKETS if m in ms]


def build_calendar(today: datetime.date, brands: set[str]) -> tuple[list, list]:
    offers, context = [], []
    for rec in _airtable_records(today):
        f = rec["fields"]
        sf = f.get("Storefront") or []
        if not ({"Babyshop", "KULING PB"} & set(sf)):
            continue
        start, end = f.get("Start date"), f.get("End date")
        if not start or not end or end < start:
            continue
        name = (f.get("Campaign") or "").strip()
        status = ("live" if start <= today.isoformat() <= end else
                  "upcoming" if start > today.isoformat() else "ended")
        markets = _markets(f.get("Markets to push in"))
        base = {"id": rec["id"], "name": name, "start": start, "end": end, "status": status,
                "type": f.get("Campaign Type"), "markets": markets,
                "hidden": bool(f.get("Hide campaign")) or "(paused)" in name.lower()}
        is_offer = f.get("Campaign Type") in OFFER_TYPES or bool(f.get("Offer type"))
        if not is_offer:
            if status != "ended":
                context.append(base)
            continue
        text = " ".join(str(f.get(k) or "") for k in ("Campaign", "Campaign Brief", "Product list name"))
        dep = _depth(text)
        scope, scope_label = _scope(f, text, brands)
        code = (f.get("Code") or "").strip() or None
        warn = []
        if status != "ended" and not base["hidden"]:
            if dep["max"] is None and scope not in ("gift", "shipping"):
                warn.append("no discount level in calendar")
            if f.get("Offer type") == "Code Offer" and not code:
                warn.append("code offer without code")
            if not (f.get("Landing Page") or "").strip():
                warn.append("no landing page")
            if not (f.get("Campaign Brief") or "").strip():
                warn.append("empty brief")
            if not markets:
                warn.append("no markets set")
        offers.append({**base, "offer_type": f.get("Offer type"), "depth": dep,
                       "scope": scope, "scope_label": scope_label, "code": code,
                       "landing": (f.get("Landing Page") or "").strip() or None,
                       "warnings": warn})
    key = {"live": 0, "upcoming": 1, "ended": 2}
    offers.sort(key=lambda o: (key[o["status"]], o["start"], o["name"]))
    context.sort(key=lambda o: (key[o["status"]], o["start"]))
    return offers, context


# ── 2. Offer economics ───────────────────────────────────────────────────────
def _cost_to_serve(today: datetime.date) -> dict:
    """SEK per order between GP1 and GP2, per market, from the Funnel export."""
    rows = _q(f"""
      SELECT market_level_1_kv m, SUM(kv_gp1) gp1, SUM(kv_gp2) gp2, SUM(kv_transactions) tx
      FROM `{FUNNEL}` WHERE Date BETWEEN @a AND @b AND shop_new = 'Babyshop'
        AND market_level_1_kv IN UNNEST(@ms) GROUP BY m""",
              a=today - D(28), b=today - D(1), ms=MARKETS)
    return {r["m"]: (r["gp1"] - r["gp2"]) / r["tx"] for r in rows if r["tx"]}


_COST_SQL = f"""
  fb AS (
    SELECT sc2.PartNo, ARRAY_AGG(STRUCT(sc2.CostUnit AS cu, sc2.CurrencyId AS cid)
           ORDER BY IF(cur.Code = 'SEK', 0, 1), sc2.PriceListId LIMIT 1)[OFFSET(0)] f
    FROM `{NORCE}.sku_costs` sc2 LEFT JOIN `{NORCE}.dim_currencies` cur ON cur.Id = sc2.CurrencyId
    GROUP BY sc2.PartNo),
  lines AS (
    SELECT l.OrderId, l.market, DATE(l.OrderDate, 'Europe/Stockholm') d, l.PartNo, l.QtyOrdered q,
      l.LineAmount * fx.to_sek rev,
      IFNULL(l.DiscountAmount, 0) * fx.to_sek code,
      GREATEST(IFNULL(NULLIF({{full}}, 0) * l.QtyOrdered, 0),
               l.LineAmount + IFNULL(l.DiscountAmount, 0)) * fx.to_sek full_val,
      COALESCE(sc.CostUnit * cfx.to_sek, fb.f.cu * ffx.to_sek) * l.QtyOrdered cogs
    FROM `{NORCE}.bm_offer_lines` l
    LEFT JOIN `{NORCE}.currency_rates` fx ON fx.currency_id = l.CurrencyId
    LEFT JOIN `{NORCE}.dim_price_lists` pl ON pl.ApplicationId = CASE l.market
      {' '.join(f"WHEN '{m}' THEN {a}" for m, a in APP_ID.items())} END
    LEFT JOIN `{NORCE}.sku_costs` sc ON sc.PartNo = l.PartNo AND sc.PriceListId = pl.PriceListId
    LEFT JOIN `{NORCE}.currency_rates` cfx ON cfx.currency_id = sc.CurrencyId
    LEFT JOIN fb ON fb.PartNo = l.PartNo
    LEFT JOIN `{NORCE}.currency_rates` ffx ON ffx.currency_id = fb.f.cid
    WHERE l.PartNo != '{SHIPPING_PART_NO}' AND l.LineAmount > 0
      AND DATE(l.OrderDate, 'Europe/Stockholm') BETWEEN @a AND @b),
  tagged AS (SELECT *, (full_val - rev) / full_val >= {OFFER_MIN_DEPTH} AS off FROM lines)
"""


def _economics_rows(a: datetime.date, b: datetime.date) -> list[dict]:
    from bm_offers_norce import FULL_UNIT
    sql = "WITH " + _COST_SQL.format(full=FULL_UNIT) + """,
      orders AS (
        SELECT OrderId, ANY_VALUE(market) m, ANY_VALUE(d) d, SUM(rev) rev,
          SUM(IF(off, rev, 0)) rev_off, SUM(full_val) full_val, SUM(IF(off, full_val, 0)) full_off,
          SUM(code) code, SUM(IF(cogs IS NOT NULL, rev, 0)) rev_c, SUM(cogs) cogs,
          SUM(IF(off AND cogs IS NOT NULL, rev, 0)) rev_c_off, SUM(IF(off, cogs, 0)) cogs_off,
          SUM(q) units
        FROM tagged GROUP BY OrderId)
      SELECT CAST(d AS STRING) d, m, rev_off >= 0.5 * rev AS offer_order, COUNT(*) orders,
        SUM(rev) rev, SUM(rev_off) rev_off, SUM(full_val) full_val, SUM(full_off) full_off,
        SUM(code) code, SUM(rev_c) rev_c, SUM(IFNULL(cogs, 0)) cogs, SUM(rev_c_off) rev_c_off,
        SUM(IFNULL(cogs_off, 0)) cogs_off, SUM(SAFE_DIVIDE(rev_off, rev)) off_orders_eq,
        SUM(units) units
      FROM orders GROUP BY d, m, offer_order"""
    return _q(sql, a=a, b=b)


def _category_rows(a: datetime.date, b: datetime.date) -> list[dict]:
    from bm_offers_norce import FULL_UNIT
    sql = "WITH " + _COST_SQL.format(full=FULL_UNIT) + """
      SELECT PartNo p, market m, SUM(rev) rev, SUM(IF(off, rev, 0)) rev_off,
             SUM(IF(off, full_val, 0)) full_off
      FROM tagged GROUP BY p, m"""
    return _q(sql, a=a, b=b)


DEPTH_BANDS = [(0.01, "Full price"), (0.20, "1–19 %"), (0.30, "20–29 %"),
               (0.40, "30–39 %"), (0.50, "40–49 %"), (9.0, "50 %+")]


def _band_rows(a: datetime.date, b: datetime.date) -> list[dict]:
    """Line-level revenue/cost per market × depth band, with each line's share
    of its order (to spread the per-order cost-to-serve)."""
    from bm_offers_norce import FULL_UNIT
    case = "CASE " + " ".join(f"WHEN dep < {lim} THEN '{lab}'" for lim, lab in DEPTH_BANDS) + " END"
    sql = "WITH " + _COST_SQL.format(full=FULL_UNIT) + f""",
      t2 AS (SELECT *, 1 - rev / full_val dep, rev / SUM(rev) OVER (PARTITION BY OrderId) share FROM tagged)
      SELECT market m, {case} band, SUM(rev) rev, SUM(full_val) full_val,
             SUM(IF(cogs IS NOT NULL, rev, 0)) rev_c, SUM(IFNULL(cogs, 0)) cogs, SUM(share) ord_eq
      FROM t2 GROUP BY m, band"""
    return _q(sql, a=a, b=b)


def _bands(rows: list[dict], cts: dict, market: str) -> list[dict]:
    rs = [r for r in rows if market == "all" or r["m"] == market]
    tot = sum(r["rev"] for r in rs) or 1
    out = []
    for _, lab in DEPTH_BANDS:
        b = [r for r in rs if r["band"] == lab]
        rev = sum(r["rev"] for r in b)
        if not rev:
            continue
        gp2 = 0.0
        for r in b:
            mg = 1 - r["cogs"] / r["rev_c"] if r["rev_c"] else 0
            gp2 += r["rev"] * mg - cts.get(r["m"], 0) * r["ord_eq"]
        full = sum(r["full_val"] for r in b)
        out.append({"band": lab, "rev": _r(rev), "share": _r(rev / tot, 4),
                    "depth": _r(1 - rev / full, 4) if full else None,
                    "gp2": _r(gp2), "gp2_m": _r(gp2 / rev, 4)})
    return out


_SUMS = ("orders", "rev", "rev_off", "full_val", "full_off", "code", "rev_c", "cogs",
         "rev_c_off", "cogs_off", "off_orders_eq", "units")


def _stats(rows: list[dict], cts: dict, days: int) -> dict | None:
    """Ratio-of-sums economics for a set of daily rows (any markets mix).
    Margins come from costed lines per market, then everything is summed."""
    if not rows:
        return None
    by_m = defaultdict(lambda: {"all": defaultdict(float), True: defaultdict(float), False: defaultdict(float)})
    for r in rows:
        for k in _SUMS:
            by_m[r["m"]]["all"][k] += r[k] or 0
            by_m[r["m"]][r["offer_order"]][k] += r[k] or 0
    T = defaultdict(float)
    for m, g in by_m.items():
        s, k_ord = g["all"], cts.get(m, 0)
        m_all = 1 - _div(s["cogs"], s["rev_c"]) if s["rev_c"] else 0
        m_off = 1 - _div(s["cogs_off"], s["rev_c_off"]) if s["rev_c_off"] else m_all
        rc_full, cg_full = s["rev_c"] - s["rev_c_off"], s["cogs"] - s["cogs_off"]
        m_full = 1 - cg_full / rc_full if rc_full > 0 else m_all
        rev_full = s["rev"] - s["rev_off"]
        T["rev"] += s["rev"]; T["rev_off"] += s["rev_off"]; T["full_val"] += s["full_val"]
        T["full_off"] += s["full_off"]; T["code"] += s["code"]; T["orders"] += s["orders"]
        T["units"] += s["units"]
        T["gp2"] += s["rev"] * m_all - k_ord * s["orders"]
        # line view: offer lines vs full-price lines
        T["gp2_off_lines"] += s["rev_off"] * m_off - k_ord * s["off_orders_eq"]
        T["gp2_full_lines"] += rev_full * m_full - k_ord * (s["orders"] - s["off_orders_eq"])
        # order view: mostly-offer orders vs mostly-full-price orders
        for cls, key in ((True, "o"), (False, "f")):
            c = g[cls]
            if c["orders"]:
                mc = 1 - c["cogs"] / c["rev_c"] if c["rev_c"] else m_all
                T[f"{key}_orders"] += c["orders"]; T[f"{key}_rev"] += c["rev"]
                T[f"{key}_gp2"] += c["rev"] * mc - k_ord * c["orders"]
    rev, rev_off = T["rev"], T["rev_off"]
    md = T["full_val"] - rev - T["code"]
    return {
        "days": days, "rev": _r(rev), "rev_day": _r(rev / days), "orders": _r(T["orders"]),
        "off_share": _r(_div(rev_off, rev), 4),
        "depth_offer": _r(1 - rev_off / T["full_off"], 4) if T["full_off"] else None,
        "depth_all": _r(1 - rev / T["full_val"], 4) if T["full_val"] else None,
        "md_share": _r(_div(md, md + T["code"]), 4),          # of the discount given, markdown part
        "discount": _r(T["full_val"] - rev),
        "gp2": _r(T["gp2"]), "gp2_day": _r(T["gp2"] / days), "gp2_m": _r(_div(T["gp2"], rev), 4),
        "gp2_m_off": _r(_div(T["gp2_off_lines"], rev_off), 4),
        "gp2_m_full": _r(_div(T["gp2_full_lines"], rev - rev_off), 4),
        "gp2_order": _r(_div(T["gp2"], T["orders"])),
        "gp2_order_off": _r(_div(T["o_gp2"], T["o_orders"])),
        "gp2_order_full": _r(_div(T["f_gp2"], T["f_orders"])),
        "aov_off": _r(_div(T["o_rev"], T["o_orders"])),
        "aov_full": _r(_div(T["f_rev"], T["f_orders"])),
        "off_orders_share": _r(_div(T["o_orders"], T["orders"]), 4),
    }


def _window(rows, a, b, market):
    a, b = a.isoformat(), b.isoformat()
    return [r for r in rows if a <= r["d"] <= b and (market == "all" or r["m"] == market)]


def build_economics(today, offers, cts):
    yday = today - D(1)
    first = datetime.date(2026, 10, 1)
    rows = _economics_rows(first - D(16), yday)
    if not rows:
        return None
    last = max(r["d"] for r in rows)
    yday = min(yday, datetime.date.fromisoformat(last))
    l14a, p14a, p14b = yday - D(13), yday - D(27), yday - D(14)
    mk = ["all"] + MARKETS
    out = {"through": yday.isoformat(),
           "windows": {"l14": {"from": l14a.isoformat(), "to": yday.isoformat()},
                       "p14": {"from": p14a.isoformat(), "to": p14b.isoformat()}},
           "cts_per_order": {m: _r(v) for m, v in cts.items()},
           "by_market": {m: {"l14": _stats(_window(rows, l14a, yday, m), cts, 14),
                              "p14": _stats(_window(rows, p14a, p14b, m), cts, 14)} for m in mk}}
    bl, bp = _band_rows(l14a, yday), _band_rows(p14a, p14b)
    out["bands"] = {m: {"l14": _bands(bl, cts, m), "p14": _bands(bp, cts, m)} for m in mk}
    # daily series since 1 Oct, per market + all
    days = [first + D(i) for i in range((yday - first).days + 1)]
    out["daily"] = {m: [dict(d=d.isoformat(), **{k: s[k] for k in ("rev", "off_share", "depth_offer", "gp2_m", "gp2")})
                        for d in days if (s := _stats(_window(rows, d, d, m), cts, 1))] for m in mk}
    # per offer: during (to yesterday) vs 14 days before start, in the offer's markets
    per = []
    for o in offers:
        s0 = datetime.date.fromisoformat(o["start"])
        e0 = min(datetime.date.fromisoformat(o["end"]), yday)
        if o["hidden"] or s0 > yday or e0 < first or not o["markets"]:
            continue
        res = {}
        for m in ["all"] + o["markets"]:
            ms = o["markets"] if m == "all" else [m]
            dur = [r for r in _window(rows, s0, e0, "all") if r["m"] in ms]
            pre = [r for r in _window(rows, s0 - D(14), s0 - D(1), "all") if r["m"] in ms]
            res[m] = {"during": _stats(dur, cts, (e0 - s0).days + 1), "pre": _stats(pre, cts, 14)}
        per.append({"id": o["id"], "from": s0.isoformat(), "to": e0.isoformat(), "by_market": res})
    out["per_offer"] = per
    return out


# ── 3/4. Feed, stock, velocity, ads ──────────────────────────────────────────
def load_feed() -> dict:
    """PartNo → SKU facts from the Channable feed (SE prices, shared stock)."""
    from inventory_client import CHANNABLE_FEED_URL, NS, _parse_sek
    if not CHANNABLE_FEED_URL:
        raise RuntimeError("CHANNABLE_FEED_URL is not set")
    items = {}
    with tempfile.NamedTemporaryFile(suffix=".xml") as tmp:
        with httpx.stream("GET", CHANNABLE_FEED_URL, timeout=180.0, follow_redirects=True) as r:
            r.raise_for_status()
            for chunk in r.iter_bytes(256 * 1024):
                tmp.write(chunk)
        tmp.flush()
        for _, el in ET.iterparse(tmp.name, events=("end",)):
            if el.tag != "item":
                continue
            def t(tag, ns=False):
                e = el.find("g:" + tag, NS) if ns else el.find(tag)
                return e.text.strip() if e is not None and e.text else ""
            sku = t("id", True)
            if sku:
                try:
                    units = int(round(float(t("units_for_sale") or 0)))
                except ValueError:
                    units = 0
                price = _parse_sek(t("price", True))
                items[sku] = {
                    "g": t("item_group_id", True) or sku,
                    "title": t("short_title", True) or t("title"),
                    "brand": t("brand", True), "cat": t("category_level_1") or "Other",
                    "price": price, "sale": _parse_sek(t("sale_price", True)) or price,
                    "stock": max(units, 0), "avail": t("availability", True),
                    "dead": t("custom_label_4", True) == "dead_stock", "url": t("link"),
                    "size": t("size", True)}
            el.clear()
    return items


def _velocity(today):
    """14-day units/revenue per PartNo × market, all Babyshop markets (norce BQ)."""
    a, b = today - D(14), today - D(1)
    return _q(f"""
      SELECT li.PartNo p, UPPER(SPLIT(o.app_key, '-')[OFFSET(1)]) m,
             SUM(li.QtyOrdered) u, SUM(li.LineAmount * fx.to_sek) rev
      FROM `{NORCE}.order_items` li JOIN `{NORCE}.orders` o ON o.Id = li.OrderId
      LEFT JOIN `{NORCE}.currency_rates` fx ON fx.currency_id = o.CurrencyId
      WHERE o.app_key LIKE 'babyshop-%' AND li.PartNo != '{SHIPPING_PART_NO}' AND li.LineAmount > 0
        AND DATE(o.OrderDate) BETWEEN @a AND @b
      GROUP BY p, m""", a=a, b=b), a, b


def _bw_uplift(today) -> dict:
    """Last year's Black Week units/day ÷ the 14-day window ending a year before
    our velocity window. Per market + all."""
    ly = lambda d: d - D(364)
    a, b = ly(today - D(14)), ly(today - D(1))
    bw_a, bw_b = bm.BLACK_WEEK[0] - D(364), bm.CYBER_MONDAY_LY
    rows = _q(f"""
      SELECT UPPER(SPLIT(o.app_key, '-')[OFFSET(1)]) m,
        SUM(IF(DATE(o.OrderDate) BETWEEN @a AND @b, li.QtyOrdered, 0)) / {(b - a).days + 1} base,
        SUM(IF(DATE(o.OrderDate) BETWEEN @c AND @e, li.QtyOrdered, 0)) / {(bw_b - bw_a).days + 1} bw
      FROM `{NORCE}.order_items` li JOIN `{NORCE}.orders` o ON o.Id = li.OrderId
      WHERE o.app_key LIKE 'babyshop-%' AND li.PartNo != '{SHIPPING_PART_NO}' AND li.LineAmount > 0
        AND DATE(o.OrderDate) BETWEEN @a AND @e
      GROUP BY m""", a=a, b=b, c=bw_a, e=bw_b)
    out = {r["m"]: _r(_div(r["bw"], r["base"]), 2) for r in rows if r["m"] in MARKETS}
    tb, tw = sum(r["base"] for r in rows), sum(r["bw"] for r in rows)
    out["all"] = _r(_div(tw, tb), 2)
    out["window_ly"] = {"base": [a.isoformat(), b.isoformat()], "bw": [bw_a.isoformat(), bw_b.isoformat()]}
    return out


def _ads_spend() -> dict:
    """Google Shopping/PMax cost per product item id per market, last 7 days, SEK.
    Read-only GAQL through refresh_roas_sims' client."""
    import refresh_roas_sims as rs
    fx = {r["c"]: r["s"] for r in _q(f"SELECT currency_code c, to_sek s FROM `{NORCE}.currency_rates`")}
    svc = rs.ads_client().get_service("GoogleAdsService")
    out = {}
    q = ("SELECT segments.product_item_id, customer.currency_code, metrics.cost_micros, "
         "metrics.clicks FROM shopping_performance_view "
         "WHERE segments.date DURING LAST_7_DAYS AND metrics.cost_micros > 0")
    for m, cid in ADS_ACCOUNTS.items():
        d = defaultdict(float)
        for row in rs._search(svc, cid, q):
            d[row.segments.product_item_id.upper()] += (
                row.metrics.cost_micros / 1e6 * fx.get(row.customer.currency_code, 1.0))
        out[m] = d
    return out


def _sku_dims() -> tuple[dict, dict]:
    """PartNo → Norce VariantId (one per product/colour; ProductId is per size)
    and PartNo → (title, brand) for SKUs that are no longer in the feed."""
    rows = _q(f"""
      SELECT s.PartNo p, CAST(COALESCE(pr.VariantId, pr.Id) AS STRING) g,
             COALESCE(t.title, CONCAT(IFNULL(CONCAT(m.Name, ' '), ''), pr.DefaultName)) t,
             COALESCE(t.brand, m.Name) b
      FROM `{NORCE}.product_skus` s
      JOIN `{NORCE}.products` pr ON pr.Id = s.ProductId
      LEFT JOIN `{NORCE}.dim_manufacturers` m ON m.ManufacturerId = pr.ManufacturerId
      LEFT JOIN `{NORCE}.sku_titles` t ON t.PartNo = s.PartNo""")
    return {r["p"]: "v" + r["g"] for r in rows}, {r["p"]: (r["t"], r["b"]) for r in rows}


def build_stock(today, feed, ads, uplift):
    vel_rows, va, vb = _velocity(today)
    pid, titles = _sku_dims()
    sku_u = defaultdict(float)                       # all markets, units 14d
    sku_m = defaultdict(lambda: defaultdict(float))  # PartNo → market → rev
    for r in vel_rows:
        sku_u[r["p"]] += r["u"] or 0
        if r["m"] in MARKETS:
            sku_m[r["p"]][r["m"]] += r["rev"] or 0
    upl = uplift.get("all") or 1.0
    # Units a SKU needs from today through Cyber Monday: current pace until Black
    # Week, last year's Black Week multiple of it during Black Week.
    pre_days = max((bm.BLACK_WEEK[0] - today).days, 0)
    bw_days = (bm.CYBER_MONDAY - max(today, bm.BLACK_WEEK[0])).days + 1

    def need(units14):
        v = units14 / 14
        return v * (pre_days + upl * max(bw_days, 0))

    # A SKU that sold in the last 14 days but is no longer in the feed has (in
    # every case we checked) sold out: Channable drops out-of-stock items.
    def oos(p):
        f = feed.get(p)
        return f is None or f["stock"] <= 0 or f["avail"] == "out_of_stock"

    def cover(p):
        v = sku_u.get(p, 0) / 14
        return (feed[p]["stock"] / v) if (v > 0 and p in feed) else None

    def low(p):
        c = cover(p)
        return not oos(p) and c is not None and c < LOW_COVER_DAYS

    groups = defaultdict(list)
    for p in set(feed) | set(sku_u):
        groups[pid.get(p) or (feed[p]["g"] if p in feed else p)].append(p)

    ad_ids = {m: {p.upper(): c for p, c in d.items()} for m, d in ads.items()}
    rows = []
    for gid, skus in groups.items():
        rev = {m: sum(sku_m[p][m] for p in skus) for m in MARKETS}
        rev["all"] = sum(rev.values())
        ad = {m: sum(d.get(p.upper(), 0) for p in skus) for m, d in ad_ids.items()}
        if rev["all"] <= 0 and not any(ad.values()):
            continue
        in_feed = [p for p in skus if p in feed]
        f0 = feed[in_feed[0]] if in_feed else None
        t0 = next((titles[p] for p in skus if p in titles), (None, None))
        units = sum(sku_u.get(p, 0) for p in skus)
        stock = sum(feed[p]["stock"] for p in in_feed)
        selling = [p for p in skus if sku_u.get(p, 0) > 0]
        oos_selling = [p for p in selling if oos(p)]
        gone = [p for p in selling if p not in feed]
        v = units / 14
        cov = stock / v if v > 0 else None
        nd = need(units)
        price = max((feed[p]["price"] for p in in_feed), default=None)
        sale = min((feed[p]["sale"] for p in in_feed), default=None)
        flag = ("oos" if stock <= 0 else
                "low" if cov is not None and cov < LOW_COVER_DAYS else
                "sizes" if selling and len(oos_selling) / len(selling) >= 0.34 else
                "bw" if stock < nd else "ok")
        rows.append({
            "id": gid, "title": (f0["title"] if f0 else t0[0]) or skus[0],
            "brand": (f0["brand"] if f0 else t0[1]) or "", "cat": f0["cat"] if f0 else "",
            "url": f0["url"] if f0 else None, "price": _r(price), "sale": _r(sale),
            "depth": _r(1 - sale / price, 3) if price and sale and sale < price * 0.99 else 0,
            "rev": {k: _r(x) for k, x in rev.items()}, "units14": _r(units, 1),
            "stock": stock, "skus_selling": len(selling), "oos_selling": len(oos_selling),
            "gone_from_feed": len(gone),
            "cover": _r(cov, 1), "need_to_cm": _r(nd), "short_to_cm": _r(max(nd - stock, 0)),
            "flag": flag, "ad7": {m: _r(x) for m, x in ad.items()},
            "ad7_oos": {m: _r(sum(ad_ids[m].get(p.upper(), 0) for p in skus if oos(p))) for m in ad_ids},
            "ad7_low": {m: _r(sum(ad_ids[m].get(p.upper(), 0) for p in skus if low(p))) for m in ad_ids}})
    # keep: top N per market + overall, markdown products among the top 300, and
    # the products with the most Shopping spend on out-of-stock / low-cover SKUs
    keep = set()
    for m in ["all"] + MARKETS:
        keep |= {r["id"] for r in sorted(rows, key=lambda r: -(r["rev"][m] or 0))[:HERO_N]}
    top300 = sorted(rows, key=lambda r: -(r["rev"]["all"] or 0))[:300]
    keep |= {r["id"] for r in top300 if r["depth"] and r["depth"] >= 0.05}
    waste = lambda r: sum(r["ad7_oos"].values()) + sum(r["ad7_low"].values())
    keep |= {r["id"] for r in sorted(rows, key=lambda r: -waste(r))[:40] if waste(r) > 0}
    heroes = sorted((r for r in rows if r["id"] in keep), key=lambda r: -(r["rev"]["all"] or 0))

    # Shopping spend split by stock state, per market (all SKUs, not just heroes)
    spend = {}
    for m, d in ad_ids.items():
        spend[m] = {"total": _r(sum(d.values())),
                    "oos": _r(sum(c for p, c in d.items() if p in feed and oos(p))),
                    "gone": _r(sum(c for p, c in d.items() if p not in feed and p in sku_u)),
                    "low": _r(sum(c for p, c in d.items() if low(p))),
                    "unknown": _r(sum(c for p, c in d.items() if p not in feed and p not in sku_u))}
    if spend:
        spend["all"] = {k: sum(spend[m][k] or 0 for m in ad_ids) for k in spend[MARKETS[0]]}
    sold_gone = [p for p in sku_u if p not in feed]
    return {"velocity_window": [va.isoformat(), vb.isoformat()], "bw_uplift": uplift,
            "need_basis": {"pre_bw_days": pre_days, "bw_days": max(bw_days, 0)},
            "products": heroes, "ad_spend": spend or None,
            "gone_from_feed": {"skus": len(sold_gone),
                               "rev14": _r(sum(sum(sku_m[p].values()) for p in sold_gone))}}


def build_width(cat_rows, feed):
    """Per category × market: revenue share sold on offer, realised depth, and
    the share of in-stock SKUs on markdown in the (SE) feed."""
    agg = defaultdict(lambda: defaultdict(lambda: [0.0, 0.0, 0.0]))
    for r in cat_rows:
        cat = (feed.get(r["p"]) or {}).get("cat") or "Sold out (not in feed)"
        for m in (r["m"], "all"):
            a = agg[cat][m]
            a[0] += r["rev"] or 0; a[1] += r["rev_off"] or 0; a[2] += r["full_off"] or 0
    cat_feed = defaultdict(lambda: [0, 0, 0.0])
    for f in feed.values():
        if f["stock"] > 0 and not f["dead"] and f["price"]:
            c = cat_feed[f["cat"]]
            c[0] += 1
            if f["sale"] < f["price"] * 0.99:
                c[1] += 1; c[2] += 1 - f["sale"] / f["price"]
    out = []
    for cat, ms in agg.items():
        row = {"cat": cat, "skus_in_stock": cat_feed[cat][0],
               "feed_on_sale": _r(_div(cat_feed[cat][1], cat_feed[cat][0]), 3),
               "feed_depth": _r(_div(cat_feed[cat][2], cat_feed[cat][1]), 3), "m": {}}
        for m, (rev, ro, fo) in ms.items():
            row["m"][m] = {"rev": _r(rev), "off_share": _r(_div(ro, rev), 3),
                           "depth": _r(1 - ro / fo, 3) if fo else None}
        out.append(row)
    out.sort(key=lambda r: -(r["m"].get("all", {}).get("rev") or 0))
    return out


# ── build ────────────────────────────────────────────────────────────────────
def build(ctx: dict) -> dict:
    today = ctx["today"]
    notes, sources, t0 = list(NOTES), {}, time.time()

    def step(name, fn, *a):
        t = time.time()
        try:
            v = fn(*a)
            sources[name] = {"ok": True, "s": round(time.time() - t, 1)}
            return v
        except Exception as e:                      # one source down ≠ section down
            sources[name] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:300]}
            print(f"[bm offers] {name} failed: {e}", flush=True)
            return None

    # Norce line prices first: economics reads the table it maintains.
    from bm_offers_norce import sync as norce_sync
    sources["norce_sync_log"] = step("norce_sync", norce_sync)
    feed = step("feed", load_feed) or {}
    brands = {f["brand"].lower() for f in feed.values() if f.get("brand")}
    cal = step("airtable", build_calendar, today, brands)
    offers, context = cal if cal else ([], [])
    cts = step("cost_to_serve", _cost_to_serve, today) or {}
    econ = step("economics", build_economics, today, offers, cts) if cts else None
    cat_rows = step("categories", _category_rows, today - D(14), today - D(1)) or []
    width = build_width(cat_rows, feed) if cat_rows and feed else []
    uplift = step("bw_uplift", _bw_uplift, today) or {"all": None}
    ads = step("google_ads", _ads_spend)
    stock = step("stock", build_stock, today, feed, ads or {}, uplift) if feed else None

    if not sources["airtable"]["ok"]:
        notes.insert(0, "Campaign calendar unavailable: " + sources["airtable"]["error"])
    if ads is None:
        notes.insert(0, "Google Ads spend per product unavailable: " + sources["google_ads"]["error"])
    if not sources["norce_sync"]["ok"]:
        notes.insert(0, "Norce line prices not refreshed this run (economics may lag): "
                     + sources["norce_sync"]["error"])
    return {"today": today.isoformat(), "offers": offers, "context": context,
            "economics": econ, "width": width[:30], "stock": stock,
            "notes": notes, "sources": sources, "build_seconds": round(time.time() - t0, 1)}
