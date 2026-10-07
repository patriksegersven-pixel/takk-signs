#!/usr/bin/env python3
"""
E-com Funnel snapshot — GA4 marts in the Bluebird warehouse (cross-project) → Firestore.

Writes the documents the "E-com Funnel" tab reads (GET /api/ecom). Modelled on
refresh_meta.py and sharing its credentials helper and BigQuery client: the
query JOB is billed to this dashboard's own project, the DATA lives in
claude-private-499703 (babyshop_marts / babyshop_staging), read cross-project.

PRESETS
    markets  all / SE / NO / DK / FI        periods  7 / 28 / 90 days

  15 documents:  funnel_cache/<ws>__ecom__<market>_<days>

  Each carries a current period, the equally long period before it and the
  same dates 364 days earlier (weekday-aligned YoY). The payload holds raw
  additive SUMS only — never a ratio. Every rate on the tab is computed by the
  page as ratio-of-sums, so a filter or a re-aggregation can never average an
  average. Missing data is null, never 0.

HOW MANY QUERIES
  A nightly run is ~11 queries for all 15 documents, not 15 × N:
    • three DAY-grain scans (sessions by channel, events by channel, item
      totals) over the widest window any preset needs — every preset's totals,
      daily series, by_market and by_channel are summed out of them in Python;
    • four KEYED scans (landing pages, brands, products + categories, on-site
      search). Their grain is too fine to pull per day (millions of
      product-days), so BigQuery aggregates them per window — but all three
      windows and all five market slices come back from ONE query per table
      (the windows are cross-joined as a parameter array);
    • one funnel-snapshot read and one client_site discovery.

PRODUCTS AND CATEGORIES (verified on real data 2026-10-07)
  GA4 item_id is useless as a product key on this property (view_item rows carry
  '(not set)', cart/purchase rows ~10 id schemes) and item_category is not
  implemented. Products are therefore keyed (market, item_name, item_brand) —
  names are localised per market — and the category comes from the Google
  Shopping feed: "<brand> <name>" is a WORD-PREFIX of the feed's product_title
  in the same market, which yields product_type_l1/l2/l3. Rows that match no
  title are "Uncategorised"; meta.category_coverage says how much matched.
  A custom range (the service's live path) is the same code with one window.

EVERY SECTION DEGRADES ON ITS OWN
  Each query is wrapped. A missing table, a renamed column or a 403 turns THAT
  section into null / [] and adds a line to meta.notes; it never fails the job
  and never blanks the rest of the tab.

SOURCES (claude-private-499703)
  babyshop_marts.agg_daily_kpis_by_channel            sessions / engagement / transactions / revenue
  babyshop_marts.agg_daily_events_by_session_channel  event counts (session-scoped channel)
  babyshop_marts.agg_daily_landing_page_sessions      landing-page sessions
  babyshop_marts.agg_daily_landing_page_events        landing-page add_to_cart / purchase events
  babyshop_marts.agg_daily_kpis_by_brand              brands + item totals (fees excluded)
  babyshop_staging.int_ga4_item_rows                  products, keyed (market, name, brand)
  babyshop_marts.agg_daily_kpis_by_gads_product       Shopping-feed product types → category
  babyshop_marts.agg_daily_on_site_search             search terms
  babyshop_marts.agg_funnel_snapshots                 USER-based closed funnel (not additive)

Run locally (the two halves authenticate as different accounts, exactly like
refresh_meta.py — see its header for why):

  # 1) query BigQuery as a kuvio user, print sizes, write nothing
  META_AUTH=gcloud CLOUDSDK_CORE_ACCOUNT=patrik@kuvio.io \\
  META_BQ_BILLING_PROJECT=claude-private-499703 \\
      python3 refresh_ecom.py --dry-run --out /tmp/ecom.json

  # 2) replay that bundle into Firestore as the legacy-project account
  ECOM_IN=/tmp/ecom.json python3 refresh_ecom.py

  # one custom range, printed (what GET /api/ecom?from=&to= computes)
  python3 refresh_ecom.py --dry-run --custom SE 2026-09-01 2026-09-14
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from google.cloud import bigquery

# Credentials + the BigQuery client are refresh_meta's, deliberately: one ADC /
# gcloud-token fallback, one client per process, one set of env knobs
# (META_AUTH, META_LIVE_AUTH, META_BQ_BILLING_PROJECT, META_BQ_LOCATION).
import refresh_meta as _rm

DATA_PROJECT    = os.environ.get("ECOM_DATA_PROJECT", _rm.DATA_PROJECT)
MARTS_DATASET   = os.environ.get("ECOM_MARTS_DATASET", "babyshop_marts")
STAGING_DATASET = os.environ.get("ECOM_STAGING_DATASET", "babyshop_staging")

WORKSPACE  = os.environ.get("FUNNEL_WORKSPACE", "-Ln87GcdqU9CMJV6zMBY")
COLLECTION = "funnel_cache"
DOC_PREFIX = "ecom"
TTL        = 30 * 24 * 3600
FIRESTORE_PROJECT = os.environ.get("FIRESTORE_PROJECT", "project-a7ade44e-e7e3-4871-a83")
DOC_BUDGET_BYTES = 900_000

# ── The filter grid ──────────────────────────────────────────────────────────
MARKETS        = ["SE", "NO", "DK", "FI"]
MARKET_OPTIONS = ["all"] + MARKETS
PERIOD_OPTIONS = [7, 28, 90]
DEFAULT_MARKET = "all"
DEFAULT_DAYS   = 28
OTHER          = "Other"          # every warehouse market that is not one of MARKETS

# GA4 keeps revising the trailing ~72 h. The window still ends yesterday (the
# last COMPLETE day); the page greys the restated tail.
RESTATED_DAYS  = 3
YOY_SHIFT_DAYS = 364              # 52 weeks: keeps the weekday alignment
TZ             = "Europe/Stockholm"

# ── Custom ranges ────────────────────────────────────────────────────────────
# Bounds a live [from, to] must satisfy; they live here so the API and the job
# cannot state different limits. DATA_START is a floor on what may be ASKED
# for, not a claim about coverage — a range older than the GA4 backfill simply
# returns nulls (the item marts reach back ~425 days).
DATA_START    = os.environ.get("ECOM_DATA_START", "2024-01-01")
MAX_SPAN_DAYS = int(os.environ.get("ECOM_MAX_SPAN_DAYS", "366"))

# ── client_site ──────────────────────────────────────────────────────────────
# The warehouse's second spatial axis (bb-marts macros/client_site.sql): one
# client slug can hold several storefronts. THE one constant that pins this tab
# to Babyshop. Empty = discover at run time: a single value in the data is used
# as is; several → the one matching "babyshop", else the largest by sessions.
# Every run logs what it found, so a wrong pick is visible in the job log.
# NOTE: the item marts (by_product / by_brand / by_category) and the search mart
# carry NO client_site column — when more than one site exists they cannot be
# filtered and meta.notes says so.
CLIENT_SITE = os.environ.get("ECOM_CLIENT_SITE", "")

# ── List caps (the contract's) ───────────────────────────────────────────────
LANDING_TOP        = 150
BRANDS_MAX         = 400
BRANDS_MIN_VIEWS   = 50
CATEGORIES_MAX     = 200
SUBCATEGORIES_MAX  = 300
PRODUCTS_TOP       = 800
BRAND_MARKET_TOP   = 60
SEARCH_TOP         = 100
TYPE_MAP_DAYS      = 120          # Shopping-feed look-back for the title → type map
UNCATEGORISED      = "Uncategorised"
REST_OF_WORLD      = "Rest of world"   # warehouse market 'unknown': countries outside the map
# On-site search counts as tracked when view_search_results events reach this
# share of sessions (measured 2026-10: ~0.01 %, i.e. effectively untracked).
SEARCH_TRACKED_MIN = 0.005
# Non-merchandise GA4 "items" (the order's shipping-fee line). Excluded from
# products, brands, categories and the item totals. Lower-cased brand values.
FEE_BRANDS         = ("order",)
FEE_NAME_RX        = r"^\s*(shipping fee|handling fee|gift wrap)"

# ── Metric sets (the contract's S / E / I) ───────────────────────────────────
S_KEYS = ["sessions", "engaged_sessions", "engaged_denom", "new_users",
          "page_views", "transactions", "revenue", "add_to_carts"]
E_KEYS = ["view_item", "add_to_cart", "begin_checkout", "add_shipping_info",
          "add_payment_info", "purchase", "view_search_results"]
I_KEYS = ["items_viewed", "items_added_to_cart", "items_purchased", "item_revenue"]
DAILY_KEYS = ["sessions", "engaged_sessions", "engaged_denom", "transactions",
              "revenue", "page_views", "new_users",
              "view_item", "add_to_cart", "begin_checkout", "purchase",
              "items_viewed", "items_added_to_cart", "items_purchased"]
LANDING_CUR  = ["sessions", "engaged_sessions", "engaged_denom", "page_views",
                "add_to_cart", "purchase"]
LANDING_PREV = ["sessions", "engaged_sessions", "engaged_denom"]
FUNNEL_STEPS = ["session_start", "view_item", "add_to_cart", "begin_checkout", "purchase"]
FUNNEL_BREAKDOWNS = ["deviceCategory", "newVsReturning", "sessionDefaultChannelGroup"]
FUNNEL_KIND = {7: "d7", 28: "d30", 90: "d90"}
FUNNEL_KIND_DAYS = {"d7": 7, "d30": 30, "d90": 90}

# ── Tables ───────────────────────────────────────────────────────────────────
def _t(dataset: str, table: str) -> str:
    return f"`{DATA_PROJECT}.{dataset}.{table}`"

T_CHANNEL  = _t(MARTS_DATASET, "agg_daily_kpis_by_channel")
T_EVENTS   = _t(MARTS_DATASET, "agg_daily_events_by_session_channel")
T_LP_SESS  = _t(MARTS_DATASET, "agg_daily_landing_page_sessions")
T_LP_EVT   = _t(MARTS_DATASET, "agg_daily_landing_page_events")
T_BRAND    = _t(MARTS_DATASET, "agg_daily_kpis_by_brand")
T_GADS_PRODUCT = _t(MARTS_DATASET, "agg_daily_kpis_by_gads_product")
T_SEARCH   = _t(MARTS_DATASET, "agg_daily_on_site_search")
T_FUNNEL   = _t(MARTS_DATASET, "agg_funnel_snapshots")
T_ITEMROWS = _t(STAGING_DATASET, "int_ga4_item_rows")

# The market bucket, in SQL and in Python — must agree.
_MKT_LIST = ", ".join(f"'{m}'" for m in MARKETS)
def _mkt_sql(alias: str = "t") -> str:
    return (f"case when upper({alias}.market) in ({_MKT_LIST}) "
            f"then upper({alias}.market) else '{OTHER}' end")

def market_bucket(raw) -> str:
    m = str(raw or "").strip().upper()
    return m if m in MARKETS else OTHER


def market_key(raw) -> str:
    """by_market / products label for a RAW warehouse market."""
    m = str(raw or "").strip()
    if m.lower() in ("unknown", "default", ""):
        return REST_OF_WORLD
    return m.upper()


_FEE_BRAND_LIST = ", ".join(f"'{b}'" for b in FEE_BRANDS)
def _not_fee_sql(alias: str = "t", with_name: bool = False) -> str:
    sql = f" and lower(trim(coalesce({alias}.item_brand, ''))) not in ({_FEE_BRAND_LIST})"
    if with_name:
        sql += (f" and not regexp_contains(lower(coalesce({alias}.item_name, '')), "
                f"r'{FEE_NAME_RX}')")
    return sql

# ── Landing-page types ───────────────────────────────────────────────────────
# ONE ordered rule list, rendered into the SQL CASE and used by classify_page()
# — first match wins. Patterns are RE2 ∩ Python-re, matched against the mart's
# normalised path (lower-cased, trailing slash stripped, bare "/" kept).
#
# Written against the real top 6 000 landing pages (2026-09-09..10-06): every
# storefront page lives under a /<ll-cc> locale; a locale + 1–4 free slugs that
# is none of the named sections is a category listing (the slugs are localised:
# /sv-se/barnskor, /fi-fi/paallysvaatteet/haalarit, /ko-kr/의류).
# '(not set)' — sessions GA4 could not tie to a first pageview — is its own
# type, "not_set", so it never hides inside "other".
_LOC = r"^/[a-z]{2}-[a-z]{2}"
PAGE_TYPE_RULES: list[tuple[str, str]] = [
    ("home",     r"^(/[a-z]{2}-[a-z]{2})?/?$"),
    ("product",  _LOC + r"/p/"),
    ("brand",    _LOC + r"/brands?(/|$)"),
    ("search",   _LOC + r"/search(/|$)"),
    ("checkout", _LOC + r"/(checkout|checkout-validation|confirmation|cart)(/|$)"),
    ("campaign", _LOC + r"/(offers|shop-by|news|campaign|campaigns)(/|$)"),
    ("other",    _LOC + r"/(customer-service|favorites|information|guides-tips|countries|account|login|register|wishlist|sitemap)(/|$)"),
    ("category", _LOC + r"(/[^/]+){1,4}$"),
]
PAGE_TYPES = ["home", "category", "brand", "product", "campaign", "search", "checkout",
              "other", "not_set"]
_PAGE_RX = [(t, re.compile(p)) for t, p in PAGE_TYPE_RULES]


def classify_page(path) -> str:
    p = str(path or "")
    if p in ("(not set)", ""):
        return "not_set"
    if not p.startswith("/"):
        return "other"
    for t, rx in _PAGE_RX:
        if rx.search(p):
            return t
    return "other"


def _page_type_sql(col: str) -> str:
    arms = "\n".join(f"      when regexp_contains({col}, r'{p}') then '{t}'"
                     for t, p in PAGE_TYPE_RULES)
    return (f"case\n      when {col} is null or {col} in ('(not set)', '') then 'not_set'\n"
            f"      when not starts_with({col}, '/') then 'other'\n"
            f"{arms}\n      else 'other' end")


# ── BigQuery plumbing ────────────────────────────────────────────────────────
class _Stats:
    """Bytes / seconds / query count for ONE fetch, shared by its worker threads."""
    def __init__(self):
        self.bytes = 0
        self.queries = 0
        self.t0 = time.time()
        self._lock = threading.Lock()

    def add(self, n):
        with self._lock:
            self.bytes += int(n or 0)
            self.queries += 1


def _param(name: str, value):
    if isinstance(value, (list, tuple)):
        return bigquery.ArrayQueryParameter(name, "STRING", [str(v) for v in value])
    if isinstance(value, datetime.date):
        return bigquery.ScalarQueryParameter(name, "DATE", value)
    if isinstance(value, bool):
        return bigquery.ScalarQueryParameter(name, "BOOL", value)
    if isinstance(value, int):
        return bigquery.ScalarQueryParameter(name, "INT64", value)
    return bigquery.ScalarQueryParameter(name, "STRING", value)


def q(sql: str, params: dict, stats: _Stats | None = None) -> list[dict]:
    """One parameterised query. Tests replace this function wholesale."""
    cfg = bigquery.QueryJobConfig(
        query_parameters=[_param(k, v) for k, v in params.items()])
    job = _rm.bq().query(sql, job_config=cfg)
    rows = [dict(r) for r in job.result()]
    if stats is not None:
        stats.add(job.total_bytes_processed)
    return rows


def _num(v):
    """BigQuery scalar → JSON number. None stays None (missing ≠ 0)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, int):
        return v
    f = float(v)                             # Decimal / float
    return int(f) if f == int(f) else round(f, 2)


