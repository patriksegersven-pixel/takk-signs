"""
bm_insights_verify.py: did the team do it? Deterministic checks for checklist items
and accepted insight cards, run by bm_insights.build() and by the daily agent
(`python3 bm_insights.py verify`).

A spec is {"type": <VERIFIERS key>, ...params}. Every check returns

    {"result": "done" | "not_done" | "cannot_verify", "evidence": "...", "checked_at": iso}

It never raises: missing data or credentials give cannot_verify with the reason.
A manual tick on the page always wins over a check (bm_insights.checklist()).
All reads are read-only: section snapshots, BigQuery roas_sims.target_changes,
Google Ads GAQL, and Meta Graph GETs on the three allowed accounts only.
"""
from __future__ import annotations

import datetime as dt
import json
import re

import bm_common as bm

BABYSHOP_META = "2927326560764919"          # Babyshop SE - New: where the November plan runs
NORDIC = ["SE", "NO", "FI", "DK"]


def _r(result: str, evidence: str) -> dict:
    return {"result": result, "evidence": evidence, "checked_at": bm.now().isoformat(timespec="seconds")}


def _num(v):
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


# ── context: everything a check may read, fetched lazily and cached ──────────

class Ctx:
    def __init__(self, today: dt.date, snaps: dict, troas: list[dict] | None, ads_events: list[dict] | None,
                 seasonality: list[dict] | None, meta_events: list[dict] | None):
        self.today, self.snaps = today, snaps
        self.troas, self.ads_events = troas, ads_events
        self.seasonality, self.meta_events = seasonality, meta_events
        self._cache: dict = {}

    def meta(self, key: str, path: str, params: dict):
        """Meta Graph GET on one of the three allowed accounts (bm_insights_meta.ACCOUNTS)."""
        import os
        import bm_insights_meta as bmm
        if key not in self._cache:
            acct = params.pop("_acct", BABYSHOP_META)
            if acct not in {a["id"] for a in bmm.ACCOUNTS}:
                raise ValueError("account not allowed")
            tok = (os.environ.get("META_ADS_ACCESS_TOKEN") or "").strip()
            if not tok:
                raise RuntimeError("META_ADS_ACCESS_TOKEN not set")
            rows, url = [], f"{bmm.GRAPH}/act_{acct}/{path}"
            for _ in range(10):
                d = bmm._get(url, params, tok)
                rows += d.get("data") or []
                url, params = (d.get("paging") or {}).get("next"), None
                if not url:
                    break
            self._cache[key] = rows
        return self._cache[key]


# ── checks ───────────────────────────────────────────────────────────────────

def v_troas_logged(c: Ctx, s: dict) -> dict:
    """tROAS changes in roas_sims.target_changes since `since` (optionally for `campaigns`,
    `tag` substring, `direction` up|down). Spec: {since, campaigns?, tag?, direction?, min?}."""
    if c.troas is None:
        return _r("cannot_verify", "target_changes could not be read")
    since = s.get("since") or "2026-11-01"
    camps = {x.lower() for x in s.get("campaigns") or []}
    hits = []
    for g in c.troas:
        if g["date"] < since or (s.get("tag") and s["tag"] not in (g.get("tag") or "")):
            continue
        if s.get("brand") and g.get("brand") not in (s["brand"], None):
            continue
        for it in g["items"]:
            if camps and (it.get("campaign") or "").lower() not in camps:
                continue
            st = it.get("step") or 0
            if s.get("direction") == "up" and st <= 0 or s.get("direction") == "down" and st >= 0:
                continue
            hits.append(f"{g['date']} {g['account']} {it['campaign']} {it['old']}→{it['new']}")
    need = s.get("min", len(camps) or 1)
    if len(hits) >= need:
        return _r("done", f"{len(hits)} logged change(s): " + "; ".join(hits[:4]) + (" …" if len(hits) > 4 else ""))
    return _r("not_done", f"no matching tROAS change logged since {since}"
              + (f" for {', '.join(sorted(camps))}" if camps else ""))


