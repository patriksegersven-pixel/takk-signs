#!/usr/bin/env python3
"""
Finland free-shipping threshold test — payload for /api/fi-shipping-test.

Babyshop FI (EUR, prices incl. 25.5 % VAT) normally charges 6.95 € for Posti
pickup and ships free above 129 €. The test lowers the free-shipping threshold
to 69 € (6.95 € still charged below). This module compares the TEST regime
against a pre-campaign BASELINE, with the Nordic "free above 25 €" campaign
that ran in between shown as context only.

OLD-STACK PORT of the babyshop-dashboard-next page (same regimes, baseline
logic and payload shape). It reads only this project's own data plus the Funnel
export the service already reads — never the claude-private warehouse.

Sources:
  • `project-a7ade44e-e7e3-4871-a83.norce.orders` / `.order_items` (norce_sync.py,
    NIGHTLY — data runs through the last sync, not live). Market = the app_key
    suffix, so FI = babyshop-fi + lekmer-fi (same as the new stack's market FI).
    Goods value incl. VAT = Σ LineAmount·(1+VatRate/100) over non-shipping lines;
    the shipping fee is the PartNo 1000014 line, grossed up the same way.
  • Funnel export via bq_source.filtered_daily({"market": "FI"}) — GP2 and net
    revenue per day (SEK), with the KV dedup/netting already applied there. A
    Funnel day keeps filling for ~2 days, so only days ≤ today − FUNNEL_LAG_DAYS
    count.
  • Funnel export `Sessions` for market_level_1_kv = FI (the same un-deduplicated
    sessions sum refresh_customer_insights.py uses) — conversion denominator.
  • Shipping cost per order is not in either source here → null (n/a).

Order filter: StatusId 4/5 (the new stack's is_revenue_status), PLUS status 2
("new", not yet processed) for orders younger than PENDING_GRACE_DAYS. Every
fresh order sits in status 2 until Norce processes it, so dropping status 2
outright would empty the most recent hours of the test; older status-2 orders
(~1 % of history) are stuck/abandoned and stay excluded. Status 6 is excluded.
"""
from __future__ import annotations

import datetime as dt
import math
import os
from collections import defaultdict
from zoneinfo import ZoneInfo

from google.cloud import bigquery

import bq_source

CACHE_KEY = "v1__fi-shipping-test"
CACHE_TTL = 1800

NORCE_PROJECT = os.environ.get("NORCE_BQ_PROJECT", "project-a7ade44e-e7e3-4871-a83")
NORCE_DATASET = os.environ.get("NORCE_BQ_DATASET", "norce")
NORCE = f"`{NORCE_PROJECT}.{NORCE_DATASET}`"
NORCE_LOCATION = os.environ.get("NORCE_BQ_LOCATION", "EU")
SHIPPING_PART_NO = "1000014"
REVENUE_STATUSES = (4, 5)        # = is_revenue_status on the new stack
MARKET_SQL = "UPPER(REGEXP_EXTRACT(app_key, r'-([a-z]+)$'))"   # babyshop-fi / lekmer-fi -> FI
FUNNEL_LAG_DAYS = 2              # Funnel days keep growing ~2 days; GP2 only on older days
HEL = ZoneInfo("Europe/Helsinki")
UTC = dt.timezone.utc

# ── Regime boundaries (UTC) — edit here ─────────────────────────────────────
# Detected from FI orders (goods value vs the shipping line):
#   Campaign start: last 6.95 € order with goods < 69 € at 2026-09-25 11:18:21,
#     first free order with goods 25–69 € at 13:26:43 (no small orders between).
#   Test start:     last free order with goods 25–69 € at 2026-09-29 07:03:16,
#     first 6.95 € order with goods < 69 € at 07:26:39.
CAMPAIGN_START = dt.datetime(2026, 9, 25, 11, 19, tzinfo=UTC)   # free > 25 €
TEST_START     = dt.datetime(2026, 9, 29, 7, 15, tzinfo=UTC)    # free > 69 €
TEST_END       = None            # open-ended: runs until the latest order seen