def _as_date(v) -> datetime.date:
    if isinstance(v, datetime.datetime):
        return v.date()
    return v if isinstance(v, datetime.date) else datetime.date.fromisoformat(str(v))


def _today() -> datetime.date:
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo(TZ)).date()
    except Exception:
        return datetime.date.today()


def last_complete_day() -> datetime.date:
    """Yesterday (Stockholm). Pin with ECOM_END_DATE=YYYY-MM-DD to reproduce a run."""
    end = os.environ.get("ECOM_END_DATE")
    return (datetime.date.fromisoformat(end) if end
            else _today() - datetime.timedelta(days=1))


def _window(start: datetime.date, end: datetime.date) -> dict:
    span = (end - start).days + 1
    return {"days": span, "cf": start, "ct": end,
            "pf": start - datetime.timedelta(days=span),
            "pt": start - datetime.timedelta(days=1)}


def preset_window(days: int, end: datetime.date | None = None) -> dict:
    end = end or last_complete_day()
    return _window(end - datetime.timedelta(days=days - 1), end)


def _win_sql(windows: list[dict]) -> tuple[str, dict]:
    """The windows as a parameter array BigQuery cross-joins against, plus the
    explicit [lo, hi] every keyed scan ALSO filters on — the join predicate on
    its own does not prune partitions."""
    structs, params = [], {}
    for i, w in enumerate(windows):
        structs.append(f"struct(@d{i} as days, @cf{i} as cf, @ct{i} as ct, "
                       f"@pf{i} as pf, @pt{i} as pt)")
        params.update({f"d{i}": int(w["days"]), f"cf{i}": w["cf"], f"ct{i}": w["ct"],
                       f"pf{i}": w["pf"], f"pt{i}": w["pt"]})
    params["lo"] = min(w["pf"] for w in windows)
    params["hi"] = max(w["ct"] for w in windows)
    return "select * from unnest([" + ", ".join(structs) + "])", params


