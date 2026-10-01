#!/usr/bin/env python3
"""
Nordic free-shipping threshold test — payload for /api/shipping-test (all
markets, or ?market=XX) and /api/fi-shipping-test (the FI slice, old URL).

Babyshop lowered the free-shipping threshold in all four Nordic markets:

  market  normal threshold  test threshold  base fee below the threshold
  FI      129 €             69 €            6.95 € (unchanged)        test since 2026-09-29
  SE      999 SEK           499 SEK         49 SEK (unchanged)        test since 2026-09-30
  NO      1 299 NOK         799 NOK         75 → 69 NOK               test since 2026-09-30
  DK      899 DKK           499 DKK         45 DKK (unchanged)        test since 2026-09-30

Each market's TEST regime is compared against its own pre-campaign BASELINE;
the shared Nordic "free above 25 € / 250 kr" campaign (25–29 Sep) and, for
SE/NO/DK, the ~1.3 normal days between the campaign and their test ("interim")
are shown as context only.

OLD-STACK module: reads only this project's own data plus the Funnel export
the service already reads — never the claude-private warehouse.

Sources:
  • `project-a7ade44e-e7e3-4871-a83.norce.orders` / `.order_items` (norce_sync.py,
    NIGHTLY — data runs through the last sync, not live). Market = the app_key
    suffix (babyshop-fi + lekmer-fi = FI, …).
    Goods value incl. VAT = Σ LineAmount·(1+VatRate/100) over non-shipping lines;
    the shipping fee is the PartNo 1000014 line, grossed up the same way.
  • Funnel export via bq_source.filtered_daily({"market": X}) — GP2 and net
    revenue per day (SEK), with the KV dedup/netting already applied there. A
    Funnel day keeps filling for ~2 days, so only days ≤ today − FUNNEL_LAG_DAYS
    count.
  • Funnel export `Sessions` per market_level_1_kv — conversion denominator.
  • Shipping cost per order is not in either source here → null (n/a).

Order filter: StatusId 4/5 (the new stack's is_revenue_status), PLUS status 2
("new", not yet processed) for orders younger than PENDING_GRACE_DAYS. Every
fresh order sits in status 2 until Norce processes it, so dropping status 2
outright would empty the most recent hours of the test; older status-2 orders
(~1 % of history) are stuck/abandoned and stay excluded. Status 6 is excluded.

Control: a market's own baseline is seasonally confounded, so each market is
also indexed against a control. Preferred: the other Nordic markets that were
NOT in a test at any point of this market's test window. Since 30 Sep all four
are treated, so the fallback is the same market LAST YEAR, same weekday
(date − 364 days, same Norce tables, which reach back to June 2025). If neither
exists the page shows the trend without a control and says so.
"""
from __future__ import annotations

import datetime as dt
import math
import os
from collections import defaultdict
from zoneinfo import ZoneInfo

from google.cloud import bigquery

import bq_source

CACHE_KEY = "v2__shipping-test"
CACHE_TTL = 1800

NORCE_PROJECT = os.environ.get("NORCE_BQ_PROJECT", "project-a7ade44e-e7e3-4871-a83")
NORCE_DATASET = os.environ.get("NORCE_BQ_DATASET", "norce")
NORCE = f"`{NORCE_PROJECT}.{NORCE_DATASET}`"
NORCE_LOCATION = os.environ.get("NORCE_BQ_LOCATION", "EU")
SHIPPING_PART_NO = "1000014"
REVENUE_STATUSES = (4, 5)        # = is_revenue_status on the new stack
MARKET_SQL = "UPPER(REGEXP_EXTRACT(app_key, r'-([a-z]+)$'))"   # babyshop-fi / lekmer-fi -> FI
FUNNEL_LAG_DAYS = 2              # Funnel days keep growing ~2 days; GP2 only on older days
UTC = dt.timezone.utc


def _utc(y, mo, d, h, mi):
    return dt.datetime(y, mo, d, h, mi, tzinfo=UTC)


# ═════════════════════════════════════════════════════════════════════════════
# REGIMES — edit here.
#
# User-provided business change, announced 2026-10-01 ("until further notice"):
#   SE  free shipping from 499 SEK   (normal 999 SEK; 49 SEK pickup below, unchanged)
#   NO  free shipping from 799 NOK   (normal 1 299 NOK) AND base fee 75 → 69 NOK
#   DK  free shipping from 499 DKK   (normal 899 DKK; 45 DKK pickup below, unchanged)
#   FI  unchanged: free from 69 € since 2026-09-29 07:15 UTC (normal 129 €, 6.95 € below)
# Shared history (all four in sync): free-shipping promos until ~11 Aug and
# 4–15 Sep 2026; Nordic campaign 25 Sep ~11:19 UTC → 29 Sep ~07:15 UTC with
# free shipping above 250 SEK/NOK/DKK and 25 € (FI).
#
# All timestamps are UTC and were DETECTED from Norce orders (goods value vs the
# shipping line). build() re-runs the same detection on every refresh and puts
# the evidence in the payload (`detection`); a pinned value that drifts more
# than DETECT_WARN_HOURS from the data raises a note on the page.
#   campaign_start  paid → free switch of orders in [campaign_thr, test)
#   campaign_end    free → paid switch of orders in [campaign_thr, test)
#   test_start      paid → free switch of orders in [test, normal)
#                   (FI: = campaign_end, the 25 € → 69 € switch)
# The detector picks the split that misclassifies the fewest orders (stray
# voucher-free orders don't move it); evidence = last order of the old regime
# and first of the new, the pinned value is their midpoint.
# Detection evidence (run 2026-10-01, Norce synced through 2026-10-01 ~00:45 UTC):
#   see the comment beside each pinned value below.
# Set test_start to None to fall back to the live detection (or, before the
# switch is visible in the data, to "waiting for data").
# ═════════════════════════════════════════════════════════════════════════════
MARKETS = ["FI", "SE", "NO", "DK"]
ANNOUNCED = dt.date(2026, 10, 1)

