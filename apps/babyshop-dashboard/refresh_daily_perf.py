#!/usr/bin/env python3
"""
refresh_daily_perf.py: the HISTORY half of the Daily performance tab.

WHAT THIS TAB IS, AND WHY IT IS NOT THE EXECUTIVE P&L
-----------------------------------------------------
Two tabs now carry a revenue number and they are on different BASES. Confusing
them is the single most expensive mistake a reader can make here, so the split
is stated on the page, in this module, and in the payload:

    Daily performance   ORDER DATE  — when the purchase took place. Norce.
    Executive P&L       POSTING DATE — when the invoice hit the ledger. BC.

Patrik's vocabulary is kept verbatim: "booked" means WHEN THE PURCHASE TOOK
PLACE, so the BC basis is called "posted" or "invoiced" and never "booked".

The two never reconcile day to day. On a calendar basis June 2026 reads
18.26 MSEK ordered against 21.69 MSEK posted, a 19% gap that is entirely
posting timing. Matched order for order the same two agree to 99.16%. They must
never be summed or differenced. `BASIS_WARNING` carries that to the page.

WHY A SNAPSHOT AT ALL, WHEN norce_today.py READS LIVE
-----------------------------------------------------
The live API answers "today and yesterday" in ~14 calls and 1-3 seconds. It
cannot answer "and how does that compare to the same weekday four weeks ago",
which would be 60+ days x 13 applications of paging, far outside the 3 req/min
token budget and far outside a page load.

So the boundary is:

    LIVE Norce API (norce_today.py)  -> today so far, and yesterday's headline
    THIS SNAPSHOT, from BigQuery     -> every comparison, every window, the
                                        60-day series, the market split

The boundary sits exactly at yesterday, and it sits there because the nightly
`norce-sync` runs at 01:00 Stockholm and therefore CLOSES the previous day: the
moment that run finishes, BigQuery's copy of yesterday is final. Today, by the
same token, is worthless in BigQuery (it holds only the sliver of orders placed
between midnight and 01:00), which is why today is live-only and has no
fallback.

Yesterday deliberately lands in BOTH halves. That overlap was verified, not
assumed, on 2026-09-16:

    live API    884 orders   788,327 SEK   1,868 units
    BigQuery    884 orders   788,327 SEK   1,868 units

They agree to the krona. The overlap is published as `integrity` so the page can
show the agreement, and so a future silent sync death — norce-sync died for four
weeks behind a green status once — shows up as a visible divergence instead of a
quietly stale comparison.

THE TIMEZONE TRAP THAT MAKES ALL OF THIS WRONG
-----------------------------------------------
`DATE(OrderDate)` is UTC. The trading day is Stockholm. Getting this wrong moves
two hours of every evening into the following day and produces a figure that is
~1% off the live API on any given day, which reads exactly like a sync hole and
is not one. That was diagnosed here for real: the same 2026-09-16 that ties to
the krona above reads 873 orders / 779,392 SEK under a UTC cut. Every date
expression in this module is `DATE(x, "Europe/Stockholm")`.

WHAT IS DELIBERATELY NOT HERE: MARGIN
--------------------------------------
There is no gross margin at day grain and there will not be one until Norce
carries cost. `order_items.CostUnit` is 0.00% populated across every month of
history (checked 2026-09-17, all 16 months), and the price-list fallback in
`sku_costs` is a CURRENT cost snapshot with no effective dating, so applying it
to a day in 2025 prices that day at today's cost. norce_today.py reached the
same conclusion and says so in MEASURE["no_margin"]. Daily is revenue, orders,
AOV and units. A margin here would be a guess wearing a decimal point.

THE TWO NORCE GOTCHAS, INHERITED FROM norce_today.py
-----------------------------------------------------
1. STATUS. Orders are born at StatusId 2 and move to 4 in about two days.
   Filtering to 4 zeroes a fresh day. Status 6 is the only state that never
   invoices (0.08% of orders). EXCLUDE 6, COUNT EVERYTHING ELSE.
2. FREIGHT. Revenue is merchandise PLUS the header FreightCost. The shipping
   pseudo-SKU (PartNo 1000014) is excluded from merchandise and the header
   figure added back; never both.

MERCHANDISE IS CARRIED SEPARATELY, AND THE PLAN CARD NEEDS IT
--------------------------------------------------------------
The rolling forecast's "Gross Sales" line is item lines EXCLUDING the shipping
pseudo-SKU (see refresh_exec_pl.py). Ordered revenue INCLUDES freight, which is
2.23% of intake. Comparing the two directly would flatter the pace by that much
every single month, so the month-to-date-against-plan card compares MERCHANDISE
against the plan and says so. Both figures are carried in `days[]`.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import statistics
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

from google.cloud import bigquery

# ── Where the data is ────────────────────────────────────────────────────────
DATA_PROJECT = os.environ.get("DAILY_PERF_DATA_PROJECT", "project-a7ade44e-e7e3-4871-a83")
NORCE_DATASET = os.environ.get("DAILY_PERF_NORCE_DATASET", "norce")
BQ_BILLING_PROJECT = os.environ.get("DAILY_PERF_BQ_BILLING_PROJECT",
                                    "project-a7ade44e-e7e3-4871-a83")
BQ_LOCATION = os.environ.get("DAILY_PERF_BQ_LOCATION", "EU")

WORKSPACE = os.environ.get("FUNNEL_WORKSPACE", "-Ln87GcdqU9CMJV6zMBY")
COLLECTION = "funnel_cache"
DOC_KEY = "daily-perf"
TTL = 30 * 24 * 3600
FIRESTORE_PROJECT = os.environ.get("FIRESTORE_PROJECT", "project-a7ade44e-e7e3-4871-a83")

TZ = os.environ.get("DAILY_PERF_TZ", "Europe/Stockholm")

# Shipping is booked as an ordinary order line; see the module docstring.
SHIPPING_PART_NO = "1000014"

# How much daily history to carry in the document. The binding requirement is
# the year-on-year comparison of the 60-day chart: 60 days of chart plus the
# 364-day alignment is 424, and the prior-year calendar month used to shape the
# plan pro-rating can start ~395 days back. 460 covers both with room, and costs
# about 25 KB of a 900 KB budget.
WINDOW_DAYS = int(os.environ.get("DAILY_PERF_WINDOW_DAYS", "460"))

# History before this is a partial ramp, not a quiet trading period: the first
# days of the Norce extract read 10-92 orders against a ~950 baseline. Anything
# earlier is excluded from every comparison rather than shown as a collapse.
USABLE_FROM = os.environ.get("DAILY_PERF_USABLE_FROM", "2025-07-01")

# Year-on-year is aligned by WEEKDAY, not by date: 364 days is 52 whole weeks,
# so a Wednesday always lands on a Wednesday. A 365-day shift would compare a
# Wednesday with a Tuesday and call the weekday effect a trend.
YOY_SHIFT_DAYS = 364

FORECAST_FILE = os.environ.get("DAILY_PERF_FORECAST_FILE", "forecast_2026_rolling.json")
# The line in that forecast this tab paces against, and the reason it is that
# line and not Net Sales: ordered intake is gross of returns.
PLAN_LINE = "Gross Sales"

DOC_BUDGET_BYTES = 900_000

MONTHS = ["jan", "feb", "mar", "apr", "may", "jun",
          "jul", "aug", "sep", "oct", "nov", "dec"]

# app_key -> reader-facing market name. Norce's application is shop x market,
# which is how the business is actually run, so it is the market dimension here.
# An unknown key falls through to the raw app_key rather than being dropped.
MARKET_NAMES = {
    "babyshop-se": "Babyshop SE", "babyshop-no": "Babyshop NO",
    "babyshop-fi": "Babyshop FI", "babyshop-dk": "Babyshop DK",
    "babyshop-eu": "Babyshop EU", "babyshop-uk": "Babyshop UK",
    "babyshop-na": "Babyshop NA", "babyshop-asia": "Babyshop Asia",
    "babyshop-row": "Babyshop ROW",
    "lekmer-se": "Lekmer SE", "lekmer-no": "Lekmer NO",
    "lekmer-fi": "Lekmer FI", "lekmer-dk": "Lekmer DK",
}

# ── Stated facts that travel with every figure ───────────────────────────────
BASIS_WARNING = (
    "Ordered and posted are two different measures and must never be summed or "
    "directly compared. On a calendar basis June 2026 reads 18.26 MSEK ordered "
    "against 21.69 MSEK posted, a 19% gap that is entirely posting timing: "
    "May's orders posted into June. Matched order for order the same two "
    "figures agree to 99.16%."
)
MEASURE = {
    "basis": "order_date",
    "name": "Ordered",
    "scope": ("Norce order intake, ex VAT, gross of returns, merchandise plus "
              "freight, every order status except 6."),
    "overstatement_pct": 0.84,
    "overstatement_note": (
        "Ordered intake runs about 0.84% above what eventually posts. That "
        "wedge is cancellation and shrinkage between the order and the "
        "invoice, and it is stated once rather than applied: no figure on this "
        "tab is scaled down by it."),
    "no_margin": (
        "Gross margin is not shown at day grain. Line-level CostUnit is 0.00% "
        "populated across all 16 months of history, and the price-list cost is "
        "an undated current snapshot, so a margin here would price 2025 at "
        "today's cost."),
    "fx_note": (
        "Non-SEK markets are converted at Norce's own currency table, a fixed "
        "rate card rather than a live FX feed, applied uniformly across "
        "history. That is constant-currency by construction and is the same "
        "convention norce_marts.sql uses."),
    "tz_note": (
        "Days are Stockholm trading days. A UTC day boundary would move two "
        "hours of every evening into the next day."),
}


def I(v) -> int:
    return int(round(float(v or 0)))


def F(v, nd=2):
    return None if v is None else round(float(v), nd)


def _credentials():
    """ADC in production (Cloud Run SA); gcloud user token as a local fallback.

    Same shape as every other refresh module here — see refresh_exec_pl.py for
    why the fallback re-shells rather than holding a bare hour-long token."""
    try:
        import google.auth
        creds, _ = google.auth.default()
        return creds
    except Exception:                                    # noqa: BLE001
        import google.oauth2.credentials

        class _GcloudToken(google.oauth2.credentials.Credentials):
            def refresh(self, request):                  # noqa: ARG002
                self.token = subprocess.check_output(
                    ["gcloud", "auth", "print-access-token"]).decode().strip()
                self.expiry = (datetime.datetime.utcnow()
                               + datetime.timedelta(minutes=45))

        tok = subprocess.check_output(
            ["gcloud", "auth", "print-access-token"]).decode().strip()
        c = _GcloudToken(tok)
        c.expiry = datetime.datetime.utcnow() + datetime.timedelta(minutes=45)
        return c


_client = None
_scanned = [0]


def bq() -> bigquery.Client:
    global _client
    if _client is None:
        _client = bigquery.Client(project=BQ_BILLING_PROJECT, location=BQ_LOCATION,
                                  credentials=_credentials())
    return _client


def _rows(sql: str, params=None) -> list:
    job = bq().query(sql, job_config=bigquery.QueryJobConfig(
        query_parameters=params or []))
    out = list(job.result())
    _scanned[0] += job.total_bytes_processed or 0
    return out


# ── The one query ────────────────────────────────────────────────────────────
# Daily x market, on the exact norce_today.py revenue definition. Everything
# else in this module is arithmetic over these rows, so there is one place where
# the measure is defined and one place a definition drift could enter.
DAILY_SQL = f"""
WITH li AS (
  SELECT OrderId,
         SUM(IF(PartNo != '{SHIPPING_PART_NO}', LineAmount,  0)) AS merch,
         SUM(IF(PartNo != '{SHIPPING_PART_NO}', QtyOrdered,  0)) AS units
  FROM `{DATA_PROJECT}.{NORCE_DATASET}.order_items`
  GROUP BY OrderId
)
SELECT
  DATE(o.OrderDate, '{TZ}')                                   AS d,
  o.app_key                                                   AS market,
  COUNT(*)                                                    AS orders,
  SUM(COALESCE(li.merch, 0) * fx.to_sek)                      AS merch_sek,
  SUM(COALESCE(o.FreightCost, 0) * fx.to_sek)                 AS freight_sek,
  SUM(COALESCE(li.units, 0))                                  AS units