def v_seasonality_present(c: Ctx, s: dict) -> dict:
    """Post-BF dag 1/2 in all nine Google accounts. Spec: {prefix, accounts}."""
    if c.seasonality is None:
        return _r("cannot_verify", "Google Ads not reachable (credentials missing in this run)")
    pre = (s.get("prefix") or "post-bf").lower()
    hits = [x for x in c.seasonality if x["name"].lower().startswith(pre)]
    accts = {x["account"] for x in hits}
    d1 = sum(x["start"].startswith("2026-12-01") for x in hits)
    d2 = sum(x["start"].startswith("2026-12-02") for x in hits)
    n = s.get("accounts", 9)
    ok = len(accts) >= n and d1 >= n and d2 >= n
    return _r("done" if ok else "not_done", f"found in {len(accts)}/{n} accounts (dag 1: {d1}, dag 2: {d2})")


def v_seasonality_modifiers(c: Ctx, s: dict) -> dict:
    """Post-BF modifiers equal `expect` [day1, day2] everywhere (e.g. softened to 0.70/0.85)."""
    if c.seasonality is None:
        return _r("cannot_verify", "Google Ads not reachable (credentials missing in this run)")
    exp = s.get("expect") or [0.70, 0.85]
    hits = [x for x in c.seasonality if x["name"].lower().startswith("post-bf")]
    d1 = {round(x["modifier"], 2) for x in hits if x["start"].startswith("2026-12-01")}
    d2 = {round(x["modifier"], 2) for x in hits if x["start"].startswith("2026-12-02")}
    ok = d1 == {exp[0]} and d2 == {exp[1]}
    return _r("done" if ok else "not_done",
              f"day 1 modifiers {sorted(d1)}, day 2 {sorted(d2)} (softened = {exp[0]}/{exp[1]})"
              + ("" if ok else "; only needed if tROAS stays raised through BF"))


def v_meta_setup(c: Ctx, s: dict) -> dict:
    """Approved plan: one ad set per country with min/max spend share, catalogue retargeting
    as ONE multi-country ad set. Reads active ad sets in Babyshop SE - New."""
    try:
        rows = c.meta("adsets_active", "adsets", {
            "fields": "name,daily_min_spend_target,daily_spend_cap,targeting{geo_locations},campaign{name,daily_budget}",
            "limit": 200, "filtering": json.dumps([{"field": "effective_status", "operator": "IN", "value": ["ACTIVE"]}])})
    except Exception as e:                       # noqa: BLE001
        return _r("cannot_verify", f"Meta API: {e}")
    def countries(a):
        return [x for x in ((a.get("targeting") or {}).get("geo_locations") or {}).get("countries") or [] if x in NORDIC]
    limited = {m for a in rows if a.get("daily_min_spend_target") or a.get("daily_spend_cap") for m in countries(a)}
    cat = [a for a in rows if re.search(r"catalog|dpa", (a.get("campaign") or {}).get("name", "") + a.get("name", ""), re.I)]
    multi = [a for a in cat if len(countries(a)) >= 2]
    ok = len(limited) >= 3 and bool(multi)
    return _r("done" if ok else "not_done",
              f"{len(rows)} active ad sets; spend limits set for {sorted(limited) or 'no market'}; "
              f"catalogue retargeting: {len(cat)} ad set(s), {len(multi)} multi-country")


def v_naming_adoption(c: Ctx, s: dict) -> dict:
    """Share of NEW ads (creatives[].new) the creatives section could parse (tagged)."""
    cr = c.snaps.get("creatives")
    if not cr:
        return _r("cannot_verify", "no creatives snapshot")
    new = [r for r in cr.get("creatives") or [] if r.get("new")]
    if not new:
        return _r("cannot_verify", "no new ads in the creatives window yet")
    share = sum(1 for r in new if r.get("tagged")) / len(new)
    th = s.get("threshold", 0.8)
    return _r("done" if share >= th else "not_done",
              f"{sum(1 for r in new if r.get('tagged'))}/{len(new)} new ads follow the convention ({share:.0%}; target {th:.0%})")