def _site_sql(site, alias: str = "t") -> str:
    return f" and {alias}.client_site = @site" if site else ""


# ── Queries ──────────────────────────────────────────────────────────────────
def discover_site(lo, hi, stats) -> tuple[str | None, list[str], list[str]]:
    """(site to filter on | None, all sites seen, notes)."""
    rows = q(f"""
        select t.client_site as client_site, sum(t.ga4_sessions) as sessions
        from {T_CHANNEL} t
        where t.date between @lo and @hi
        group by 1 order by 2 desc""", {"lo": lo, "hi": hi}, stats)
    sites = [(str(r["client_site"]), int(r["sessions"] or 0)) for r in rows
             if r.get("client_site") is not None]
    print("   client_site values (sessions): "
          + (", ".join(f"{s}={n:,}" for s, n in sites) or "none"), flush=True)
    names = [s for s, _ in sites]
    active = [s for s, n in sites if n > 0]
    notes: list[str] = []
    if CLIENT_SITE:
        if names and CLIENT_SITE not in names:
            notes.append(f"Configured site '{CLIENT_SITE}' has no GA4 sessions in the "
                         "scanned range.")
        return CLIENT_SITE, names, notes
    if len(names) <= 1:
        return (names[0] if names else None), names, notes
    pick = next((s for s in names if "babyshop" in s.lower()), names[0])
    # Only worth a note when ANOTHER storefront actually has traffic (lekmer is
    # configured but had 0 sessions when this was written).
    if len(active) > 1:
        notes.append(f"Several storefronts have GA4 traffic ({', '.join(active)}); all "
                     f"figures are '{pick}' only, except brands and on-site search, "
                     "which cannot be split by storefront and include all of them.")
    return pick, names, notes


def _ranges_sql(ranges: list[tuple], alias: str = "t") -> tuple[str, dict]:
    parts, params = [], {}
    for i, (a, b) in enumerate(ranges):
        parts.append(f"({alias}.date between @r{i}a and @r{i}b)")
        params[f"r{i}a"], params[f"r{i}b"] = a, b
    return "(" + " or ".join(parts) + ")", params


def q_sessions(ranges, site, stats):
    where, p = _ranges_sql(ranges)
    if site:
        p["site"] = site
    return q(f"""
        select t.date as date, t.market as market, t.channel as ch,
               any_value(t.channel_display)     as label,
               sum(t.ga4_sessions)              as sessions,
               sum(t.engaged_sessions)          as engaged_sessions,
               sum(t.engaged_sessions_denom)    as engaged_denom,
               sum(t.new_users)                 as new_users,
               sum(t.page_views)                as page_views,
               sum(t.ga4_transactions)          as transactions,
               sum(t.ga4_revenue)               as revenue,
               sum(t.ga4_add_to_carts)          as add_to_carts
        from {T_CHANNEL} t
        where {where}{_site_sql(site)}
        group by 1, 2, 3""", p, stats)


def q_events(ranges, site, stats):
    where, p = _ranges_sql(ranges)
    if site:
        p["site"] = site
    pivots = ",\n               ".join(
        f"sum(if(t.event_name = '{e}', t.event_count, null)) as {e}" for e in E_KEYS)
    names = ", ".join(f"'{e}'" for e in E_KEYS)
    return q(f"""
        select t.date as date, t.market as market, t.traffic_source as ch,
               any_value(t.channel_display) as label,
               {pivots}
        from {T_EVENTS} t
        where {where} and t.event_name in ({names}){_site_sql(site)}
        group by 1, 2, 3""", p, stats)


def q_items_daily(ranges, stats):
    """Item totals per day and market, fee lines excluded. Read from the BRAND
    mart because it is the cheapest table that still carries the one column
    (item_brand) the fee exclusion needs."""
    where, p = _ranges_sql(ranges)
    return q(f"""
        select t.date as date, t.market as market,
               sum(t.items_viewed)        as items_viewed,
               sum(t.items_added_to_cart) as items_added_to_cart,
               sum(t.items_purchased)     as items_purchased,
               sum(t.item_revenue)        as item_revenue
        from {T_BRAND} t
        where {where}{_not_fee_sql()}
        group by 1, 2""", p, stats)


_LP_C = ["c_sessions", "c_engaged_sessions", "c_engaged_denom", "c_page_views",
         "c_add_to_cart", "c_purchase", "p_sessions", "p_engaged_sessions",
         "p_engaged_denom"]


def q_landing(windows, site, only, stats, with_events=True):
    win, p = _win_sql(windows)
    p.update({"only": only, "n": LANDING_TOP})
    if site:
        p["site"] = site
    cols = ", ".join(_LP_C)
    sums = ", ".join(f"sum({c}) as {c}" for c in _LP_C)
    if with_events:
        e_cte = f"""
    e as (
      select w.days as days, {_mkt_sql()} as m, t.landing_page as k,
             sum(if(t.event_name = 'add_to_cart', t.event_count, null)) as c_add_to_cart,
             sum(if(t.event_name = 'purchase', t.event_count, null))    as c_purchase
      from {T_LP_EVT} t cross join win w
      where t.date between @lo and @hi and t.date between w.cf and w.ct
        and t.event_name in ('add_to_cart', 'purchase'){_site_sql(site)}
      group by 1, 2, 3),
    j as (
      select s.days, s.m, s.k, s.c_sessions, s.c_engaged_sessions, s.c_engaged_denom,
             s.c_page_views, e.c_add_to_cart, e.c_purchase,
             s.p_sessions, s.p_engaged_sessions, s.p_engaged_denom
      from s left join e using (days, m, k)),"""
    else:
        e_cte = """
    j as (
      select s.days, s.m, s.k, s.c_sessions, s.c_engaged_sessions, s.c_engaged_denom,
             s.c_page_views, cast(null as int64) as c_add_to_cart,
             cast(null as int64) as c_purchase,
             s.p_sessions, s.p_engaged_sessions, s.p_engaged_denom
      from s),"""
    return q(f"""
    with win as ({win}),
    s as (
      select w.days as days, {_mkt_sql()} as m, t.landing_page as k,
             sum(if(t.date between w.cf and w.ct, t.sessions, null))               as c_sessions,
             sum(if(t.date between w.cf and w.ct, t.engaged_sessions, null))       as c_engaged_sessions,
             sum(if(t.date between w.cf and w.ct, t.engaged_sessions_denom, null)) as c_engaged_denom,
             sum(if(t.date between w.cf and w.ct, t.page_views, null))             as c_page_views,
             sum(if(t.date between w.pf and w.pt, t.sessions, null))               as p_sessions,
             sum(if(t.date between w.pf and w.pt, t.engaged_sessions, null))       as p_engaged_sessions,
             sum(if(t.date between w.pf and w.pt, t.engaged_sessions_denom, null)) as p_engaged_denom
      from {T_LP_SESS} t cross join win w
      where t.date between @lo and @hi and t.date between w.pf and w.ct{_site_sql(site)}
      group by 1, 2, 3),{e_cte}
    x as (
      select days, m, k, {cols} from j where m != '{OTHER}'
      union all
      select days, 'all' as m, k, {sums} from j group by days, k),
    f as (
      select days, m, k, {_page_type_sql('k')} as pt, {cols}
      from x where (@only = '' or m = @only))
    select * from (
      select 'page' as kind, days, m, k, pt, {cols}
      from f where c_sessions > 0
      qualify row_number() over (partition by days, m order by c_sessions desc, k) <= @n)
    union all
    select 'type' as kind, days, m, pt as k, pt, {sums}
    from f group by days, m, pt""", p, stats)


