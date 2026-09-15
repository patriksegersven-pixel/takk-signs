"""
norce_today.py: live "ordered today" figures for the Executive P&L tab.

WHY THIS EXISTS, AND WHY IT IS NOT BIGQUERY
-------------------------------------------
The Executive P&L ladder is built on Business Central POSTING dates, because
that is the basis Finance's own management report reconciles to. A posting date
answers "when did the invoice hit the ledger", which is a shipment-and-invoicing
batch: a Saturday posts almost nothing and a Monday carries the backlog. It is a
true statement about posting and a useless statement about trading.

A day card has to answer "how did we trade", which is ORDER date. The user's own
vocabulary, kept verbatim throughout this module and on the page:

    "booked" means WHEN THE PURCHASE TOOK PLACE (order date),
    NOT when the invoice posted.

So the BC ledger basis is called "posted" or "invoiced" everywhere, never
"booked", and this module's basis is called "ordered".

Three candidate sources were audited (2026-09-15) and two lose:

  • Funnel `kv_revenue` is date-fresh but ~7% complete for today, literally 0
    until the ~11:12Z load, no timestamp column at all (grain is DATE), and a
    given date keeps growing for ~2 days. A card on it reads 0 SEK for the first
    half of every working day.
  • Norce via BigQuery has the right shape, but the nightly
    sync has been writing nothing since 2026-08-19, and even repaired it is
    nightly, so it is no fresher than BC for today.
  • A faster `bc-sync` surfaces postings sooner but cannot invent them.

That leaves the Norce API, queried live. This module does that.

THE TWO GOTCHAS THAT SILENTLY BREAK THIS
----------------------------------------
1. STATUS. Orders are born at StatusId 2 and move to 4 within about two days.
   Filtering to status 4, the obvious "confirmed" filter, zeroes the card for
   the first part of every day. Status 5 is NOT a cancellation either: every
   status-5 order in the audit produced a BC invoice. Status **6** is the only
   state that never invoices, and it is 0.12% of orders. So: EXCLUDE 6, COUNT
   EVERYTHING ELSE. See `ORDER_FILTER`.

2. FREIGHT. Revenue is merchandise PLUS the header `FreightCost`. BC includes
   freight in invoiced revenue, and the Norce header `FreightCost` matches the
   shipping line (PartNo 1000014) exactly: 1.492 MSEK against 1.492 MSEK over
   Jun+Jul 2026. Use one or the other, never both: merchandise here EXCLUDES the
   shipping line and then adds `FreightCost` back. Merchandise alone reconciles
   to BC at 102.74% (i.e. short); merchandise plus freight at 99.16%.

WHAT THIS IS NOT
----------------
Order intake is not booked-and-posted revenue and must never be summed with, or
differenced against, the monthly ladder. On a calendar basis June 2026 reads
18.26 MSEK ordered against 21.69 MSEK posted, a 19% gap that is pure posting
timing, not performance. Matched order-for-order the two reconcile to 99.16%.
`BASIS_WARNING` carries that sentence to the page so it cannot be dropped.

COST
----
One page per application per day in the window, `PAGE_SIZE` 500, so a two-day
window over 13 applications is ~15 requests. The token endpoint allows 3 req/min
per IP and a token lasts an hour, so one token serves many refreshes. Results
are cached in-process for `CACHE_TTL_S` so a page refresh cannot hammer the API.

This module deliberately does NOT import from `norce_sync.py`. It reads the same
two environment variables the same way and speaks the same OAuth flow, but a
serving path must not be able to break because a nightly batch job's module was
edited underneath it.
"""
from __future__ import annotations

import datetime as _dt
import os
import threading
import time
from typing import Any
from zoneinfo import ZoneInfo

import httpx

