#!/usr/bin/env python3
"""
bm_insights.py: the "Today's actions" section of the Black Month tab.

Four things, all aimed at "what do we do today to lift November GP3":

  cards       Insight cards in Firestore `blackmonth_insights`, written by the
              daily agent (bm_insights_agent.py) or by hand through this CLI.
              The page shows today's and still-open cards with Accept / Reject /
              Done buttons (bm_insights_api.py) and a history with a hit rate.
  alerts      Deterministic rules over the other sections' stored snapshots
              (bm_common.read_section), so the section is useful without the
              agent. Every rule reads its inputs defensively: a missing section
              or key skips the rule and leaves a note, never an exception.
  change log  tROAS changes (BQ roas_sims.target_changes), Google Ads
              change_event (budgets, bidding strategy, status; read-only) and
              the current Black Month seasonality adjustments, plus manual
              entries posted from the page (Firestore `blackmonth_events`).
  checklist   Dated to-dos for the month; some auto-check themselves from the
              data above, the rest are ticked on the page.

GP3 = GP2 − ad cost, in SEK. Every card states its expected GP3 impact as a
range so it can be scored 3–7 days after it is done.

CLI (used by the agent and for manual work):

    python3 bm_insights.py add --file cards.json         # 1 card or a list
    python3 bm_insights.py list [--status proposed] [--days 14]
    python3 bm_insights.py score --id <id> --verdict worked|no_effect|backfired|inconclusive \
        --measured 42000 --method "..." [--by agent]
    python3 bm_insights.py set-status --id <id> --status accepted --by Patrik
    python3 bm_insights.py event --date 2026-11-06 --kind offer_launch --market SE \
        --title "Kuling −30 % live" [--detail ...]
    python3 bm_insights.py validate --file cards.json    # check without writing

Read-only against every ad platform: this module only SELECTs.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Iterable

import bm_common as bm

CARDS = "blackmonth_insights"
EVENTS = "blackmonth_events"

LEVERS = ["google_troas", "google_budget", "meta_allocation", "creative",
          "offer", "stock", "site", "other"]
STATUSES = ["proposed", "accepted", "rejected", "done", "measured"]
CONFIDENCE = ["low", "medium", "high"]
VERDICTS = ["worked", "no_effect", "backfired", "inconclusive"]
EVENT_KINDS = ["offer_launch", "offer_end", "meta_budget", "google_budget", "troas",
               "seasonality", "bidding", "status", "site", "stock", "checklist", "other"]
MARKET_VALUES = bm.MARKETS + ["ROW", "all"]
SECTION_KEYS = [s for s in bm.SECTIONS if s != "insights"]

# Scoring window: an accepted/done card is scored once it is this old.
SCORE_AFTER_DAYS = 3
SCORE_BY_DAYS = 7

# Rule thresholds (one place, so the page can show them next to the alerts).
RULES = {
    "gp3_behind_pct": 0.05,        # MTD GP3 more than 5 % behind target-to-date
    "spend_pace_pct": 0.15,        # market spend ±15 % off plan-to-date
    "budget_limited_headroom": 1.10,   # profit ROAS ≥ 110 % of tROAS while budget-limited
    "kill_spend_sek": 500,         # a Kill creative that spent ≥ 500 SEK in the window
    "oos_spend_sek": 300,          # out-of-stock hero product with ≥ 300 SEK ad spend
    "troas_step_pct": 0.20,        # a logged tROAS step above ±20 %
}


# ════════════════════════════════════════════════════════════════════════════
#  Firestore
# ════════════════════════════════════════════════════════════════════════════

_db_singleton = None


def _db():
    global _db_singleton
    if _db_singleton is None:
        from google.cloud import firestore
        _db_singleton = firestore.Client(project=bm.GCP_PROJECT)
    return _db_singleton


def _ff(field: str, op: str, value):
    from google.cloud.firestore_v1.base_query import FieldFilter
    return FieldFilter(field, op, value)


def _now_iso() -> str:
    return bm.now().isoformat(timespec="seconds")


def card_id(created: str, headline: str, market: str) -> str:
    """Deterministic, so re-running the agent's add is idempotent."""
    h = hashlib.sha1(f"{created}|{market}|{headline.strip().lower()}".encode()).hexdigest()[:8]
    return f"{created.replace('-', '')}-{h}"


def list_cards(days: int | None = 45, status: str | None = None) -> list[dict]:
    q = _db().collection(CARDS)
    if days is not None:
        since = (bm.today() - dt.timedelta(days=days)).isoformat()
        q = q.where(filter=_ff("created", ">=", since))
    out = [d.to_dict() for d in q.stream()]
    if status:
        out = [c for c in out if c.get("status") == status]
    return sorted(out, key=lambda c: (c.get("created", ""), c.get("created_at", "")), reverse=True)


def list_events(days: int = 60) -> list[dict]:
    since = (bm.today() - dt.timedelta(days=days)).isoformat()
    q = _db().collection(EVENTS).where(filter=_ff("date", ">=", since))
    return sorted((d.to_dict() for d in q.stream()),
                  key=lambda e: (e.get("date", ""), e.get("ts", "")), reverse=True)


# ════════════════════════════════════════════════════════════════════════════
#  Cards: validation and writes
# ════════════════════════════════════════════════════════════════════════════