# ── Baseline ─────────────────────────────────────────────────────────────────
# The "normal 129 €" regime is regularly interrupted by other free-shipping
# promos (FI/SE/NO/DK in sync: free shipping until ~11 Aug and again 4–15 Sep
# 2026). A plain "last 28 days" window would mix those in, so the baseline is
# the last BASELINE_DAYS Helsinki days before the campaign day on which the
# normal policy verifiably applied: ≤ NORMAL_MAX_FREE_SMALL of orders with
# goods < 69 € shipped free (normal days sit at 0 %, promo days at 90 %+).
BASELINE_DAYS  = 28
BASELINE_LOOKBACK_DAYS = 120     # how far back to search for normal days
NORMAL_MAX_FREE_SMALL  = 0.10
PROMO_MIN_FREE_SMALL   = 0.50    # days above this are shaded as promo on the trend
BASELINE_EXCLUDE: set[dt.date] = set()   # manual exclusions, e.g. {dt.date(2026, 9, 1)}

TREND_DAYS     = 60              # daily chart window
MIN_TEST_DAYS  = 14              # below this the page says "too early to call"
PENDING_GRACE_DAYS = 3

THRESH_TEST, THRESH_NORMAL, THRESH_CAMPAIGN = 69, 129, 25
FEE = 6.95
BANDS = [(0, 25, "< 25 €"), (25, 55, "25–55 €"), (55, 69, "55–69 €"),
         (69, 85, "69–85 €"), (85, 129, "85–129 €"), (129, None, "129 € +")]
HIST_BIN, HIST_MAX = 5, 200      # 5 € bins, last bin = 200 € +
CONTROL_MARKETS = ["SE", "DK", "NO"]
PERIODS = ("baseline", "campaign", "test")


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