MARKET_CFG: dict[str, dict] = {
    "FI": dict(
        name="Finland", currency="EUR", unit="€", tz="Europe/Helsinki",
        normal=129, test=69, campaign_thr=25,
        fee_normal=6.95, fee_test=6.95, fee_label="Posti pickup",
        # last 6.95 € order (< 69 €) 11:18:21, first free 25–69 € order 13:26:43
        campaign_start=_utc(2026, 9, 25, 11, 19),
        # last free 25–69 € order 07:03:16, first 6.95 € order (< 69 €) 07:26:39
        campaign_end=_utc(2026, 9, 29, 7, 15),        # = test start (25 € → 69 €)
        test_start=_utc(2026, 9, 29, 7, 15),
        test_end=None,                                # open-ended
        test_announced=dt.date(2026, 9, 29),
        bands=[(0, 25, "< 25 €"), (25, 55, "25–55 €"), (55, 69, "55–69 €"),
               (69, 85, "69–85 €"), (85, 129, "85–129 €"), (129, None, "129 € +")],
        hist_bin=5, hist_max=200),
    "SE": dict(
        name="Sweden", currency="SEK", unit="kr", tz="Europe/Stockholm",
        normal=999, test=499, campaign_thr=250,
        fee_normal=49, fee_test=49, fee_label="pickup",
        # last paid 250–499 kr order 11:51:45 (309 kr / 59 kr), first free 12:12:30 (401 kr)
        campaign_start=_utc(2026, 9, 25, 12, 2),
        # last free 250–499 kr order 07:05 (419 kr), first 49 kr one 07:14 (439 kr). NB ~15–20 % of
        # 250–499 kr orders kept shipping free after this (vs ~1.5 % before the campaign).
        campaign_end=_utc(2026, 9, 29, 7, 10),
        # last 49 kr order in 499–999 kr 13:22:24 (669 kr), first free 13:31:03 (799 kr)
        test_start=_utc(2026, 9, 30, 13, 26),
        test_end=None,
        test_announced=ANNOUNCED,
        bands=[(0, 250, "< 250 kr"), (250, 399, "250–399 kr"), (399, 499, "399–499 kr"),
               (499, 650, "499–650 kr"), (650, 999, "650–999 kr"), (999, None, "999 kr +")],
        hist_bin=50, hist_max=2000),
    "NO": dict(
        name="Norway", currency="NOK", unit="kr", tz="Europe/Oslo",
        normal=1299, test=799, campaign_thr=250,
        fee_normal=75, fee_test=69, fee_label="pickup",
        # last 75 kr order in 250–799 kr 11:52:58 (360 kr), first free 12:33:34 (719 kr)
        campaign_start=_utc(2026, 9, 25, 12, 13),
        # last free 250–799 kr order 07:33:45 (699 kr), first 75 kr one 07:52:52 (728 kr)
        campaign_end=_utc(2026, 9, 29, 7, 43),
        # last 75 kr order in 799–1299 kr 12:34:17 (959 kr), first free 13:30:33 (899 kr);
        # first 69 kr base fee 13:41 (678 kr). Small orders: 99 kr (< 149 kr) before the campaign,
        # 95 kr on 127–187 kr baskets from 29 Sep — see the report; not modelled separately.
        test_start=_utc(2026, 9, 30, 13, 2),
        test_end=None,
        test_announced=ANNOUNCED,
        bands=[(0, 149, "< 149 kr"), (149, 600, "149–600 kr"), (600, 799, "600–799 kr"),
               (799, 1000, "799–1 000 kr"), (1000, 1299, "1 000–1 299 kr"), (1299, None, "1 299 kr +")],
        hist_bin=50, hist_max=2000),
    "DK": dict(
        name="Denmark", currency="DKK", unit="kr", tz="Europe/Copenhagen",
        normal=899, test=499, campaign_thr=250,
        fee_normal=45, fee_test=45, fee_label="pickup",
        # last 45 kr order in 250–499 kr 11:31:53 (398 kr), first free 13:46:10 (389 kr)
        campaign_start=_utc(2026, 9, 25, 12, 39),
        # last free 250–499 kr order 06:11:02 (359 kr), first 45 kr one 08:49:39 (449 kr).
        # NB orders < 250 kr shipped free from ~08:00 on 29 Sep until ~10:20 on 30 Sep.
        campaign_end=_utc(2026, 9, 29, 7, 30),
        # last 45 kr order in 499–899 kr 12:50:24 (519 kr), first free 14:51:38 (784 kr)
        test_start=_utc(2026, 9, 30, 13, 51),
        test_end=None,
        test_announced=ANNOUNCED,
        bands=[(0, 250, "< 250 kr"), (250, 399, "250–399 kr"), (399, 499, "399–499 kr"),
               (499, 650, "499–650 kr"), (650, 899, "650–899 kr"), (899, None, "899 kr +")],
        hist_bin=50, hist_max=1500),
}

