#!/usr/bin/env python3
"""
Daily target curve — spreads a MONTHLY budget target across the days of a month.

The budget (budget_2026.json, parsed from Finance's rolling P&L) is monthly. The
KV Overview needs to answer "are we on track *today*", which needs a target per
day. A flat monthly/days line is useless for that: a Saturday is not a Tuesday,
the 25th is payday in Sweden, and Black Friday moves by a week between years.

So this module builds a multiplicative daily INDEX from the daily history in the
Funnel→BigQuery export and normalises it so the month's daily targets sum
EXACTLY to the monthly target.

    target(d) = monthly_target · index(d) / Σ index

Index model — three multiplicative layers
─────────────────────────────────────────
1. Day-of-week factor.  Classic multiplicative decomposition over ALL available
   history: each day's value ÷ its centred 7-day mean, then the MEDIAN of those
   ratios per weekday (median, not mean, so a single Black Friday cannot bend
   "Friday"). Normalised to average 1 across the week. Holiday dates are held
   out of this estimate — they would otherwise smear onto whichever weekday they
   happened to land on.

2. Day-of-month shape, learned from the SAME calendar month in prior years and
   de-weekdayed first.  Prior-year daily values are divided by that year's
   day-of-week factor before being turned into shares, which strips the prior
   year's weekday layout out of the shape. What is left is the genuine
   within-month profile: the payday bump, the month-end tail, campaign timing.
   Shares from several prior years are blended with recency weights
   (PRIOR_YEAR_WEIGHTS). This is the client's "(b) last year / (c) the year
   before" component, and it degrades on its own: with no prior year for that
   month the model is pure day-of-week + calendar events.

3. Calendar events, aligned BY EVENT rather than by date.  A day's slot in the
   shape table is keyed as one of:
       ('H', name)    a Swedish red day, or a moving retail event (Black Friday,
                      Cyber Monday, Singles' Day, Mother's/Father's Day)
       ('P', offset)  the payday window, offset −1…+2 around that month's payday
       ('D', dom)     an ordinary day, keyed by day-of-month
   Because Easter, Midsummer, All Saints, Black Friday and the payday all move
   between years, keying them by NAME/OFFSET is what makes "last year's Black
   Friday" line up with "this year's Black Friday" instead of with whatever
   ordinary Thursday shares its date.

   Swedish payday: salaries land on the 25th; when the 25th is a Saturday or
   Sunday the transfer is pulled back to the preceding Friday. PAYDAY_WINDOW
   covers the day before through two days after, which is where the spending
   bump actually sits.

Known limitations (deliberate, documented rather than papered over)
───────────────────────────────────────────────────────────────────
• The export starts 2025-01-01, so for any month of 2026 there is exactly ONE
  prior year. The client's "(c) the year before that" layer is implemented and
  weighted, but has no data to read yet — it starts contributing automatically
  in 2027. `model.prior_years` in the payload reports what was actually used.
• GP1/GP2/GP3 borrow the REVENUE index. Their daily values can be negative
  (a heavy ad day, a returns spike), and shares of a series that crosses zero
  are meaningless. Marketing spend gets its own index — its daily shape is
  budget pacing, not demand. See SHAPE_FIELD.
• The index is a *shape*, not a forecast. It answers "how much of this month
  should have landed by day N", which is the pacing question the dashboard asks.
"""
from __future__ import annotations

import datetime
import statistics
import time

# bq_source owns the BigQuery wiring and, importantly, the cost de-duplication
# CTE — reusing it means the target curve and the dashboard's actuals come from
# exactly the same aggregate.
import bq_source as bs

# ── Metric wiring ────────────────────────────────────────────────────────────
# budget line key → daily field in the KV daily series.
METRIC_FIELD = {
    "gross_sales": "revenue",
    "gp1":         "gp1",
    "gp2":         "gp2",
    "gp3":         "gp3",
    "marketing":   "cost",
}
# Which daily field's history drives each metric's index. GP lines can go
# negative on a single day, so shares of them are undefined — they ride the
# revenue shape, which is what actually drives them.
SHAPE_FIELD = {
    "gross_sales": "revenue",
    "gp1":         "revenue",
    "gp2":         "revenue",
    "gp3":         "revenue",
    "marketing":   "cost",
}