_I_C = [f"{pre}_{k}" for pre in ("c", "p") for k in I_KEYS]


def q_items_keyed(table, key_col, windows, only, stats, *, top, min_views=0,
                  with_name=False, market_top=0):
    """Brands / categories / products: one row per (window, market slice, key).

    `market_top` > 0 also returns kind='bm' rows — the top N keys of the 'all'
    slice broken out per market (brand_by_market)."""
    win, p = _win_sql(windows)
    p.update({"only": only, "n": top, "minv": min_views})
    sel = ",\n             ".join(
        [f"sum(if(t.date between w.cf and w.ct, t.{k}, null)) as c_{k}" for k in I_KEYS]
        + [f"sum(if(t.date between w.pf and w.pt, t.{k}, null)) as p_{k}" for k in I_KEYS])
    cols = ", ".join(_I_C)
    sums = ", ".join(f"sum({c}) as {c}" for c in _I_C)
    name_a = "any_value(t.item_name) as name," if with_name else "cast(null as string) as name,"
    bm = ""
    if market_top:
        p["bmn"] = market_top
        bm = f"""
    union all
    select 'bm' as kind, a.days, a.m, a.k, a.name, {", ".join("a." + c for c in _I_C)}
    from a join (
      select days, k from x where m = 'all'
      qualify row_number() over (partition by days order by c_items_viewed desc, k) <= @bmn
    ) b using (days, k)
    where @only in ('', 'all') and a.m != '{OTHER}'"""
    return q(f"""
    with win as ({win}),
    a as (
      select w.days as days, {_mkt_sql()} as m,
             coalesce(nullif(cast(t.{key_col} as string), ''), '(not set)') as k,
             {name_a}
             {sel}
      from {table} t cross join win w
      where t.date between @lo and @hi and t.date between w.pf and w.ct{_not_fee_sql()}
      group by 1, 2, 3),
    x as (
      select days, m, k, name, {cols} from a where m != '{OTHER}'
      union all
      select days, 'all' as m, k, any_value(name) as name, {sums} from a group by days, k)
    select * from (
      select 'top' as kind, days, m, k, name, {cols}
      from x
      where (@only = '' or m = @only) and coalesce(c_items_viewed, 0) >= @minv
        and (c_items_viewed is not null or c_items_added_to_cart is not null
             or c_items_purchased is not null)
      qualify row_number() over (partition by days, m
                                 order by c_items_viewed desc, c_items_purchased desc, k) <= @n){bm}""",
             p, stats)


def q_search(windows, only, stats):
    win, p = _win_sql(windows)
    p.update({"only": only, "n": SEARCH_TOP})
    return q(f"""
    with win as ({win}),
    a as (
      select w.days as days, {_mkt_sql()} as m, t.search_term as k,
             sum(if(t.date between w.cf and w.ct, t.event_count, null)) as c_count,
             sum(if(t.date between w.pf and w.pt, t.event_count, null)) as p_count
      from {T_SEARCH} t cross join win w
      where t.date between @lo and @hi and t.date between w.pf and w.ct
      group by 1, 2, 3),
    x as (
      select days, m, k, c_count, p_count from a where m != '{OTHER}'
      union all
      select days, 'all' as m, k, sum(c_count) as c_count, sum(p_count) as p_count
      from a group by days, k)
    select days, m, k, c_count, p_count
    from x where (@only = '' or m = @only) and c_count > 0
    qualify row_number() over (partition by days, m order by c_count desc, k) <= @n""",
             p, stats)


def q_funnel(site, stats, today=None):
    """The newest few rolling windows of each kind plus the window one length
    earlier (prev_steps). Partition-filtered on window_end. Never summed across
    windows — build picks exactly one per kind."""
    today = today or _today()
    p = {"lo": today - datetime.timedelta(days=110), "hi": today,
         "recent": today - datetime.timedelta(days=6)}
    if site:
        p["site"] = site
    return q(f"""
        select t.window_kind as window_kind, t.window_start as window_start,
               t.window_end as window_end, t.market as market,
               t.scope_kind as scope_kind, t.scope_value as scope_value,
               t.breakdown as breakdown, t.breakdown_value as breakdown_value,
               t.step_index as step_index, t.step_name as step_name,
               sum(t.users) as users, logical_or(coalesce(t.sampled, false)) as sampled
        from {T_FUNNEL} t
        where t.window_end between @lo and @hi
          and t.window_kind in ('d7', 'd30', 'd90')
          and t.is_current_version
          and t.scope_kind in ('all', 'market'){_site_sql(site)}
          and (t.window_end >= @recent
               or t.window_end between
                    date_sub(@recent, interval cast(substr(t.window_kind, 2) as int64) day)
                    and date_sub(@hi, interval cast(substr(t.window_kind, 2) as int64) day))
        group by 1, 2, 3, 4, 5, 6, 7, 8, 9, 10""", p, stats)


_P_C = _I_C + ["c_v_noid", "c_v_nocat"]


def q_products(windows, site, only, stats):
    """Products keyed (market, brand, name) with their Shopping-feed type, plus
    the category roll-up of ALL products — one scan of the item rows.

      kind='prod'  top PRODUCTS_TOP per (window, market) by current views
      kind='cat'   every OTHER product, summed per (window, market, l1, l2);
                   l1 NULL = no feed match.
    A category total is therefore prod + cat rows of that (l1, l2). All rows
    carry the two tracking diagnostics: views on rows with no item_id, and
    views on rows with no real item_category.

    The type map is built once, from the last TYPE_MAP_DAYS of the feed: every
    2..14-word prefix of each product_title, per market, resolved to the type
    of the most-clicked title sharing it."""
    win, p = _win_sql(windows)
    hi = p["hi"]
    p.update({"only": only, "n": PRODUCTS_TOP, "thi": hi,
              "tlo": hi - datetime.timedelta(days=TYPE_MAP_DAYS)})
    if site:
        p["site"] = site
    sel = ",\n             ".join(
        [f"sum(if(t.date between w.cf and w.ct, t.{k}, null)) as c_{k}" for k in I_KEYS]
        + [f"sum(if(t.date between w.pf and w.pt, t.{k}, null)) as p_{k}" for k in I_KEYS])
    cols = ", ".join(_P_C)
    sums = ", ".join(f"sum({c}) as {c}" for c in _P_C)
    return q(f"""
    with win as ({win}),
    gt as (
      select upper(g.market) as market,
             regexp_replace(lower(trim(g.product_title)), r'\\s+', ' ') as t,
             any_value(g.product_type_l1) as l1, any_value(g.product_type_l2) as l2,
             any_value(g.product_type_l3) as l3, sum(g.clicks) as c
      from {T_GADS_PRODUCT} g
      where g.date between @tlo and @thi and g.product_type_l1 is not null
        and g.product_title is not null
        and (@only in ('', 'all') or upper(g.market) = @only)
      group by 1, 2),
    pre as (
      select market,
             array_to_string(array(select w from unnest(split(t, ' ')) w with offset o
                                   where o < n order by o), ' ') as p,
             l1, l2, l3, c
      from gt, unnest(generate_array(2, least(array_length(split(t, ' ')), 14))) n),
    mp as (
      select market, p,
             array_agg(struct(l1, l2, l3) order by c desc limit 1)[offset(0)] as ty
      from pre group by 1, 2),
    a as (
      select w.days as days, upper(t.market) as market,
             trim(coalesce(t.item_brand, '')) as brand,
             regexp_replace(trim(coalesce(t.item_name, '')), r'\\s+', ' ') as name,
             {sel},
             sum(if(t.date between w.cf and w.ct
                    and coalesce(t.item_id, '') in ('', '(not set)'), t.items_viewed, null)) as c_v_noid,
             sum(if(t.date between w.cf and w.ct
                    and lower(coalesce(t.item_category, '')) in ('', '(not set)', 'not implemented'),
                    t.items_viewed, null)) as c_v_nocat
      from {T_ITEMROWS} t cross join win w
      where t.date between @lo and @hi and t.date between w.pf and w.ct{_site_sql(site)}
        and (@only in ('', 'all') or upper(t.market) = @only){_not_fee_sql(with_name=True)}
      group by 1, 2, 3, 4),
    j as (
      select a.*, mp.ty.l1 as l1, mp.ty.l2 as l2, mp.ty.l3 as l3
      from a left join mp
        on mp.market = a.market and mp.p = lower(concat(a.brand, ' ', a.name))),
    ranked as (
      select j.*,
             row_number() over (partition by days, market
                                order by c_items_viewed desc, c_items_purchased desc,
                                         name, brand) <= @n
               and coalesce(c_items_viewed, 0) + coalesce(c_items_added_to_cart, 0)
                   + coalesce(c_items_purchased, 0) > 0 as is_top
      from j)
    -- ONE pass over the item rows (BigQuery re-runs a CTE per reference, and
    -- this view is the slow part): the top products keep their own row, every
    -- other product collapses into its (l1, l2) remainder row.
    select if(is_top, 'prod', 'cat') as kind, days, market,
           if(is_top, brand, null) as brand, if(is_top, name, null) as name,
           l1, l2, if(is_top, l3, null) as l3, {sums}
    from ranked
    group by 1, 2, 3, 4, 5, 6, 7, 8""", p, stats)