# ── Baseline ─────────────────────────────────────────────────────────────────
# The "normal" regime is regularly interrupted by other free-shipping promos
# (all four markets in sync: free shipping until ~11 Aug and again 4–15 Sep
# 2026). A plain "last 28 days" window would mix those in, so a market's
# baseline is the last BASELINE_DAYS local days before its campaign day on
# which the normal policy verifiably applied: ≤ NORMAL_MAX_FREE_SMALL of orders
# with goods below the TEST threshold shipped free (normal days sit at 0–2 %,
# promo days at 90 %+).
BASELINE_DAYS  = 28
BASELINE_LOOKBACK_DAYS = 120     # how far back to search for normal days
NORMAL_MAX_FREE_SMALL  = 0.10
PROMO_MIN_FREE_SMALL   = 0.50    # days above this are shaded as promo on the trend
BASELINE_EXCLUDE: set[dt.date] = set()   # manual exclusions, e.g. {dt.date(2026, 9, 1)}

TREND_DAYS     = 60              # daily chart window
MIN_TEST_DAYS  = 14              # below this the page says "too early to call"
PENDING_GRACE_DAYS = 3
LY_OFFSET_DAYS = 364             # last-year control: same weekday, 52 weeks back
DETECT_MIN_AFTER = 5             # orders after a detected switch needed to accept it
DETECT_MIN_SHARE = 0.7           # …and the share of them that must follow the new regime
MIN_ADJ_DAYS   = 3               # whole test days before a control-adjusted change is shown
DETECT_WARN_HOURS = 3            # pinned vs detected drift that raises a page note
PERIODS = ("baseline", "campaign", "interim", "test")


_client = None


def _norce_bq() -> bigquery.Client:
    global _client
    if _client is None:
        _client = bigquery.Client(project=NORCE_PROJECT, location=NORCE_LOCATION,
                                  credentials=bq_source._credentials())
    return _client


def _rows(sql, params):
    cfg = bigquery.QueryJobConfig(query_parameters=params)
    return list(_norce_bq().query(sql, job_config=cfg).result())


def _order_sql() -> str:
    # `orders` is partitioned on OrderDate, so the @t0 bound prunes; order_items
    # has no date and is reached through the order ids (clustered on OrderId).
    return f"""
    WITH o AS (
      SELECT Id, OrderDate AS order_date, {MARKET_SQL} AS market
      FROM {NORCE}.orders
      WHERE OrderDate >= @t0 AND {MARKET_SQL} IN UNNEST(@mk)
        AND (StatusId IN ({", ".join(map(str, REVENUE_STATUSES))})
             OR (StatusId = 2 AND OrderDate >=
                 TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {PENDING_GRACE_DAYS} DAY)))
    ), i AS (
      SELECT OrderId AS Id,
             SUM(IF(PartNo = '{SHIPPING_PART_NO}', LineAmount * (1 + IFNULL(VatRate, 0) / 100), 0))  AS ship,
             SUM(IF(PartNo != '{SHIPPING_PART_NO}' OR PartNo IS NULL,
                    LineAmount * (1 + IFNULL(VatRate, 0) / 100), 0))                             AS goods
      FROM {NORCE}.order_items
      WHERE OrderId IN (SELECT Id FROM o)
      GROUP BY 1
    )
    SELECT o.order_date, o.market, IFNULL(i.goods, 0) AS goods, IFNULL(i.ship, 0) AS ship
    FROM o LEFT JOIN i USING (Id)"""


def _ly_sql() -> str:
    """Last-year daily order counts per market and LOCAL day (count only — the
    control is an orders index, so order_items aren't needed)."""
    day = "CASE " + " ".join(
        f"WHEN {MARKET_SQL} = '{m}' THEN DATE(OrderDate, '{c['tz']}')" for m, c in MARKET_CFG.items()) + " END"
    return f"""
    SELECT {MARKET_SQL} AS market, {day} AS d, COUNT(*) AS n
    FROM {NORCE}.orders
    WHERE OrderDate >= @l0 AND OrderDate < @l1 AND {MARKET_SQL} IN UNNEST(@mk)
      AND StatusId IN ({", ".join(map(str, REVENUE_STATUSES))})
    GROUP BY 1, 2"""


def _stats(xs):
    n = len(xs)
    if n == 0:
        return 0, 0.0, 0.0
    m = sum(xs) / n
    v = sum((x - m) ** 2 for x in xs) / (n - 1) if n > 1 else 0.0
    return n, m, v


def _days(a, b):
    return [a + dt.timedelta(days=i) for i in range((b - a).days + 1)] if b >= a else []


def _free(sh):
    return sh < 0.005


def _rel(a, b):
    return round((a - b) / b, 4) if a is not None and b else None