# Most recent prior year first. Trimmed to the years that actually have data.
PRIOR_YEAR_WEIGHTS = (0.65, 0.35)
PAYDAY_WINDOW = (-1, 2)          # inclusive offsets around payday
HISTORY_TTL = 30 * 60            # seconds; the underlying export refreshes daily

# ── Regularisation of the prior-year shape ───────────────────────────────────
# One prior year is ONE noisy observation of "what this month looks like", and
# in a business growing ~38% YoY it also carries that year's own growth ramp.
# Taken raw it is much worse than a flat line: backtesting July 2026 against the
# unregularised July-2025 shape gave 46.6% daily MAPE vs 22.9% for flat, because
# July 2025 ran 285k on the 1st and 1.47M on the 31st (back-to-school ramping
# from a far smaller base) and the model duly predicted a 5x ramp that did not
# repeat.
#
# Two standard corrections, both tuned by backtest over Feb–Jul 2026
# (see `sweep` at the bottom of this file — it re-runs the tuning):
#   • DOM_SMOOTH  — moving average across ordinary day-of-month slots, so a
#     single campaign day in the prior year does not transfer as "the 12th is
#     big". Event and payday slots are NEVER smoothed: those are real, recurring
#     and already event-aligned, and smoothing would blunt exactly the signal
#     they carry.
#   • PRIOR_SHRINK — shrink what is left toward a flat month. Classic
#     shrinkage: with one noisy sample the minimum-error estimate sits between
#     the sample and the grand mean, not on the sample.
# Both are relaxed as more prior years accumulate (2027 onwards), since the
# blend of two years is itself less noisy.
DOM_SMOOTH = 7                   # slots, centred, odd — one full week
PRIOR_SHRINK = 0.30              # weight on the observed shape, 1 prior year
PRIOR_SHRINK_2Y = 0.50           # …with two prior years (untuned: no data yet)


# ── Swedish calendar ─────────────────────────────────────────────────────────
def easter_sunday(year: int) -> datetime.date:
    """Gregorian Easter (anonymous computus). Drives 4 of the 13 red days."""
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    lu = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * lu) // 451
    month, day = divmod(h + lu - 7 * m + 114, 31)
    return datetime.date(year, month, day + 1)