# ── The one fetch ────────────────────────────────────────────────────────────
def fetch(windows: list[dict], only: str = "") -> dict:
    """Everything BigQuery is asked for, for a set of windows.

    `only` = '' returns every market slice (the job); a market name restricts
    the keyed scans to that slice (the service's custom range). The day-grain
    scans always carry every market — by_market needs them regardless."""
    stats = _Stats()
    notes: list[str] = []
    ranges = []
    for w in windows:
        ranges.append((w["pf"], w["ct"]))
        ranges.append((w["pf"] - datetime.timedelta(days=YOY_SHIFT_DAYS),
                       w["ct"] - datetime.timedelta(days=YOY_SHIFT_DAYS)))
    # Collapse to at most two ranges: the union of the windows, and of their YoY twins.
    main = (min(r[0] for r in ranges[0::2]), max(r[1] for r in ranges[0::2]))
    yoy = (min(r[0] for r in ranges[1::2]), max(r[1] for r in ranges[1::2]))
    ranges = [main, yoy]

    site, sites = None, []
    try:
        site, sites, site_notes = discover_site(main[0], main[1], stats)
        notes += site_notes
    except Exception as e:
        _log_fail("client_site discovery", e)
        site = CLIENT_SITE or None
        notes.append("Could not check which storefronts the warehouse holds; "
                     + (f"filtering on '{site}'." if site else "no storefront filter applied."))

    def landing():
        try:
            return q_landing(windows, site, only, stats, with_events=True)
        except Exception as e:
            _log_fail("landing pages (with events)", e)
            rows = q_landing(windows, site, only, stats, with_events=False)
            notes.append("Landing-page add-to-cart and purchase events are unavailable; "
                         "sessions and engagement are shown.")
            return rows

    jobs = {
        "sessions":   (lambda: q_sessions(ranges, site, stats),
                       "Sessions, engagement, transactions and revenue"),
        "events":     (lambda: q_events(ranges, site, stats), "Event counts"),
        "items":      (lambda: q_items_daily(ranges, stats), "Item totals"),
        "landing":    (landing, "Landing pages"),
        "brands":     (lambda: q_items_keyed(T_BRAND, "item_brand", windows, only, stats,
                                             top=BRANDS_MAX, min_views=BRANDS_MIN_VIEWS,
                                             market_top=BRAND_MARKET_TOP), "Brands"),
        "products":   (lambda: q_products(windows, site, only, stats),
                       "Products and categories"),
        "search":     (lambda: q_search(windows, only, stats), "On-site search"),
        "funnel":     (lambda: q_funnel(site, stats), "The user funnel"),
    }
    out: dict = {}
    timings: dict = {}

    def run(name):
        fn, label = jobs[name]
        t1 = time.time()
        try:
            rows = fn()
            timings[name] = round(time.time() - t1, 1)
            return name, rows, None
        except Exception as e:
            _log_fail(name, e)
            return name, None, f"{label} unavailable ({type(e).__name__})."

    with ThreadPoolExecutor(max_workers=int(os.environ.get("ECOM_WORKERS", "6"))) as ex:
        for name, rows, note in ex.map(run, list(jobs)):
            out[name] = rows
            if note:
                notes.append(note)

    # Log what the warehouse calls its markets.
    seen: dict[str, int] = {}
    for r in out.get("sessions") or []:
        seen[str(r.get("market"))] = seen.get(str(r.get("market")), 0) + int(r.get("sessions") or 0)
    print("   market values (sessions): "
          + (", ".join(f"{m}={n:,}" for m, n in sorted(seen.items(), key=lambda x: -x[1]))
             or "none"), flush=True)

    print("   query seconds: " + ", ".join(f"{k} {v}" for k, v in timings.items()), flush=True)
    out.update({"site": site, "sites": sites, "notes": notes, "ranges": ranges,
                "bytes_processed": stats.bytes, "queries": stats.queries,
                "elapsed": round(time.time() - stats.t0, 2)})
    return out


def _log_fail(what: str, e: Exception) -> None:
    print(f"WARN ecom: {what} failed — {type(e).__name__}: {str(e)[:400]}", flush=True)


# ── Assembly ─────────────────────────────────────────────────────────────────
def _blank(keys) -> dict:
    return {k: None for k in keys}


def _acc(dst: dict, src: dict, keys) -> None:
    """Null-preserving add: a key stays None until some row carries a value."""
    for k in keys:
        v = src.get(k)
        if v is None:
            continue
        dst[k] = v if dst[k] is None else dst[k] + v


def _clean(d: dict) -> dict:
    return {k: _num(v) for k, v in d.items()}


def _has(d: dict) -> bool:
    return any(v is not None for v in d.values())


def _iso(d) -> str:
    return _as_date(d).isoformat()


def _dates(a: datetime.date, b: datetime.date) -> list[datetime.date]:
    return [a + datetime.timedelta(days=i) for i in range((b - a).days + 1)]


def _prefixed(row: dict, prefix: str, keys) -> dict:
    return {k: _num(row.get(f"{prefix}_{k}")) for k in keys}


def _build_day_sections(data: dict, market: str, w: dict) -> dict:
    """totals / daily / daily_yoy / by_market / by_channel out of the day scans."""
    shift = datetime.timedelta(days=YOY_SHIFT_DAYS)
    cf, ct, pf, pt = w["cf"], w["ct"], w["pf"], w["pt"]
    ycf, yct, ypf = cf - shift, ct - shift, pf - shift
    ALL = S_KEYS + E_KEYS + I_KEYS

    totals = {p: _blank(ALL) for p in ("cur", "prev", "yoy")}
    daily: dict = {}
    daily_y: dict = {}
    by_market: dict = {}
    by_channel: dict = {}
    labels: dict = {}
    present = set()

    def period(d):
        if cf <= d <= ct:
            return "cur"
        if pf <= d <= pt:
            return "prev"
        return None

    for src, keys, has_ch in (("sessions", S_KEYS, True), ("events", E_KEYS, True),
                              ("items", I_KEYS, False)):
        for r in data.get(src) or []:
            d = _as_date(r["date"])
            mk = str(r.get("market") or "").strip().upper()
            per = period(d)
            if per:
                # by_market ignores the market filter — it is the benchmark row set.
                slot = by_market.setdefault(mk, {"cur": _blank(ALL), "prev": _blank(ALL)})
                _acc(slot[per], r, keys)
                if src == "sessions" and r.get("sessions"):
                    present.add(mk)
            if market != "all" and mk != market:
                continue
            if per:
                _acc(totals[per], r, keys)
                _acc(daily.setdefault(d, _blank(ALL)), r, keys)
                if has_ch:
                    ch = str(r.get("ch") or "(unknown)")
                    if r.get("label"):
                        labels.setdefault(ch, str(r["label"]))
                    slot = by_channel.setdefault(
                        ch, {"cur": _blank(S_KEYS + E_KEYS), "prev": _blank(S_KEYS + E_KEYS)})
                    _acc(slot[per], r, keys)
            if ycf <= d <= yct:
                _acc(totals["yoy"], r, keys)
            if ypf <= d <= yct:
                _acc(daily_y.setdefault(d, _blank(ALL)), r, keys)

    def series(store, a, b):
        ds = _dates(a, b)
        out = {"date": [x.isoformat() for x in ds]}
        for k in DAILY_KEYS:
            out[k] = [_num((store.get(x) or {}).get(k)) for x in ds]
        return out

    # One row per RAW market that has sessions: the four selectable ones first,
    # then the rest by size, 'unknown' (every country outside the map) last.
    def _mk_sort(kv):
        k, v = kv
        if k in MARKETS:
            return (0, MARKETS.index(k), 0)
        return (2 if k == REST_OF_WORLD else 1, 0,
                -(v["cur"].get("sessions") or 0))
    merged: dict = {}
    for k, v in by_market.items():
        if not any(v[p].get("sessions") for p in ("cur", "prev")):
            continue
        slot = merged.setdefault(market_key(k), {"cur": _blank(ALL), "prev": _blank(ALL)})
        _acc(slot["cur"], v["cur"], ALL)
        _acc(slot["prev"], v["prev"], ALL)
    mk_rows = [{"key": k, "cur": _clean(v["cur"]), "prev": _clean(v["prev"])}
               for k, v in sorted(merged.items(), key=_mk_sort)]
    ch_rows = []
    for ch, v in by_channel.items():
        # kuvio / pending and other buckets with no GA4 activity carry zeros only.
        if not any(v[p].get(k) for p in ("cur", "prev") for k in S_KEYS + E_KEYS):
            continue
        label = labels.get(ch) or ch.replace("_", " ").title()
        ch_rows.append({"key": label, "id": ch, "label": label,
                        "cur": _clean(v["cur"]), "prev": _clean(v["prev"])})
    ch_rows.sort(key=lambda r: -(r["cur"].get("sessions") or 0))

    return {
        "totals": {"cur": _clean(totals["cur"]), "prev": _clean(totals["prev"]),
                   "yoy": _clean(totals["yoy"]) if _has(totals["yoy"]) else None},
        "daily": series(daily, pf, ct),
        "daily_yoy": series(daily_y, ypf, yct) if daily_y else None,
        "by_market": mk_rows,
        "by_channel": ch_rows,
        "_present": [m for m in MARKETS if m in present],
    }