def _order_sql(markets_filter: str) -> str:
    # `orders` is partitioned on OrderDate, so the @t0 bound prunes; order_items
    # has no date and is reached through the order ids (clustered on OrderId).
    return f"""
    WITH o AS (
      SELECT Id, OrderDate AS order_date, {MARKET_SQL} AS market
      FROM {NORCE}.orders
      WHERE OrderDate >= @t0 AND {markets_filter}
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


def _params(d0, d1, t0):
    return [bigquery.ScalarQueryParameter("t0", "TIMESTAMP", t0)]


def _stats(xs):
    n = len(xs)
    if n == 0:
        return 0, 0.0, 0.0
    m = sum(xs) / n
    v = sum((x - m) ** 2 for x in xs) / (n - 1) if n > 1 else 0.0
    return n, m, v


def _days(a, b):
    return [a + dt.timedelta(days=i) for i in range((b - a).days + 1)] if b >= a else []


def _hel(ts):
    return ts.astimezone(HEL).date()


def build() -> dict:
    today_hel = dt.datetime.now(HEL).date()
    camp_day, test_day = _hel(CAMPAIGN_START), _hel(TEST_START)
    trend_start = today_hel - dt.timedelta(days=TREND_DAYS - 1)
    d0 = min(trend_start, camp_day - dt.timedelta(days=BASELINE_LOOKBACK_DAYS))
    t0 = dt.datetime.combine(d0, dt.time(), HEL).astimezone(UTC)
    date_p = [bigquery.ScalarQueryParameter("a", "DATE", d0),
              bigquery.ScalarQueryParameter("b", "DATE", today_hel)]

    # ── FI orders (order level) ───────────────────────────────────────────
    fi = [(r["order_date"].astimezone(UTC), float(r["goods"]), float(r["ship"]))
          for r in _rows(_order_sql(f'{MARKET_SQL} = "FI"'), _params(d0, today_hel, t0))]
    if not fi:
        raise RuntimeError("no FI orders returned")
    max_ts = max(ts for ts, _, _ in fi)
    t_end = TEST_END or max_ts
    last_day = _hel(max_ts)

    # ── Day classification → baseline days ───────────────────────────────
    small = defaultdict(lambda: [0, 0])          # day -> [orders < 69 €, of which free]
    for ts, g, sh in fi:
        if g < THRESH_TEST:
            small[_hel(ts)][0] += 1
            small[_hel(ts)][1] += sh < 0.005

    def free_small(d):
        n, f = small.get(d, (0, 0))
        return f / n if n >= 5 else None

    def day_class(d):
        if d >= test_day:
            return "test"
        if d >= camp_day:
            return "campaign"
        fs = free_small(d)
        if fs is None:
            return "unknown"
        return "normal" if fs <= NORMAL_MAX_FREE_SMALL else "promo" if fs >= PROMO_MIN_FREE_SMALL else "mixed"

    candidates = [d for d in reversed(_days(d0, camp_day - dt.timedelta(days=1)))
                  if day_class(d) == "normal" and d not in BASELINE_EXCLUDE]
    base_days = sorted(candidates[:BASELINE_DAYS])
    base_set = set(base_days)

    def regime(ts):
        if _hel(ts) in base_set:
            return "baseline"
        if CAMPAIGN_START <= ts < TEST_START:
            return "campaign"
        if TEST_START <= ts <= t_end:
            return "test"
        return None

    per = defaultdict(list)
    hour_share = [0.0] * 24
    for ts, g, sh in fi:
        k = regime(ts)
        if k:
            per[k].append((ts, g, sh))
        if k == "baseline":
            hour_share[ts.astimezone(HEL).hour] += 1
    nb_ = len(per["baseline"])
    hour_share = [h / nb_ for h in hour_share] if nb_ else [1 / 24] * 24

    def eq_days(s, e):
        """Σ over [s,e) of the baseline hour-of-day order share — 1.0 per full day.
        Normalises a partial day (e.g. the test's first hours) to 'days' of demand."""
        tot, cur = 0.0, s
        while cur < e:
            nxt = min(e, (cur + dt.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0))
            tot += hour_share[cur.astimezone(HEL).hour] * (nxt - cur).total_seconds() / 3600
            cur = nxt
        return tot

    # ── Funnel export: GP2 + net revenue (SEK) and sessions, FI, daily ────
    # GP2/net revenue come from bq_source.filtered_daily, i.e. the KV Overview's
    # own dedup + returns netting. Only days old enough to be complete count.
    gp2_cutoff = today_hel - dt.timedelta(days=FUNNEL_LAG_DAYS)
    lad = {dt.date.fromisoformat(r["d"]): r
           for r in bq_source.filtered_daily({"market": "FI"}, d0, gp2_cutoff)
           if r["rev"] > 0}
    sess = {r["date"]: int(r["s"] or 0) for r in bq_source._rows(f"""
        SELECT Date AS date, SUM(Sessions) s FROM `{bq_source.BQ_TABLE}`
        WHERE Date BETWEEN @a AND @b AND market_level_1_kv = "FI"
          AND Sessions IS NOT NULL GROUP BY 1""", date_p)}

    # Whole days per regime; the two switch days are mixed and belong to neither.
    full_days = {
        "baseline": base_days,
        "campaign": _days(camp_day + dt.timedelta(days=1), test_day - dt.timedelta(days=1)),
        "test": _days(test_day + dt.timedelta(days=1), min(last_day - dt.timedelta(days=1), _hel(t_end))),
    }
    windows = {"campaign": (CAMPAIGN_START, TEST_START), "test": (TEST_START, t_end)}

    # ── Per-period KPIs ──────────────────────────────────────────────────
    periods = {}
    for k in PERIODS:
        rows = per[k]
        n, aov, var = _stats([g for _, g, _ in rows])
        ed = float(len(base_days)) if k == "baseline" else eq_days(*windows[k])
        goods = sum(g for _, g, _ in rows)
        fd = full_days[k]
        lad_days = [d for d in fd if d in lad]
        gp2 = sum(float(lad[d]["gp2"] or 0) for d in lad_days)
        net = sum(float(lad[d]["net_rev"] or 0) for d in lad_days)
        cset = {d for d in fd if sess.get(d, 0) > 500}      # whole days with GA4 sessions
        c_orders = sum(1 for ts, _, _ in rows if _hel(ts) in cset)
        c_sess = sum(sess[d] for d in cset)
        if k == "baseline":
            s_lbl, e_lbl = (base_days[0].isoformat(), base_days[-1].isoformat()) if base_days else ("", "")
        else:
            s_lbl = windows[k][0].astimezone(HEL).strftime("%Y-%m-%d %H:%M")
            e_lbl = windows[k][1].astimezone(HEL).strftime("%Y-%m-%d %H:%M")
        periods[k] = {
            "start_hel": s_lbl, "end_hel": e_lbl,
            "n": n, "eq_days": round(ed, 2),
            "orders_day": round(n / ed, 2) if ed else None,
            "goods_day": round(goods / ed, 1) if ed else None,
            "aov": round(aov, 2), "aov_var": round(var, 2),
            "free_share": round(sum(1 for _, _, sh in rows if sh < 0.005) / n, 4) if n else None,
            "ship_per_order": round(sum(sh for _, _, sh in rows) / n, 3) if n else None,
            "paid_695_share": round(sum(1 for _, _, sh in rows if abs(sh - FEE) < 0.01) / n, 4) if n else None,
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

    # ── Histogram (share of orders per 5 € bin) & bands ──────────────────
    nbins = HIST_MAX // HIST_BIN + 1
    hist, bands = {}, {}
    for k in PERIODS:
        cnt = [0] * nbins
        bc = [[0, 0.0, 0] for _ in BANDS]            # n, goods, free
        for _, g, sh in per[k]:
            cnt[min(nbins - 1, int(g // HIST_BIN))] += 1
            for j, (lo_, hi_, _) in enumerate(BANDS):
                if g >= lo_ and (hi_ is None or g < hi_):
                    bc[j][0] += 1
                    bc[j][1] += g
                    bc[j][2] += sh < 0.005
                    break
        n = len(per[k]) or 1
        hist[k] = [round(c / n, 5) for c in cnt]
        bands[k] = [{"share": round(c / n, 4), "n": c, "aov": round(v / c, 2) if c else None,
                     "free_share": round(f / c, 3) if c else None} for c, v, f in bc]
    hist_labels = [str(i * HIST_BIN) for i in range(nbins - 1)] + [f"{HIST_MAX}+"]

    # ── Daily trend (Helsinki days) + control markets ────────────────────
    daily = defaultdict(lambda: {"n": 0, "goods": 0.0, "free": 0})
    for ts, g, sh in fi:
        x = daily[_hel(ts)]
        x["n"] += 1
        x["goods"] += g
        x["free"] += sh < 0.005

    ctrl = _rows(f"""
      WITH base AS ({_order_sql(f'{MARKET_SQL} IN UNNEST(@mk)')})
      SELECT DATE(order_date, "Europe/Helsinki") d, COUNT(*) n, COUNTIF(ship < 0.005) free
      FROM base GROUP BY 1""",
        _params(d0, today_hel, t0) + [bigquery.ArrayQueryParameter("mk", "STRING", CONTROL_MARKETS)])
    cday = {r["d"]: (int(r["n"]), int(r["free"])) for r in ctrl}
    ctrl_base = [cday[d][0] for d in base_days if d in cday]
    ctrl_base_avg = sum(ctrl_base) / len(ctrl_base) if ctrl_base else None
    fi_base_avg = b["orders_day"]

    series = []
    for d in _days(trend_start, last_day):
        x = daily.get(d, {"n": 0, "goods": 0.0, "free": 0})
        cn, cf = cday.get(d, (0, 0))
        fs = free_small(d)
        series.append({
            "iso": d.isoformat(), "d": f"{d.day}/{d.month}", "cls": day_class(d),
            "baseline": d in base_set, "switch": d in (camp_day, test_day),
            "partial": d == last_day,
            "orders": x["n"], "aov": round(x["goods"] / x["n"], 2) if x["n"] else None,
            "free_share": round(x["free"] / x["n"], 3) if x["n"] else None,
            "free_small": round(fs, 3) if fs is not None else None,
            "fi_idx": round(x["n"] / fi_base_avg * 100, 1) if fi_base_avg else None,
            "ctrl_orders": cn,
            "ctrl_idx": round(cn / ctrl_base_avg * 100, 1) if ctrl_base_avg else None,
            "ctrl_free_share": round(cf / cn, 3) if cn else None,
            "gp2_sek": round(float(lad[d]["gp2"])) if d in lad else None,
            "sessions": sess.get(d),
        })

    test_days = t["eq_days"]
    return {
        "source": "norce (nightly sync) + funnel-export",
        "generated_at": dt.datetime.now(UTC).isoformat(timespec="seconds"),
        "max_order_ts": max_ts.isoformat(),
        "max_order_hel": max_ts.astimezone(HEL).strftime("%Y-%m-%d %H:%M"),
        "regimes": {
            "normal": {"threshold": THRESH_NORMAL, "fee": FEE},
            "campaign": {"threshold": THRESH_CAMPAIGN,
                         "start_hel": CAMPAIGN_START.astimezone(HEL).strftime("%Y-%m-%d %H:%M"),
                         "end_hel": TEST_START.astimezone(HEL).strftime("%Y-%m-%d %H:%M")},
            "test": {"threshold": THRESH_TEST, "fee": FEE,
                     "start_hel": TEST_START.astimezone(HEL).strftime("%Y-%m-%d %H:%M"),
                     "end_hel": TEST_END.astimezone(HEL).strftime("%Y-%m-%d %H:%M") if TEST_END else None},
        },
        "baseline": {"days": [d.isoformat() for d in base_days], "n_days": len(base_days),
                     "target_days": BASELINE_DAYS, "max_free_small": NORMAL_MAX_FREE_SMALL},
        "test_days": test_days, "test_full_days": len(full_days["test"]),
        "min_test_days": MIN_TEST_DAYS, "too_early": test_days < MIN_TEST_DAYS,
        "periods": periods, "welch_aov": welch,
        "hist": {"bin": HIST_BIN, "labels": hist_labels, **hist},
        "bands": {"labels": [b_[2] for b_ in BANDS], **bands},
        "daily": series,
        "control": {"markets": CONTROL_MARKETS,
                    "baseline_avg_orders": round(ctrl_base_avg, 1) if ctrl_base_avg else None},
        "notes": {
            "status_filter": f"status 4/5 + status 2 (unprocessed) younger than {PENDING_GRACE_DAYS} days",
            "gp2": f"GP2 from the Funnel export (SEK, KV market FI, whole days, only days ≥ {FUNNEL_LAG_DAYS} days old); the two switch days are excluded.",
            "conv": "Norce orders ÷ Funnel FI sessions on whole days. Sessions carry a large unattributed-market share, so the level is indicative — compare periods, not absolutes.",
            "freshness": "Norce orders come from the nightly norce-sync, so the latest hours are missing until the next run.",
        },
    }


def get_payload() -> dict:
    from funnel_client import _cached
    return _cached(CACHE_KEY, build, ttl=CACHE_TTL)


if __name__ == "__main__":
    import json
    p = build()
    print(json.dumps({k: p[k] for k in ("max_order_hel", "baseline", "test_days",
                                         "test_full_days", "too_early", "periods", "welch_aov",
                                         "control")}, indent=1, default=str))
    for k in PERIODS:
        print(k, [(lbl, x["share"], x["aov"], x["free_share"]) for lbl, x in zip(p["bands"]["labels"], p["bands"][k])])
    print("daily tail", [(x["iso"], x["cls"], x["orders"], x["ctrl_idx"]) for x in p["daily"][-8:]])