def v_meta_ads_named(c: Ctx, s: dict) -> dict:
    """Ads created since `since` whose name matches `pattern`; `approved` → none pending/
    rejected; `inactive_after` → all of them off after that date. Spec: {pattern, since,
    approved?, inactive_after?, accounts?}."""
    import bm_insights_meta as bmm
    pat = re.compile(s.get("pattern") or r"black|\bbf\b|\bbw\b|cyber", re.I)
    since = s.get("since") or "2026-11-01"
    accts = s.get("accounts") or [a["id"] for a in bmm.ACCOUNTS]
    found = []
    try:
        for a in accts:
            rows = c.meta(f"ads_since_{a}_{since}", "ads", {
                "_acct": a, "fields": "name,effective_status,created_time", "limit": 500,
                "filtering": json.dumps([{"field": "created_time", "operator": "GREATER_THAN",
                                          "value": int(dt.datetime.fromisoformat(since).timestamp())}])})
            found += [r for r in rows if pat.search(r.get("name", ""))]
    except Exception as e:                       # noqa: BLE001
        return _r("cannot_verify", f"Meta API: {e}")
    if not found:
        return _r("not_done", f"no ads matching /{pat.pattern}/ created since {since}")
    from collections import Counter
    st = Counter(r.get("effective_status") for r in found)
    desc = ", ".join(f"{n} {k.lower()}" for k, n in st.most_common())
    if s.get("inactive_after") and c.today.isoformat() >= s["inactive_after"]:
        live = st.get("ACTIVE", 0)
        return _r("done" if not live else "not_done", f"{len(found)} matching ads: {desc}")
    if s.get("approved"):
        bad = st.get("PENDING_REVIEW", 0) + st.get("DISAPPROVED", 0) + st.get("WITH_ISSUES", 0)
        return _r("done" if not bad else "not_done", f"{len(found)} matching ads: {desc}")
    return _r("done", f"{len(found)} matching ads: {desc}")


def v_kill_paused(c: Ctx, s: dict) -> dict:
    """No ACTIVE Kill-verdict creative with spend ≥ min_spend in the judging window."""
    cr = c.snaps.get("creatives")
    if not cr:
        return _r("cannot_verify", "no creatives snapshot")
    ms = s.get("min_spend", 500)
    live = [r for r in cr.get("creatives") or [] if str(r.get("verdict")).lower() == "kill"
            and str(r.get("status", "")).upper() == "ACTIVE" and (_num(r.get("spend")) or 0) >= ms
            and (not s.get("market") or r.get("market") == s["market"])
            and (not s.get("names") or any(n.lower() in (r.get("name") or r.get("ad_name") or "").lower() for n in s["names"]))]
    if not live:
        return _r("done", "no active Kill creative is still spending")
    return _r("not_done", f"{len(live)} Kill creative(s) still active: "
              + "; ".join((r.get("name") or "?")[:40] + f" ({r.get('market')}, {round(_num(r.get('spend')) or 0)} kr)" for r in live[:3]))


def v_lekmer_meta(c: Ctx, s: dict) -> dict:
    cr = c.snaps.get("creatives")
    if not cr:
        return _r("cannot_verify", "no creatives snapshot")
    n = sum(1 for r in (cr.get("creatives") or []) + (cr.get("dpa") or []) if r.get("brand") == "lekmer")
    return _r("done" if n else "not_done", f"{n} Lekmer rows in the creatives section")


def v_oos_spend_below(c: Ctx, s: dict) -> dict:
    """offers stock.ad_spend.<brand>.<market>.oos_7d ≤ max_sek. Spec: {brand, market, max_sek}."""
    o = c.snaps.get("offers")
    if not o:
        return _r("cannot_verify", "no offers snapshot")
    br, m = s.get("brand") or "all", s.get("market") or "all"
    blk = ((o.get("stock") or {}).get("ad_spend") or {}).get(br, {}).get(m) or {}
    v = _num(blk.get("oos_7d"))
    if v is None:
        return _r("cannot_verify", f"offers has no OOS spend for {br}/{m}")
    mx = s.get("max_sek", 1000)
    return _r("done" if v <= mx else "not_done", f"{round(v)} SEK in 7 d on out-of-stock days ({br} {m}; limit {mx})")


def v_meta_budget_change(c: Ctx, s: dict) -> dict:
    """Meta activity log budget change since `since` (optionally `campaigns` substrings,
    `direction` up|down)."""
    if c.meta_events is None:
        return _r("cannot_verify", "Meta activity log not loaded")
    since = s.get("since") or "2026-11-01"
    names = [x.lower() for x in s.get("campaigns") or []]
    hits = [e for e in c.meta_events if e.get("kind") == "meta_budget" and e["date"] >= since
            and (not names or any(n in e["title"].lower() for n in names))]
    if s.get("direction"):
        def up(e):
            m = re.findall(r"([\d ]+)\s*SEK", e["title"])
            try:
                return float(m[1].replace(" ", "")) > float(m[0].replace(" ", ""))
            except (IndexError, ValueError):
                return None
        hits = [e for e in hits if up(e) is (s["direction"] == "up")]
    if hits:
        return _r("done", f"{len(hits)} budget change(s): " + "; ".join(e["title"][:70] for e in hits[:3]))
    return _r("not_done", f"no matching Meta budget change since {since}")