def _keyed(rows, days: int, m: str, kind: str | None = None) -> list[dict]:
    return [r for r in rows or []
            if int(r["days"]) == days and r["m"] == m
            and (kind is None or r.get("kind") == kind)]


def _build_landing(data, market, days):
    rows = data.get("landing")
    if rows is None:
        return [], []
    pages = sorted(_keyed(rows, days, market, "page"),
                   key=lambda r: -(r.get("c_sessions") or 0))[:LANDING_TOP]
    by_landing = [{"key": r["k"], "page_type": r.get("pt") or classify_page(r["k"]),
                   "cur": _prefixed(r, "c", LANDING_CUR),
                   "prev": _prefixed(r, "p", LANDING_PREV)} for r in pages]
    types = {r["k"]: r for r in _keyed(rows, days, market, "type")}
    by_type = [{"key": t, "cur": _prefixed(types[t], "c", LANDING_CUR),
                "prev": _prefixed(types[t], "p", LANDING_PREV)}
               for t in PAGE_TYPES if t in types]
    by_type.sort(key=lambda r: -(r["cur"].get("sessions") or 0))
    return by_landing, by_type


def _build_items(rows, market, days, top):
    out = [{"key": r["k"], "cur": _prefixed(r, "c", I_KEYS),
            "prev": _prefixed(r, "p", I_KEYS)}
           for r in _keyed(rows, days, market, "top")]
    out.sort(key=lambda r: -(r["cur"].get("items_viewed") or 0))
    return out[:top]


def _prod_rows(data, days, market, kind):
    return [r for r in data.get("products") or []
            if int(r["days"]) == days and (kind is None or r.get("kind") == kind)
            and (market == "all" or str(r.get("market") or "").upper() == market)]


def _build_products(data, market, days):
    """Per-market rows keyed (market, name, brand). With market='all' the same
    product appears once per market language; the list is the top 800 overall."""
    out = [{"id": None, "name": r.get("name") or "(not set)",
            "brand": r.get("brand") or "(not set)",
            "market": market_key(r.get("market")),
            "category": r.get("l1"), "subcategory": r.get("l2"), "type": r.get("l3"),
            "cur": _prefixed(r, "c", I_KEYS), "prev": _prefixed(r, "p", I_KEYS)}
           for r in _prod_rows(data, days, market, "prod")]
    out.sort(key=lambda r: (-(r["cur"].get("items_viewed") or 0),
                            -(r["cur"].get("items_purchased") or 0), r["name"]))
    return out[:PRODUCTS_TOP]


def _build_categories(data, market, days):
    """(categories, subcategories, coverage, diagnostics) from the 'cat' roll-up,
    which covers EVERY product — not just the ones in the top-800 list."""
    rows = _prod_rows(data, days, market, None)        # prod + remainder rows
    cats: dict = {}
    subs: dict = {}
    diag = {"views": 0, "matched": 0, "noid": 0, "nocat": 0}
    for r in rows:
        l1 = r.get("l1") or UNCATEGORISED
        c = cats.setdefault(l1, {"cur": _blank(I_KEYS), "prev": _blank(I_KEYS)})
        cur = {k: r.get(f"c_{k}") for k in I_KEYS}
        prev = {k: r.get(f"p_{k}") for k in I_KEYS}
        _acc(c["cur"], cur, I_KEYS)
        _acc(c["prev"], prev, I_KEYS)
        if r.get("l1") and r.get("l2"):
            sc = subs.setdefault((r["l1"], r["l2"]),
                                 {"cur": _blank(I_KEYS), "prev": _blank(I_KEYS)})
            _acc(sc["cur"], cur, I_KEYS)
            _acc(sc["prev"], prev, I_KEYS)
        v = int(r.get("c_items_viewed") or 0)
        diag["views"] += v
        diag["matched"] += v if r.get("l1") else 0
        diag["noid"] += int(r.get("c_v_noid") or 0)
        diag["nocat"] += int(r.get("c_v_nocat") or 0)

    def live(v):
        return _has(v["cur"]) or _has(v["prev"])
    by_views = lambda r: -(r["cur"].get("items_viewed") or 0)   # noqa: E731
    categories = sorted(({"key": k, "cur": _clean(v["cur"]), "prev": _clean(v["prev"])}
                         for k, v in cats.items() if live(v)), key=by_views)[:CATEGORIES_MAX]
    subcategories = sorted(({"key": l2, "category": l1, "cur": _clean(v["cur"]),
                             "prev": _clean(v["prev"])}
                            for (l1, l2), v in subs.items() if live(v)),
                           key=by_views)[:SUBCATEGORIES_MAX]
    coverage = round(diag["matched"] / diag["views"], 4) if diag["views"] else None
    return categories, subcategories, coverage, diag


def _build_brand_market(data, market, days):
    if market != "all":
        return []
    rows = [r for r in data.get("brands") or []
            if int(r["days"]) == days and r.get("kind") == "bm"]
    out = [{"brand": r["k"], "market": r["m"], "cur": _prefixed(r, "c", I_KEYS)}
           for r in rows if r["m"] in MARKETS and _has(_prefixed(r, "c", I_KEYS))]
    out.sort(key=lambda r: (r["brand"], r["market"]))
    return out


def _build_search(data, market, days):
    # users: total_users is a per-day distinct count and cannot be summed across
    # days or markets, so it is shipped as null rather than as a wrong number.
    out = [{"term": r["k"], "cur": {"count": _num(r.get("c_count")), "users": None},
            "prev": {"count": _num(r.get("p_count"))}}
           for r in _keyed(data.get("search"), days, market)]
    out.sort(key=lambda r: -(r["cur"]["count"] or 0))
    return out[:SEARCH_TOP]