FROM `{DATA_PROJECT}.{NORCE_DATASET}.orders` o
LEFT JOIN li ON li.OrderId = o.Id
-- LEFT, not INNER: an unknown currency must surface as a NULL-valued row, not
-- silently delete a day's orders. `fx_missing` below counts them.
LEFT JOIN `{DATA_PROJECT}.{NORCE_DATASET}.currency_rates` fx
       ON fx.currency_id = o.CurrencyId
WHERE o.StatusId != 6
  AND DATE(o.OrderDate, '{TZ}') >= @from_day
  AND DATE(o.OrderDate, '{TZ}') <= @to_day
GROUP BY d, market
ORDER BY d, market
"""

RANGE_SQL = f"""
SELECT MIN(DATE(OrderDate, '{TZ}')) AS min_d,
       MAX(DATE(OrderDate, '{TZ}')) AS max_d,
       COUNT(*)                     AS rows_total,
       COUNTIF(CurrencyId IS NULL
               OR CurrencyId NOT IN (SELECT currency_id
                                     FROM `{DATA_PROJECT}.{NORCE_DATASET}.currency_rates`))
                                    AS fx_missing
FROM `{DATA_PROJECT}.{NORCE_DATASET}.orders`
WHERE StatusId != 6
"""


def _p(name, value):
    kind = "DATE" if isinstance(value, datetime.date) else "STRING"
    return bigquery.ScalarQueryParameter(name, kind, value)


# ── Shaping helpers ──────────────────────────────────────────────────────────
def _blank():
    return {"rev": 0.0, "merch": 0.0, "freight": 0.0, "orders": 0, "units": 0}


def _add(dst, src):
    for k in ("rev", "merch", "freight", "orders", "units"):
        dst[k] += src[k]
    return dst


def _fin(d: dict) -> dict:
    """Round a bucket for the wire and attach AOV.

    AOV is revenue over orders, computed here rather than on the page, because
    a ratio of two already-rounded numbers drifts."""
    o = d["orders"]
    return {"rev": I(d["rev"]), "merch": I(d["merch"]), "freight": I(d["freight"]),
            "orders": I(o), "units": I(d["units"]),
            "aov": I(d["rev"] / o) if o else None}


def _delta(cur: float | None, base: float | None):
    """Percent change, or None when the base cannot carry one.

    A zero or missing base returns None rather than infinity or 100%: the page
    prints a dash, which is the truth."""
    if cur is None or base is None or base == 0:
        return None
    return round(100.0 * (cur - base) / base, 1)


def _cmp(cur: dict, base: dict | None, label: str, detail: str) -> dict | None:
    if not base:
        return None
    return {
        "label": label, "detail": detail,
        "rev": I(base["rev"]), "orders": I(base["orders"]),
        "aov": I(base["rev"] / base["orders"]) if base["orders"] else None,
        "d_rev": _delta(cur["rev"], base["rev"]),
        "d_orders": _delta(cur["orders"], base["orders"]),
        "d_aov": _delta(cur["rev"] / cur["orders"] if cur["orders"] else None,
                        base["rev"] / base["orders"] if base["orders"] else None),
    }


def _window(by_day: dict, end: datetime.date, n: int) -> dict | None:
    """The n days ending on `end` inclusive, or None if any day is missing.

    All-or-nothing on purpose. A 7-day total silently built from 5 days is the
    exact failure mode that makes a rolling window lie, and it would lie
    downwards, which reads as a crash."""
    out = _blank()
    for i in range(n):
        day = end - datetime.timedelta(days=i)
        row = by_day.get(day)
        if row is None:
            return None
        _add(out, row)
    return out


# ── The plan pro-rating ──────────────────────────────────────────────────────
def _weekday_factors(by_day: dict, end: datetime.date, weeks: int = 26) -> list[float]:
    """Multiplicative day-of-week factors from the trailing `weeks` of intake.

    Same decomposition daily_targets.py uses and for the same reason: each day's
    revenue over its own centred 7-day mean, then the MEDIAN of those ratios per
    weekday so one Black Friday cannot bend "Friday". Normalised to average 1.

    Falls back to a flat week if there is not enough history to centre on, which
    makes the pro-rating degrade to "every day is equal" rather than break.
    """
    ratios: list[list[float]] = [[] for _ in range(7)]
    start = end - datetime.timedelta(days=weeks * 7)
    day = start
    while day <= end:
        centre = [by_day.get(day + datetime.timedelta(days=k)) for k in range(-3, 4)]
        if all(c is not None for c in centre):
            mean = sum(c["rev"] for c in centre) / 7.0
            here = by_day[day]["rev"]
            if mean > 0:
                ratios[day.weekday()].append(here / mean)
        day += datetime.timedelta(days=1)

    if not all(ratios):
        return [1.0] * 7
    fac = [statistics.median(r) for r in ratios]
    mean = sum(fac) / 7.0
    if mean <= 0:
        return [1.0] * 7
    return [f / mean for f in fac]


def _month_shape(by_day: dict, year: int, month: int,
                 wf: list[float]) -> tuple[list[float], str]:
    """Expected share of the month's intake per day, summing to 1.

    Two layers, in the order that matters:

    1. Day-of-week, from `wf` above.
    2. Day-of-month, learned from the SAME calendar month last year and
       DE-WEEKDAYED first — each prior-year day is divided by the weekday factor
       for its own weekday before being turned into shares, so what is left is
       the genuine within-month profile (the Swedish 25th payday, the month-end
       tail) rather than last year's weekday layout.

    Without layer 2 the card would under-weight the payday window and report a
    month-to-date that looks worse than it is for the first three weeks of every
    month. Layer 2 degrades on its own: no prior-year month, no layer, and the
    method string says which was used.

    CAVEAT, PUBLISHED RATHER THAN BURIED. Layer 2 has exactly ONE prior year to
    learn from, so a campaign in that month becomes "shape". September 2025 ran
    43.7% of its intake in the first 16 days because of a spike on the 18th-20th;
    August 2026 ran 54.0%, which is almost flat. The pace figure therefore moves
    with a judgement call, so `_month_shape`'s answer is published alongside a
    flat-calendar pace and the page states that the truth sits between them.
    """
    days_in = (datetime.date(year + (month == 12), month % 12 + 1, 1)
               - datetime.date(year, month, 1)).days
    ly = [by_day.get(datetime.date(year - 1, month, dom))
          for dom in range(1, days_in + 1)]
    have = [x for x in ly if x is not None]

    # Demand at least most of the prior-year month. A half-present month would
    # put a spurious hole in the shape on whatever days happened to be missing.
    if len(have) >= max(20, int(days_in * 0.9)) and sum(x["rev"] for x in have) > 0:
        dom_w: list[float] = []
        for dom in range(1, days_in + 1):
            row = ly[dom - 1]
            if row is None:
                dom_w.append(0.0)
                continue
            d = datetime.date(year - 1, month, dom)
            dom_w.append(row["rev"] / (wf[d.weekday()] or 1.0))
        # Days the prior year did not have (a 29th in a leap year against a
        # 28-day prior Feb, or a genuine gap) take the month's own mean weight
        # rather than zero.
        nz = [w for w in dom_w if w > 0]
        fill = (sum(nz) / len(nz)) if nz else 1.0
        dom_w = [w if w > 0 else fill for w in dom_w]
        method = f"weekday x day-of-month learned from {year - 1}-{month:02d}"
    else:
        dom_w = [1.0] * days_in
        method = "weekday only (no usable prior-year month)"

    raw = [dom_w[dom - 1] * wf[datetime.date(year, month, dom).weekday()]
           for dom in range(1, days_in + 1)]
    total = sum(raw)
    if total <= 0:
        return [1.0 / days_in] * days_in, "flat (degenerate shape)"
    return [r / total for r in raw], method


def _load_plan(year: int) -> tuple[dict | None, dict]:
    """The rolling forecast's Gross Sales line, or (None, meta) if unusable.

    Read from disk next to this module, the same way refresh_exec_pl.py does.
    A missing file degrades the plan card to "no plan loaded" and never stops
    the run: the rest of the tab does not depend on it.

    THE YEAR GUARD. The forecast file keys months as bare "jan".."dec" with no
    year in them, and the file on disk is a 2026 vintage. Without a guard, the
    first run in January 2027 would quietly pace January 2027 against January
    2026's plan and report a confident, wrong number. So the file's own year is
    required to match the year being paced; a mismatch drops the plan card and
    says why, which is the failure a reader can see."""
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), FORECAST_FILE)
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except Exception as e:                                 # noqa: BLE001
        return None, {"loaded": False, "reason": f"{type(e).__name__}: {e}"}
    line = (doc.get("lines") or {}).get(PLAN_LINE)
    if not line:
        return None, {"loaded": False, "reason": f"no '{PLAN_LINE}' line in {FORECAST_FILE}"}
    meta = doc.get("_meta") or {}

    # The year the file is FOR: an explicit override, else the first 20xx found
    # in its vintage/source strings, else the filename.
    blob = f"{meta.get('vintage', '')} {meta.get('source', '')} {FORECAST_FILE}"
    found = re.findall(r"20\d{2}", blob)
    plan_year = int(os.environ.get("DAILY_PERF_PLAN_YEAR") or (found[0] if found else 0))
    if plan_year and plan_year != year:
        return None, {"loaded": False, "plan_year": plan_year,
                      "reason": (f"{FORECAST_FILE} is a {plan_year} forecast and this month is "
                                 f"in {year}. Its months are keyed 'jan'..'dec' with no year, so "
                                 f"pacing against it would compare {year} with {plan_year}. "
                                 f"Drop in the {year} forecast to restore this card.")}
    return line, {"loaded": True, "line": PLAN_LINE, "plan_year": plan_year,
                  "vintage": meta.get("vintage"), "source": meta.get("source")}


# ── Build ────────────────────────────────────────────────────────────────────
def build() -> dict:
    t0 = time.time()

    rng = _rows(RANGE_SQL)[0]
    min_d, max_d = rng["min_d"], rng["max_d"]

    # The latest day BigQuery can speak for is the last COMPLETE one. Today's
    # partition holds only the orders placed between midnight and the 01:00
    # sync, so it is dropped outright rather than shown as a collapse. Today is
    # the live API's job and has no fallback here.
    today = datetime.datetime.now(ZoneInfo(TZ)).date()
    latest = min(max_d, today - datetime.timedelta(days=1))

    from_day = latest - datetime.timedelta(days=WINDOW_DAYS)
    rows = _rows(DAILY_SQL, [_p("from_day", from_day), _p("to_day", latest)])

    by_day: dict[datetime.date, dict] = {}
    by_day_mkt: dict[datetime.date, dict[str, dict]] = {}
    for r in rows:
        d = r["d"]
        merch = float(r["merch_sek"] or 0)
        freight = float(r["freight_sek"] or 0)
        cell = {"rev": merch + freight, "merch": merch, "freight": freight,
                "orders": int(r["orders"] or 0), "units": float(r["units"] or 0)}
        _add(by_day.setdefault(d, _blank()), cell)
        by_day_mkt.setdefault(d, {})[r["market"]] = cell

    if latest not in by_day:
        raise RuntimeError(
            f"no rows for {latest}, the day BigQuery should have closed at 01:00 "
            f"— norce-sync may have stopped again")

    usable_from = datetime.date.fromisoformat(USABLE_FROM)

    # ── Yesterday, and the three comparisons that make it mean something ─────
    y = by_day[latest]
    lw = by_day.get(latest - datetime.timedelta(days=7))

    # Trailing 4-week same-weekday average: weeks 1-4 back, which deliberately
    # INCLUDES last week. It is the level this weekday normally runs at, not a
    # second opinion on last week.
    same_wd = [by_day.get(latest - datetime.timedelta(days=7 * k)) for k in (1, 2, 3, 4)]
    same_wd = [x for x in same_wd if x is not None]
    avg4 = None
    if same_wd:
        avg4 = {k: sum(x[k] for x in same_wd) / len(same_wd)
                for k in ("rev", "merch", "freight", "orders", "units")}

    yoy_day = latest - datetime.timedelta(days=YOY_SHIFT_DAYS)
    ly = by_day.get(yoy_day) if yoy_day >= usable_from else None

    yesterday = dict(_fin(y), d=latest.isoformat(), wd=latest.strftime("%a"),
                     weekday_index=latest.weekday())
    yesterday["vs"] = {
        "last_week": _cmp(y, lw, "Same weekday last week",
                          (latest - datetime.timedelta(days=7)).isoformat()),
        "avg4": _cmp(y, avg4, f"{len(same_wd)}-week same-weekday average",
                     f"the last {len(same_wd)} {latest.strftime('%A')}s")
        if avg4 else None,
        "last_year": _cmp(y, ly, "Same weekday last year",
                          f"{yoy_day.isoformat()} (-{YOY_SHIFT_DAYS}d, weekday aligned)"),
    }

    # ── Rolling windows, which are how you read past the weekday sawtooth ────
    rolling = {}
    for n in (7, 28):
        cur = _window(by_day, latest, n)
        if cur is None:
            rolling[f"r{n}"] = None
            continue
        prev = _window(by_day, latest - datetime.timedelta(days=n), n)
        yoy_end = latest - datetime.timedelta(days=YOY_SHIFT_DAYS)
        yoy = (_window(by_day, yoy_end, n)
               if yoy_end - datetime.timedelta(days=n - 1) >= usable_from else None)
        rolling[f"r{n}"] = dict(
            _fin(cur), n=n,
            **{"from": (latest - datetime.timedelta(days=n - 1)).isoformat(),
               "to": latest.isoformat()},
            vs={
                "prev": _cmp(cur, prev, f"Previous {n} days",
                             f"{(latest - datetime.timedelta(days=2*n-1)).isoformat()} to "
                             f"{(latest - datetime.timedelta(days=n)).isoformat()}"),
                "last_year": _cmp(cur, yoy, f"Same {n} days last year",
                                  f"ending {yoy_end.isoformat()} (-{YOY_SHIFT_DAYS}d)"),
            })

    # ── Market split: yesterday, and the rolling 7 that smooths it ──────────
    def _mkt_window(end: datetime.date, n: int) -> dict[str, dict] | None:
        out: dict[str, dict] = {}
        for i in range(n):
            day = end - datetime.timedelta(days=i)
            if day not in by_day_mkt:
                return None
            for key, cell in by_day_mkt[day].items():
                _add(out.setdefault(key, _blank()), cell)
        return out

    def _mkt_table(end: datetime.date, n: int, prev_end: datetime.date | None,
                   yoy_end: datetime.date | None) -> list[dict]:
        cur = _mkt_window(end, n) or {}
        prev = _mkt_window(prev_end, n) if prev_end else None
        yoy = (_mkt_window(yoy_end, n)
               if yoy_end and yoy_end - datetime.timedelta(days=n - 1) >= usable_from
               else None)
        total = sum(v["rev"] for v in cur.values()) or 0.0
        out = []
        for key, v in cur.items():
            row = dict(_fin(v), key=key, name=MARKET_NAMES.get(key, key),
                       share=round(100.0 * v["rev"] / total, 1) if total else None)
            # The BASES travel with the deltas. A long-tail market can show
            # +568% off nine orders, which is noise wearing a percentage sign;
            # the page suppresses a delta whose base is too small to carry one,
            # and it can only do that if it knows the base.
            row["prev_rev"] = I((prev or {}).get(key, {}).get("rev") or 0) if prev else None
            row["yoy_rev"] = I((yoy or {}).get(key, {}).get("rev") or 0) if yoy else None
            row["d_prev"] = _delta(v["rev"], (prev or {}).get(key, {}).get("rev"))
            row["d_yoy"] = _delta(v["rev"], (yoy or {}).get(key, {}).get("rev"))
            out.append(row)
        out.sort(key=lambda r: -r["rev"])
        return out

    markets = {
        "yesterday": _mkt_table(latest, 1, latest - datetime.timedelta(days=7),
                                latest - datetime.timedelta(days=YOY_SHIFT_DAYS)),
        "r7": _mkt_table(latest, 7, latest - datetime.timedelta(days=7),
                         latest - datetime.timedelta(days=YOY_SHIFT_DAYS)),
        "note": ("Market is the Norce application, which is shop x market. "
                 "The percentage columns compare like with like: yesterday "
                 "against the same weekday a week earlier, the rolling 7 "
                 "against the 7 days before it."),
    }

    # ── Month to date against the plan ──────────────────────────────────────
    plan_line, plan_meta = _load_plan(latest.year)
    wf = _weekday_factors(by_day, latest)
    first = latest.replace(day=1)
    shape, shape_method = _month_shape(by_day, latest.year, latest.month, wf)

    mtd = _blank()
    day = first
    while day <= latest:
        if day in by_day:
            _add(mtd, by_day[day])
        day += datetime.timedelta(days=1)

    # Last year's same month to the same day-of-month, on the same basis. This
    # is the honest MTD comparison — both sides are ordered intake — and it is
    # presented ahead of the plan for exactly that reason.
    ly_mtd = None
    try:
        ly_first = datetime.date(latest.year - 1, latest.month, 1)
        ly_last = datetime.date(latest.year - 1, latest.month, latest.day)
        if ly_first >= usable_from:
            acc, day, ok = _blank(), ly_first, True
            while day <= ly_last:
                if day not in by_day:
                    ok = False
                    break
                _add(acc, by_day[day])
                day += datetime.timedelta(days=1)
            ly_mtd = acc if ok else None
    except ValueError:                                     # 29 Feb against a non-leap year
        ly_mtd = None

    plan_month = None
    if plan_line:
        plan_month = plan_line.get(MONTHS[latest.month - 1])
    share_to_date = sum(shape[:latest.day])
    plan_to_date = plan_month * share_to_date if plan_month else None

    mtd_out = {
        "month": f"{latest.year}-{latest.month:02d}",
        "month_label": latest.strftime("%B %Y"),
        "days_elapsed": latest.day,
        "days_in_month": len(shape),
        **_fin(mtd),
        "ly": (dict(_fin(ly_mtd), d_rev=_delta(mtd["rev"], ly_mtd["rev"]),
                    d_orders=_delta(mtd["orders"], ly_mtd["orders"]),
                    detail=f"1-{latest.day} {latest.strftime('%B')} {latest.year - 1}, "
                           f"same basis")
               if ly_mtd else None),
        "plan": ({
            "month_sek": I(plan_month),
            "to_date_sek": I(plan_to_date),
            "share_to_date_pct": round(100 * share_to_date, 1),
            # Merchandise, NOT merchandise+freight: the plan line excludes the
            # shipping pseudo-SKU. See the module docstring.
            "actual_to_date_sek": I(mtd["merch"]),
            "pace_pct": _delta(mtd["merch"], plan_to_date),
            "shape_method": shape_method,
            "weekday_factors": [round(f, 3) for f in wf],
            # The same pace against a flat calendar share. Published because the
            # shaped answer rests on a single prior year: where the two differ,
            # the honest reading sits between them. See _month_shape's caveat.
            "flat_to_date_sek": I(plan_month * latest.day / len(shape)),
            "flat_share_to_date_pct": round(100.0 * latest.day / len(shape), 1),
            "flat_pace_pct": _delta(mtd["merch"],
                                    plan_month * latest.day / len(shape)),
        } if plan_to_date else None),
        "plan_meta": plan_meta,
        "plan_note": (
            "The plan is the rolling forecast's Gross Sales line. It is global "
            "only: there is no market-level target, and none is implied here. "
            "It is also a P&L line, so it is stated on the POSTED basis while "
            "this tab is ordered. Read it as a pace reference, not a "
            "reconciliation. The comparison uses merchandise only, because the "
            "plan line excludes shipping while ordered revenue includes "
            "freight. The month is spread across days by a weekday and "
            "day-of-month shape rather than a flat 1/N, so the payday window "
            "around the 25th is not smeared over the whole month. That shape "
            "has one prior year to learn from, so the flat-calendar pace is "
            "shown beside it and the honest reading sits between the two."),
    }

    # ── The series ──────────────────────────────────────────────────────────
    series = []
    day = from_day
    while day <= latest:
        row = by_day.get(day)
        if row is not None:
            series.append({"d": day.isoformat(), "wd": day.weekday(),
                           "r": I(row["rev"]), "m": I(row["merch"]),
                           "o": I(row["orders"]), "u": I(row["units"])})
        day += datetime.timedelta(days=1)

    covered = sum(1 for s in series)
    span = (latest - from_day).days + 1

    payload = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc)
                                 .isoformat(timespec="seconds"),
        "timezone": TZ,
        "measure": MEASURE,
        "basis_warning": BASIS_WARNING,
        "latest_complete_day": latest.isoformat(),
        "history": {
            "min_day": min_d.isoformat() if min_d else None,
            "max_day": max_d.isoformat() if max_d else None,
            "usable_from": USABLE_FROM,
            "yoy_available": bool(yesterday["vs"]["last_year"]),
            "yoy_shift_days": YOY_SHIFT_DAYS,
            "window_days": WINDOW_DAYS,
            "days_in_window": covered,
            "days_missing_in_window": span - covered,
            "note": (
                "Norce order history starts "
                f"{min_d.isoformat() if min_d else '?'}, but the first weeks are "
                f"a partial extract ramp (10-92 orders a day against a ~950 "
                f"baseline), so every comparison on this tab ignores anything "
                f"before {USABLE_FROM}. Year-on-year is aligned by weekday at "
                f"-{YOY_SHIFT_DAYS} days, not by calendar date."),
        },
        "yesterday": yesterday,
        "rolling": rolling,
        "markets": markets,
        "mtd": mtd_out,
        "series": series,
        "diagnostics": {
            "elapsed_s": round(time.time() - t0, 2),
            "bytes_scanned": _scanned[0],
            "rows_read": len(rows),
            "orders_all_time": I(rng["rows_total"]),
            "orders_missing_fx": I(rng["fx_missing"]),
            "market_keys": sorted(MARKET_NAMES),
        },
    }
    return payload


def write_firestore(payload: dict) -> str:
    from google.cloud import firestore
    db = firestore.Client(project=FIRESTORE_PROJECT, credentials=_credentials())
    doc_id = f"{WORKSPACE}__{DOC_KEY}"
    db.collection(COLLECTION).document(doc_id).set({
        "data": payload, "fetched_at": firestore.SERVER_TIMESTAMP,
        "expires_at": time.time() + TTL, "ttl_seconds": TTL, "workspace": WORKSPACE})
    return f"{COLLECTION}/{doc_id}"


def main():
    p = build()
    size = len(json.dumps(p, separators=(",", ":")))
    y = p["yesterday"]
    print(f"daily-perf: latest complete day {p['latest_complete_day']} "
          f"({y['wd']}) {y['rev']:,} SEK, {y['orders']:,} orders", flush=True)
    for k, v in (p["yesterday"]["vs"] or {}).items():
        if v:
            print(f"  vs {k:<10} {v['rev']:>12,} SEK  {v['d_rev']:+.1f}%", flush=True)
    print(f"  series {len(p['series'])} days, doc {size:,} bytes, "
          f"{p['diagnostics']['bytes_scanned']:,} bytes scanned, "
          f"{p['diagnostics']['elapsed_s']}s", flush=True)
    if size > DOC_BUDGET_BYTES:
        raise RuntimeError(f"payload {size} bytes exceeds the {DOC_BUDGET_BYTES} budget "
                           f"— shorten DAILY_PERF_WINDOW_DAYS rather than raising it")
    if os.environ.get("DUMP_JSON"):
        with open(os.environ["DUMP_JSON"], "w", encoding="utf-8") as fh:
            json.dump(p, fh, indent=1)
        print(f"  wrote {os.environ['DUMP_JSON']}", flush=True)
    where = "(skipped)" if os.environ.get("SKIP_FIRESTORE") else write_firestore(p)
    print(f"  -> {where}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