# ── Tenant ───────────────────────────────────────────────────────────────────
# Same defaults as norce_sync.py. `scope` is the environment name, not a
# per-API permission; API access comes from the integration user's resources.
BASE_URL  = os.environ.get("NORCE_BASE_URL", "https://babyshop.api-se.norce.tech")
TOKEN_URL = os.environ.get("NORCE_TOKEN_URL", f"{BASE_URL}/identity/1.0/connect/token")
QUERY_URL = os.environ.get("NORCE_QUERY_URL", f"{BASE_URL}/commerce/query/2.0")
SCOPE     = os.environ.get("NORCE_SCOPE", "prod")

CLIENT_ID     = os.environ.get("NORCE_CLIENT_ID")
CLIENT_SECRET = os.environ.get("NORCE_CLIENT_SECRET")

# Applications are market x shop. Orders are served per application: the id is
# both the `applicationId` context header and an explicit $filter, because the
# header alone does not restrict Orders.
APPLICATIONS: dict[str, int] = {
    "babyshop-se": 1244, "babyshop-no": 1264, "babyshop-fi": 1265, "babyshop-dk": 1266,
    "babyshop-eu": 1267, "babyshop-na": 1268, "babyshop-uk": 1269, "babyshop-row": 1270,
    "babyshop-asia": 1271,
    "lekmer-se": 1272, "lekmer-no": 1273, "lekmer-fi": 1274, "lekmer-dk": 1275,
}

# Tenant-wide sets (Core/*) still need an applicationId CONTEXT header on this
# tenant even though they are not per-application data: without one they 500 on
# every request. The header sets context only, it does not filter these sets.
CONTEXT_APP_ID = int(os.environ.get("NORCE_CONTEXT_APP_ID", "1244"))  # babyshop-se

# Shipping is booked as an ordinary order line. It is excluded from merchandise
# and re-added from the header FreightCost. See the module docstring.
SHIPPING_PART_NO = "1000014"

# Status 6 is the only state that never produces a BC invoice. Everything else
# counts, including the status-2 orders that make up most of a fresh day.
ORDER_FILTER = "StatusId ne 6"

PAGE_SIZE      = int(os.environ.get("NORCE_TODAY_PAGE_SIZE", "500"))
MAX_PAGES      = int(os.environ.get("NORCE_TODAY_MAX_PAGES", "6"))
HTTP_TIMEOUT   = float(os.environ.get("NORCE_TODAY_TIMEOUT", "25"))
CACHE_TTL_S    = int(os.environ.get("NORCE_TODAY_TTL_S", "120"))
LOOKUP_TTL_S   = int(os.environ.get("NORCE_TODAY_LOOKUP_TTL_S", "43200"))  # 12 h
TOKEN_SKEW_S   = 60
TZ_NAME        = os.environ.get("NORCE_TODAY_TZ", "Europe/Stockholm")


def _tz():
    """The trading-day zone, resolved lazily.

    A module-level ZoneInfo() would raise at IMPORT time on any image without the
    IANA database, and an import error inside the endpoint is an unhandled 500
    rather than the graceful "unavailable" this module promises. `tzdata` is in
    requirements.txt so this should always succeed; the fallback exists so that a
    packaging mistake degrades the day cards to UTC boundaries with a visible
    label instead of breaking the tab."""
    global _TZ
    if _TZ is None:
        try:
            _TZ = ZoneInfo(TZ_NAME)
        except Exception:                      # noqa: BLE001
            print(f"WARN norce_today: no tz database for {TZ_NAME}, "
                  f"falling back to UTC day boundaries", flush=True)
            _TZ = _dt.timezone.utc
    return _TZ


_TZ = None