def _build_funnel(data, market: str, kind: str):
    """(funnel | None, funnel_window | None, notes). One window, never a sum of windows."""
    rows = data.get("funnel")
    if not rows:
        return None, None, []
    rows = [r for r in rows if r.get("window_kind") == kind]
    if market == "all":
        scoped = [r for r in rows if r.get("scope_kind") == "all"]
    else:
        # A country-split property carries per-market rows as scope 'market';
        # a one-property-per-market client carries them as that property's 'all'.
        scoped = [r for r in rows if r.get("scope_kind") == "market"
                  and market_bucket(r.get("market")) == market]
        if not scoped:
            scoped = [r for r in rows if r.get("scope_kind") == "all"
                      and market_bucket(r.get("market")) == market]
    if not scoped:
        return None, None, [f"No {kind} user funnel is available for this market."]

    wins: dict = {}
    for r in scoped:
        wkey = (_as_date(r["window_start"]), _as_date(r["window_end"]))
        cell = wins.setdefault(wkey, {}).setdefault(
            (r.get("breakdown") or "total", r.get("breakdown_value") or ""),
            {"steps": {}, "sampled": False, "markets": set()})
        nm = str(r.get("step_name") or "")
        cell["steps"][nm] = (cell["steps"].get(nm) or 0) + int(r.get("users") or 0)
        cell["sampled"] = cell["sampled"] or bool(r.get("sampled"))
        cell["markets"].add(str(r.get("market")))

    def total(wkey):
        c = (wins.get(wkey) or {}).get(("total", ""))
        if not c or not any(n in c["steps"] for n in FUNNEL_STEPS):
            return None
        return c

    with_total = sorted((k for k in wins if ("total", "") in wins[k]), key=lambda k: k[1])
    if not with_total:
        return None, None, [f"No {kind} user funnel is available for this market."]
    cur_key = with_total[-1]
    cur = total(cur_key)
    if cur is None:
        return None, None, [f"No {kind} user funnel is available for this market."]
    notes = []
    if cur["sampled"]:
        notes.append(f"The {FUNNEL_KIND_DAYS[kind]}-day user funnel is SAMPLED by GA4 "
                     "(an estimate from a subset of events); the 7-day funnel is exact.")
    if len(cur["markets"]) > 1:
        notes.append("The all-markets user funnel is the sum of per-market funnels; a "
                     "shopper active in two markets is counted in both.")
    n = FUNNEL_KIND_DAYS[kind]
    prev_key = next((k for k in wins
                     if k[1] == cur_key[0] - datetime.timedelta(days=1)
                     and (k[1] - k[0]).days + 1 == n), None)
    prev = total(prev_key) if prev_key else None

    def steps(c):
        return [{"name": s, "users": c["steps"].get(s)} for s in FUNNEL_STEPS]

    breakdowns: dict = {}
    bd_sampled: dict = {}
    for (bd, val), c in wins[cur_key].items():
        if bd == "total" or bd not in FUNNEL_BREAKDOWNS:
            continue
        bd_sampled[bd] = bd_sampled.get(bd, False) or c["sampled"]
        breakdowns.setdefault(bd, []).append(
            {"key": val or "(not set)", "steps": [c["steps"].get(s) for s in FUNNEL_STEPS]})
    for bd in breakdowns:
        breakdowns[bd].sort(key=lambda r: -(r["steps"][0] or 0))

    # Sampling is reported, never a reason to drop: on this property every
    # 30- and 90-day window is sampled and only the 7-day one is exact.
    funnel = {"steps": steps(cur), "prev_steps": steps(prev) if prev else None,
              "sampled": bool(cur["sampled"]),
              "prev_sampled": bool(prev["sampled"]) if prev else None,
              "breakdowns": breakdowns, "breakdowns_sampled": bd_sampled}
    fw = {"kind": kind, "from": cur_key[0].isoformat(), "to": cur_key[1].isoformat()}
    return funnel, fw, notes


def build_combo(data: dict, market: str, w: dict, *, days: int | None = None,
                custom: bool = False, generated_at: str | None = None) -> dict:
    """One payload — the contract's shape, for a preset or a custom range."""
    wdays = int(w["days"])
    notes = list(data.get("notes") or [])
    day = _build_day_sections(data, market, w)
    present = day.pop("_present")

    data_through = w["ct"]
    sess_dates = [_as_date(r["date"]) for r in data.get("sessions") or [] if r.get("sessions")]
    if sess_dates:
        newest = max(sess_dates)
        if newest < w["ct"]:
            data_through = newest
            notes.append(f"GA4 data is only loaded through {newest.isoformat()}; later "
                         "days in this period are incomplete.")

    by_landing, by_page_type = _build_landing(data, market, wdays)
    kind = FUNNEL_KIND.get(days if not custom else None, "d30")
    try:
        funnel, fw, fnotes = _build_funnel(data, market, kind)
    except Exception as e:                      # a shape surprise costs the funnel only
        _log_fail("funnel assembly", e)
        funnel, fw, fnotes = None, None, ["The user funnel could not be read."]
    notes += fnotes
    if funnel and fw:
        if custom:
            notes.append(f"The user funnel is a fixed GA4 window ({fw['from']} to "
                         f"{fw['to']}, 30 days) — it does not follow a custom date range.")
        elif (fw["from"], fw["to"]) != (_iso(w["cf"]), _iso(w["ct"])):
            notes.append(f"The user funnel covers {fw['from']} to {fw['to']} "
                         f"({FUNNEL_KIND_DAYS[kind]} days), not exactly the selected period.")

    categories, subcategories, coverage, diag = _build_categories(data, market, wdays)
    cur_tot = day["totals"]["cur"]

    # On-site search: only a section when the event is actually being sent.
    sess, vsr = cur_tot.get("sessions"), cur_tot.get("view_search_results")
    search_tracked = bool(sess) and (vsr or 0) >= SEARCH_TRACKED_MIN * sess
    search = _build_search(data, market, wdays) if search_tracked else []

    # GA4 implementation problems DETECTED IN THIS RUN'S DATA (never a static list).
    gaps = []
    if diag["views"] and diag["noid"] >= 0.5 * diag["views"]:
        gaps.append({
            "what": f"view_item is sent without an item_id "
                    f"({diag['noid'] / diag['views']:.0%} of product views)",
            "impact": "Products cannot be joined on id between view, cart and purchase; "
                      "they are matched on name + brand per market instead, and cannot "
                      "be joined to stock, margin or feed data by SKU.",
            "fix": "Send the same item_id (the feed's product id) in the items array "
                   "of view_item, add_to_cart, begin_checkout and purchase."})
    if diag["views"] and diag["nocat"] >= 0.5 * diag["views"]:
        gaps.append({
            "what": f"item_category is not implemented "
                    f"({diag['nocat'] / diag['views']:.0%} of product views carry no category)",
            "impact": "Categories are inferred by matching product names to the Google "
                      "Shopping feed"
                      + (f"; {coverage:.0%} of product views could be categorised."
                         if coverage is not None else "."),
            "fix": "Populate item_category … item_category3 in every ecommerce event "
                   "from the product-type hierarchy."})
    if sess and not search_tracked:
        gaps.append({
            "what": f"On-site search is not tracked ({int(vsr or 0):,} view_search_results "
                    f"events on {int(sess):,} sessions)",
            "impact": "Search terms, search usage and search-to-purchase cannot be reported.",
            "fix": "Fire view_search_results with the search_term parameter on every "
                   "search results page (/<locale>/search/<term>)."})
    sampled_kinds = sorted({FUNNEL_KIND_DAYS[r["window_kind"]] for r in data.get("funnel") or []
                            if r.get("sampled") and (r.get("breakdown") or "total") == "total"
                            and r.get("window_kind") in FUNNEL_KIND_DAYS})
    if sampled_kinds:
        gaps.append({
            "what": "GA4 samples the " + "- and ".join(str(k) for k in sampled_kinds)
                    + "-day user funnels",
            "impact": "Those funnel user counts are estimates from a subset of events; "
                      "only an unsampled window (7 days) is exact.",
            "fix": "Inherent to the GA4 funnel API at this event volume (GA4 360 raises "
                   "the limit). Use the 7-day funnel for exact figures."})

    notes += [
        "Revenue is GA4 purchase revenue in SEK on the warehouse's configured VAT "
        "basis; it will not match order-system revenue.",
        "Bounce rate is GA4's (1 − engaged sessions ÷ sessions), not the Universal "
        "Analytics single-page bounce.",
        "Products are matched on name + brand within each market (names are "
        "localised), so the same article is a separate row per market. Shipping-fee "
        "lines are excluded from all item figures.",
        "Categories come from the Google Shopping feed's product types, matched on "
        "product name; unmatched products are 'Uncategorised'.",
        "Landing-page types are inferred from URL patterns.",
    ]
    if any(r.get("id") == "not_consented" for r in day["by_channel"]):
        notes.append("Channel 'Not Consented' is traffic GA4 reports with source "
                     "'(not set)': visits without analytics consent (modelled, no "
                     "source) and events GA4 could not join to a session.")
    if any(r["key"] == "not_set" for r in by_page_type):
        notes.append("Landing page '(not set)' is sessions GA4 could not tie to a first "
                     "page view (typically no page_view was recorded, e.g. consent "
                     "declined or a session that started with another event); they show "
                     "very low engagement.")
    if any(r["key"] == REST_OF_WORLD for r in day["by_market"]):
        notes.append(f"'{REST_OF_WORLD}' is every country outside the market map "
                     "(e.g. KR, DE, the rest of the EU).")

    return {
        "meta": {
            "generated_at": generated_at
                            or datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "market": market,
            "days": wdays,
            "custom": bool(custom),
            "cur": {"from": _iso(w["cf"]), "to": _iso(w["ct"])},
            "prev": {"from": _iso(w["pf"]), "to": _iso(w["pt"])},
            "markets": present,
            "data_through": _iso(data_through),
            "restated_days": RESTATED_DAYS,
            "funnel_window": fw,
            "category_coverage": coverage,
            "search_tracked": search_tracked,
            "tracking_gaps": gaps,
            "notes": notes,
        },
        "totals": day["totals"],
        "daily": day["daily"],
        "daily_yoy": day["daily_yoy"],
        "by_market": day["by_market"],
        "by_channel": day["by_channel"],
        "by_landing": by_landing,
        "by_page_type": by_page_type,
        "brands": _build_items(data.get("brands"), market, wdays, BRANDS_MAX),
        "categories": categories,
        "subcategories": subcategories,
        "products": _build_products(data, market, wdays),
        "brand_by_market": _build_brand_market(data, market, wdays),
        "funnel": funnel,
        "search": search,
    }


