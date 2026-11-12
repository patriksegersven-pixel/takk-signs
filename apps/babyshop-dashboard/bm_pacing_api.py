"""
bm_pacing_api.py: live ad spend for the Pacing section's "Yesterday and today" cards.
Mounted by app.py behind Basic auth.

    GET /api/blackmonth/pacing/spend-live

Google Ads + Meta spend for yesterday and today so far, per market, in SEK,
straight from the two APIs. Together they are ~99 % of November's ad cost
(Funnel MTD; Naver, affiliate and price comparison are the rest), so the page
divides this by the live Norce intake to get a same-day COS. Funnel can't be
used for today: it only carries complete days.

Read-only. Cached in-process for CACHE_TTL_S so page loads can't hammer the APIs.
"""
from __future__ import annotations

import datetime
import os
import threading
import time

from fastapi import APIRouter

import bm_common as bm

router = APIRouter(prefix="/api/blackmonth/pacing")

CACHE_TTL_S = int(os.environ.get("BM_SPEND_LIVE_TTL_S", "600"))
META_ACCOUNT = "2927326560764919"        # the only Meta account spending (SEK)
CORE = set(bm.MARKETS)

_cache: tuple[dict, float] | None = None
_fx_cache: tuple[dict, float] | None = None
_lock = threading.Lock()


def _mk(code: str | None) -> str:
    return code if code in CORE else "ROW"


def _accounts() -> dict:
    import bm_paid
    return bm_paid.GOOGLE_ACCOUNTS


def _fx() -> dict[str, float]:
    global _fx_cache
    if _fx_cache and _fx_cache[1] > time.time():
        return _fx_cache[0]
    import bm_paid
    fx = bm_paid._fx()
    _fx_cache = (fx, time.time() + 12 * 3600)
    return fx


def _google(d0: datetime.date, d1: datetime.date, out: dict, errors: list[str]) -> None:
    import bm_paid
    import refresh_roas_sims as R
    try:
        service = R.ads_client().get_service("GoogleAdsService")
        fx = _fx()
    except Exception as e:                       # noqa: BLE001
        errors.append(f"Google: {type(e).__name__}: {str(e)[:160]}")
        return
    for cid, (brand, mkt) in bm_paid.GOOGLE_ACCOUNTS.items():
        try:
            cur = "SEK"
            rows = list(R._search(service, cid,
                                  "SELECT customer.currency_code, segments.date, metrics.cost_micros "
                                  f"FROM customer WHERE segments.date BETWEEN '{d0}' AND '{d1}'"))
            for row in rows:
                cur = row.customer.currency_code or cur
                rate = fx.get(cur)
                if rate is None:
                    raise ValueError(f"no SEK rate for {cur}")
                day = out.setdefault(row.segments.date, {})
                m = day.setdefault(_mk(mkt), {"google": 0.0, "meta": 0.0})
                m["google"] += row.metrics.cost_micros / 1e6 * rate
        except Exception as e:                   # noqa: BLE001
            errors.append(f"Google {brand} {mkt}: {type(e).__name__}: {str(e)[:160]}")


def _meta(d0: datetime.date, d1: datetime.date, out: dict, errors: list[str]) -> None:
    token = os.environ.get("META_ADS_ACCESS_TOKEN", "").strip()
    if not token:
        errors.append("Meta: no access token on this service")
        return
    import json
    from bm_insights_meta import GRAPH, _get
    try:
        url = f"{GRAPH}/act_{META_ACCOUNT}/insights"
        params = {"level": "account", "fields": "spend,account_currency",
                  "breakdowns": "country", "time_increment": 1, "limit": 500,
                  "time_range": json.dumps({"since": str(d0), "until": str(d1)})}
        while url:
            d = _get(url, params, token)
            for r in d.get("data", []):
                if (r.get("account_currency") or "SEK") != "SEK":
                    raise ValueError(f"account currency {r.get('account_currency')}")
                day = out.setdefault(r["date_start"], {})
                m = day.setdefault(_mk(r.get("country")), {"google": 0.0, "meta": 0.0})
                m["meta"] += float(r.get("spend") or 0)
            url, params = d.get("paging", {}).get("next"), None
    except Exception as e:                       # noqa: BLE001
        errors.append(f"Meta: {type(e).__name__}: {str(e)[:160]}")


def build() -> dict:
    now = datetime.datetime.now(bm.TZ)
    today = now.date()
    yday = today - datetime.timedelta(days=1)
    by_day: dict = {}
    errors: list[str] = []
    _google(yday, today, by_day, errors)
    _meta(yday, today, by_day, errors)
    days = {}
    for key, d in (("today", today), ("yesterday", yday)):
        mk = {m: {k: round(v) for k, v in x.items()} for m, x in by_day.get(str(d), {}).items()}
        allm = {"google": sum(x["google"] for x in mk.values()),
                "meta": sum(x["meta"] for x in mk.values())}
        mk["all"] = allm
        for x in mk.values():
            x["total"] = x["google"] + x["meta"]
        days[key] = {"date": str(d), "markets": mk}
    g_err = [e for e in errors if e.startswith("Google")]
    g_down = bool(g_err) and (g_err[0].startswith("Google:") or len(g_err) >= len(_accounts()))
    return {"available": bool(by_day) and not g_down,
            "reason": "Google Ads spend could not be read" if g_down else None,
            "fetched_at_local": now.strftime("%Y-%m-%d %H:%M"),
            "channels": ["Google Ads", "Meta"], "days": days, "errors": errors,
            "cache_ttl_s": CACHE_TTL_S}


@router.get("/spend-live")
def spend_live():
    global _cache
    with _lock:
        if _cache and _cache[1] > time.time():
            return _cache[0]
        try:
            payload = build()
        except Exception as e:                   # noqa: BLE001
            print(f"ERROR /api/blackmonth/pacing/spend-live: {type(e).__name__}: {e}", flush=True)
            payload = {"available": False, "reason": f"{type(e).__name__}: {str(e)[:200]}"}
        ttl = CACHE_TTL_S if payload.get("available") and not payload.get("errors") else 60
        _cache = (payload, time.time() + ttl)
        return payload