def _num(v) -> float | None:
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def normalise_card(raw: dict, source: str = "agent") -> dict:
    """Validate an incoming card and fill defaults. Raises ValueError with every
    problem at once so the agent can fix its file in one go."""
    errs = []
    c = dict(raw)
    c.setdefault("created", bm.today().isoformat())
    c["lever"] = c.get("lever") or "other"
    if c["lever"] not in LEVERS:
        errs.append(f"lever {c['lever']!r} not in {LEVERS}")
    c["market"] = c.get("market") or "all"
    if c["market"] not in MARKET_VALUES:
        errs.append(f"market {c['market']!r} not in {MARKET_VALUES}")
    for k in ("headline", "action"):
        if not str(c.get(k) or "").strip():
            errs.append(f"{k} is required")
    imp = c.get("impact") or {}
    mid, lo, hi = _num(imp.get("gp3_sek")), _num(imp.get("low_sek")), _num(imp.get("high_sek"))
    if mid is None:
        errs.append("impact.gp3_sek (expected GP3 change, SEK) is required")
    else:
        lo = mid if lo is None else lo
        hi = mid if hi is None else hi
        if not lo <= mid <= hi:
            errs.append(f"impact range must satisfy low ≤ gp3 ≤ high, got {lo} / {mid} / {hi}")
    c["impact"] = {"gp3_sek": mid, "low_sek": lo, "high_sek": hi,
                   "horizon": imp.get("horizon") or "rest of November",
                   "basis": imp.get("basis") or ""}
    c["confidence"] = c.get("confidence") or "medium"
    if c["confidence"] not in CONFIDENCE:
        errs.append(f"confidence {c['confidence']!r} not in {CONFIDENCE}")
    ev = c.get("evidence") or []
    if not isinstance(ev, list) or not ev:
        errs.append("evidence must be a non-empty list of {section, metric, value, note?}")
    else:
        for i, e in enumerate(ev):
            if not isinstance(e, dict) or not e.get("metric"):
                errs.append(f"evidence[{i}] needs at least a metric")
            elif e.get("section") and e["section"] not in SECTION_KEYS + ["bq", "ads", "meta", "site", "other"]:
                errs.append(f"evidence[{i}].section {e['section']!r} unknown")
    if errs:
        raise ValueError("; ".join(errs))
    c["status"] = c.get("status") or "proposed"
    if c["status"] not in STATUSES:
        raise ValueError(f"status {c['status']!r} not in {STATUSES}")
    c["source"] = c.get("source") or source
    c["owner"] = c.get("owner") or ""
    c["id"] = c.get("id") or card_id(c["created"], c["headline"], c["market"])
    c.setdefault("created_at", _now_iso())
    c.setdefault("decided_by", None)
    c.setdefault("decided_at", None)
    c.setdefault("outcome", None)
    c.setdefault("history", [])
    keep = {"id", "created", "created_at", "lever", "market", "headline", "action", "rationale",
            "impact", "confidence", "evidence", "status", "owner", "decided_by", "decided_at",
            "done_at", "outcome", "source", "history", "links", "expires"}
    return {k: v for k, v in c.items() if k in keep}


def add_cards(cards: list[dict], source: str = "agent", dry_run: bool = False) -> list[str]:
    norm, errs = [], []
    for i, c in enumerate(cards):
        try:
            norm.append(normalise_card(c, source))
        except ValueError as e:
            errs.append(f"card {i} ({str(c.get('headline') or '')[:40]!r}): {e}")
    if errs:
        raise ValueError("\n".join(errs))
    if dry_run:
        return [c["id"] for c in norm]
    db = _db()
    ids = []
    for c in norm:
        ref = db.collection(CARDS).document(c["id"])
        snap = ref.get()
        if snap.exists:
            # Idempotent re-run: keep the human decision, refresh the content.
            old = snap.to_dict() or {}
            for k in ("status", "owner", "decided_by", "decided_at", "done_at", "outcome", "history", "created_at"):
                if old.get(k) is not None:
                    c[k] = old[k]
        ref.set(c)
        ids.append(c["id"])
    return ids


# Allowed moves. 'proposed' is reachable again so a mis-click can be undone.
TRANSITIONS = {
    "proposed": {"accepted", "rejected", "done"},
    "accepted": {"done", "rejected", "proposed"},
    "rejected": {"proposed", "accepted"},
    "done": {"accepted", "proposed"},
    "measured": set(),
}


def set_status(cid: str, status: str, by: str = "", note: str = "", owner: str | None = None) -> dict:
    if status not in STATUSES or status == "measured":
        raise ValueError(f"status must be one of {[s for s in STATUSES if s != 'measured']}")
    ref = _db().collection(CARDS).document(cid)
    snap = ref.get()
    if not snap.exists:
        raise KeyError(cid)
    c = snap.to_dict()
    cur = c.get("status", "proposed")
    if status != cur and status not in TRANSITIONS.get(cur, set()):
        raise ValueError(f"cannot move {cur} → {status}")
    ts = _now_iso()
    upd = {"status": status,
           "history": (c.get("history") or []) + [{"at": ts, "by": by, "from": cur, "to": status, "note": note}]}
    if status in ("accepted", "rejected"):
        upd.update(decided_by=by or None, decided_at=ts)
    if status == "done":
        upd["done_at"] = ts
        if not c.get("decided_at"):
            upd.update(decided_by=by or None, decided_at=ts)
    if owner is not None:
        upd["owner"] = owner
    ref.update(upd)
    c.update(upd)
    return c


def score_card(cid: str, verdict: str, measured: float | None, method: str, by: str = "agent",
               detail: str = "") -> dict:
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}")
    if not method.strip():
        raise ValueError("method is required: say how the effect was measured")
    ref = _db().collection(CARDS).document(cid)
    snap = ref.get()
    if not snap.exists:
        raise KeyError(cid)
    c = snap.to_dict()
    if c.get("status") not in ("accepted", "done", "measured"):
        raise ValueError(f"only accepted/done cards are scored (this one is {c.get('status')})")
    ts = _now_iso()
    upd = {"status": "measured",
           "outcome": {"verdict": verdict, "measured_gp3_sek": measured, "method": method,
                       "detail": detail, "measured_at": ts, "measured_by": by},
           "history": (c.get("history") or []) + [{"at": ts, "by": by, "from": c.get("status"),
                                                   "to": "measured", "note": verdict}]}
    ref.update(upd)
    c.update(upd)
    return c