# ── Stated facts that belong beside any figure this module returns ───────────
# These come from the 2026-09-15 reconciliation of 52,058 Jun+Jul orders against
# their own BC invoices. They are constants rather than live measures because
# they are properties of the two systems, not of today.
LAG = {
    "median_days": 1,
    "p90_days": 4,
    "within_7_days_pct": 99.7,
    "note": ("Orders post to the ledger a median of 1 day later, 90% within 4, "
             "99.7% within 7. The lag moves month to month (June median 2, "
             "July median 1), so it is stated as a range and never as a "
             "conversion factor."),
}
RECONCILIATION = {
    "ordered_vs_posted_pct": 99.16,
    "note": ("Matched order for order, Norce intake reconciles to eventually "
             "posted BC revenue at 99.16%: 42.87 MSEK ordered against 42.51 "
             "MSEK invoiced over Jun+Jul 2026. Ordered therefore runs about "
             "0.8% high, which is the cancellation and shrinkage wedge."),
}
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
    "gross_of_returns": True,
    "no_margin": ("Gross margin is not shown. Line-level CostUnit is 0.00% "
                  "populated and the price-list cost covers 61% of order "
                  "value, so a margin here would be a guess wearing a decimal "
                  "point."),
    "fx_note": ("Non-SEK markets are converted at Norce's own currency table, "
                "which is a fixed EUR-based rate card, not a live FX feed. It "
                "is the same convention norce_marts.sql uses."),
}


class NorceUnavailable(RuntimeError):
    """The live API cannot answer right now. Carries a reader-facing reason."""


# ── OAuth ────────────────────────────────────────────────────────────────────
_token: tuple[str, float] | None = None
_token_lock = threading.Lock()


def configured() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET)


def _access_token(client: httpx.Client) -> str:
    global _token
    with _token_lock:
        if _token and _token[1] > time.time():
            return _token[0]
        if not configured():
            missing = [n for n, v in (("NORCE_CLIENT_ID", CLIENT_ID),
                                      ("NORCE_CLIENT_SECRET", CLIENT_SECRET)) if not v]
            raise NorceUnavailable(
                "Norce credentials are not wired to this service (missing "
                + ", ".join(missing) + ").")
        r = client.post(TOKEN_URL,
                        data={"grant_type": "client_credentials",
                              "client_id": CLIENT_ID,
                              "client_secret": CLIENT_SECRET,
                              "scope": SCOPE},
                        headers={"Content-Type": "application/x-www-form-urlencoded",
                                 "Accept": "application/json"})
        if r.status_code != 200:
            raise NorceUnavailable(
                f"Norce rejected the credentials ({r.status_code}).")
        body = r.json()
        _token = (body["access_token"],
                  time.time() + max(int(body.get("expires_in", 3600)) - TOKEN_SKEW_S, 60))
        return _token[0]


def _get(client: httpx.Client, entity: str, params: dict[str, Any],
         app_id: int | None) -> dict:
    """One GET with auth and the application context header.

    Retries are deliberately shallow: this sits on a page load, so a couple of
    seconds of backoff is the whole budget. A caller that runs out degrades to a
    stated reason rather than blocking the tab.
    """
    headers = {"Authorization": f"Bearer {_access_token(client)}",
               "Accept": "application/json"}
    if app_id is not None:
        headers["applicationId"] = str(app_id)
    delay = 1.0
    for attempt in range(3):
        r = client.get(f"{QUERY_URL}/{entity}", params=params, headers=headers)
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
            time.sleep(delay)
            delay *= 2
            headers["Authorization"] = f"Bearer {_access_token(client)}"
            continue
        raise NorceUnavailable(f"Norce returned {r.status_code} on {entity}.")
    raise NorceUnavailable(f"Norce did not answer for {entity}.")


# ── Lookups (small, slow-moving, cached hard) ────────────────────────────────
_lookups: tuple[dict, float] | None = None
_lookup_lock = threading.Lock()