def _weekday_in(year: int, month: int, lo: int, hi: int, weekday: int) -> datetime.date:
    """The single date in [lo, hi] of `month` falling on `weekday` (Mon=0)."""
    for day in range(lo, hi + 1):
        d = datetime.date(year, month, day)
        if d.weekday() == weekday:
            return d
    raise ValueError(f"no weekday {weekday} in {year}-{month} {lo}..{hi}")


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> datetime.date:
    """n-th `weekday` of the month (n starts at 1)."""
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(weekday - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def _last_weekday(year: int, month: int, weekday: int) -> datetime.date:
    nxt = datetime.date(year + (month == 12), (month % 12) + 1, 1)
    d = nxt - datetime.timedelta(days=1)
    return d - datetime.timedelta(days=(d.weekday() - weekday) % 7)


def calendar_events(year: int) -> dict[datetime.date, str]:
    """Swedish red days + the moving retail events a baby retailer actually feels.

    Named, not dated, because that is the whole point: Easter, Midsummer, All
    Saints, Black Friday and Mother's/Father's Day land on different dates every
    year, and the shape table has to line last year's event up with this year's.
    """
    e = easter_sunday(year)
    ev: dict[datetime.date, str] = {
        datetime.date(year, 1, 1):   "nyarsdagen",
        datetime.date(year, 1, 6):   "trettondedag",
        e - datetime.timedelta(days=3): "skartorsdag",
        e - datetime.timedelta(days=2): "langfredag",
        e:                              "paskdagen",
        e + datetime.timedelta(days=1): "annandag_pask",
        datetime.date(year, 5, 1):   "forsta_maj",
        e + datetime.timedelta(days=39): "kristi_himmelsfard",
        e + datetime.timedelta(days=49): "pingstdagen",
        datetime.date(year, 6, 6):   "nationaldagen",
        datetime.date(year, 12, 24): "julafton",
        datetime.date(year, 12, 25): "juldagen",
        datetime.date(year, 12, 26): "annandag_jul",
        datetime.date(year, 12, 31): "nyarsafton",
    }
    # Midsummer Eve = the Friday in 19–25 Jun; Midsummer Day the Saturday after.
    mid_eve = _weekday_in(year, 6, 19, 25, 4)
    ev[mid_eve] = "midsommarafton"
    ev[mid_eve + datetime.timedelta(days=1)] = "midsommardagen"
    # All Saints' Day = the Saturday in 31 Oct – 6 Nov (so it can land in either
    # month, which is exactly why it has to be keyed by name and not by date).
    oct31 = datetime.date(year, 10, 31)
    ev[oct31 if oct31.weekday() == 5 else _weekday_in(year, 11, 1, 6, 5)] = "alla_helgons"
    # Retail events. Black Friday = the Friday after the US 4th-Thursday
    # Thanksgiving; it moves by a full week between years and is the single
    # largest daily outlier of the retail year, so event alignment matters most
    # here. The two days either side ride along as the "BF weekend".
    bf = _nth_weekday(year, 11, 3, 4) + datetime.timedelta(days=1)
    ev[bf - datetime.timedelta(days=1)] = "bf_eve"
    ev[bf] = "black_friday"
    ev[bf + datetime.timedelta(days=1)] = "bf_saturday"
    ev[bf + datetime.timedelta(days=2)] = "bf_sunday"
    ev[bf + datetime.timedelta(days=3)] = "cyber_monday"
    ev[datetime.date(year, 11, 11)] = "singles_day"
    ev[_last_weekday(year, 5, 6)] = "mors_dag"          # SE: last Sunday of May
    ev[_nth_weekday(year, 11, 6, 2)] = "fars_dag"       # SE: 2nd Sunday of Nov
    return ev


def swedish_payday(year: int, month: int) -> datetime.date:
    """Salary day: the 25th, pulled back to the preceding Friday on a weekend."""
    d = datetime.date(year, month, 25)
    if d.weekday() == 5:      # Saturday → Friday the 24th
        return d - datetime.timedelta(days=1)
    if d.weekday() == 6:      # Sunday → Friday the 23rd
        return d - datetime.timedelta(days=2)
    return d


def day_key(d: datetime.date, events: dict[datetime.date, str]):
    """The shape-table slot for one date. See the module docstring."""
    name = events.get(d)
    if name:
        return ("H", name)
    payday = swedish_payday(d.year, d.month)
    off = (d - payday).days
    if PAYDAY_WINDOW[0] <= off <= PAYDAY_WINDOW[1]:
        return ("P", off)
    return ("D", d.day)


# ── Daily history ────────────────────────────────────────────────────────────
_hist_cache: dict = {"at": 0.0, "rows": None}


def daily_history(force: bool = False) -> list[dict]:
    """Full de-duplicated KV daily series, oldest → newest.

    Same `_kv_cte` + KVD aggregate the dashboard's own daily_long uses, so the
    target curve is built on exactly the numbers it will be drawn against.
    Cached in-process: the export only moves once a day.
    """
    now = time.time()
    if not force and _hist_cache["rows"] is not None and now - _hist_cache["at"] < HISTORY_TTL:
        return _hist_cache["rows"]
    rows = [
        {"iso": r["d"], "revenue": float(r["rev"] or 0), "gp1": float(r["gp1"] or 0),
         "gp2": float(r["gp2"] or 0), "gp3": float(r["gp3"] or 0),
         "cost": float(r["cost"] or 0)}
        for r in bs._rows(
            # 3 years is everything the export holds today and everything the
            # model can use (two prior years + the current one); bounding it
            # also lets BigQuery prune the Date partition as history grows.
            f"WITH {bs._kv_cte('Date >= DATE_SUB(CURRENT_DATE(), INTERVAL 3 YEAR)')} "
            f"SELECT CAST(Date AS STRING) d, {bs.KVD} FROM kv_rows GROUP BY d ORDER BY d", [])
    ]
    # The current (incomplete) day would drag every model down. The export's last
    # date is "today so far", so drop it whenever it is today.
    today = datetime.date.today().isoformat()
    rows = [r for r in rows if r["iso"] < today]
    _hist_cache.update(at=now, rows=rows)
    return rows


# ── Model ────────────────────────────────────────────────────────────────────
def _dow_factors(series: dict[datetime.date, float],
                 events_by_year: dict[int, dict]) -> list[float]:
    """Median multiplicative day-of-week factor, normalised to mean 1.

    value ÷ centred 7-day mean isolates the weekday effect from the level and
    from slow trend. Holidays are excluded so e.g. Midsummer Eve (always a
    Friday) does not become "what Fridays look like".
    """
    days = sorted(series)
    idx = {d: i for i, d in enumerate(days)}
    ratios: dict[int, list[float]] = {w: [] for w in range(7)}
    for d in days:
        i = idx[d]
        if i < 3 or i > len(days) - 4:
            continue
        window = days[i - 3:i + 4]
        # Only trust a window that is 7 consecutive calendar days.
        if (window[-1] - window[0]).days != 6:
            continue
        if d in events_by_year.get(d.year, {}):
            continue
        mean = sum(series[x] for x in window) / 7.0
        if mean <= 0:
            continue
        ratios[d.weekday()].append(series[d] / mean)
    out = []
    for w in range(7):
        out.append(statistics.median(ratios[w]) if ratios[w] else 1.0)
    m = sum(out) / 7.0
    return [x / m for x in out] if m > 0 else [1.0] * 7


def _holiday_factor(series: dict[datetime.date, float], dow: list[float],
                    events_by_year: dict[int, dict]) -> float:
    """Global "a red day is worth X of a normal day" multiplier.

    Only used as the fallback for a target-month event that has no counterpart
    in the prior-year shape (Easter crossing a month boundary, say).
    """
    days = sorted(series)
    idx = {d: i for i, d in enumerate(days)}
    vals = []
    for d in days:
        if d not in events_by_year.get(d.year, {}):
            continue
        i = idx[d]
        if i < 3 or i > len(days) - 4:
            continue
        window = days[i - 3:i + 4]
        if (window[-1] - window[0]).days != 6:
            continue
        base = (sum(series[x] for x in window) / 7.0) * dow[d.weekday()]
        if base > 0:
            vals.append(series[d] / base)
    return statistics.median(vals) if vals else 1.0


def _month_shape(series: dict[datetime.date, float], year: int, month: int,
                 dow: list[float], events: dict[datetime.date, str]) -> dict | None:
    """De-weekdayed day-key → share for one (year, month). None if incomplete."""
    import calendar
    dim = calendar.monthrange(year, month)[1]
    dates = [datetime.date(year, month, d) for d in range(1, dim + 1)]
    if any(d not in series for d in dates):
        return None                       # partial month → not a usable shape
    w = {}
    for d in dates:
        f = dow[d.weekday()] or 1.0
        w[day_key(d, events)] = max(series[d], 0.0) / f
    tot = sum(w.values())
    if tot <= 0:
        return None
    return {k: v / tot for k, v in w.items()}


def _smooth_dom(shape: dict, width: int) -> dict:
    """Moving-average the ordinary day-of-month slots; leave events untouched.

    The prior year's ('D', n) slots are one sample each, so a single campaign
    day reads as "the 12th is big". Averaging over neighbouring days keeps the
    slow within-month profile (payday run-up, month-end tail) and drops the
    single-day noise. Event and payday slots are excluded — those ARE the
    single-day signal, and they are already aligned across years by name.
    """
    if width < 3:
        return shape
    doms = sorted(k[1] for k in shape if k[0] == "D")
    if len(doms) < width:
        return shape
    vals = {n: shape[("D", n)] for n in doms}
    half = width // 2
    out = dict(shape)
    for i, n in enumerate(doms):
        lo, hi = max(0, i - half), min(len(doms), i + half + 1)
        window = [vals[doms[j]] for j in range(lo, hi)]
        out[("D", n)] = sum(window) / len(window)
    return out


def _shrink(shape: dict, lam: float) -> dict:
    """Pull the shape toward a flat month: lam·observed + (1−lam)·uniform."""
    if not shape or lam >= 1.0:
        return shape
    flat = 1.0 / len(shape)
    return {k: lam * v + (1.0 - lam) * flat for k, v in shape.items()}


def build_index(year: int, month: int, field: str,
                history: list[dict] | None = None,
                smooth: int | None = None, shrink: float | None = None) -> dict:
    """Daily index (sums to 1) for one month, plus model diagnostics.

    `history` is only passed explicitly by the backtest, which truncates it to
    what was knowable before the month started. `smooth`/`shrink` override the
    tuned defaults and exist for the parameter sweep.
    """
    import calendar
    rows = history if history is not None else daily_history()
    series = {datetime.date.fromisoformat(r["iso"]): float(r[field] or 0) for r in rows}
    if not series:
        raise ValueError("no daily history available")

    years = sorted({d.year for d in series} | {year, year - 1, year - 2})
    events_by_year = {y: calendar_events(y) for y in years}
    dow = _dow_factors(series, events_by_year)
    hol_f = _holiday_factor(series, dow, events_by_year)

    # Prior-year shapes for the same calendar month, most recent first.
    shapes, used_years = [], []
    for back in range(1, len(PRIOR_YEAR_WEIGHTS) + 1):
        y = year - back
        sh = _month_shape(series, y, month, dow, events_by_year[y])
        if sh:
            shapes.append(sh)
            used_years.append(y)
    weights = list(PRIOR_YEAR_WEIGHTS[:len(shapes)])
    wsum = sum(weights) or 1.0
    weights = [w / wsum for w in weights]

    # Regularise each prior year's shape before blending — see the DOM_SMOOTH /
    # PRIOR_SHRINK note at the top of the file.
    sm = DOM_SMOOTH if smooth is None else smooth
    lam = shrink
    if lam is None:
        lam = PRIOR_SHRINK_2Y if len(shapes) >= 2 else PRIOR_SHRINK
    shapes = [_shrink(_smooth_dom(sh, sm), lam) for sh in shapes]

    blended: dict = {}
    for sh, w in zip(shapes, weights):
        for k, v in sh.items():
            blended[k] = blended.get(k, 0.0) + v * w

    # Fallback level for a target-month key the prior years never produced:
    # the average ordinary-day share, so an unmatched day is "a normal day".
    ord_shares = [v for k, v in blended.items() if k[0] == "D"]
    default_share = (statistics.mean(ord_shares) if ord_shares
                     else (statistics.mean(blended.values()) if blended else 0.0))

    dim = calendar.monthrange(year, month)[1]
    tgt_events = events_by_year[year]
    raw, matched = [], []
    for day in range(1, dim + 1):
        d = datetime.date(year, month, day)
        k = day_key(d, tgt_events)
        if k in blended:
            base, hit = blended[k], "exact"
        elif ("D", day) in blended:
            base, hit = blended[("D", day)], "dom"
        else:
            # No prior-year slot at all: a normal day, nudged by the global
            # holiday factor when this day IS an event.
            base = default_share * (hol_f if k[0] == "H" else 1.0)
            hit = "default"
        if not blended:
            base = 1.0                     # pure day-of-week model
        raw.append(max(base, 0.0) * dow[d.weekday()])
        matched.append(hit)

    tot = sum(raw)
    index = [r / tot for r in raw] if tot > 0 else [1.0 / dim] * dim
    return {
        "index": index,
        "model": {
            "field": field,
            "days": dim,
            "dow_factors": [round(x, 4) for x in dow],
            "prior_years": used_years,
            "prior_year_weights": [round(w, 3) for w in weights],
            "dom_smooth": sm, "prior_shrink": round(lam, 3),
            "holiday_factor": round(hol_f, 4),
            "payday": swedish_payday(year, month).isoformat(),
            "key_match": matched,
            "history_from": rows[0]["iso"], "history_to": rows[-1]["iso"],
            "events": {d.isoformat(): n for d, n in sorted(tgt_events.items())
                       if d.year == year and d.month == month},
        },
    }


def month_targets(year: int, month: int, monthly_targets: dict,
                  history: list[dict] | None = None) -> dict:
    """Daily + cumulative targets per metric for one month.

    `monthly_targets` maps a budget line key (METRIC_FIELD) to that month's
    target. Each metric's daily series sums to its monthly target exactly (the
    last day absorbs the rounding remainder), so the cumulative curve lands on
    the target on the final day of the month by construction.
    """
    rows = history if history is not None else daily_history()
    idx_cache: dict[str, dict] = {}
    out: dict = {}
    for key, target in monthly_targets.items():
        if key not in METRIC_FIELD or target is None:
            continue
        field = SHAPE_FIELD[key]
        if field not in idx_cache:
            idx_cache[field] = build_index(year, month, field, rows)
        ix = idx_cache[field]["index"]
        daily = [int(round(target * w)) for w in ix]
        drift = int(round(target)) - sum(daily)
        if daily:
            daily[-1] += drift            # keep Σ daily == monthly target exactly
        cum, run = [], 0
        for v in daily:
            run += v
            cum.append(run)
        out[key] = {"target_month": int(round(target)), "daily": daily,
                    "cum": cum, "shape_field": field,
                    "index": [round(w, 6) for w in ix]}
    return {"metrics": out,
            "model": {f: idx_cache[f]["model"] for f in idx_cache}}


def month_actuals(year: int, month: int, history: list[dict] | None = None) -> dict:
    """Per-day actuals for the month (0 for days not yet in the export)."""
    import calendar
    rows = history if history is not None else daily_history()
    dim = calendar.monthrange(year, month)[1]
    pre = f"{year:04d}-{month:02d}-"
    by = {r["iso"]: r for r in rows if r["iso"].startswith(pre)}
    out, asof = {}, None
    for key, field in METRIC_FIELD.items():
        vals = []
        for day in range(1, dim + 1):
            iso = f"{pre}{day:02d}"
            r = by.get(iso)
            if r is not None:
                asof = iso if asof is None or iso > asof else asof
            vals.append(int(round(float(r[field]))) if r else None)
        out[key] = vals
    return {"actual": out, "asof": asof, "days": dim}


def build_payload(year: int, month: int, budget: dict | None = None) -> dict:
    """Everything the chart needs: daily + cumulative targets and actuals."""
    if budget is None:
        import budget_source
        budget = budget_source.load_budget_json()
    lines = budget.get("lines", {})
    monthly = {k: (lines.get(k, {}).get("monthly") or [None] * 12)[month - 1]
               for k in METRIC_FIELD}
    hist = daily_history()
    t = month_targets(year, month, monthly, hist)
    a = month_actuals(year, month, hist)
    return {
        "year": year, "month": month, "days": a["days"], "asof": a["asof"],
        "payday": swedish_payday(year, month).isoformat(),
        "targets": t["metrics"], "actual": a["actual"],
        "model": t["model"],
        "currency": budget.get("currency", "SEK"),
        "note": ("Daily targets = monthly budget target x a day-of-week / "
                 "day-of-month / calendar-event index learned from the daily "
                 "history. Sums to the monthly target exactly."),
    }


# ── Backtest ─────────────────────────────────────────────────────────────────
def backtest(year: int, month: int, field: str = "revenue",
             smooth: int | None = None, shrink: float | None = None) -> dict:
    """How well the daily SHAPE tracked a month that has already closed.

    Honest setup: the index is rebuilt from history strictly BEFORE the first of
    the month (no leakage), then scaled to the month's ACTUAL total. Scaling to
    the actual — not to the budget — is deliberate: it measures the daily-index
    model on its own, without folding in whether Finance's monthly number was
    right, which is a different question the dashboard already answers.
    """
    import calendar
    rows = daily_history()
    cutoff = f"{year:04d}-{month:02d}-01"
    past = [r for r in rows if r["iso"] < cutoff]
    if not past:
        raise ValueError(f"no history before {cutoff}")
    dim = calendar.monthrange(year, month)[1]
    pre = f"{year:04d}-{month:02d}-"
    act = {r["iso"]: float(r[field] or 0) for r in rows if r["iso"].startswith(pre)}
    if len(act) < dim:
        raise ValueError(f"{year}-{month:02d} is not a complete month in the export")

    ix = build_index(year, month, field, past, smooth=smooth, shrink=shrink)
    total = sum(act.values())
    pred = [total * w for w in ix["index"]]
    real = [act[f"{pre}{d:02d}"] for d in range(1, dim + 1)]
    flat = [total / dim] * dim

    def _mape(p):
        return 100.0 * statistics.mean(
            abs(p[i] - real[i]) / real[i] for i in range(dim) if real[i] > 0)

    def _cum_err(p):
        cp = cr = 0.0
        worst = 0.0
        for i in range(dim):
            cp += p[i]; cr += real[i]
            if total > 0:
                worst = max(worst, abs(cp - cr) / total * 100.0)
        return worst

    return {
        "month": f"{year:04d}-{month:02d}", "field": field, "days": dim,
        "actual_total": int(round(total)),
        "model_mape_pct": round(_mape(pred), 2),
        "flat_mape_pct": round(_mape(flat), 2),
        "model_max_cum_err_pct": round(_cum_err(pred), 2),
        "flat_max_cum_err_pct": round(_cum_err(flat), 2),
        "prior_years": ix["model"]["prior_years"],
        "dow_factors": ix["model"]["dow_factors"],
        "daily": [{"day": i + 1, "actual": int(round(real[i])),
                   "target": int(round(pred[i])), "flat": int(round(flat[i]))}
                  for i in range(dim)],
    }


def sweep(months: list[tuple[int, int]], field: str = "revenue") -> list[dict]:
    """Grid-search DOM_SMOOTH x PRIOR_SHRINK over completed months.

    Reruns the tuning behind the constants at the top of the file. Ranked on
    mean daily MAPE, with mean max-cumulative-error reported alongside because
    the cumulative curve is the one the dashboard actually shows.
    """
    out = []
    for sm in (1, 3, 5, 7, 9):
        for lam in (0.0, 0.15, 0.3, 0.45, 0.6, 0.8, 1.0):
            mapes, cums = [], []
            for y, m in months:
                r = backtest(y, m, field, smooth=sm, shrink=lam)
                mapes.append(r["model_mape_pct"]); cums.append(r["model_max_cum_err_pct"])
            out.append({"smooth": sm, "shrink": lam,
                        "mape": round(statistics.mean(mapes), 2),
                        "cum": round(statistics.mean(cums), 2),
                        "worst_mape": round(max(mapes), 2)})
    out.sort(key=lambda r: r["mape"])
    return out


if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "sweep":
        fld = sys.argv[2] if len(sys.argv) > 2 else "revenue"
        mths = [(2026, m) for m in range(2, 8)]
        base = [backtest(y, m, fld) for y, m in mths]
        print(f"flat baseline: mean MAPE "
              f"{statistics.mean(r['flat_mape_pct'] for r in base):.2f}%  "
              f"mean maxCum {statistics.mean(r['flat_max_cum_err_pct'] for r in base):.2f}%")
        for r in sweep(mths, fld)[:14]:
            print(f"  smooth={r['smooth']} shrink={r['shrink']:<4}  "
                  f"MAPE {r['mape']:>6.2f}%  maxCum {r['cum']:>6.2f}%  worstMAPE {r['worst_mape']:>6.2f}%")
    elif len(sys.argv) > 1 and sys.argv[1] == "backtest":
        y, m = (int(x) for x in sys.argv[2].split("-"))
        f = sys.argv[3] if len(sys.argv) > 3 else "revenue"
        r = backtest(y, m, f)
        print(f"Backtest {r['month']} · {r['field']} · prior years {r['prior_years']}")
        print(f"  daily MAPE   model {r['model_mape_pct']:.2f}%   flat {r['flat_mape_pct']:.2f}%")
        print(f"  max cum err  model {r['model_max_cum_err_pct']:.2f}%   flat {r['flat_max_cum_err_pct']:.2f}%")
        for d in r["daily"]:
            print(f"   {d['day']:>2}  actual {d['actual']:>10,}  target {d['target']:>10,}  flat {d['flat']:>10,}")
    else:
        today = datetime.date.today()
        print(json.dumps(build_payload(today.year, today.month), indent=2)[:4000])