def build_custom(market: str, start, end) -> tuple[dict, dict]:
    """One custom range, fetched and built — the service's entire live path.

    Returns (payload, stats); the stats belong in the service log, not in the
    response, which must be shape-identical to a preset snapshot."""
    start, end = _as_date(start), _as_date(end)
    w = _window(start, end)
    data = fetch([w], only=market)
    payload = build_combo(data, market, w, custom=True)
    _fit(payload, f"custom {market} {start}..{end}")
    stats = {"bytes_processed": data["bytes_processed"], "elapsed": data["elapsed"],
             "queries": data["queries"], "span": w["days"], "site": data["site"]}
    return payload, stats


def build_all() -> dict:
    """Every preset combination out of one fetch. {"combos": {"<market>_<days>": payload}}"""
    end = last_complete_day()
    windows = [preset_window(d, end) for d in PERIOD_OPTIONS]
    generated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    data = fetch(windows)
    print(f"   fetch through {end}: {data['queries']} queries, "
          f"{data['bytes_processed'] / 1e9:.2f} GB scanned in {data['elapsed']}s, "
          f"site={data['site']!r}; rows: "
          + ", ".join(f"{k} {len(data[k]) if data.get(k) is not None else 'FAILED'}"
                      for k in ("sessions", "events", "items", "landing", "brands",
                                "products", "search", "funnel")),
          flush=True)
    combos = {}
    for market in MARKET_OPTIONS:
        for w in windows:
            combos[f"{market}_{w['days']}"] = build_combo(
                data, market, w, days=w["days"], generated_at=generated_at)
    return {"combos": combos}


# ── Document size ────────────────────────────────────────────────────────────
def _payload_bytes(payload: dict) -> int:
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


# Trim LIST LENGTHS, never fields, cheapest loss first. Each step is
# (list key, new length); the walk stops as soon as the payload fits.
_TRIM_STEPS = [("products", 600), ("products", 400), ("brands", 250), ("products", 250),
               ("by_landing", 100), ("subcategories", 150), ("categories", 100), ("search", 50),
               ("brand_by_market", 120), ("products", 120), ("brands", 100),
               ("by_landing", 50), ("products", 50), ("brands", 40),
               ("brand_by_market", 0), ("search", 0), ("products", 0), ("brands", 0),
               ("subcategories", 0), ("categories", 0), ("by_landing", 0)]


def _fit(payload: dict, label: str) -> tuple[int, list[str]]:
    """Bring one payload under the Firestore budget. Never raises: the last
    steps empty whole lists, which is still a working tab."""
    n = _payload_bytes(payload)
    cut: dict[str, tuple[int, int]] = {}
    for key, keep in _TRIM_STEPS:
        if n <= DOC_BUDGET_BYTES:
            break
        rows = payload.get(key) or []
        if len(rows) <= keep:
            continue
        cut[key] = (cut.get(key, (len(rows), 0))[0], keep)
        payload[key] = rows[:keep]
        n = _payload_bytes(payload)
    trimmed = [f"{k} {a}→{b} rows" for k, (a, b) in cut.items()]
    if trimmed:
        payload["meta"]["notes"].append(
            "Lists shortened to fit the snapshot size limit: " + ", ".join(trimmed) + ".")
        n = _payload_bytes(payload)
        print(f"   {label}: trimmed {', '.join(trimmed)} → {n:,} B", flush=True)
    return n, trimmed


def combo_key(market: str, days: int) -> str:
    return f"{DOC_PREFIX}__{market}_{days}"


def write_firestore(combos: dict) -> dict:
    """Every combination as its own document. Returns {key: bytes}."""
    from google.cloud import firestore

    db = firestore.Client(project=FIRESTORE_PROJECT, credentials=_rm._credentials())
    sizes: dict[str, int] = {}
    for combo, payload in combos.items():
        market, days = combo.rsplit("_", 1)
        key = combo_key(market, int(days))
        n, _ = _fit(payload, combo)
        if n > DOC_BUDGET_BYTES:
            print(f"ERROR ecom: {combo} is {n:,} B after trimming — NOT written", flush=True)
            continue
        db.collection(COLLECTION).document(f"{WORKSPACE}__{key}").set({
            "data": payload, "fetched_at": firestore.SERVER_TIMESTAMP,
            "expires_at": time.time() + TTL, "ttl_seconds": TTL,
            "workspace": WORKSPACE})
        sizes[key] = n
    return sizes


def _json_default(o):
    if isinstance(o, (datetime.date, datetime.datetime)):
        return o.isoformat()
    return float(o)


def main(argv=None):
    ap = argparse.ArgumentParser(description="E-com Funnel snapshot refresh")
    ap.add_argument("--dry-run", action="store_true",
                    help="query and assemble, print payload sizes, write nothing to Firestore")
    ap.add_argument("--out", default=os.environ.get("ECOM_OUT"),
                    help="also write the assembled bundle as JSON to this path")
    ap.add_argument("--custom", nargs=3, metavar=("MARKET", "FROM", "TO"),
                    help="build one custom range instead of the presets (implies --dry-run)")
    args = ap.parse_args(argv)
    t0 = time.time()
    dry = args.dry_run or bool(os.environ.get("SKIP_FIRESTORE")) or bool(args.custom)

    if args.custom:
        market, a, b = args.custom
        payload, st = build_custom(market, a, b)
        bundle = {"combos": {f"{market}_custom": payload}}
        print(f"   custom {market} {a}..{b}: {st}")
    elif os.environ.get("ECOM_IN"):
        src = os.environ["ECOM_IN"]
        with open(src, encoding="utf-8") as fh:
            bundle = json.load(fh)
        print(f"   loaded {src} ({os.path.getsize(src):,} bytes, "
              f"{len(bundle['combos'])} combinations) — BigQuery skipped")
    else:
        bundle = build_all()

    combos = bundle["combos"]
    if dry:
        sizes = {k: _fit(p, k)[0] for k, p in combos.items()}
        where = "(dry run — Firestore not written)"
    else:
        sizes = write_firestore(combos)
        where = f"{COLLECTION}/{WORKSPACE}__{DOC_PREFIX}__<market>_<days>"

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(bundle, fh, ensure_ascii=False, default=_json_default)
        print(f"   wrote {args.out} ({os.path.getsize(args.out):,} bytes)")

    default = combos.get(f"{DEFAULT_MARKET}_{DEFAULT_DAYS}") or next(iter(combos.values()))
    c = default["totals"]["cur"]
    m = default["meta"]
    print(f"✓ E-com refresh · {where} · {m['market']} {m['cur']['from']}..{m['cur']['to']} · "
          f"sessions {c.get('sessions')} / transactions {c.get('transactions')} / "
          f"revenue {c.get('revenue')} · funnel {m['funnel_window']} · "
          f"{len(sizes)} docs, {max(sizes.values()) if sizes else 0:,} B max · "
          f"{time.time() - t0:.1f}s")
    for key in sorted(sizes, key=lambda x: -sizes[x]):
        print(f"     {key:<22} {sizes[key]:>9,} B")
    for note in m["notes"]:
        print(f"     note: {note}")


if __name__ == "__main__":
    main()