def _fetch_lookups(client: httpx.Client) -> dict:
    """Country id -> ISO-2 code, and currency id -> SEK multiplier.

    Market is resolved from DeliveryCountryId, NOT from the application key.
    Four applications are single-country but the rest are not: `babyshop-asia`
    alone carries KR, KZ, JP and IL. Slicing by application would silently merge
    them and re-create the "ROW" bucket problem that already affects ad cost.
    """
    global _lookups
    with _lookup_lock:
        if _lookups and _lookups[1] > time.time():
            return _lookups[0]

        countries: dict[int, str] = {}
        for page in range(4):
            body = _get(client, "Core/Countries",
                        {"$select": "Id,Code", "$top": PAGE_SIZE,
                         "$skip": page * PAGE_SIZE}, CONTEXT_APP_ID)
            rows = body.get("value") or []
            for r in rows:
                if r.get("Id") is not None and r.get("Code"):
                    countries[int(r["Id"])] = str(r["Code"])
            if len(rows) < PAGE_SIZE:
                break

        rates: dict[int, float] = {}
        sek = None
        for page in range(4):
            body = _get(client, "Core/Currencies",
                        {"$select": "Id,Code,ExchangeRate", "$top": PAGE_SIZE,
                         "$skip": page * PAGE_SIZE}, CONTEXT_APP_ID)
            rows = body.get("value") or []
            for r in rows:
                rid, rate = r.get("Id"), r.get("ExchangeRate")
                if rid is None or not rate:
                    continue
                rates[int(rid)] = float(rate)
                if r.get("Code") == "SEK":
                    sek = float(rate)
            if len(rows) < PAGE_SIZE:
                break

        if not sek:
            raise NorceUnavailable("Norce currency table has no SEK rate.")
        # Rates are EUR-based: ExchangeRate is the value of one unit in EUR.
        # SEK value = amount * rate_of_currency / rate_of_SEK.
        to_sek = {cid: rate / sek for cid, rate in rates.items()}

        out = {"countries": countries, "to_sek": to_sek, "sek_rate": sek}
        _lookups = (out, time.time() + LOOKUP_TTL_S)
        return out


# ── Orders ───────────────────────────────────────────────────────────────────
def _iso_z(d: _dt.datetime) -> str:
    return d.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fetch_orders(client: httpx.Client, start: _dt.datetime,
                  end: _dt.datetime) -> tuple[list[dict], dict]:
    """Every order in [start, end) across all applications, one page at a time.

    Returns the raw rows plus a diagnostics dict. A per-application page cap
    exists so a bad window can never turn one page load into hundreds of calls;
    if it bites, the shortfall is reported rather than hidden.
    """
    window = (f"OrderDate ge {_iso_z(start)} and OrderDate lt {_iso_z(end)} "
              f"and {ORDER_FILTER}")
    params_base = {
        "$select": "OrderNo,OrderDate,StatusId,CurrencyId,FreightCost,DeliveryCountryId",
        "$expand": "Items($select=PartNo,QtyOrdered,LineAmount)",
        "$orderby": "OrderDate",
        "$top": PAGE_SIZE,
    }
    rows: list[dict] = []
    calls = 0
    capped: list[str] = []
    for key, app_id in APPLICATIONS.items():
        # The applicationId HEADER sets context but does NOT restrict Orders:
        # without the explicit ApplicationId clause every application returns
        # the whole tenant and the day is counted 13 times over. This was seen
        # for real here: 13,221 orders on a day that had 1,016.
        flt = f"ApplicationId eq {app_id} and {window}"
        for page in range(MAX_PAGES):
            params = dict(params_base, **{"$filter": flt, "$skip": page * PAGE_SIZE})
            body = _get(client, "Orders/Orders", params, app_id)
            calls += 1
            got = body.get("value") or []
            for r in got:
                r["_app"] = key
            rows.extend(got)
            if len(got) < PAGE_SIZE:
                break
        else:
            capped.append(key)
    return rows, {"http_calls": calls, "page_capped_applications": capped}