def add_event(ev: dict) -> dict:
    e = {"date": ev.get("date") or bm.today().isoformat(),
         "kind": ev.get("kind") or "other",
         "market": ev.get("market") or "all",
         "title": str(ev.get("title") or "").strip()[:200],
         "detail": str(ev.get("detail") or "").strip()[:1000],
         "by": str(ev.get("by") or "")[:80],
         "source": ev.get("source") or "manual",
         "ref": ev.get("ref"),
         "ts": _now_iso()}
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", e["date"]):
        raise ValueError("date must be YYYY-MM-DD")
    if e["kind"] not in EVENT_KINDS:
        raise ValueError(f"kind must be one of {EVENT_KINDS}")
    if e["market"] not in MARKET_VALUES:
        raise ValueError(f"market must be one of {MARKET_VALUES}")
    if not e["title"]:
        raise ValueError("title is required")
    e["id"] = f"{e['date'].replace('-', '')}-{hashlib.sha1((e['ts'] + e['title']).encode()).hexdigest()[:8]}"
    _db().collection(EVENTS).document(e["id"]).set(e)
    return e


def hit_rate(cards: list[dict]) -> dict:
    measured = [c for c in cards if (c.get("outcome") or {}).get("verdict")]
    decided = [c for c in cards if c.get("status") in ("accepted", "rejected", "done", "measured")]
    worked = [c for c in measured if c["outcome"]["verdict"] == "worked"]
    scored = [c for c in measured if c["outcome"]["verdict"] != "inconclusive"]
    exp = sum((c.get("impact") or {}).get("gp3_sek") or 0 for c in measured)
    got = sum((c.get("outcome") or {}).get("measured_gp3_sek") or 0 for c in measured)
    return {"cards": len(cards), "decided": len(decided),
            "accepted": sum(c.get("status") in ("accepted", "done", "measured") for c in cards),
            "rejected": sum(c.get("status") == "rejected" for c in cards),
            "measured": len(measured), "worked": len(worked),
            "hit_rate": (len(worked) / len(scored)) if scored else None,
            "expected_gp3_measured": exp, "measured_gp3": got}


# ════════════════════════════════════════════════════════════════════════════
#  Rule alerts over the other sections' snapshots
# ════════════════════════════════════════════════════════════════════════════

def dig(d: Any, *paths: str, default=None):
    """First non-None value among dotted paths: dig(p, 'mtd.gp3', 'kpis.gp3')."""
    for path in paths:
        cur = d
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                cur = None
                break
        if cur is not None:
            return cur
    return default


def _rows(d: Any, *paths: str) -> list[dict]:
    v = dig(d, *paths)
    if isinstance(v, dict):     # {SE: {...}, NO: {...}} → rows with a market key
        return [dict(market=k, **x) for k, x in v.items() if isinstance(x, dict)]
    return [r for r in (v or []) if isinstance(r, dict)]


def _alert(rule, severity, market, headline, action, section, evidence, lever="other", impact=None):
    return {"rule": rule, "severity": severity, "market": market or "all", "headline": headline,
            "action": action, "section": section, "evidence": evidence, "lever": lever,
            "impact_sek": impact}


def rule_alerts(snaps: dict[str, dict | None], today: dt.date) -> tuple[list[dict], list[str]]:
    """Every rule is wrapped: an unexpected shape becomes a note, not a failure."""
    alerts: list[dict] = []
    notes: list[str] = []
    for key in SECTION_KEYS:
        s = snaps.get(key)
        if not s:
            notes.append(f"No {key} snapshot yet: its rules were skipped.")
        elif s.get("generated_at") and s["generated_at"][:10] < (today - dt.timedelta(days=1)).isoformat():
            notes.append(f"The {key} snapshot is from {s['generated_at'][:10]}; its alerts may be stale.")
    for fn in (_rule_gp3_pacing, _rule_spend_pacing, _rule_budget_limited, _rule_meta_allocation,
               _rule_negative_gp3, _rule_kill_spending, _rule_oos_spend):
        try:
            alerts.extend(fn(snaps, today))
        except Exception as e:                       # noqa: BLE001 — a rule must never sink the section
            notes.append(f"Rule {fn.__name__[6:]} skipped: {type(e).__name__}: {e}")
    sev = {"high": 0, "medium": 1, "low": 2}
    alerts.sort(key=lambda a: (sev.get(a["severity"], 3), -(abs(a.get("impact_sek") or 0))))
    return alerts, notes


def _rule_gp3_pacing(snaps, today):
    """pacing: markets.<m>.summary.gp3 {mtd, target_mtd, landing, target_month},
    and markets.<m>.summary.cost for marketing-cost pacing."""
    p = snaps.get("pacing") or {}
    out = []
    for m, blk in (dig(p, "markets") or {}).items():
        g = dig(blk, "summary.gp3") or {}
        act, tgt = _num(g.get("mtd")), _num(g.get("target_mtd"))
        land, month = _num(g.get("landing")), _num(g.get("target_month"))
        if act is not None and tgt and act / tgt - 1 < -RULES["gp3_behind_pct"]:
            gap = act / tgt - 1
            out.append(_alert(
                "gp3_behind", "high" if gap < -2 * RULES["gp3_behind_pct"] else "medium", m,
                f"MTD GP3 {gap*100:+.0f} % vs target ({_sek(act)} vs {_sek(tgt)})",
                "Find the market/channel behind the gap in Paid media levers; cut spend where "
                "profit ROAS is below break-even before adding budget elsewhere.",
                "pacing",
                [{"section": "pacing", "metric": "MTD GP3", "value": act},
                 {"section": "pacing", "metric": "GP3 target to date", "value": tgt}]
                + ([{"section": "pacing", "metric": "GP3 landing", "value": land}] if land else []),
                "other", act - tgt))
        elif land and month and land / month - 1 < -RULES["gp3_behind_pct"]:
            out.append(_alert(
                "gp3_landing", "medium", m,
                f"GP3 landing {_sek(land)} is {(land/month-1)*100:+.0f} % vs the month target {_sek(month)}",
                "On track to date but the run rate lands short: look for budget-limited winners and "
                "offers to pull forward before Black Week.",
                "pacing", [{"section": "pacing", "metric": "GP3 landing", "value": land},
                           {"section": "pacing", "metric": "GP3 target month", "value": month}],
                "other", land - month))
        c = dig(blk, "summary.cost") or {}
        cact, ctgt = _num(c.get("mtd")), _num(c.get("target_mtd"))
        if cact is not None and ctgt and abs(cact / ctgt - 1) > RULES["spend_pace_pct"] and (today.day >= 4):
            dev = cact / ctgt - 1
            gp3_ok = act is not None and tgt and act >= tgt
            out.append(_alert(
                "cost_pacing", "low" if (dev < 0 and gp3_ok) else "medium", m,
                f"Marketing cost {dev*100:+.0f} % vs plan to date ({_sek(cact)} vs {_sek(ctgt)})",
                ("Under plan while GP3 is ahead: room to loosen tROAS / lift budgets on campaigns "
                 "earning above target." if dev < 0 and gp3_ok else
                 "Under plan and GP3 is not ahead: check for budget caps, disapprovals or tROAS set too high."
                 if dev < 0 else
                 "Over plan: keep the extra spend only where profit ROAS ≥ tROAS."),
                "pacing", [{"section": "pacing", "metric": "marketing cost MTD", "value": cact},
                           {"section": "pacing", "metric": "cost plan to date", "value": ctgt}],
                "google_budget"))
    return out


