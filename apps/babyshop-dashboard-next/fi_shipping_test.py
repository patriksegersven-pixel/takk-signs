#!/usr/bin/env python3
"""
Finland free-shipping threshold test — payload for /api/fi-shipping-test.

Babyshop FI (EUR, prices incl. 25.5 % VAT) normally charges 6.95 € for Posti
pickup and ships free above 129 €. The test lowers the free-shipping threshold
to 69 € (6.95 € still charged below). This module compares the TEST regime
against a pre-campaign BASELINE, with the Nordic "free above 25 €" campaign
that ran in between shown as context only.

Sources (all claude-private-499703):
  • babyshop_staging.stg_norce__orders / stg_norce__order_items — order-level,
    near-live. Goods value incl. VAT = Σ line_amount·(1+vat_rate/100) over
    non-shipping lines; the shipping fee is the is_shipping line (part 1000014).
  • babyshop_marts.agg_daily_profit_ladder — GP2 (SEK, daily grain, ~1-day lag).
  • babyshop_marts.agg_daily_kpis_by_channel — GA4 sessions (daily grain).

Order filter: is_revenue_status, PLUS status 2 ("new", not yet processed) for
orders younger than PENDING_GRACE_DAYS. Every order placed today sits in
status 2 until Norce processes it, so dropping status 2 outright would empty
the most recent hours of the test; older status-2 orders (~1 % of history) are
stuck/abandoned and stay excluded.
"""
from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from zoneinfo import ZoneInfo

from google.cloud import bigquery

from marts_source import MART_PROJECT, _rows

CACHE_KEY = "v1__fi-shipping-test"
CACHE_TTL = 1800

STAGING = f"`{MART_PROJECT}.babyshop_staging`"
MARTS = f"`{MART_PROJECT}.babyshop_marts`"
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


def _order_sql(markets_filter: str) -> str:
    return f"""
    WITH o AS (
      SELECT order_link, order_date, market
      FROM {STAGING}.stg_norce__orders
      WHERE date BETWEEN @d0 AND @d1 AND {markets_filter}
        AND order_date >= @t0
        AND (is_revenue_status OR (status_id = 2 AND order_date >=
             TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {PENDING_GRACE_DAYS} DAY)))
    ), i AS (
      SELECT order_link,
             SUM(IF(is_shipping, line_amount * (1 + vat_rate / 100), 0))     AS ship,
             SUM(IF(NOT is_shipping, line_amount * (1 + vat_rate / 100), 0)) AS goods
      FROM {STAGING}.stg_norce__order_items
      WHERE date BETWEEN @d0i AND @d1
      GROUP BY 1
    )
    SELECT o.order_date, o.market, IFNULL(i.goods, 0) AS goods, IFNULL(i.ship, 0) AS ship
    FROM o LEFT JOIN i USING (order_link)"""


def _params(d0, d1, t0):
    # `date` pads a day either side: it is the source's partition-style column
    # and not guaranteed to be the Helsinki date of order_date.
    return [bigquery.ScalarQueryParameter("d0", "DATE", d0 - dt.timedelta(days=1)),
            bigquery.ScalarQueryParameter("d0i", "DATE", d0 - dt.timedelta(days=3)),
            bigquery.ScalarQueryParameter("d1", "DATE", d1 + dt.timedelta(days=1)),
            bigquery.ScalarQueryParameter("t0", "TIMESTAMP", t0)]


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
          for r in _rows(_order_sql('market = "FI"'), _params(d0, today_hel, t0))]
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

    # ── Ladder (GP2, SEK) and GA4 sessions, whole Helsinki days ──────────
    lad = {r["date"]: r for r in _rows(f"""
        SELECT date, SUM(revenue) rev, SUM(gp2) gp2, SUM(cost_shipping) ship_cost,
               SUM(cost_returns) ret, SUM(orders) orders
        FROM {MARTS}.agg_daily_profit_ladder
        WHERE date BETWEEN @a AND @b AND market = "FI" GROUP BY 1""", date_p)
        if (r["orders"] or 0) > 0}
    sess = {r["date"]: int(r["s"] or 0) for r in _rows(f"""
        SELECT date, SUM(ga4_sessions) s FROM {MARTS}.agg_daily_kpis_by_channel
        WHERE date BETWEEN @a AND @b AND market = "FI" GROUP BY 1""", date_p)}

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
        net = sum(float(lad[d]["rev"] or 0) - float(lad[d]["ret"] or 0) for d in lad_days)
        shc = sum(float(lad[d]["ship_cost"] or 0) for d in lad_days)
        lo = sum(int(lad[d]["orders"] or 0) for d in lad_days)
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
            "ship_cost_order_sek": round(shc / lo, 1) if lo else None,
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
      WITH base AS ({_order_sql('market IN UNNEST(@mk)')})
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
        "source": "norce-staging + bluebird-marts",
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
            "status_filter": f"is_revenue_status + status 2 (unprocessed) younger than {PENDING_GRACE_DAYS} days",
            "gp2": "GP2 from agg_daily_profit_ladder (SEK, whole Helsinki days, ~1-day lag); the two switch days are excluded.",
            "conv": "Norce orders ÷ GA4 FI sessions on whole days. GA4 has a large 'unknown' market bucket, so the level is indicative — compare periods, not absolutes.",
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