# ── Switch detection ─────────────────────────────────────────────────────────
def detect_switch(orders, lo, hi, w0, w1, to_free: bool):
    """Change point in the free/paid sequence of orders with goods in [lo, hi)
    placed in [w0, w1). `to_free`: the switch makes these orders free (else it
    makes them pay again). Picks the split minimising misclassified orders, so
    the odd voucher-free order in a paid regime doesn't move it. Returns the
    evidence dict, or None when no switch is visible (yet)."""
    xs = sorted((ts, g, sh) for ts, g, sh in orders
                if w0 <= ts < w1 and g >= lo and (hi is None or g < hi))
    n = len(xs)
    if n < 2:
        return None
    tgt = [_free(sh) == to_free for _, _, sh in xs]      # True = looks like the new regime
    tot = sum(tgt)
    best_k, best_err, pre = n, tot, 0                    # k = n: no switch
    for k in range(n):
        err = pre + (n - k) - (tot - pre)                # new-looking before k + old-looking from k
        if err < best_err:
            best_k, best_err = k, err
        pre += tgt[k]
    after = n - best_k
    after_ok = sum(tgt[best_k:])
    if after < DETECT_MIN_AFTER or after_ok / after < DETECT_MIN_SHARE:
        return None
    before_idx = [i for i in range(best_k) if not tgt[i]]
    if not before_idx:
        return None
    lb = xs[before_idx[-1]]
    fa = next(xs[i] for i in range(best_k, n) if tgt[i])
    mid = lb[0] + (fa[0] - lb[0]) / 2

    def o(x):
        return {"ts_utc": x[0].strftime("%Y-%m-%d %H:%M:%S"), "goods": round(x[1], 2), "fee": round(x[2], 2)}
    return {"band": f"{lo}–{hi if hi is not None else '∞'}", "to": "free" if to_free else "paid",
            "last_before": o(lb), "first_after": o(fa),
            "switch_utc": mid.strftime("%Y-%m-%d %H:%M"), "_ts": mid,
            "n_before": best_k, "n_after": after,
            "after_share": round(after_ok / after, 3), "misfits": best_err}


def _detect_market(cfg, orders, max_ts):
    cs, ce = cfg["campaign_start"], cfg["campaign_end"]
    det = {
        "campaign_start": detect_switch(orders, cfg["campaign_thr"], cfg["test"],
                                        cs - dt.timedelta(days=3), cs + dt.timedelta(days=2), True),
        "campaign_end": detect_switch(orders, cfg["campaign_thr"], cfg["test"],
                                      cs + dt.timedelta(hours=1), cs + dt.timedelta(days=10), False),
    }
    if cfg["test_start"] is not None and cfg["test_start"] == ce:
        det["test_start"] = det["campaign_end"]
    else:
        det["test_start"] = detect_switch(orders, cfg["test"], cfg["normal"], ce + dt.timedelta(hours=1),
                                          cfg["test_end"] or (max_ts + dt.timedelta(seconds=1)), True)
    return det


# ── Build ────────────────────────────────────────────────────────────────────
def build() -> dict:
    now = dt.datetime.now(UTC)
    # Query window: wide enough for every market's trend and baseline search.
    d0_all = min(min(now.astimezone(ZoneInfo(c["tz"])).date() - dt.timedelta(days=TREND_DAYS - 1),
                     c["campaign_start"].astimezone(ZoneInfo(c["tz"])).date()
                     - dt.timedelta(days=BASELINE_LOOKBACK_DAYS)) for c in MARKET_CFG.values())
    t0 = dt.datetime.combine(d0_all - dt.timedelta(days=1), dt.time(), UTC)
    mk = bigquery.ArrayQueryParameter("mk", "STRING", MARKETS)

    orders: dict[str, list] = defaultdict(list)
    for r in _rows(_order_sql(), [bigquery.ScalarQueryParameter("t0", "TIMESTAMP", t0), mk]):
        orders[r["market"]].append((r["order_date"].astimezone(UTC), float(r["goods"]), float(r["ship"])))
    for m in MARKETS:
        if not orders[m]:
            raise RuntimeError(f"no {m} orders returned")
        orders[m].sort()

    # Last-year counts (same weekday, −364 d) for the seasonality fallback.
    ly_off = dt.timedelta(days=LY_OFFSET_DAYS)
    ly = defaultdict(dict)
    for r in _rows(_ly_sql(), [bigquery.ScalarQueryParameter("l0", "TIMESTAMP", t0 - ly_off),
                               bigquery.ScalarQueryParameter("l1", "TIMESTAMP", now - ly_off + dt.timedelta(days=1)),
                               mk]):
        ly[r["market"]][r["d"]] = int(r["n"])

    # Funnel sessions per market (one query) — conversion denominator.
    sess = defaultdict(dict)
    for r in bq_source._rows(f"""
        SELECT market_level_1_kv AS m, Date AS date, SUM(Sessions) s FROM `{bq_source.BQ_TABLE}`
        WHERE Date BETWEEN @a AND @b AND market_level_1_kv IN UNNEST(@mk)
          AND Sessions IS NOT NULL GROUP BY 1, 2""",
            [bigquery.ScalarQueryParameter("a", "DATE", d0_all),
             bigquery.ScalarQueryParameter("b", "DATE", now.date()), mk]):
        sess[r["m"]][r["date"]] = int(r["s"] or 0)

    out = {m: _build_market(m, MARKET_CFG[m], orders, ly.get(m, {}), sess[m], now) for m in MARKETS}

    summary = []
    for m in MARKETS:
        p = out[m]
        b, t = p["periods"]["baseline"], p["periods"]["test"]
        has_t = t["n"] > 0
        summary.append({
            "market": m, "name": p["name"], "currency": p["currency"], "unit": p["unit"],
            "state": p["state"], "test_started": p["test_started"],
            "test_threshold": MARKET_CFG[m]["test"], "normal_threshold": MARKET_CFG[m]["normal"],
            "test_start_local": p["regimes"]["test"]["start_local"],
            "test_days": p["test_days"], "too_early": p["too_early"],
            "orders_day": t["orders_day"] if has_t else None,
            "d_orders": _rel(t["orders_day"], b["orders_day"]) if has_t else None,
            "aov": t["aov"] if has_t else None,
            "d_aov": _rel(t["aov"], b["aov"]) if has_t else None,
            "gp2_day_sek": t["gp2_day_sek"], "d_gp2": _rel(t["gp2_day_sek"], b["gp2_day_sek"]),
            "control_method": p["control"]["method"], "adj_orders": p["control"]["adj_orders_change"],
        })

    return {
        "source": "norce (nightly sync) + funnel-export",
        "generated_at": now.isoformat(timespec="seconds"),
        "announced": ANNOUNCED.isoformat(),
        "markets_order": MARKETS,
        "summary": summary,
        "markets": out,
    }


