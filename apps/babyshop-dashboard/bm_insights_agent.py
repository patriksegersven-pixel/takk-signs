#!/usr/bin/env python3
"""
bm_insights_agent.py: tools for the daily Black Month insights agent.

A scheduled Claude task (weekdays ~07:30 Stockholm) follows the prompt in
bm_insights_agent.md. This module gives it deterministic inputs so the model
spends its effort on judgement, not plumbing:

    python3 bm_insights_agent.py context --dir <dir> [--mock]
        Writes <dir>/digest.json (compact: key numbers per market, rule alerts,
        checklist, open cards, cards due for scoring, recent decisions, change
        log) and <dir>/<section>.json (each full section snapshot, for drilling in).
    python3 bm_insights_agent.py measure --market SE --start 2026-11-06 [--days 5]
        Market-level GP3 effect estimate for scoring a card: pacing's daily
        GP3 actual/target ratio after `start` vs the same number of days before,
        turned into SEK, with a noise band. Prints JSON.

Cards are written with `python3 bm_insights.py add --file cards.json` and scored
with `python3 bm_insights.py score ...`. Nothing here touches an ad platform.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import statistics
import sys
from pathlib import Path

import bm_common as bm
import bm_insights as bi


def _pacing_block(src: dict) -> dict:
    out = {"markets": {}, "headroom": {}}
    for m, blk in (src.get("markets") or {}).items():
        s = (blk or {}).get("summary") or {}
        out["markets"][m] = {k: {x: s.get(k, {}).get(x) for x in
                                 ("mtd", "target_mtd", "ly_mtd", "landing", "target_month", "run_rate_ratio")}
                             for k in ("gp2", "cost", "gp3", "net")}
    for m, h in (src.get("headroom") or {}).items():     # daily arrays left out
        out["headroom"][m] = {k: v for k, v in (h or {}).items() if not isinstance(v, list)}
    out["customers"] = src.get("customers")
    return out


def _digest_pacing(p: dict | None) -> dict | None:
    """Combined Babyshop + Lekmer, plus `brands.<b>` with the same shape when pacing splits by brand."""
    if not p:
        return None
    out = {"asof": p.get("asof"), "scope": p.get("scope"), "budget_month": p.get("budget_month"),
           **_pacing_block(p)}
    if isinstance(p.get("brands"), dict):
        out["brands"] = {b: _pacing_block(v or {}) for b, v in p["brands"].items()}
    return out


def _digest_paid(p: dict | None) -> dict | None:
    if not p:
        return None
    g, me = p.get("google") or {}, p.get("meta") or {}
    camps = sorted(g.get("campaigns") or [], key=lambda c: -(c.get("l7_cost_sek") or 0))
    keep = ("market", "name", "channel", "segment", "troas", "budget_day_sek", "mtd_cost_sek", "mtd_roas",
            "l7_cost_sek", "l7_roas", "l14_roas", "is", "lost_budget", "lost_rank", "budget_limited",
            "cooldown_until", "marginal_roas", "incrementality", "inc_marginal", "inc_gp3_per_10k",
            "headroom_why", "suggestion")
    return {"asof": p.get("asof"), "plan_meta": p.get("plan"), "guardrail": p.get("guardrail"),
            "gp3_l14": (p.get("gp3") or {}).get("l14"), "gp3_mtd": (p.get("gp3") or {}).get("mtd"),
            "marginal": p.get("marginal"),
            # top 20 by 7-day spend, plus every other campaign with a tROAS suggestion or a budget cap
            "google_campaigns": [{k: c.get(k) for k in keep} for i, c in enumerate(camps)
                                 if i < 20 or (c.get("suggestion") or {}).get("action") in ("lower", "raise")
                                 or c.get("budget_limited")],
            "meta_lines": [{k: v for k, v in l.items() if k != "fit"} for l in me.get("lines") or []],
            "meta_brand_note": p.get("meta_brand_note"),
            "meta_campaigns": me.get("campaigns"),
            "meta_market_type_mtd": (me.get("pacing") or {}).get("market_type_mtd"),
            "notes": p.get("notes")}


def _digest_creatives(c: dict | None) -> dict | None:
    if not c:
        return None
    keep = ("market", "name", "campaign", "type", "format", "status", "spend", "nov_spend", "purchases",
            "roas", "cpa", "rel_roas", "verdict", "reason", "days_live")
    rows = c.get("creatives") or []
    pick = [r for r in rows if r.get("verdict") in ("Kill", "Scale")]
    pick += sorted([r for r in rows if r not in pick], key=lambda r: -(r.get("spend") or 0))[:15]
    return {"windows": c.get("windows"), "market_ref": c.get("market_ref"),
            "creatives": [{k: r.get(k) for k in keep} for r in pick],
            "dpa": [{k: r.get(k) for k in keep} for r in (c.get("dpa") or [])]}


def _digest_offers(o: dict | None) -> dict | None:
    if not o:
        return None
    st = o.get("stock") or {}
    prods = st.get("products") or []
    flagged = [r for r in prods if r.get("flag") not in (None, "ok")]
    top = sorted(prods, key=lambda r: -sum((r.get("ad7") or {}).values() or [0]))[:20]
    seen, keep = set(), []
    for r in flagged + top:
        if r.get("id") not in seen:
            seen.add(r.get("id"))
            keep.append({k: r.get(k) for k in ("id", "title", "cat", "price", "sale", "depth", "units14",
                                                "stock", "cover", "cover_bw", "flag", "ad7", "ad7_oos", "ad7_low")})
    eco = o.get("economics") or {}
    return {"offers": o.get("offers"), "context": o.get("context"), "width": o.get("width"),
            "economics": {k: v for k, v in eco.items() if k != "daily"},
            "stock": {"bw_uplift": st.get("bw_uplift"), "ad_spend": st.get("ad_spend"),
                      "sold_not_in_feed": st.get("sold_not_in_feed"), "products": keep[:50]},
            "notes": o.get("notes")}


def due_for_scoring(cards: list[dict], today: dt.date) -> list[dict]:
    out = []
    for c in cards:
        if c.get("status") not in ("accepted", "done") or c.get("outcome"):
            continue
        start = (c.get("done_at") or c.get("decided_at") or "")[:10]
        if not start:
            continue
        age = (today - dt.date.fromisoformat(start)).days
        if age >= bi.SCORE_AFTER_DAYS:
            out.append({"id": c["id"], "market": c.get("market"), "lever": c.get("lever"),
                        "headline": c.get("headline"), "impact": c.get("impact"),
                        "start": start, "age_days": age, "overdue": age > bi.SCORE_BY_DAYS})
    return out


def _mock(key: str) -> dict | None:
    f = bm.HERE / "bm-mock" / f"{key}.json"
    return json.loads(f.read_text()) if f.is_file() else None


def context(out_dir: str, mock: bool = False) -> dict:
    """mock=True reads bm-mock/*.json instead of Firestore (dry runs, no writes anywhere)."""
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    today = bm.today()
    snaps = {k: _mock(k) for k in bi.SECTION_KEYS} if mock else bi.read_snapshots()
    for k, v in snaps.items():
        (d / f"{k}.json").write_text(json.dumps(v, default=str))
    insights = (_mock("insights") if mock else bm.read_section("insights")) or {}
    cards = insights.get("cards", []) if mock else bi.list_cards(days=45)
    alerts, notes = bi.rule_alerts(snaps, today)
    ver = {c["id"]: c.get("verification") for c in insights.get("cards") or [] if c.get("verification")}
    for c in cards:                                  # the job's latest check per card
        if c["id"] in ver:
            c["verification"] = ver[c["id"]]
    chk = insights.get("checklist") or []
    overdue = [{k: i.get(k) for k in ("id", "due", "until", "title", "brand", "lever", "owner", "state", "check")}
               for i in chk if (i.get("until") or i.get("due", "9")) < today.isoformat()
               and i.get("state") not in ("done", "verified", "explained")]
    digest = {
        "today": today.isoformat(),
        "days_to_black_friday": (bm.BLACK_FRIDAY - today).days,
        "spend_policy": ("Google has no budget; maximise GP3. COS 10.5 % is a derived envelope, not a cap; "
                         "the binding target is the forecast's GP3. GP3 above forecast = headroom to spend more "
                         "on Google AND Meta where incremental. Meta 200k plan = starting point, not a ceiling. "
                         "Incrementality factors are untested assumptions."),
        "cos_target": getattr(bm, "COS_TARGET", None),
        "incrementality_assumed": getattr(bm, "INCREMENTALITY", None),
        "meta_plan_start": {"month": bm.META_PLAN_MONTH, "by_type": bm.META_PLAN_BY_TYPE,
                            "share": bm.META_PLAN_MARKET_SHARE},
        "finance_forecast": bm.budget_lines(),
        "snapshot_times": {k: (v or {}).get("generated_at") for k, v in snaps.items()},
        "rule_alerts": alerts, "notes": notes,
        "checklist": chk,
        "checklist_overdue_not_done": overdue,
        "verify_cmd": "python3 bm_insights.py verify --json  (re-runs every check now; needs GOOGLE_ADS_* and "
                      "META_ADS_ACCESS_TOKEN in env for the Ads/Meta checks, else those come back cannot_verify)",
        "change_log_14d": [e for e in (insights.get("events") or [])
                           if e.get("date", "") >= (today - dt.timedelta(days=14)).isoformat()],
        "open_cards": [c for c in cards if c.get("status") in ("proposed", "accepted")],
        "due_for_scoring": due_for_scoring(cards, today),
        "recent_decisions": [{k: c.get(k) for k in ("id", "created", "market", "lever", "headline", "status",
                                                    "decided_by", "impact", "outcome")}
                             for c in cards if c.get("status") in ("rejected", "done", "measured")][:25],
        "hit_rate": bi.hit_rate(cards),
        "pacing": _digest_pacing(snaps.get("pacing")),
        "paid": _digest_paid(snaps.get("paid")),
        "creatives": _digest_creatives(snaps.get("creatives")),
        "offers": _digest_offers(snaps.get("offers")),
    }
    (d / "digest.json").write_text(json.dumps(digest, indent=1, default=str, ensure_ascii=False))
    return {"dir": str(d), "digest_kb": round(len(json.dumps(digest, default=str)) / 1024),
            "sections": {k: bool(v) for k, v in snaps.items()},
            "open_cards": len(digest["open_cards"]), "due_for_scoring": len(digest["due_for_scoring"]),
            "rule_alerts": len(alerts)}


def measure(market: str, start: str, days: int = 5, pacing: dict | None = None) -> dict:
    """GP3 effect after `start` for one market, net of the plan's day shape.

    ratio_d = actual GP3 / target GP3 per complete day. Baseline = Σactual/Σtarget
    over the `days` days before start; effect = Σ(actual − baseline × target) over
    the `days` days from start. Noise = sd of the daily (actual − baseline × target)
    in the pre-window × √n. |effect| < noise → the market moved within normal noise,
    so a card on a small lever should be scored 'inconclusive'."""
    p = pacing or bm.read_section("pacing") or {}
    blk = (p.get("markets") or {}).get(market or "all")
    if not blk:
        return {"ok": False, "error": f"no pacing data for market {market!r}"}
    days_list = p.get("days") or []
    asof = p.get("asof") or ""
    act = (blk.get("actual") or {}).get("gp3") or []
    tgt = ((blk.get("targets") or {}).get("event") or {}).get("gp3") or []
    idx = {d: i for i, d in enumerate(days_list)}
    s = dt.date.fromisoformat(start)
    pre = [(s - dt.timedelta(days=k)).isoformat() for k in range(days, 0, -1)]
    post = [(s + dt.timedelta(days=k)).isoformat() for k in range(days)]
    post = [d for d in post if d <= asof]

    def vals(ds):
        return [(act[idx[d]], tgt[idx[d]]) for d in ds
                if d in idx and idx[d] < len(act) and act[idx[d]] is not None and tgt[idx[d]]]
    pv, qv = vals(pre), vals(post)
    if len(pv) < 2 or not qv:
        return {"ok": False, "error": "not enough complete days around start "
                f"(pre {len(pv)} incl. only November days, post {len(qv)}, data to {asof})"}
    base = sum(a for a, _ in pv) / sum(t for _, t in pv)
    eff = sum(a - base * t for a, t in qv)
    resid = [a - base * t for a, t in pv]
    noise = (statistics.pstdev(resid) if len(resid) > 1 else 0) * math.sqrt(len(qv))
    return {"ok": True, "market": market, "start": start, "pre_days": len(pv), "post_days": len(qv),
            "baseline_ratio": round(base, 3),
            "post_ratio": round(sum(a for a, _ in qv) / sum(t for _, t in qv), 3),
            "effect_gp3_sek": round(eff), "noise_sek": round(noise),
            "within_noise": abs(eff) < noise,
            "method": (f"pacing GP3 actual/target ratio, {len(qv)} days from {start} vs {len(pv)} days before "
                       f"(baseline {base:.2f}); effect = Σ(actual − baseline×target); noise ±{round(noise)} SEK")}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("context"); c.add_argument("--dir", required=True)
    c.add_argument("--mock", action="store_true", help="read bm-mock/*.json instead of Firestore")
    m = sub.add_parser("measure"); m.add_argument("--market", default="all")
    m.add_argument("--start", required=True); m.add_argument("--days", type=int, default=5)
    m.add_argument("--pacing-file", help="use a local pacing payload (e.g. bm-mock/pacing.json)")
    a = ap.parse_args(argv)
    if a.cmd == "context":
        print(json.dumps(context(a.dir, a.mock), indent=1))
    else:
        pacing = json.loads(Path(a.pacing_file).read_text()) if a.pacing_file else None
        print(json.dumps(measure(a.market, a.start, a.days, pacing), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