def _paid_series_mtd(p, blk) -> tuple[float | None, float | None]:
    """Sum a paid pacing block {plan: [30], actual: [30]} over complete days
    (days[i] ≤ asof; today's partial value is left out)."""
    days, asof = p.get("days") or [], p.get("asof")
    plan, act = blk.get("plan") or [], blk.get("actual") or []
    idx = [i for i, d in enumerate(days) if asof and d <= asof]
    if not idx:
        return None, None
    return (sum(_num(act[i]) or 0 for i in idx if i < len(act)),
            sum(_num(plan[i]) or 0 for i in idx if i < len(plan)))


def _rule_spend_pacing(snaps, today):
    """paid: google.pacing.<m> and meta.pacing.market.<m> {plan[], actual[]} (SEK/day),
    days[] + asof → spend to date vs plan to date."""
    p = snaps.get("paid") or {}
    out = []
    for ch, path in (("google", "google.pacing"), ("meta", "meta.pacing.market")):
        for m, blk in (dig(p, path) or {}).items():
            if not isinstance(blk, dict):
                continue
            spend, plan = _paid_series_mtd(p, blk)
            if spend is None or not plan or plan < 3000:
                continue
            dev = spend / plan - 1
            if abs(dev) <= RULES["spend_pace_pct"]:
                continue
            over = dev > 0
            out.append(_alert(
                "spend_pacing", "medium", m,
                f"{ch.title()} {m} spend {dev*100:+.0f} % vs plan to date ({_sek(spend)} vs {_sek(plan)})",
                ("Keep the extra spend only where profit ROAS ≥ tROAS; otherwise trim budgets "
                 "or let the raised tROAS bite." if over and ch == "google" else
                 "Over plan: tighten the ad-set daily caps so the month total stays at the approved plan."
                 if over else
                 "Under plan: if the campaigns are above break-even, loosen tROAS or lift budgets so the "
                 "plan is used before Black Week." if ch == "google" else
                 "Under plan: check delivery (learning, audience size) and raise caps on Scale creatives."),
                "paid",
                [{"section": "paid", "metric": f"{ch} spend to date", "value": round(spend)},
                 {"section": "paid", "metric": f"{ch} plan to date", "value": round(plan)}],
                "meta_allocation" if ch == "meta" else "google_budget"))
    return out


def _rule_budget_limited(snaps, today):
    """paid: google.campaigns[] with budget_limited and l7_roas (profit ROAS: Babyshop
    conversion value = profit) ≥ 110 % of troas."""
    p = snaps.get("paid") or {}
    out = []
    for r in _rows(p, "google.campaigns"):
        roas, troas = _num(r.get("l7_roas")), _num(r.get("troas"))
        if not r.get("budget_limited") or roas is None or not troas:
            continue
        if roas >= troas * RULES["budget_limited_headroom"]:
            lost = _num(r.get("lost_budget"))
            out.append(_alert(
                "budget_limited", "high", r.get("market"),
                f"{r.get('name', 'campaign')}: budget-limited at profit ROAS {roas:.2f} vs tROAS {troas:.2f}",
                f"Raise the daily budget (now {_sek(r.get('budget_day_sek'))} SEK): it earns above target. "
                "tROAS changes only via apply_troas.py.",
                "paid",
                [{"section": "paid", "metric": "profit ROAS 7 d", "value": roas},
                 {"section": "paid", "metric": "tROAS", "value": troas}]
                + ([{"section": "paid", "metric": "lost IS (budget)", "value": round(lost, 3)}] if lost is not None else []),
                "google_budget"))
    return out


def _rule_meta_allocation(snaps, today):
    """paid: Meta spend share by market (meta.pacing.market) vs the approved 60/20/10/10."""
    p = snaps.get("paid") or {}
    spend = {}
    for m, blk in (dig(p, "meta.pacing.market") or {}).items():
        if m in bm.META_PLAN_MARKET_SHARE and isinstance(blk, dict):
            spend[m] = _paid_series_mtd(p, blk)[0] or 0
    tot = sum(spend.values())
    if tot < 5000:
        return []
    out = []
    for m, v in spend.items():
        share, plan = v / tot, bm.META_PLAN_MARKET_SHARE[m]
        if abs(share - plan) > 0.05:
            out.append(_alert(
                "meta_allocation", "low", m,
                f"Meta {m} share {share*100:.0f} % vs plan {plan*100:.0f} %",
                "Adjust the ad-set min/max spend shares so the market split follows the approved plan.",
                "paid", [{"section": "paid", "metric": "Meta spend share", "value": round(share, 3)},
                         {"section": "paid", "metric": "plan share", "value": plan}],
                "meta_allocation"))
    return out


