"""
bm_insights_meta.py: Meta ad-account activity log → change-log events for the
Black Month "Today's actions" section. Read-only Graph API GETs.

    events, notes = meta_activities(since_date)

Only the three accounts below are ever queried (the token also sees other
clients' accounts). Kept, one row each: campaign / ad set budget, bid and
bid-strategy changes, campaign and ad set status changes, campaign and ad set
creation, with old → new values from extra_data. Ad-level events (created,
status, updated, renamed) are rolled up per Stockholm day per campaign. Image
library, delivery, audience, targeting and account events are dropped.

Graph naming: CAMPAIGN_GROUP = campaign, CAMPAIGN = ad set, ADGROUP = ad.
Money in extra_data is in minor units (öre). Token: env META_ADS_ACCESS_TOKEN.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re

import bm_common as bm

GRAPH = "https://graph.facebook.com/v21.0"
ACCOUNTS = [
    {"id": "2927326560764919", "label": "Babyshop SE - New", "brand": "babyshop"},
    {"id": "686612050420009", "label": "Kuling", "brand": "babyshop"},
    {"id": "1465585123471722", "label": "Lekmer ACTIVE", "brand": "lekmer"},
]
FIELDS = "event_time,event_type,translated_event_type,object_id,object_name,object_type,extra_data,actor_name"
MAX_PAGES = 40                      # safety net; paging stops at `since` long before this

ROLLUP = {                           # ad-level event → (singular, plural) in the per-campaign roll-up
    "create_ad": ("ad created", "ads created"),
    "update_ad_run_status": ("ad status change", "ad status changes"),
    "update_ad_run_status_to_be_set_after_review": ("ad status change", "ad status changes"),
    "update_ad_creative": ("ad updated", "ads updated"),
    "update_ad_friendly_name": ("ad renamed", "ads renamed"),
}
BUDGET = {"update_campaign_budget", "update_ad_set_budget", "update_campaign_spend_cap",
          "update_ad_set_lifetime_budget", "update_campaign_lifetime_budget"}
BIDDING = {"update_ad_set_bid_strategy", "update_ad_set_bid_amount", "update_campaign_bid_strategy",
           "update_ad_set_bid_adjustments", "update_ad_set_min_roas"}
STATUS = {"update_campaign_run_status": "Campaign", "update_ad_set_run_status": "Ad set"}
CREATE = {"create_campaign_group": "Campaign", "create_ad_set": "Ad set"}

_MKT = re.compile(r"(?:^|[^A-Za-z])(SE|SWE|NO|NOR|FI|FIN|DK|DEN)(?=$|[^A-Za-z])")
_MKT_MAP = {"SWE": "SE", "NOR": "NO", "FIN": "FI", "DEN": "DK"}


def market_of(*names: str) -> str:
    """'Product - NO - …', 'KIR | DK-STOY-evergreen', 'FI-Winter-bred' → NO / DK / FI."""
    for n in names:
        m = _MKT.search(n or "")
        if m:
            return _MKT_MAP.get(m.group(1), m.group(1))
    return "all"


def _get(url: str, params: dict | None, token: str) -> dict:
    import requests
    r = requests.get(url, params=params, headers={"Authorization": f"Bearer {token}"}, timeout=60)
    try:
        d = r.json()
    except ValueError:
        raise RuntimeError(f"HTTP {r.status_code}")
    if "error" in d:
        raise RuntimeError(d["error"].get("message", "error")[:160])
    return d


def _fetch(acct: dict, since_ts: int, token: str, notes: list[str]) -> list[dict]:
    """Newest first; stop at the first event older than since_ts (the API's own
    `since` does not end the paging)."""
    out, url = [], f"{GRAPH}/act_{acct['id']}/activities"
    params = {"fields": FIELDS, "since": since_ts, "limit": 500}
    for _ in range(MAX_PAGES):
        try:
            d = _get(url, params, token)
        except Exception as e:                       # noqa: BLE001 — keep what we have
            notes.append(f"Meta activity log {acct['label']}: {e}" + (" (partial)" if out else ""))
            return out
        stop = False
        for e in d.get("data") or []:
            t = _ts(e.get("event_time"))
            if t is None:
                continue
            if t.timestamp() < since_ts:
                stop = True
                break
            out.append(e)
        url, params = (d.get("paging") or {}).get("next"), None
        if stop or not url:
            break
    return out


def _ts(s: str | None) -> dt.datetime | None:
    try:
        return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S%z")
    except (TypeError, ValueError):
        return None


def _extra(e: dict) -> dict:
    x = e.get("extra_data")
    if isinstance(x, str):
        try:
            x = json.loads(x)
        except ValueError:
            return {}
    return x if isinstance(x, dict) else {}


def _campaign_id(x: dict) -> str | None:
    c = x.get("campaign_id")
    if isinstance(c, dict):
        c = c.get("new") or c.get("mutation_input")
    return str(c) if c else None


def _money(v) -> str:
    """{'type': 'payment_amount', 'currency': 'SEK', 'new_value': 230000, 'additional_value': 'Per day'}"""
    if isinstance(v, dict):
        amt = v.get("new_value", v.get("old_value"))
        if isinstance(amt, (int, float)):
            per = v.get("additional_value") or ""
            return f"{amt / 100:,.0f} {v.get('currency', '')}{'/day' if 'day' in str(per).lower() else ''}".replace(",", " ")
        return "–"
    if isinstance(v, (int, float)):
        return f"{v / 100:,.0f}".replace(",", " ")
    return str(v) if v not in (None, "") else "–"


def _names(acct: dict, token: str, notes: list[str]) -> dict[str, str]:
    """Ad set id → campaign name for one of the three accounts. Ad-level events carry
    the AD SET id as `campaign_id` (Graph's legacy naming: an ad set is a "campaign")."""
    out, url = {}, f"{GRAPH}/act_{acct['id']}/adsets"
    params = {"fields": "id,campaign{name}", "limit": 500,
              "filtering": json.dumps([{"field": "effective_status", "operator": "IN",
                                        "value": ["ACTIVE", "PAUSED", "ARCHIVED", "IN_PROCESS", "WITH_ISSUES",
                                                  "CAMPAIGN_PAUSED", "ADSET_PAUSED", "PENDING_REVIEW",
                                                  "DISAPPROVED"]}])}
    for _ in range(10):
        try:
            d = _get(url, params, token)
        except Exception as e:                       # noqa: BLE001
            notes.append(f"Meta campaign names {acct['label']}: {e}")
            break
        out.update({a["id"]: (a.get("campaign") or {}).get("name", "") for a in d.get("data") or []})
        url, params = (d.get("paging") or {}).get("next"), None
        if not url:
            break
    return out


def meta_activities(since: dt.date) -> tuple[list[dict], list[str]]:
    notes: list[str] = []
    token = (os.environ.get("META_ADS_ACCESS_TOKEN") or "").strip()
    if not token:
        return [], ["Meta activity log not loaded: META_ADS_ACCESS_TOKEN is not set."]
    since_ts = int(dt.datetime.combine(since, dt.time(), tzinfo=bm.TZ).timestamp())
    events: list[dict] = []
    rolls: dict[tuple, dict] = {}
    names: dict[str, str] = {}
    for acct in ACCOUNTS:
        got = _fetch(acct, since_ts, token, notes)
        if any(e.get("event_type") in ROLLUP for e in got):
            names.update(_names(acct, token, notes))
        for e in got:
            t = _ts(e.get("event_time"))
            day = t.astimezone(bm.TZ).date().isoformat()
            et, x = e.get("event_type") or "", _extra(e)
            name = e.get("object_name") or ""
            base = {"date": day, "when": t.astimezone(bm.TZ).isoformat(timespec="seconds"),
                    "source": "meta", "brand": acct["brand"], "account": acct["label"],
                    "by": e.get("actor_name") or ""}
            if et in ROLLUP:
                cid = _campaign_id(x) or "?"
                r = rolls.setdefault((acct["id"], day, cid), dict(
                    base, kind="meta_ads", campaign_id=cid, counts={}, ad_names=[], actors=set()))
                lab = ROLLUP[et]
                r["counts"][lab] = r["counts"].get(lab, 0) + 1          # keyed by the (singular, plural) pair
                if len(r["ad_names"]) < 40:
                    r["ad_names"].append(name)
                if e.get("actor_name"):
                    r["actors"].add(e["actor_name"])
                r["when"] = max(r["when"], base["when"])
                continue
            level = "Campaign" if e.get("object_type") == "CAMPAIGN_GROUP" else "Ad set"
            if et in BUDGET:
                title = f"{level} budget {name}: {_money(x.get('old_value'))} → {_money(x.get('new_value'))}"
                kind = "meta_budget"
            elif et in BIDDING:
                ov, nv = x.get("old_value"), x.get("new_value")
                roas = x.get("adset_minimum_return_on_ad_spend") or {}
                extra = (f", min ROAS {roas.get('old_value')} → {roas.get('new_value')}"
                         if roas.get("new_value") not in (None, roas.get("old_value")) else "")
                title = (f"Ad set bid strategy {name}: {ov} → {nv}{extra}" if ov else
                         f"Ad set bid strategy {name} set to {nv or '–'}{extra}")
                kind = "bidding"
            elif et in STATUS:
                ov, nv = x.get("old_value"), x.get("new_value")
                if ov in (None, "Pending process"):      # part of creation, shown by the create row
                    continue
                title = f"{STATUS[et]} {name}: {str(ov).lower()} → {str(nv).lower()}"
                kind = "status"
            elif et in CREATE:
                nv = x.get("new_value")
                budget = f", budget {_money(nv)}" if isinstance(nv, dict) and nv.get("type") == "payment_amount" else ""
                title = f"{CREATE[et]} created: {name}{budget}"
                kind = "status"
            else:
                continue
            events.append(dict(base, kind=kind, market=market_of(name), title=title,
                               detail=e.get("translated_event_type") or et))
    merged: dict[tuple, dict] = {}                   # ad sets → their campaign
    for (aid, day, cid), r in rolls.items():
        cname = names.get(cid, "")
        m = merged.setdefault((aid, day, cname or cid), dict(r, counts={}, ad_names=[], actors=set(),
                                                              campaign=cname))
        for lab, n in r["counts"].items():
            m["counts"][lab] = m["counts"].get(lab, 0) + n
        m["ad_names"] += r["ad_names"][:10]
        m["actors"] |= r["actors"]
        m["when"] = max(m["when"], r["when"])
    for r in merged.values():
        cname = r.pop("campaign")
        parts = ", ".join(f"{n} {lab[0] if n == 1 else lab[1]}"
                          for lab, n in sorted(r["counts"].items(), key=lambda kv: -kv[1]))
        events.append({k: v for k, v in r.items() if k not in ("counts", "ad_names", "actors")} | {
            "market": market_of(cname, *r["ad_names"]),
            "title": f"{parts} in " + (cname or ("an unknown campaign" if r["campaign_id"] == "?"
                                                 else f"campaign {r['campaign_id']}")),
            "detail": "",
            "by": ", ".join(sorted(r["actors"]))[:80],
            "count": sum(r["counts"].values())})
    return events, notes
