#!/usr/bin/env python3
"""
Internal search snapshot — GA4 site search in the Bluebird warehouse (cross-project) → Firestore.

Writes the documents the "Internal search" tab reads (GET /api/search). Runs
inside the ecom-refresh job (refresh_ecom.main calls run() after its own
documents) and shares its BigQuery plumbing, market buckets and windows, so the
two tabs can never disagree on what "SE, last 28 days" means.

PRESETS
    markets  all + the E-com grid (SE / NO / DK / FI + every market with >= 1 %
             of sessions, see refresh_ecom.discover_grid)
    periods  7 / 28 / 90 days

  3 × (1 + grid) documents:  funnel_cache/<ws>__search__<market>_<days>

COUNTRIES
  The mart's `market` only resolves the configured markets; every other country
  is 'unknown'. A `country` column (ISO2 from GA4 countryId) is being added
  upstream. Probed like search_zero_results: present → by_country fills in.

TRACKING START
  view_search_results was effectively untracked until GTM version 274 went live
  on 2026-10-27 (~10 events a day before, ~3 800 on the first day after). Any
  window, or comparison window, that starts before SEARCH_TRACKING_START would
  compare tracking against no tracking, so:
    • meta.tracked_from is the later of the window start and the tracking start;
    • the previous period is shipped as null (not 0) unless it lies entirely
      after the tracking start — meta.prev_available says which.

ZERO RESULTS
  The GTM tag also sends search_zero_results (yes / no / unknown) and
  search_results_count. The warehouse's GA4 connector does not import them yet
  (henkkaBB/agents PR pending). This module probes the mart for a
  `search_zero_results` column on every run: absent → meta.zero_available is
  false and every zero/unknown field is null; present → they fill in with no
  code change here.

SOURCES (claude-private-499703)
  babyshop_marts.agg_daily_on_site_search     date × market × term → event_count
  babyshop_marts.agg_daily_kpis_by_channel    GA4 sessions (search rate denominator)
  babyshop_marts.agg_daily_kpis_by_brand      brand names (brand-demand matching)

The search mart has no client_site column, so searches cover every storefront
while sessions are the discovered Babyshop site's (refresh_ecom.discover_site);
lekmer had 0 GA4 sessions when this was written.

Run locally (same two-account split as refresh_ecom.py):

  META_AUTH=gcloud CLOUDSDK_CORE_ACCOUNT=patrik@kuvio.io \\
  META_BQ_BILLING_PROJECT=claude-private-499703 \\
      python3 refresh_search.py --dry-run --out /tmp/search.json
  SEARCH_IN=/tmp/search.json python3 refresh_search.py
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import time

import refresh_ecom as _e

COLLECTION = _e.COLLECTION
WORKSPACE  = _e.WORKSPACE
DOC_PREFIX = "search"
TTL        = _e.TTL

MARKETS        = _e.MARKETS
MARKET_OPTIONS = _e.MARKET_OPTIONS
PERIOD_OPTIONS = _e.PERIOD_OPTIONS
DEFAULT_MARKET = _e.DEFAULT_MARKET
DEFAULT_DAYS   = _e.DEFAULT_DAYS
OTHER          = _e.OTHER

TRACKING_START = datetime.date.fromisoformat(
    os.environ.get("SEARCH_TRACKING_START", "2026-10-27"))

TERMS_SCAN  = 5000   # terms per (window, market) pulled for brand matching
TERMS_TOP   = 400    # terms per document
BRANDS_TOP  = 100
BRAND_LOOKBACK_DAYS = 120
BRAND_MIN_VIEWS     = 50
BRAND_MIN_LEN       = 3   # shorter brand names match too many unrelated words

# The run's market grid (refresh_ecom.discover_grid), set by build_all before
# the fetch; every query bucketing by market passes it as @grid.
_GRID: list[str] = list(MARKETS)

T_SEARCH  = _e.T_SEARCH
T_CHANNEL = _e.T_CHANNEL
T_BRAND   = _e.T_BRAND

q = _e.q


# ── Queries ──────────────────────────────────────────────────────────────────
def zero_available(stats) -> bool:
    """True once the connector imports search_zero_results into the mart."""
    try:
        q(f"select search_zero_results from {T_SEARCH} t where false", {}, stats)
        return True
    except Exception:
        return False


def country_available(stats) -> bool:
    """True once the connector adds the GA4 country to the mart."""
    try:
        q(f"select country from {T_SEARCH} t where false", {}, stats)
        return True
    except Exception:
        return False


def q_countries(windows, stats):
    """Searches per (window, market bucket incl. 'all', country)."""
    win, p = _e._win_sql(windows)
    return q(f"""
    with win as ({win}),
    a as (
      select w.days as days, {_e._mkt_sql()} as m,
             coalesce(nullif(upper(t.country), ''), 'UNKNOWN') as k,
             sum(if(t.date between w.cf and w.ct, t.event_count, 0)) as c,
             sum(if(t.date between w.pf and w.pt, t.event_count, 0)) as p
      from {T_SEARCH} t cross join win w
      where t.date between @lo and @hi and t.date between w.pf and w.ct
      group by 1, 2, 3)
    select days, m, k, c, p from a where m != '{OTHER}'
    union all
    select days, 'all', k, sum(c), sum(p) from a group by days, k""",
             dict(p, grid=_GRID), stats)


def _zero_cols(zero: bool, cond: str) -> str:
    if not zero:
        return "cast(null as int64)", "cast(null as int64)"
    return (f"sum(if({cond} and t.search_zero_results = 'yes', t.event_count, 0))",
            f"sum(if({cond} and t.search_zero_results = 'unknown', t.event_count, 0))")


def q_terms(windows, zero, stats):
    """Top terms per (window, market bucket) plus 'all' with a per-market split."""
    win, p = _e._win_sql(windows)
    p.update({"n": TERMS_SCAN, "grid": _GRID})
    cz, cu = _zero_cols(zero, "t.date between w.cf and w.ct")
    return q(f"""
    with win as ({win}),
    a as (
      select w.days as days, {_e._mkt_sql()} as m, t.search_term as k,
             sum(if(t.date between w.cf and w.ct, t.event_count, 0)) as c,
             sum(if(t.date between w.pf and w.pt, t.event_count, 0)) as p,
             {cz} as cz, {cu} as cu
      from {T_SEARCH} t cross join win w
      where t.date between @lo and @hi and t.date between w.pf and w.ct
        and t.search_term is not null and trim(t.search_term) != ''
      group by 1, 2, 3),
    x as (
      select days, m, k, c, p, cz, cu,
             cast(null as array<struct<mk string, n int64>>) as split
      from a where m != '{OTHER}'
      union all
      select days, 'all', k, sum(c), sum(p), sum(cz), sum(cu),
             array_agg(struct(m as mk, c as n) order by c desc)
      from a group by days, k)
    select * from x where c > 0
    qualify row_number() over (partition by days, m order by c desc, k) <= @n""",
             p, stats)


def q_totals(windows, zero, stats):
    """Searches and DISTINCT terms per window — distinct counts cannot be summed
    out of the term rows (they are capped) nor across markets, hence grouping sets."""
    win, p = _e._win_sql(windows)
    z = "t.search_zero_results" if zero else "cast(null as string)"
    cz, cu = (("sum(if(cur and z = 'yes', ev, 0))", "sum(if(cur and z = 'unknown', ev, 0))")
              if zero else ("cast(null as int64)", "cast(null as int64)"))
    return q(f"""
    with win as ({win}),
    b as (
      select w.days as days, {_e._mkt_sql()} as m, t.search_term as k,
             t.event_count as ev, {z} as z,
             t.date between w.cf and w.ct as cur, t.date between w.pf and w.pt as prv
      from {T_SEARCH} t cross join win w
      where t.date between @lo and @hi and t.date between w.pf and w.ct)
    select days, m as m_raw, grouping(m) = 1 as is_all,
           sum(if(cur, ev, 0)) as c, sum(if(prv, ev, 0)) as p,
           count(distinct if(cur, k, null)) as c_terms,
           count(distinct if(prv, k, null)) as p_terms,
           {cz} as cz, {cu} as cu
    from b
    group by grouping sets ((days, m), (days))""", dict(p, grid=_GRID), stats)


def q_daily(lo, hi, zero, stats):
    cz, cu = _zero_cols(zero, "true")
    return q(f"""
    select t.date as date, {_e._mkt_sql()} as m, sum(t.event_count) as searches,
           {cz} as zero, {cu} as unknown
    from {T_SEARCH} t
    where t.date between @lo and @hi
    group by 1, 2""", {"lo": lo, "hi": hi, "grid": _GRID}, stats)


def q_sessions(lo, hi, site, stats):
    p = {"lo": lo, "hi": hi, "grid": _GRID}
    if site:
        p["site"] = site
    return q(f"""
    select t.date as date, {_e._mkt_sql()} as m, sum(t.ga4_sessions) as sessions
    from {T_CHANNEL} t
    where t.date between @lo and @hi{_e._site_sql(site)}
    group by 1, 2""", p, stats)


def q_brands(hi, stats):
    return q(f"""
    select lower(trim(t.item_brand)) as b
    from {T_BRAND} t
    where t.date between @lo and @hi and t.item_brand is not null{_e._not_fee_sql()}
    group by 1 having sum(t.items_viewed) >= @minv""",
             {"lo": hi - datetime.timedelta(days=BRAND_LOOKBACK_DAYS), "hi": hi,
              "minv": BRAND_MIN_VIEWS}, stats)


def fetch(windows: list[dict]) -> dict:
    """Every section is wrapped: a failure nulls THAT section and adds a note."""
    stats = _e._Stats()
    lo = min(w["pf"] for w in windows)
    hi = max(w["ct"] for w in windows)
    out: dict = {"notes": []}
    zero = zero_available(stats)
    out["zero"] = zero
    out["country"] = country_available(stats)
    try:
        site, _, notes = _e.discover_site(lo, hi, stats)
        out["notes"] += notes
    except Exception as e:
        _e._log_fail("search site discovery", e)
        site = None
    for name, fn in (("terms", lambda: q_terms(windows, zero, stats)),
                     ("totals", lambda: q_totals(windows, zero, stats)),
                     ("daily", lambda: q_daily(lo, hi, zero, stats)),
                     ("sessions", lambda: q_sessions(lo, hi, site, stats)),
                     ("brands", lambda: q_brands(hi, stats)),
                     ("countries", lambda: q_countries(windows, stats) if out["country"] else None)):
        try:
            out[name] = fn()
        except Exception as e:
            _e._log_fail(f"search {name}", e)
            out[name] = None
            out["notes"].append(f"The {name} query failed; that part of the tab is empty.")
    out.update({"bytes_processed": stats.bytes, "queries": stats.queries,
                "elapsed": round(time.time() - stats.t0, 1), "site": site})
    return out


# ── Brand matching ───────────────────────────────────────────────────────────
def brand_matcher(rows):
    """term → brand, when a known brand appears in it as whole words
    ("konges sløjd swim" → "konges sløjd"). Longest brand wins."""
    brands = sorted({r["b"] for r in rows or [] if r.get("b") and len(r["b"]) >= BRAND_MIN_LEN},
                    key=len, reverse=True)

    def match(term: str):
        t = f" {' '.join(str(term).lower().split())} "
        for b in brands:
            if f" {b} " in t:
                return b
        return None
    return match


# ── Build ────────────────────────────────────────────────────────────────────
def _n(v):
    return None if v is None else _e._num(v)


def _sum_days(rows, a, b, m, key):
    """Sum `key` over [a, b] for market m ('all' = every bucket). None when no row."""
    vals = [r.get(key) for r in rows or []
            if a <= _e._as_date(r["date"]) <= b and (m == "all" or r["m"] == m)]
    vals = [v for v in vals if v is not None]
    return _e._num(sum(vals)) if vals else None


def build_combo(data: dict, market: str, w: dict, generated_at: str, match) -> dict:
    days = w["days"]
    tracked_from = max(w["cf"], TRACKING_START)
    prev_ok = w["pf"] >= TRACKING_START
    zero = data["zero"]
    notes = list(data["notes"])
    if not zero:
        notes.append("Zero-result searches are tracked on the site but not yet imported "
                     "into the warehouse.")
    if w["cf"] < TRACKING_START:
        notes.append(f"Search tracking started {TRACKING_START.isoformat()}; earlier days "
                     "in this period have (almost) no searches.")

    def pv(v):
        return _n(v) if prev_ok else None

    tot = next((r for r in data.get("totals") or []
                if r["days"] == days and ((market == "all" and r["is_all"])
                                          or (not r["is_all"] and r["m_raw"] == market))), None)
    sessions = data.get("sessions")
    totals = {
        "cur": {"searches": _n(tot and tot["c"]), "terms": _n(tot and tot["c_terms"]),
                "zero": _n(tot and tot["cz"]), "unknown": _n(tot and tot["cu"]),
                "sessions": _sum_days(sessions, tracked_from, w["ct"], market, "sessions")},
        "prev": {"searches": pv(tot and tot["p"]), "terms": pv(tot and tot["p_terms"]),
                 "sessions": _sum_days(sessions, w["pf"], w["pt"], market, "sessions")
                 if prev_ok else None},
    }

    daily = []
    d = tracked_from
    while d <= w["ct"]:
        daily.append({"date": d.isoformat(),
                      "searches": _sum_days(data.get("daily"), d, d, market, "searches"),
                      "zero": _sum_days(data.get("daily"), d, d, market, "zero"),
                      "unknown": _sum_days(data.get("daily"), d, d, market, "unknown"),
                      "sessions": _sum_days(sessions, d, d, market, "sessions")})
        d += datetime.timedelta(days=1)

    by_market = []
    for m in (_GRID + [OTHER]) if market == "all" else [market]:
        r = next((x for x in data.get("totals") or []
                  if x["days"] == days and not x["is_all"] and x["m_raw"] == m), None)
        by_market.append({
            "market": m,
            "cur": {"searches": _n(r and r["c"]), "terms": _n(r and r["c_terms"]),
                    "zero": _n(r and r["cz"]),
                    "sessions": _sum_days(sessions, tracked_from, w["ct"], m, "sessions")},
            "prev": {"searches": pv(r and r["p"]),
                     "sessions": _sum_days(sessions, w["pf"], w["pt"], m, "sessions")
                     if prev_ok else None}})

    # null until the mart carries `country` (see COUNTRIES above).
    by_country = None
    if data.get("countries") is not None:
        by_country = sorted(
            ({"country": r["k"], "cur": {"searches": _n(r["c"])}, "prev": {"searches": pv(r["p"])}}
             for r in data["countries"] if r["days"] == days and r["m"] == market and r["c"]),
            key=lambda x: -(x["cur"]["searches"] or 0))

    rows = [r for r in data.get("terms") or [] if r["days"] == days and r["m"] == market]
    rows.sort(key=lambda r: (-(r["c"] or 0), r["k"]))
    terms, brands = [], {}
    for r in rows:
        b = match(r["k"])
        if b:
            agg = brands.setdefault(b, {"brand": b, "cur": 0, "prev": 0, "terms": 0})
            agg["cur"] += r["c"] or 0
            agg["prev"] += r["p"] or 0
            agg["terms"] += 1
        if len(terms) < TERMS_TOP:
            t = {"term": r["k"], "brand": b,
                 "cur": {"count": _n(r["c"]), "zero": _n(r["cz"]), "unknown": _n(r["cu"])},
                 "prev": {"count": pv(r["p"])}}
            if r.get("split"):
                t["markets"] = {s["mk"]: _n(s["n"]) for s in r["split"] if s["n"]}
            terms.append(t)
    brand_rows = sorted(brands.values(), key=lambda x: -x["cur"])[:BRANDS_TOP]
    for b in brand_rows:
        if not prev_ok:
            b["prev"] = None

    return {
        "meta": {"market": market, "days": days,
                 "cur": {"from": w["cf"].isoformat(), "to": w["ct"].isoformat()},
                 "prev": {"from": w["pf"].isoformat(), "to": w["pt"].isoformat()},
                 "tracking_start": TRACKING_START.isoformat(),
                 "tracked_from": tracked_from.isoformat(),
                 "prev_available": prev_ok, "zero_available": zero,
                 "country_available": bool(data.get("country")),
                 "market_options": ["all"] + list(_GRID),
                 "brand_matched_searches": sum(b["cur"] for b in brands.values()),
                 "terms_scanned": len(rows), "generated_at": generated_at,
                 "notes": notes},
        "totals": totals,
        "daily": daily,
        "by_market": by_market,
        "by_country": by_country,
        "terms": terms,
        "brands": brand_rows,
    }


def build_all() -> dict:
    end = _e.last_complete_day()
    windows = [_e.preset_window(d, end) for d in PERIOD_OPTIONS]
    generated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    global _GRID
    _GRID = _e.discover_grid(end, _e._Stats())
    # Count only tracked days: the pre-GTM days hold the old 404 /search?q= hits.
    data = fetch([dict(w, cf=max(w["cf"], TRACKING_START)) for w in windows])
    print(f"   search fetch through {end}: {data['queries']} queries, "
          f"{data['bytes_processed'] / 1e9:.2f} GB in {data['elapsed']}s, zero={data['zero']}; rows: "
          + ", ".join(f"{k} {len(data[k]) if data.get(k) is not None else 'FAILED'}"
                      for k in ("terms", "totals", "daily", "sessions", "brands")), flush=True)
    match = brand_matcher(data.get("brands"))
    return {"combos": {f"{m}_{w['days']}": build_combo(data, m, w, generated_at, match)
                       for m in ["all"] + _GRID for w in windows}}


# ── Write ────────────────────────────────────────────────────────────────────
def combo_key(market: str, days: int) -> str:
    return f"{DOC_PREFIX}__{market}_{days}"


def _fit(payload: dict) -> int:
    """Shorten the term list until the document fits; it is the only long list."""
    n = _e._payload_bytes(payload)
    while n > _e.DOC_BUDGET_BYTES and payload["terms"]:
        payload["terms"] = payload["terms"][: len(payload["terms"]) // 2]
        n = _e._payload_bytes(payload)
    return n


def write_firestore(combos: dict) -> dict:
    from google.cloud import firestore

    db = firestore.Client(project=_e.FIRESTORE_PROJECT, credentials=_e._rm._credentials())
    sizes = {}
    for combo, payload in combos.items():
        market, days = combo.rsplit("_", 1)
        key = combo_key(market, int(days))
        sizes[key] = _fit(payload)
        db.collection(COLLECTION).document(f"{WORKSPACE}__{key}").set({
            "data": payload, "fetched_at": firestore.SERVER_TIMESTAMP,
            "expires_at": time.time() + TTL, "ttl_seconds": TTL, "workspace": WORKSPACE})
    return sizes


def run(dry: bool = False, out: str | None = None, src: str | None = None) -> None:
    t0 = time.time()
    if src:
        with open(src, encoding="utf-8") as fh:
            bundle = json.load(fh)
        print(f"   loaded {src} — BigQuery skipped")
    else:
        bundle = build_all()
    combos = bundle["combos"]
    sizes = ({combo_key(*k.rsplit("_", 1)): _fit(p) for k, p in combos.items()} if dry
             else write_firestore(combos))
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(bundle, fh, ensure_ascii=False, default=_e._json_default)
        print(f"   wrote {out} ({os.path.getsize(out):,} bytes)")
    d = combos.get(f"{DEFAULT_MARKET}_{DEFAULT_DAYS}") or next(iter(combos.values()))
    c = d["totals"]["cur"]
    print(f"✓ Search refresh {'(dry run) ' if dry else ''}· {d['meta']['tracked_from']}.."
          f"{d['meta']['cur']['to']} · searches {c['searches']} / terms {c['terms']} / "
          f"sessions {c['sessions']} · zero_available={d['meta']['zero_available']} · "
          f"{len(sizes)} docs, {max(sizes.values()) if sizes else 0:,} B max · "
          f"{time.time() - t0:.1f}s", flush=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Internal search snapshot refresh")
    ap.add_argument("--dry-run", action="store_true", help="assemble only, write nothing")
    ap.add_argument("--out", help="also write the bundle as JSON to this path")
    args = ap.parse_args(argv)
    run(dry=args.dry_run or bool(os.environ.get("SKIP_FIRESTORE")), out=args.out,
        src=os.environ.get("SEARCH_IN"))


if __name__ == "__main__":
    main()