def _order_value(o: dict, to_sek: dict[int, float]) -> tuple[float, float, int, float]:
    """(revenue_sek, freight_sek, units, shipping_line_sek) for one order.

    Revenue is merchandise EXCLUDING the shipping pseudo-SKU plus the header
    FreightCost. The shipping line is returned separately only so the caller can
    prove the two agree; it is never added on top.
    """
    fx = to_sek.get(int(o.get("CurrencyId") or 0), 1.0)
    merch = 0.0
    ship_line = 0.0
    units = 0
    for it in (o.get("Items") or []):
        amt = float(it.get("LineAmount") or 0)
        if str(it.get("PartNo") or "") == SHIPPING_PART_NO:
            ship_line += amt
            continue
        merch += amt
        units += int(it.get("QtyOrdered") or 0)
    freight = float(o.get("FreightCost") or 0)
    return (merch + freight) * fx, freight * fx, units, ship_line * fx


def _parse_dt(s: str) -> _dt.datetime:
    # Norce sends "2026-09-15T09:44:48.6133333Z"; Python's parser wants at most
    # microsecond precision and a +00:00 offset.
    t = s.rstrip("Z")
    if "." in t:
        head, frac = t.split(".", 1)
        t = head + "." + frac[:6]
    return _dt.datetime.fromisoformat(t).replace(tzinfo=_dt.timezone.utc)


def _blank_day(date_str: str) -> dict:
    return {"date": date_str, "orders": 0, "revenue_sek": 0.0, "freight_sek": 0.0,
            "units": 0, "shipping_line_sek": 0.0, "by_country": {}, "by_hour": [0.0] * 24}


# ── Public ───────────────────────────────────────────────────────────────────
_cache: tuple[dict, float] | None = None
_cache_lock = threading.Lock()


def get(now: _dt.datetime | None = None, force: bool = False) -> dict:
    """The live day-grain payload, cached for CACHE_TTL_S.

    Never raises: an unavailable API comes back as `available: false` with a
    `reason` the page can print, because a dead upstream should degrade one card
    and not the tab.
    """
    global _cache
    with _cache_lock:
        if not force and _cache and _cache[1] > time.time():
            return _cache[0]
        try:
            payload = _build(now or _dt.datetime.now(_dt.timezone.utc))
        except NorceUnavailable as e:
            payload = _unavailable(str(e))
        except Exception as e:                       # noqa: BLE001 - never 500 the tab
            payload = _unavailable(
                f"Live Norce read failed ({type(e).__name__}).")
        # A failure is cached briefly too, so a hard-down API cannot be turned
        # into a request storm by someone holding down refresh.
        ttl = CACHE_TTL_S if payload.get("available") else min(CACHE_TTL_S, 60)
        _cache = (payload, time.time() + ttl)
        return payload


def _unavailable(reason: str) -> dict:
    return {
        "available": False,
        "reason": reason,
        "fetched_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "measure": MEASURE, "lag": LAG, "reconciliation": RECONCILIATION,
        "basis_warning": BASIS_WARNING,
    }