def _rule_negative_gp3(snaps, today):
    """paid: gp3.l14.rows[] {market, channel, spend, gp2, gp3}: channels losing GP3."""
    p = snaps.get("paid") or {}
    out = []
    for r in _rows(p, "gp3.l14.rows"):
        g, sp = _num(r.get("gp3")), _num(r.get("spend")) or 0
        if g is None or g >= 0 or sp < 5000:
            continue
        ch = r.get("channel", "paid")
        out.append(_alert(
            "negative_gp3", "high" if g < -0.25 * sp else "medium", r.get("market"),
            f"{ch.title()} {r.get('market')} lost {_sek(-g)} GP3 in 14 days (GP2/spend {r.get('gp2_roas') or 0:.2f})",
            "Every krona here costs margin: cut to the profitable campaigns/ad sets, or move the budget to "
            "a market where this channel is above break-even.",
            "paid", [{"section": "paid", "metric": "GP3 14 d", "value": round(g)},
                     {"section": "paid", "metric": "spend 14 d", "value": round(sp)}],
            "meta_allocation" if ch == "meta" else "google_budget", g))
    return out


def _rule_kill_spending(snaps, today):
    """creatives: creatives[] with verdict Kill, status ACTIVE and spend in the
    judging window (`spend`, windows.judge) ≥ the threshold."""
    c = snaps.get("creatives") or {}
    win = dig(c, "windows.judge.days", default=7)
    kills = [r for r in _rows(c, "creatives")
             if str(r.get("verdict", "")).lower() == "kill"
             and str(r.get("status", "ACTIVE")).upper() == "ACTIVE"
             and (_num(r.get("spend")) or 0) >= RULES["kill_spend_sek"]]
    if not kills:
        return []
    by_m: dict[str, list] = {}
    for r in kills:
        by_m.setdefault(r.get("market") or "all", []).append(r)
    out = []
    for m, rs in by_m.items():
        spend = sum(_num(r.get("spend")) or 0 for r in rs)
        nov = sum(_num(r.get("nov_spend")) or 0 for r in rs)
        names = "; ".join((r.get("name") or r.get("ad_name") or "?")[:48] for r in rs[:3])
        out.append(_alert(
            "kill_spending", "medium", m,
            f"{len(rs)} Kill creative{'s' if len(rs) > 1 else ''} still active ({_sek(spend)} in {win} d)",
            f"Pause in Meta Ads Manager and move the budget to Scale creatives: {names}{' …' if len(rs) > 3 else ''}",
            "creatives", [{"section": "creatives", "metric": f"spend on Kill creatives, {win} d", "value": round(spend)},
                          {"section": "creatives", "metric": "their November spend", "value": round(nov)}],
            "creative", None))
    return out


def _rule_oos_spend(snaps, today):
    """offers: stock.products[] {title, flag ('oos'|'sizes'|'low'|'bw'|'ok'), ad7{m}, ad7_oos{m}}
    and stock.ad_spend{m}{oos, low}: Google spend (7 d) on out-of-stock heroes / variants."""
    o = snaps.get("offers") or {}
    by_m: dict[str, list] = {}
    for r in _rows(o, "stock.products"):
        flag = r.get("flag")
        for m in bm.MARKETS:
            spend = _num(dig(r, f"ad7.{m}")) if flag == "oos" else _num(dig(r, f"ad7_oos.{m}"))
            if spend and spend >= RULES["oos_spend_sek"]:
                by_m.setdefault(m, []).append((spend, r))
    out = []
    for m, items in by_m.items():
        items.sort(key=lambda x: -x[0])
        tot = sum(x[0] for x in items)
        names = "; ".join((r.get("title") or r.get("id") or "?")[:45] for _, r in items[:3])
        out.append(_alert(
            "oos_spend", "high" if tot >= 5 * RULES["oos_spend_sek"] else "medium", m,
            f"{len(items)} out-of-stock product{'s' if len(items) > 1 else ''} still getting ads ({_sek(tot)} / 7 d)",
            f"Exclude the sold-out variants from Shopping/PMax (feed label) or swap the hero to an in-stock "
            f"alternative: {names}{' …' if len(items) > 3 else ''}",
            "offers", [{"section": "offers", "metric": "ad spend on OOS, 7 d", "value": round(tot)},
                       {"section": "offers", "metric": "products", "value": len(items)}],
            "stock", -tot))
    return out


def _sek(v) -> str:
    v = _num(v)
    if v is None:
        return "–"
    a = abs(v)
    if a >= 1e6:
        return f"{v/1e6:.1f} M".replace(".", ",")
    if a >= 1e4:
        return f"{v/1e3:.0f} k"
    return f"{v:.0f}"


# ════════════════════════════════════════════════════════════════════════════
#  Change log sources
# ════════════════════════════════════════════════════════════════════════════

LOG_FROM = dt.date(2026, 10, 20)


def _market_of(account_label: str) -> str:
    tail = (account_label or "").split()[-1:] or ["all"]
    return tail[0] if tail[0] in MARKET_VALUES else "ROW"


def troas_changes(since: dt.date) -> list[dict]:
    """roas_sims.target_changes grouped per (day, account, source): one timeline
    entry per batch, the campaigns as detail."""
    from google.cloud import bigquery
    import roas_sims_bq
    c = bigquery.Client(project=roas_sims_bq.BQ_PROJECT)
    rows = list(c.query(f"""
        SELECT change_date, customer_name, campaign_name, old_target, new_target, source,
               predicted_gp3_delta_7d, currency
        FROM `{roas_sims_bq.T('target_changes')}`
        WHERE change_date >= '{since.isoformat()}'
        ORDER BY change_date, customer_name, campaign_name""").result())
    groups: dict[tuple, dict] = {}
    for r in rows:
        k = (r.change_date.isoformat(), r.customer_name, r.source or "")
        g = groups.setdefault(k, {"date": k[0], "kind": "troas", "market": _market_of(r.customer_name),
                                  "account": r.customer_name, "source": "target_changes",
                                  "tag": r.source, "items": [], "big_steps": 0})
        step = (r.new_target / r.old_target - 1) if r.old_target else None
        if step is not None and abs(step) > RULES["troas_step_pct"]:
            g["big_steps"] += 1
        g["items"].append({"campaign": r.campaign_name, "old": r.old_target, "new": r.new_target,
                           "step": None if step is None else round(step, 3),
                           "pred_gp3_7d": r.predicted_gp3_delta_7d, "currency": r.currency})
    out = []
    for g in groups.values():
        ups = sum(1 for i in g["items"] if (i["step"] or 0) > 0)
        downs = sum(1 for i in g["items"] if (i["step"] or 0) < 0)
        g["title"] = (f"tROAS: {len(g['items'])} campaign{'s' if len(g['items']) > 1 else ''} in "
                      f"{g['account']} ({ups}↑ {downs}↓)")
        g["detail"] = g["tag"] or ""
        out.append(g)
    return out