def _treated_between(m, a, b):
    """True if market m is in its threshold test at any time in [a, b)."""
    c = MARKET_CFG[m]
    ts = c["test_start"]
    return ts is not None and ts < b and (c["test_end"] is None or c["test_end"] > a)


def _build_market(m, cfg, orders_all, lym, sess, now):
    TZ = ZoneInfo(cfg["tz"])

    def loc(ts):
        return ts.astimezone(TZ).date()

    def lbl(ts):
        return ts.astimezone(TZ).strftime("%Y-%m-%d %H:%M") if ts else None

    rows = orders_all[m]
    max_ts = max(ts for ts, _, _ in rows)
    today = now.astimezone(TZ).date()
    last_day = loc(max_ts)

    det = _detect_market(cfg, rows, max_ts)
    CS, CE = cfg["campaign_start"], cfg["campaign_end"]
    TS = cfg["test_start"]
    test_source = "pinned"
    if TS is None:
        if det["test_start"]:
            TS, test_source = det["test_start"]["_ts"], "detected"
        else:
            # Not visible in the data yet: anchor just after the data so the
            # test period is empty and the page shows "waiting for data".
            TS, test_source = max(max_ts + dt.timedelta(seconds=1), CE), "pending"
    test_started = max_ts >= TS
    TE = cfg["test_end"]
    t_end = TE or max_ts
    camp_day, ce_day, test_day = loc(CS), loc(CE), loc(TS)
    trend_start = today - dt.timedelta(days=TREND_DAYS - 1)
    d0 = min(trend_start, camp_day - dt.timedelta(days=BASELINE_LOOKBACK_DAYS))
    rows = [x for x in rows if loc(x[0]) >= d0]

    warn = []
    for k, pinned in (("campaign_start", CS), ("campaign_end", CE), ("test_start", cfg["test_start"])):
        d_ = det.get(k)
        if pinned is not None and d_ and abs((d_["_ts"] - pinned).total_seconds()) > DETECT_WARN_HOURS * 3600:
            warn.append(f"{k.replace('_', ' ')}: pinned {lbl(pinned)} but the orders suggest "
                        f"{lbl(d_['_ts'])} (local time)")

    # ── Day classification → baseline days ───────────────────────────────
    small = defaultdict(lambda: [0, 0])          # day -> [orders < test threshold, of which free]
    for ts, g, sh in rows:
        if g < cfg["test"]:
            small[loc(ts)][0] += 1
            small[loc(ts)][1] += _free(sh)

    def free_small(d):
        n, f = small.get(d, (0, 0))
        return f / n if n >= 5 else None

    ce_local_hour = CE.astimezone(TZ).hour

    def day_class(d):
        if test_started and d >= test_day and (TE is None or d <= loc(TE)):
            return "test"
        if camp_day <= d < ce_day:
            return "campaign"
        if d == ce_day:                    # campaign-end switch day: label by the larger part
            return "campaign" if ce_local_hour >= 12 else "normal"
        if d > ce_day:                     # back on the normal policy (interim, or test not started)
            return "normal"
        fs = free_small(d)
        if fs is None:
            return "unknown"
        return "normal" if fs <= NORMAL_MAX_FREE_SMALL else "promo" if fs >= PROMO_MIN_FREE_SMALL else "mixed"

    candidates = [d for d in reversed(_days(d0, camp_day - dt.timedelta(days=1)))
                  if day_class(d) == "normal" and d not in BASELINE_EXCLUDE]
    base_days = sorted(candidates[:BASELINE_DAYS])
    base_set = set(base_days)

    has_interim = CE < TS
    windows = {"campaign": (CS, CE),
               "interim": (CE, min(TS, max(max_ts, CE))),
               "test": (TS, t_end)}

    def regime(ts):
        if loc(ts) in base_set:
            return "baseline"
        if CS <= ts < CE:
            return "campaign"
        if has_interim and CE <= ts < TS:
            return "interim"
        if TS <= ts <= t_end:
            return "test"
        return None

    per = defaultdict(list)
    hour_share = [0.0] * 24
    for ts, g, sh in rows:
        k = regime(ts)
        if k:
            per[k].append((ts, g, sh))
        if k == "baseline":
            hour_share[ts.astimezone(TZ).hour] += 1
    nb_ = len(per["baseline"])
    hour_share = [h / nb_ for h in hour_share] if nb_ else [1 / 24] * 24

    def eq_days(s, e):
        """Σ over [s,e) of the baseline hour-of-day order share — 1.0 per full day.
        Normalises a partial day (e.g. the test's first hours) to 'days' of demand."""
        tot, cur = 0.0, s
        while cur < e:
            nxt = min(e, (cur + dt.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0))
            tot += hour_share[cur.astimezone(TZ).hour] * (nxt - cur).total_seconds() / 3600
            cur = nxt
        return tot

    # ── Funnel export: GP2 + net revenue (SEK), daily ────────────────────
    gp2_cutoff = today - dt.timedelta(days=FUNNEL_LAG_DAYS)
    lad = {dt.date.fromisoformat(r["d"]): r
           for r in bq_source.filtered_daily({"market": m}, d0, gp2_cutoff)
           if r["rev"] > 0}

    # Whole days per regime; switch days are mixed and belong to neither.
    full_days = {
        "baseline": base_days,
        "campaign": _days(camp_day + dt.timedelta(days=1), ce_day - dt.timedelta(days=1)),
        "interim": _days(ce_day + dt.timedelta(days=1), test_day - dt.timedelta(days=1)) if has_interim else [],
        "test": (_days(test_day + dt.timedelta(days=1), min(last_day - dt.timedelta(days=1), loc(t_end)))
                 if test_started else []),
    }

    # ── Per-period KPIs ──────────────────────────────────────────────────
    periods = {}
    for k in PERIODS:
        if k == "interim" and not has_interim:
            continue
        prow = per[k]
        n, aov, var = _stats([g for _, g, _ in prow])
        if k == "baseline":
            ed = float(len(base_days))
        else:
            ed = eq_days(*windows[k]) if windows[k][1] > windows[k][0] else 0.0
        goods = sum(g for _, g, _ in prow)
        fd = full_days[k]
        lad_days = [d for d in fd if d in lad]
        gp2 = sum(float(lad[d]["gp2"] or 0) for d in lad_days)
        net = sum(float(lad[d]["net_rev"] or 0) for d in lad_days)
        cset = {d for d in fd if sess.get(d, 0) > 500}      # whole days with GA4 sessions
        c_orders = sum(1 for ts, _, _ in prow if loc(ts) in cset)
        c_sess = sum(sess[d] for d in cset)
        if k == "baseline":
            s_lbl, e_lbl = (base_days[0].isoformat(), base_days[-1].isoformat()) if base_days else ("", "")
        else:
            s_lbl, e_lbl = lbl(windows[k][0]), lbl(windows[k][1])
        fee_ref = cfg["fee_test"] if k == "test" else cfg["fee_normal"]
        periods[k] = {
            "start_local": s_lbl, "end_local": e_lbl,
            "n": n, "eq_days": round(ed, 2),
            "orders_day": round(n / ed, 2) if ed else None,
            "goods_day": round(goods / ed, 1) if ed else None,
            "aov": round(aov, 2), "aov_var": round(var, 2),
            "free_share": round(sum(1 for _, _, sh in prow if _free(sh)) / n, 4) if n else None,
            "ship_per_order": round(sum(sh for _, _, sh in prow) / n, 3) if n else None,
            "paid_base_share": round(sum(1 for _, _, sh in prow if abs(sh - fee_ref) < 0.01) / n, 4) if n else None,
            "gp2_days": len(lad_days),
            "gp2_day_sek": round(gp2 / len(lad_days)) if lad_days else None,
            "gp2_margin": round(gp2 / net, 4) if lad_days and net else None,
            "ship_cost_order_sek": None,     # no shipping-cost source on this stack
            "conv_days": len(cset), "sessions": c_sess,
            "conv": round(c_orders / c_sess, 5) if c_sess else None,
        }

    # ── Welch 95 % CI on AOV (test − baseline) ───────────────────────────
    b, t = periods["baseline"], periods["test"]
    welch = None
    if b["n"] > 1 and t["n"] > 1:
        diff = t["aov"] - b["aov"]
        se = math.sqrt(b["aov_var"] / b["n"] + t["aov_var"] / t["n"])
        welch = {"diff": round(diff, 2), "lo": round(diff - 1.96 * se, 2),
                 "hi": round(diff + 1.96 * se, 2), "se": round(se, 2),
                 "significant": abs(diff) > 1.96 * se}

    # ── Histogram (share of orders per bin) & bands ──────────────────────
    BANDS, HB, HM = cfg["bands"], cfg["hist_bin"], cfg["hist_max"]
    nbins = HM // HB + 1
    hist, bands = {}, {}
    for k in periods:
        cnt = [0] * nbins
        bc = [[0, 0.0, 0] for _ in BANDS]            # n, goods, free
        for _, g, sh in per[k]:
            cnt[min(nbins - 1, max(0, int(g // HB)))] += 1
            for j, (lo_, hi_, _) in enumerate(BANDS):
                if g >= lo_ and (hi_ is None or g < hi_):
                    bc[j][0] += 1
                    bc[j][1] += g
                    bc[j][2] += _free(sh)
                    break
        n = len(per[k]) or 1
        hist[k] = [round(c / n, 5) for c in cnt]
        bands[k] = [{"share": round(c / n, 4), "n": c, "aov": round(v / c, 2) if c else None,
                     "free_share": round(f / c, 3) if c else None} for c, v, f in bc]
    hist_labels = [str(i * HB) for i in range(nbins - 1)] + [f"{HM}+"]
    # the bands just below and just above the test threshold get highlighted
    hl = [j for j, (lo_, hi_, _) in enumerate(BANDS) if hi_ == cfg["test"] or lo_ == cfg["test"]]

    # ── Daily trend (local days) + controls ──────────────────────────────
    daily = defaultdict(lambda: {"n": 0, "goods": 0.0, "free": 0})
    for ts, g, sh in rows:
        x = daily[loc(ts)]
        x["n"] += 1
        x["goods"] += g
        x["free"] += _free(sh)

    # Nordic control = the other markets, bucketed into THIS market's local days.
    others = [o for o in MARKETS if o != m]
    ocnt = {o: defaultdict(int) for o in others}
    ofree = defaultdict(int)
    for o in others:
        for ts, g, sh in orders_all[o]:
            d = loc(ts)
            if d >= d0:
                ocnt[o][d] += 1
                ofree[d] += _free(sh)

    def csum(mks, d):
        return sum(ocnt[o].get(d, 0) for o in mks)

    def day_utc(d):
        s = dt.datetime.combine(d, dt.time(), TZ).astimezone(UTC)
        return s, s + dt.timedelta(days=1)

    def ctrl_clean(d):
        a, e = day_utc(d)
        return not any(_treated_between(o, a, e) for o in others)

    ctrl_base = [csum(others, d) for d in base_days]
    ctrl_base_avg = sum(ctrl_base) / len(ctrl_base) if ctrl_base else None
    base_avg = b["orders_day"]

    # Last year, same weekday.
    off = dt.timedelta(days=LY_OFFSET_DAYS)
    ly_base = [lym[d - off] for d in base_days if (d - off) in lym]
    ly_base_avg = (sum(ly_base) / len(ly_base)
                   if ly_base and len(ly_base) >= 0.8 * len(base_days) else None)

    # Control choice: Nordic markets clean through the whole test window, else
    # last year, else none.
    clean = ([o for o in others if not _treated_between(o, TS, t_end + dt.timedelta(seconds=1))]
             if test_started else [])
    tfd = full_days["test"]
    x_ratio = (sum(daily[d]["n"] for d in tfd) / len(tfd) / base_avg) if tfd and base_avg else None
    method, adj = "none", None
    if not test_started:
        method = "pending"
        ctrl_note = "The test has not started in the order data yet."
    elif clean:
        method = "nordic"
        cb = [csum(clean, d) for d in base_days]
        if len(tfd) >= MIN_ADJ_DAYS and sum(cb) and x_ratio is not None:
            c_ratio = (sum(csum(clean, d) for d in tfd) / len(tfd)) / (sum(cb) / len(cb))
            adj = round(x_ratio / c_ratio - 1, 4) if c_ratio else None
        ctrl_note = (f"{'+'.join(clean)} stayed on their normal policy through {m}'s whole test window, "
                     f"so they are the control.")
    elif ly_base_avg:
        method = "last_year"
        lyt = [lym.get(d - off) for d in tfd]
        if len(tfd) >= MIN_ADJ_DAYS and all(v is not None for v in lyt) and x_ratio is not None:
            l_ratio = (sum(lyt) / len(lyt)) / ly_base_avg
            adj = round(x_ratio / l_ratio - 1, 4) if l_ratio else None
        ctrl_note = (f"Every other Nordic market is in its own threshold test during {m}'s test window, so there "
                     f"is no clean same-period control. Fallback: {m} last year, same weekday (date − "
                     f"{LY_OFFSET_DAYS} days), indexed to the same baseline days a year earlier. Last year had its "
                     f"own promo calendar, so read it as a seasonality guide, not a counterfactual.")
    else:
        ctrl_note = ("No clean Nordic control and no last-year data for the baseline days — the trend is shown "
                     "without a control.")

    series = []
    for d in _days(trend_start, last_day):
        x = daily.get(d, {"n": 0, "goods": 0.0, "free": 0})
        cn = csum(others, d)
        fs = free_small(d)
        lyv = lym.get(d - off)
        series.append({
            "iso": d.isoformat(), "d": f"{d.day}/{d.month}", "cls": day_class(d),
            "baseline": d in base_set, "switch": d in (camp_day, ce_day, test_day),
            "partial": d == last_day,
            "orders": x["n"], "aov": round(x["goods"] / x["n"], 2) if x["n"] else None,
            "free_share": round(x["free"] / x["n"], 3) if x["n"] else None,
            "free_small": round(fs, 3) if fs is not None else None,
            "idx": round(x["n"] / base_avg * 100, 1) if base_avg else None,
            "ctrl_orders": cn,
            "ctrl_idx": round(cn / ctrl_base_avg * 100, 1) if ctrl_base_avg and ctrl_clean(d) else None,
            "ctrl_free_share": round(ofree.get(d, 0) / cn, 3) if cn else None,
            "ly_orders": lyv,
            "ly_idx": round(lyv / ly_base_avg * 100, 1) if ly_base_avg and lyv is not None else None,
            "gp2_sek": round(float(lad[d]["gp2"])) if d in lad else None,
            "sessions": sess.get(d),
        })

    det_out = {k: ({kk: vv for kk, vv in v.items() if not kk.startswith("_")} if v else None)
               for k, v in det.items()}
    test_days = t["eq_days"]
    return {
        "market": m, "name": cfg["name"], "currency": cfg["currency"], "unit": cfg["unit"], "tz": cfg["tz"],
        "source": "norce (nightly sync) + funnel-export",
        "generated_at": now.isoformat(timespec="seconds"),
        "max_order_ts": max_ts.isoformat(),
        "max_order_local": lbl(max_ts),
        "state": "test" if test_started else "waiting",
        "test_started": test_started, "test_start_source": test_source,
        "regimes": {
            "normal": {"threshold": cfg["normal"], "fee": cfg["fee_normal"], "fee_label": cfg["fee_label"]},
            "campaign": {"threshold": cfg["campaign_thr"], "start_local": lbl(CS), "end_local": lbl(CE)},
            "interim": {"start_local": lbl(CE), "end_local": lbl(TS)} if has_interim else None,
            "test": {"threshold": cfg["test"], "fee": cfg["fee_test"],
                     "start_local": lbl(TS) if test_source != "pending" else None,
                     "end_local": lbl(TE), "announced": cfg["test_announced"].isoformat()},
        },
        "detection": det_out, "detection_warnings": warn,
        "baseline": {"days": [d.isoformat() for d in base_days], "n_days": len(base_days),
                     "target_days": BASELINE_DAYS, "max_free_small": NORMAL_MAX_FREE_SMALL},
        "test_days": test_days, "test_full_days": len(tfd),
        "min_test_days": MIN_TEST_DAYS, "too_early": test_days < MIN_TEST_DAYS,
        "periods": periods, "welch_aov": welch,
        "hist": {"bin": HB, "labels": hist_labels, **hist},
        "bands": {"labels": [b_[2] for b_ in BANDS], "hl": hl, **bands},
        "daily": series,
        "control": {"markets": others, "clean_markets": clean, "method": method, "note": ctrl_note,
                    "baseline_avg_orders": round(ctrl_base_avg, 1) if ctrl_base_avg else None,
                    "ly_offset_days": LY_OFFSET_DAYS,
                    "ly_baseline_avg_orders": round(ly_base_avg, 1) if ly_base_avg else None,
                    "adj_orders_change": adj, "adj_full_days": len(tfd)},
        "notes": {
            "status_filter": f"status 4/5 + status 2 (unprocessed) younger than {PENDING_GRACE_DAYS} days",
            "gp2": (f"GP2 from the Funnel export (SEK, KV market {m}, whole days, only days ≥ {FUNNEL_LAG_DAYS} "
                    f"days old); the switch days are excluded."),
            "conv": (f"Norce orders ÷ Funnel {m} sessions on whole days. Sessions carry a large unattributed-market "
                     f"share, so the level is indicative — compare periods, not absolutes."),
            "freshness": "Norce orders come from the nightly norce-sync, so the latest hours are missing until the next run.",
        },
    }


def get_payload() -> dict:
    from funnel_client import _cached
    return _cached(CACHE_KEY, build, ttl=CACHE_TTL)


def get_market(market: str) -> dict:
    """One market's slice of the cached payload (KeyError for an unknown market)."""
    p = get_payload()
    return p["markets"][market.upper()]


if __name__ == "__main__":
    import json
    import sys
    p = build()
    sel = [a.upper() for a in sys.argv[1:]] or MARKETS
    for m in sel:
        q = p["markets"][m]
        print(f"\n════ {m} ═══ data through {q['max_order_local']} local · state {q['state']} "
              f"({q['test_start_source']}) · test start {q['regimes']['test']['start_local']} local")
        for k, v in q["detection"].items():
            print(f"  detect {k:15s}", json.dumps(v, ensure_ascii=False) if v else "— not visible")
        if q["detection_warnings"]:
            print("  WARN", q["detection_warnings"])
        print(f"  baseline {q['baseline']['n_days']} days {q['baseline']['days'][:1]}…{q['baseline']['days'][-1:]}")
        for k, x in q["periods"].items():
            print(f"  {k:9s} n={x['n']:6d} eqd={x['eq_days']:6.2f} o/d={x['orders_day']} aov={x['aov']} "
                  f"free={x['free_share']} fee/o={x['ship_per_order']} base_fee={x['paid_base_share']} "
                  f"gp2/d={x['gp2_day_sek']} conv={x['conv']}")
        print("  welch", q["welch_aov"], "| control", {k: q["control"][k] for k in
              ("method", "clean_markets", "adj_orders_change", "ly_baseline_avg_orders", "baseline_avg_orders")})
        for k in q["periods"]:
            print("  band", k, [(lbl, x["share"], x["free_share"]) for lbl, x in zip(q["bands"]["labels"], q["bands"][k])])
        print("  daily tail", [(x["iso"], x["cls"], x["orders"], x["ctrl_idx"], x["ly_idx"]) for x in q["daily"][-7:]])
    print("\nsummary", json.dumps(p["summary"], indent=1, ensure_ascii=False))
    print("payload bytes", len(json.dumps(p, default=str)))