def _build(now_utc: _dt.datetime) -> dict:
    TZ = _tz()
    now_local = now_utc.astimezone(TZ)
    today_local = now_local.date()
    prev_local = today_local - _dt.timedelta(days=1)

    # The window is two LOCAL days: the latest complete one and today so far.
    # Local, not UTC, because "today" is a trading day in Stockholm and a UTC
    # cut would move two hours of the evening into the wrong day.
    start = _dt.datetime.combine(prev_local, _dt.time.min, tzinfo=TZ)
    end = _dt.datetime.combine(today_local + _dt.timedelta(days=1),
                               _dt.time.min, tzinfo=TZ)

    t0 = time.time()
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        look = _fetch_lookups(client)
        rows, diag = _fetch_orders(client, start, end)
    elapsed = time.time() - t0

    countries, to_sek = look["countries"], look["to_sek"]
    days = {str(prev_local): _blank_day(str(prev_local)),
            str(today_local): _blank_day(str(today_local))}

    # Today truncated at the same clock time as now, and the complete day
    # truncated at that SAME clock time. That pair is the only honest
    # comparison available intraday: both are order-date, both stop at the same
    # point of the day, so neither is maturing relative to the other.
    cut = now_local.time()
    lfl = {"cutoff_local": cut.strftime("%H:%M"),
           "today": {"orders": 0, "revenue_sek": 0.0},
           "prev": {"orders": 0, "revenue_sek": 0.0}}

    unknown_country = 0
    for o in rows:
        od = o.get("OrderDate")
        if not od:
            continue
        ts = _parse_dt(od).astimezone(TZ)
        key = str(ts.date())
        d = days.get(key)
        if d is None:                       # boundary drift, should not happen
            continue
        rev, freight, units, ship_line = _order_value(o, to_sek)
        d["orders"] += 1
        d["revenue_sek"] += rev
        d["freight_sek"] += freight
        d["units"] += units
        d["shipping_line_sek"] += ship_line
        d["by_hour"][ts.hour] += rev
        cid = o.get("DeliveryCountryId")
        code = countries.get(int(cid)) if cid is not None else None
        if not code:
            unknown_country += 1
            code = "unknown"
        c = d["by_country"].setdefault(code, {"orders": 0, "revenue_sek": 0.0})
        c["orders"] += 1
        c["revenue_sek"] += rev
        if ts.time() < cut:
            side = "today" if key == str(today_local) else "prev"
            lfl[side]["orders"] += 1
            lfl[side]["revenue_sek"] += rev

    complete = days[str(prev_local)]
    partial = days[str(today_local)]

    # Completeness is measured, not assumed: it is the share of the previous
    # day's intake that had arrived by this same clock time. That keeps it
    # self-calibrating and keeps the wording precise: it is a comparison to one
    # named day, not a general claim about how complete "today" is.
    prev_rev = lfl["prev"]["revenue_sek"]
    complete_rev = complete["revenue_sek"]
    share = (prev_rev / complete_rev) if complete_rev > 0 else None

    for d in (complete, partial):
        d["aov_sek"] = (d["revenue_sek"] / d["orders"]) if d["orders"] else None
        d["weekday"] = _dt.date.fromisoformat(d["date"]).strftime("%a")
        d["by_country"] = dict(sorted(d["by_country"].items(),
                                      key=lambda kv: -kv[1]["revenue_sek"]))

    # The header FreightCost should equal the shipping line to the krona. If it
    # ever stops doing so the revenue definition is wrong, so the gap is
    # published rather than assumed away.
    ship_gap = complete["revenue_sek"] and abs(
        complete["freight_sek"] - complete["shipping_line_sek"])

    return {
        "available": True,
        "reason": None,
        "fetched_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        "fetched_at_local": now_local.strftime("%Y-%m-%d %H:%M"),
        "timezone": str(_tz()),
        "cache_ttl_s": CACHE_TTL_S,
        "measure": MEASURE,
        "lag": LAG,
        "reconciliation": RECONCILIATION,
        "basis_warning": BASIS_WARNING,
        # The card leads with this one. It is a whole day, closed, on order date.
        "complete_day": complete,
        # Explicitly provisional. Never drives a comparison on its own.
        "partial_day": dict(partial, provisional=True,
                            elapsed_share_of_prev_day=share,
                            elapsed_note=(
                                "Share of the previous day's intake that had "
                                "arrived by this clock time. A measured "
                                "reference point, not a forecast of today.")),
        "like_for_like": lfl,
        "diagnostics": dict(
            diag,
            elapsed_s=round(elapsed, 2),
            orders_read=len(rows),
            applications=len(APPLICATIONS),
            window_start_local=start.strftime("%Y-%m-%d %H:%M"),
            window_end_local=end.strftime("%Y-%m-%d %H:%M"),
            orders_without_country=unknown_country,
            freight_vs_shipping_line_sek=(round(ship_gap, 2)
                                          if ship_gap is not None else None),
        ),
    }