def ads_change_events(since: dt.date) -> tuple[list[dict], list[dict], list[str]]:
    """Google Ads change_event (budgets, bidding strategy, status) for every client
    account, plus the seasonality adjustments that touch Nov/Dec. Read-only GAQL."""
    import refresh_roas_sims as rrs
    notes: list[str] = []
    client = rrs.ads_client()                       # raises MissingCredentials
    svc = client.get_service("GoogleAdsService")
    accounts = _client_accounts(svc, notes)
    since = max(since, bm.today() - dt.timedelta(days=29))   # change_event keeps ~30 days
    until = bm.today() + dt.timedelta(days=1)
    q = f"""
        SELECT change_event.change_date_time, change_event.change_resource_type,
               change_event.changed_fields, change_event.old_resource, change_event.new_resource,
               change_event.user_email, change_event.client_type, change_event.campaign,
               change_event.resource_change_operation
        FROM change_event
        WHERE change_event.change_date_time >= '{since.isoformat()}'
          AND change_event.change_date_time <= '{until.isoformat()}'
          AND change_event.change_resource_type IN ('CAMPAIGN', 'CAMPAIGN_BUDGET')
        ORDER BY change_event.change_date_time
        LIMIT 10000"""
    events: list[dict] = []
    seas: list[dict] = []
    for a in accounts:
        cid, label = a["cid"], a["label"]
        try:
            rows = list(rrs._search(svc, cid, q))
        except Exception as e:                       # noqa: BLE001
            notes.append(f"change_event {label}: {_ads_err(e)}")
            rows = []
        camp_ids = set()
        for row in rows:
            ce = row.change_event
            ev = _describe_change(ce, label)
            if ev:
                if ce.campaign:
                    ev["campaign_id"] = ce.campaign.split("/")[-1]
                    camp_ids.add(ev["campaign_id"])
                events.append(ev)
        if camp_ids:
            names = {str(r.campaign.id): r.campaign.name for r in rrs._search(svc, cid,
                     f"SELECT campaign.id, campaign.name FROM campaign WHERE campaign.id IN ({','.join(sorted(camp_ids))})")}
            for ev in events:
                if ev.get("account") == label and ev.get("campaign_id") in names:
                    ev["title"] = ev["title"].replace("{campaign}", names[ev["campaign_id"]])
        try:
            for row in rrs._search(svc, cid, """
                    SELECT bidding_seasonality_adjustment.name, bidding_seasonality_adjustment.start_date_time,
                           bidding_seasonality_adjustment.end_date_time,
                           bidding_seasonality_adjustment.conversion_rate_modifier,
                           bidding_seasonality_adjustment.scope,
                           bidding_seasonality_adjustment.advertising_channel_types
                    FROM bidding_seasonality_adjustment"""):
                s = row.bidding_seasonality_adjustment
                # GAQL allows no range operator on start_date_time: filter here.
                if str(s.end_date_time) < "2026-11-01":
                    continue
                seas.append({"account": label, "cid": cid, "market": _market_of(label), "name": s.name,
                             "start": str(s.start_date_time), "end": str(s.end_date_time),
                             "modifier": float(s.conversion_rate_modifier),
                             "channels": [c.name for c in s.advertising_channel_types]})
        except Exception as e:                       # noqa: BLE001
            notes.append(f"seasonality {label}: {_ads_err(e)}")
    for ev in events:
        ev["title"] = ev["title"].replace("{campaign}", "campaign " + ev.get("campaign_id", "?"))
    return events, seas, notes


def _ads_err(e: Exception) -> str:
    try:
        return "; ".join(x.message for x in e.failure.errors)[:200]      # GoogleAdsException
    except AttributeError:
        return f"{type(e).__name__}: {str(e)[:160]}"