def v_google_change(c: Ctx, s: dict) -> dict:
    """Google Ads change history (budget/status/bidding) since `since` for `campaigns`."""
    if c.ads_events is None:
        return _r("cannot_verify", "Google Ads change history not loaded in this run")
    since = s.get("since") or "2026-11-01"
    kinds = set(s.get("kinds") or ["google_budget", "status", "bidding"])
    names = [x.lower() for x in s.get("campaigns") or []]
    hits = [e for e in c.ads_events if e["date"] >= since and e["kind"] in kinds
            and (not names or any(n in e["title"].lower() for n in names))]
    if hits:
        return _r("done", f"{len(hits)} change(s): " + "; ".join(e["title"][:70] for e in hits[:3]))
    return _r("not_done", f"no matching Google Ads change since {since}")


def v_google_promotions(c: Ctx, s: dict) -> dict:
    """A promotion asset covering Black Friday in each Babyshop Nordic Google account."""
    try:
        import refresh_roas_sims as rrs
        svc = rrs.ads_client().get_service("GoogleAdsService")
    except Exception as e:                       # noqa: BLE001
        return _r("cannot_verify", f"Google Ads not reachable ({type(e).__name__})")
    day = s.get("covers", "2026-11-27")
    accts = {"4851485396": "SE", "8623945183": "NO", "6161399704": "FI", "2054294342": "DK"}
    have = []
    for cid, m in accts.items():
        try:
            rows = list(rrs._search(svc, cid, f"""
                SELECT asset.id, asset.promotion_asset.start_date, asset.promotion_asset.end_date
                FROM asset WHERE asset.type = 'PROMOTION'
                  AND asset.promotion_asset.start_date <= '{day}' AND asset.promotion_asset.end_date >= '{day}'"""))
            if rows:
                have.append(m)
        except Exception as e:                   # noqa: BLE001
            return _r("cannot_verify", f"Google Ads query failed ({type(e).__name__})")
    ok = len(have) == len(accts)
    return _r("done" if ok else "not_done", f"promotion covering {day} in {have or 'no account'} of SE/NO/FI/DK")


def v_all_of(c: Ctx, s: dict) -> dict:
    res = [run(c, x) for x in s.get("checks") or []]
    if any(r["result"] == "not_done" for r in res):
        out = "not_done"
    elif any(r["result"] == "cannot_verify" for r in res):
        out = "cannot_verify"
    else:
        out = "done"
    return _r(out, " · ".join(r["evidence"] for r in res))


def v_manual(c: Ctx, s: dict) -> dict:
    return _r("cannot_verify", s.get("why") or "not visible in our data: tick it manually")


VERIFIERS = {
    "troas_logged": v_troas_logged, "seasonality_present": v_seasonality_present,
    "seasonality_modifiers": v_seasonality_modifiers, "meta_setup": v_meta_setup,
    "naming_adoption": v_naming_adoption, "meta_ads_named": v_meta_ads_named, "kill_paused": v_kill_paused,
    "lekmer_meta": v_lekmer_meta, "oos_spend_below": v_oos_spend_below,
    "meta_budget_change": v_meta_budget_change, "google_change": v_google_change,
    "google_promotions": v_google_promotions, "all_of": v_all_of, "manual": v_manual,
}


def run(c: Ctx, spec: dict | None) -> dict:
    if not spec:
        return _r("cannot_verify", "no check defined: tick it manually")
    fn = VERIFIERS.get(spec.get("type"))
    if not fn:
        return _r("cannot_verify", f"unknown check {spec.get('type')!r}")
    try:
        return fn(c, dict(spec))
    except Exception as e:                       # noqa: BLE001 — a check never sinks the build
        return _r("cannot_verify", f"check failed: {type(e).__name__}: {str(e)[:120]}")


def card_spec(card: dict) -> dict | None:
    """The card's own `verify`, or a default from its lever when it names campaigns."""
    if card.get("verify"):
        v = dict(card["verify"])
        v.setdefault("since", (card.get("decided_at") or card.get("created") or "")[:10] or None)
        return v
    return None