def _client_accounts(svc, notes) -> list[dict]:
    """All enabled non-manager clients under the MCC (nine incl. Babyshop RU),
    falling back to refresh_roas_sims.ACCOUNTS."""
    import os
    import refresh_roas_sims as rrs
    mcc = re.sub(r"\D", "", os.environ.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID", ""))
    try:
        out = []
        for r in rrs._search(svc, mcc, """
                SELECT customer_client.id, customer_client.descriptive_name, customer_client.manager,
                       customer_client.status, customer_client.level
                FROM customer_client WHERE customer_client.level = 1"""):
            cc = r.customer_client
            if not cc.manager and cc.status.name == "ENABLED":
                out.append({"cid": str(cc.id), "label": cc.descriptive_name})
        if out:
            return out
    except Exception as e:                           # noqa: BLE001
        notes.append(f"customer_client listing failed ({type(e).__name__}); using the 8 known accounts")
    return [{"cid": a["cid"], "label": a["label"]} for a in rrs.ACCOUNTS]


def _describe_change(ce, label: str) -> dict | None:
    rt = ce.change_resource_type.name
    paths = list(ce.changed_fields.paths)
    when = str(ce.change_date_time)
    base = {"date": when[:10], "when": when, "account": label, "market": _market_of(label),
            "source": "change_event", "by": ce.user_email or ce.client_type.name}
    created = ce.resource_change_operation.name == "CREATE"
    if rt == "CAMPAIGN_BUDGET":
        if "amount_micros" not in paths:
            return None
        old = ce.old_resource.campaign_budget.amount_micros / 1e6
        new = ce.new_resource.campaign_budget.amount_micros / 1e6
        name = ce.new_resource.campaign_budget.name or "budget"
        title = (f"New budget {name}: {new:,.0f}/day ({label})" if created else
                 f"Budget {name}: {old:,.0f} → {new:,.0f}/day ({label})")
        return dict(base, kind="google_budget", title=title, detail="", old=None if created else old, new=new)
    if rt == "CAMPAIGN":
        if created:
            return dict(base, kind="status", detail="",
                        title=f"{{campaign}} created ({ce.new_resource.campaign.status.name.lower()}, {label})")
        if "status" in paths:
            return dict(base, kind="status",
                        title=f"{{campaign}} {ce.old_resource.campaign.status.name.lower()} → "
                              f"{ce.new_resource.campaign.status.name.lower()} ({label})", detail="")
        if any(p.startswith(("bidding_strategy_type", "bidding_strategy", "maximize_conversion_value.",
                             "target_roas.")) for p in paths) and not any(
                p in ("target_roas.target_roas", "maximize_conversion_value.target_roas") for p in paths):
            return dict(base, kind="bidding",
                        title=f"{{campaign}} bidding changed: {', '.join(paths)[:80]} ({label})", detail="")
    return None


# ════════════════════════════════════════════════════════════════════════════
#  Checklist
# ════════════════════════════════════════════════════════════════════════════

# Sources: memory "Babyshop BF 2026 bidding" (agreed 2026-11-04), "PB campaign"
# (review 18 Nov), "Q4 budget split" (Meta setup before Black Week), "ads assets
# routine" (offers → Google Ads assets). `auto` items compute their own state.
CHECKLIST = [
    {"id": "troas-raise", "due": "2026-11-04", "title": "Pre-BF tROAS raise (+10–15 %) applied via apply_troas.py",
     "detail": "Logged in target_changes. 14-day cooldown → those campaigns can move again from 18 Nov.",
     "auto": "troas_raise"},
    {"id": "meta-setup", "due": "2026-11-07", "title": "Meta November setup matches the approved 200k plan",
     "detail": "Kuling 60k / prospecting 20k / catalogue retargeting 120k; campaign budgets, one ad set per "
               "country, SE60/NO20/FI10/DK10 as min/max spend shares."},
    {"id": "ly-anomaly", "due": "2026-11-17", "title": "Explain 14–20 Nov 2025: 1.31M SEK spend at ROAS ~1.38",
     "detail": "Unexplained last year (CPC ~7 SEK). Check before the 18 Nov review so it does not repeat."},
    {"id": "troas-review", "due": "2026-11-18", "title": "tROAS review: back to normal before BF, or stay raised?",
     "detail": "Cooldown from the 4 Nov batch ends today. No positive BF seasonality adjustment is planned.",
     "auto": "cooldown"},
    {"id": "pb-review", "due": "2026-11-18", "title": "Private-label search review (SE 600 %, NO/FI/DK 350 %)",
     "detail": "ps-search-*-pb-all: impression share, CPC, Kuling winter landing pages."},
    {"id": "postbf-soften", "due": "2026-11-18", "title": "If tROAS stays raised through BF: soften Post-BF to −30 % / −15 %",
     "detail": "Edit the two seasonality adjustments in all nine accounts."},
    {"id": "bf-assets", "due": "2026-11-23", "title": "BF offers in Google Ads: promotions, sitelinks, promo RSAs (SE/NO/FI/DK)",
     "detail": "Run the ads-assets routine from the Airtable calendar; promo_schedule script must be scheduled."},
    {"id": "sale-end", "due": "2026-11-30", "title": "Confirm the sale end date in the campaign calendar",
     "detail": "Post-BF adjustments assume the sale ends on Cyber Monday 30 Nov; shift them if it runs longer."},
    {"id": "postbf-seasonality", "due": "2026-11-30",
     "title": "“Post-BF dag 1/2” (−45 % / −30 %) on 1–2 Dec in all nine accounts",
     "detail": "Search + Shopping, account-local 00:00–23:59. Created 2026-11-04.", "auto": "seasonality"},
    {"id": "post-bf-troas", "due": "2026-12-03", "title": "After the sale: tROAS back to normal, score the month",
     "detail": "Score every accepted card; write up what worked for December."},
]


def checklist(today: dt.date, troas_log: list[dict], seas: list[dict] | None, seas_known: bool,
              events: list[dict]) -> list[dict]:
    ticked = {}
    for e in sorted(events, key=lambda e: e.get("ts", "")):
        if e.get("kind") == "checklist" and e.get("ref"):
            ticked[e["ref"]] = e
    out = []
    for item in CHECKLIST:
        it = dict(item)
        state, auto_note = None, None
        if item.get("auto") == "troas_raise":
            n = sum(len(g["items"]) for g in troas_log
                    if g["date"] == "2026-11-04" and "black-month" in (g.get("tag") or ""))
            if n:
                state, auto_note = "done", f"{n} campaigns logged on 4 Nov"
        elif item.get("auto") == "cooldown":
            recent = sorted({g["date"] for g in troas_log})
            last = recent[-1] if recent else None
            if last:
                free = dt.date.fromisoformat(last) + dt.timedelta(days=14)
                auto_note = f"Last logged change {last}; cooldown ends {free.isoformat()}"
        elif item.get("auto") == "seasonality" and seas_known:
            hits = [s for s in (seas or []) if s["name"].lower().startswith("post-bf")]
            accts = {s["account"] for s in hits}
            d1 = sum(1 for s in hits if s["start"].startswith("2026-12-01"))
            d2 = sum(1 for s in hits if s["start"].startswith("2026-12-02"))
            auto_note = f"Found in {len(accts)} accounts (dag 1: {d1}, dag 2: {d2})"
            if len(accts) >= 9 and d1 >= 9 and d2 >= 9:
                state = "verified"
        t = ticked.get(item["id"])
        if t and t.get("detail") != "undo":
            state = state or "done"
            it["ticked_by"], it["ticked_at"] = t.get("by"), t.get("ts")
        it["state"] = state or ("overdue" if item["due"] < today.isoformat() else
                                "today" if item["due"] == today.isoformat() else "open")
        it["auto_note"] = auto_note
        out.append(it)
    return out


# ════════════════════════════════════════════════════════════════════════════
#  build()
# ════════════════════════════════════════════════════════════════════════════

def read_snapshots() -> dict[str, dict | None]:
    out = {}
    for k in SECTION_KEYS:
        try:
            out[k] = bm.read_section(k)
        except Exception as e:                       # noqa: BLE001
            print(f"[insights] read_section({k}) failed: {e}", flush=True)
            out[k] = None
    return out


def build(ctx: dict) -> dict:
    today: dt.date = ctx["today"]
    notes: list[str] = []

    snaps = read_snapshots()
    alerts, rnotes = rule_alerts(snaps, today)
    notes += rnotes

    try:
        cards = list_cards(days=45)
    except Exception as e:                           # noqa: BLE001
        cards = []
        notes.append(f"Insight cards could not be read: {type(e).__name__}")
    try:
        manual = list_events(days=60)
    except Exception as e:                           # noqa: BLE001
        manual = []
        notes.append(f"Manual events could not be read: {type(e).__name__}")

    try:
        troas = troas_changes(LOG_FROM)
    except Exception as e:                           # noqa: BLE001
        troas = []
        notes.append(f"target_changes could not be read: {type(e).__name__}: {str(e)[:100]}")
    big: dict[str, list[dict]] = {}
    for g in troas:
        if g["big_steps"]:
            big.setdefault(g["date"], []).append(g)
    for day, gs in sorted(big.items(), reverse=True)[:3]:
        n = sum(g["big_steps"] for g in gs)
        mk = sorted({g["market"] for g in gs})
        alerts.append(_alert(
            "troas_big_step", "low", mk[0] if len(mk) == 1 else "all",
            f"{n} tROAS step{'s' if n > 1 else ''} above ±20 % on {day} "
            f"({', '.join(g['account'] for g in gs)})",
            "Check these were intended (apply_troas.py caps steps at 20 % unless overridden); "
            "the campaigns are listed in the change log.",
            "paid", [{"section": "bq", "metric": "roas_sims.target_changes", "value": gs[0]["tag"]}],
            "google_troas"))

    ads_events, seas, seas_known = [], None, False
    try:
        ads_events, seas, anotes = ads_change_events(LOG_FROM)
        seas_known = True
        notes += anotes
    except Exception as e:                           # noqa: BLE001
        notes.append("Google Ads change history not loaded "
                     f"({type(e).__name__}: {str(e)[:140]}). The change log shows tROAS and manual entries only.")

    events = (troas + ads_events +
              [dict(e, source="manual") for e in manual if e.get("kind") != "checklist"])
    events.sort(key=lambda e: (e.get("date", ""), e.get("when") or e.get("ts") or ""), reverse=True)

    return {
        "today": today.isoformat(),
        "cards": cards,
        "stats": hit_rate(cards),
        "alerts": alerts,
        "rules": RULES,
        "events": events[:400],
        "seasonality": seas,
        "checklist": checklist(today, troas, seas, seas_known, manual),
        "snapshots": {k: (v or {}).get("generated_at") for k, v in snaps.items()},
        "levers": LEVERS, "event_kinds": [k for k in EVENT_KINDS if k != "checklist"],
        "notes": notes,
    }


# ════════════════════════════════════════════════════════════════════════════
#  CLI
# ════════════════════════════════════════════════════════════════════════════

def _load_cards(path: str) -> list[dict]:
    data = json.loads(Path(path).read_text())
    return data if isinstance(data, list) else data.get("cards", [data])


def main(argv: Iterable[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("add"); a.add_argument("--file", required=True)
    a.add_argument("--source", default="agent"); a.add_argument("--dry-run", action="store_true")
    v = sub.add_parser("validate"); v.add_argument("--file", required=True)
    ls = sub.add_parser("list"); ls.add_argument("--status", choices=STATUSES)
    ls.add_argument("--days", type=int, default=30); ls.add_argument("--json", action="store_true")
    s = sub.add_parser("score"); s.add_argument("--id", required=True)
    s.add_argument("--verdict", required=True, choices=VERDICTS)
    s.add_argument("--measured", type=float); s.add_argument("--method", required=True)
    s.add_argument("--detail", default=""); s.add_argument("--by", default="agent")
    st = sub.add_parser("set-status"); st.add_argument("--id", required=True)
    st.add_argument("--status", required=True, choices=[x for x in STATUSES if x != "measured"])
    st.add_argument("--by", default=""); st.add_argument("--note", default="")
    e = sub.add_parser("event"); e.add_argument("--date"); e.add_argument("--kind", default="other", choices=EVENT_KINDS)
    e.add_argument("--market", default="all"); e.add_argument("--title", required=True)
    e.add_argument("--detail", default=""); e.add_argument("--by", default="")
    args = ap.parse_args(list(argv) if argv is not None else None)

    if args.cmd in ("add", "validate"):
        cards = _load_cards(args.file)
        try:
            ids = add_cards(cards, getattr(args, "source", "agent"), dry_run=args.cmd == "validate" or args.dry_run)
        except ValueError as err:
            print(f"INVALID:\n{err}", file=sys.stderr)
            return 2
        print(("validated " if args.cmd == "validate" or args.dry_run else "wrote ") + ", ".join(ids))
    elif args.cmd == "list":
        cards = list_cards(days=args.days, status=args.status)
        if args.json:
            print(json.dumps(cards, indent=1, default=str))
        else:
            for c in cards:
                imp = (c.get("impact") or {}).get("gp3_sek")
                print(f"{c['id']}  {c.get('created')}  {c.get('status'):9} {c.get('market'):4} "
                      f"{c.get('lever'):16} {_sek(imp):>7}  {c.get('headline', '')[:70]}")
            print(json.dumps(hit_rate(cards)))
    elif args.cmd == "score":
        c = score_card(args.id, args.verdict, args.measured, args.method, args.by, args.detail)
        print(f"scored {c['id']}: {args.verdict}")
    elif args.cmd == "set-status":
        c = set_status(args.id, args.status, args.by, args.note)
        print(f"{c['id']} → {c['status']}")
    elif args.cmd == "event":
        ev = add_event(vars(args) | {"source": "manual"})
        print(f"event {ev['id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
