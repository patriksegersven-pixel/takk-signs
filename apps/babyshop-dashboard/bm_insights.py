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
              change_event (budgets, bidding strategy, status; read-only), the
              Meta activity log (bm_insights_meta.py), the current Black Month
              seasonality adjustments, plus manual
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
    python3 bm_insights.py event --date 2026-11-06 --kind offer_launch --market SE --brand babyshop \
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
BRANDS = ["babyshop", "lekmer"]
BRAND_VALUES = BRANDS + ["all"]
SECTION_KEYS = [s for s in bm.SECTIONS if s != "insights"]

# Scoring window: an accepted/done card is scored once it is this old.
SCORE_AFTER_DAYS = 3
SCORE_BY_DAYS = 7

# Rule thresholds (one place, so the page can show them next to the alerts).
# Spend policy (bm_common "Spend philosophy", 2026-11-04): Google has NO budget and
# is run for GP3; the binding target is the forecast's GP3, not COS or a spend plan.
# GP3 margin above forecast is headroom to spend more on Google AND Meta where the
# extra krona adds incremental GP3. No rule here judges spend against a plan.
RULES = {
    "gp3_behind_pct": 0.05,        # MTD GP3 more than 5 % behind target-to-date
    "headroom_min_per_day": 2000,  # positive headroom worth mentioning, SEK/day
    "min_gp3_target_mtd": 10000,   # skip GP3-behind on scopes smaller than this (tiny Lekmer markets)
    "min_negative_headroom": 20000,  # skip negative headroom smaller than this, SEK to month end
    "suggestion_min_l7_sek": 2000, # tROAS suggestions on campaigns spending ≥ this per 7 d
    "kill_spend_sek": 500,         # a Kill creative that spent ≥ 500 SEK in the window
    "oos_spend_sek": 1000,         # ≥ 1 000 SEK / 7 d of spend on out-of-stock days, per brand × market
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
    c["brand"] = str(c.get("brand") or "all").lower()
    if c["brand"] not in BRAND_VALUES:
        errs.append(f"brand {c['brand']!r} not in {BRAND_VALUES}")
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
    if c.get("verify") is not None:
        import bm_insights_verify as bv
        if not isinstance(c["verify"], dict) or c["verify"].get("type") not in bv.VERIFIERS:
            raise ValueError(f"verify must be {{type: one of {sorted(bv.VERIFIERS)}, ...}}")
    c["source"] = c.get("source") or source
    c["owner"] = c.get("owner") or ""
    c["id"] = c.get("id") or card_id(c["created"], c["headline"],
                                     c["market"] if c["brand"] == "all" else f"{c['market']}-{c['brand']}")
    c.setdefault("created_at", _now_iso())
    c.setdefault("decided_by", None)
    c.setdefault("decided_at", None)
    c.setdefault("outcome", None)
    c.setdefault("history", [])
    keep = {"id", "created", "created_at", "lever", "market", "brand", "headline", "action", "rationale",
            "impact", "confidence", "evidence", "status", "owner", "decided_by", "decided_at",
            "done_at", "outcome", "source", "history", "links", "expires", "verify"}
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
         "brand": str(ev.get("brand") or "all").lower(),
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
    if e["brand"] not in BRAND_VALUES:
        raise ValueError(f"brand must be one of {BRAND_VALUES}")
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


def _brand(row: dict | None, *names: str) -> str:
    """'babyshop' | 'lekmer' | 'all' for a row: its own `brand` field when it holds
    one of BRANDS (product rows carry product brands like 'Kuling' there, which are
    ignored), else 'Lekmer'/'Babyshop' in an account/campaign label."""
    b = str((row or {}).get("brand") or "").lower()
    if b in BRANDS:
        return b
    for key in ("account", "customer_name", "account_name", "shop"):
        v = str((row or {}).get(key) or "").lower()
        for br in BRANDS:
            if br in v:
                return br
    for n in names:
        for br in BRANDS:
            if br in str(n or "").lower():
                return br
    return "all"


def _alert(rule, severity, market, headline, action, section, evidence, lever="other", impact=None,
           brand="all"):
    return {"rule": rule, "severity": severity, "market": market or "all", "brand": brand or "all",
            "headline": headline,
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
    for fn in (_rule_gp3_pacing, _rule_headroom, _rule_budget_limited, _rule_troas_suggestions,
               _rule_meta_lines, _rule_negative_gp3, _rule_kill_spending, _rule_oos_spend):
        try:
            alerts.extend(fn(snaps, today))
        except Exception as e:                       # noqa: BLE001 — a rule must never sink the section
            notes.append(f"Rule {fn.__name__[6:]} skipped: {type(e).__name__}: {e}")
    for a in alerts:                                 # "Lekmer · …" on brand-specific headlines
        lab = _brand_label(a.get("brand", "all"))
        if lab and not a["headline"].startswith(lab):
            a["headline"] = lab + a["headline"]
    sev = {"high": 0, "medium": 1, "low": 2}
    alerts.sort(key=lambda a: (sev.get(a["severity"], 3), -(abs(a.get("impact_sek") or 0))))
    return alerts, notes


def _rule_gp3_pacing(snaps, today):
    """pacing: markets.<m>.summary.gp3 {mtd, target_mtd, landing, target_month}.
    GP3 vs the forecast is the binding target; spend is judged only through it."""
    p = snaps.get("pacing") or {}
    out = []
    for br, m, blk in _pacing_scopes(p, "markets"):
        g = dig(blk, "summary.gp3") or {}
        act, tgt = _num(g.get("mtd")), _num(g.get("target_mtd"))
        land, month = _num(g.get("landing")), _num(g.get("target_month"))
        if act is not None and tgt and tgt >= RULES["min_gp3_target_mtd"] and act / tgt - 1 < -RULES["gp3_behind_pct"]:
            gap = act / tgt - 1
            out.append(_alert(
                "gp3_behind", "high" if gap < -2 * RULES["gp3_behind_pct"] else "medium", m,
                f"MTD GP3 {gap*100:+.0f} % vs target ({_sek(act)} vs {_sek(tgt)})",
                "GP3 vs forecast is the binding target. Find the driver (margin, volume or cost: see the "
                "pacing bridge) and cut spend whose incremental marginal ROAS is < 1 in Paid media levers.",
                "pacing",
                [{"section": "pacing", "metric": "MTD GP3", "value": act},
                 {"section": "pacing", "metric": "GP3 target to date", "value": tgt}]
                + ([{"section": "pacing", "metric": "GP3 landing", "value": land}] if land else []),
                "other", act - tgt, brand=br))
        elif land and month and tgt and tgt >= RULES["min_gp3_target_mtd"] and land / month - 1 < -RULES["gp3_behind_pct"]:
            out.append(_alert(
                "gp3_landing", "medium", m,
                f"GP3 landing {_sek(land)} is {(land/month-1)*100:+.0f} % vs the month target {_sek(month)}",
                "On track to date but the run rate lands short: look for budget-limited winners and "
                "offers to pull forward before Black Week.",
                "pacing", [{"section": "pacing", "metric": "GP3 landing", "value": land},
                           {"section": "pacing", "metric": "GP3 target month", "value": month}],
                "other", land - month, brand=br))
    return out


def _pacing_scopes(p: dict, key: str):
    """(brand, market, block) for pacing `markets` or `headroom`. With the brand split
    (brands.<b>.<key>) alerts are per brand, plus the company-wide 'all' market for
    GP3; without it, the combined blocks as brand 'all'."""
    brands = dig(p, "brands") or {}
    if isinstance(brands, dict) and any(isinstance(v, dict) and v.get(key) for v in brands.values()):
        if key == "markets" and isinstance(dig(p, "markets.all"), dict):
            yield "all", "all", p["markets"]["all"]
        for br, v in brands.items():
            for m, blk in ((v or {}).get(key) or {}).items():
                if isinstance(blk, dict):
                    yield br, m, blk
    else:
        for m, blk in (dig(p, key) or {}).items():
            if isinstance(blk, dict):
                yield "all", m, blk


def _brand_label(br: str) -> str:
    return {"babyshop": "Babyshop · ", "lekmer": "Lekmer · "}.get(br, "")


# tROAS set high on purpose; never suggest lowering (memory "PB campaign", 2026-11-04).
# Keyed (brand, name): Lekmer reuses Babyshop's campaign names.
DELIBERATE_TROAS = {("babyshop", "ps-search-se-pb-all")}

_RETURNS_CAVEAT = ("Late-November headroom reads high: Funnel books returns on the return date, so "
                   "Black Week returns land in December and GP3 to date is flattered.")


def _rule_headroom(snaps, today):
    """pacing: headroom.<m> {headroom_remaining, headroom_per_day, remaining_days,
    gp3_mtd, gp3_target_mtd, earned_cos_mtd, cos_target}. Headroom = spend the
    forecast's GP3 already covers. Positive with GP3 on track → room to spend more
    where incremental; negative → find spend with incremental marginal ROAS < 1
    (paid google.campaigns[].inc_marginal, meta.lines[].inc_marginal)."""
    pc, pd = snaps.get("pacing") or {}, snaps.get("paid") or {}
    out = []
    for br, m, h in _pacing_scopes(pc, "headroom"):
        if m in ("ROW", "all"):
            continue
        rem, per_day = _num(h.get("headroom_remaining")), _num(h.get("headroom_per_day"))
        g, gt = _num(h.get("gp3_mtd")), _num(h.get("gp3_target_mtd"))
        if rem is None or per_day is None:
            continue
        ev = [{"section": "pacing", "metric": "headroom remaining", "value": round(rem)},
              {"section": "pacing", "metric": "headroom per day", "value": round(per_day)},
              {"section": "pacing", "metric": "GP3 MTD vs target", "value": f"{_sek(g)} vs {_sek(gt)}"},
              {"section": "pacing", "metric": "earned COS MTD", "value": h.get("earned_cos_mtd")}]
        if rem > 0 and per_day >= RULES["headroom_min_per_day"] and g is not None and gt and g >= gt:
            more = _incremental_candidates(pd, m, up=True, brand=br)
            out.append(_alert(
                "headroom", "medium", m,
                f"Room to spend ~{_sek(per_day)}/day more ({_sek(rem)} to month end) with GP3 on track",
                "Spend it only where the extra krona is incremental (incremental marginal ROAS > 1): "
                + (f"candidates {more}. " if more else "") + _RETURNS_CAVEAT,
                "pacing", ev, "google_troas", brand=br))
        elif rem <= -RULES["min_negative_headroom"]:
            less = _incremental_candidates(pd, m, up=False, brand=br)
            out.append(_alert(
                "headroom_negative", "high" if (gt and g is not None and g < gt) else "medium", m,
                f"Spend is {_sek(-rem)} over what the GP3 forecast covers ({_sek(-per_day)}/day to month end)",
                "Find spend with incremental marginal ROAS < 1 and cut it first"
                + (f": {less}" if less else "") + ". Don't cut spend that still adds GP3.",
                "pacing", ev, "google_troas", rem, brand=br))
    return out


def _incremental_candidates(pd, market, up: bool, n: int = 3, brand: str = "all") -> str:
    """Top Google campaigns / Meta lines in a market by incremental marginal ROAS:
    > 1 when up (can take more), < 1 when not (spend to cut)."""
    rows = []
    for c in _rows(pd, "google.campaigns"):
        im = _num(c.get("inc_marginal"))
        if c.get("market") != market or im is None or (_num(c.get("l7_cost_sek")) or 0) < RULES["suggestion_min_l7_sek"]:
            continue
        if brand != "all" and _brand(c, c.get("name")) != brand:
            continue
        if up and (_brand(c, c.get("name")), c.get("name")) in DELIBERATE_TROAS:
            continue
        if (im > 1) if up else (im < 1):
            cd = c.get("cooldown_until")
            rows.append((im, f"{c.get('name')} ({im:.2f}"
                             + (f", cooldown to {cd[8:10]}/{cd[5:7]}" if cd and cd > bm.today().isoformat() else "") + ")"))
    for l in _rows(pd, "meta.lines"):
        im = _num(l.get("inc_marginal"))
        if l.get("market") != market or im is None:
            continue
        if brand != "all" and _brand(l) != brand:
            continue
        if (im > 1) if up else (im < 1):
            rows.append((im, f"Meta {l.get('type')} ({im:.2f}{', test first' if up else ''})"))
    rows.sort(key=lambda x: -x[0] if up else x[0])
    return ", ".join(r[1] for r in rows[:n])


def _rule_budget_limited(snaps, today):
    """paid: google.campaigns[].budget_limited and guardrail.budget_errors[]. Google has
    no budget, so ANY budget-limited campaign is an error, whatever its ROAS."""
    p = snaps.get("paid") or {}
    out, seen = [], set()
    for r in _rows(p, "google.campaigns"):
        if not r.get("budget_limited") or str(r.get("status", "enabled")).lower() != "enabled":
            continue
        seen.add(str(r.get("id") or r.get("name")))
        lost = _num(r.get("lost_budget"))
        out.append(_alert(
            "budget_limited", "high", r.get("market"),
            f"{r.get('name', 'campaign')} is limited by budget ({_sek(r.get('budget_day_sek'))} SEK/day)",
            "Error under the spend policy: Google has no budget. Raise the cap well above daily spend so "
            "tROAS alone governs; if the spend isn't wanted, raise tROAS via apply_troas.py instead.",
            "paid",
            [{"section": "paid", "metric": "lost IS (budget)", "value": round(lost, 3) if lost is not None else None},
             {"section": "paid", "metric": "profit ROAS 7 d", "value": r.get("l7_roas")},
             {"section": "paid", "metric": "tROAS", "value": r.get("troas")}],
            "google_budget", brand=_brand(r, r.get("name"))))
    for e in dig(p, "guardrail.budget_errors") or []:
        e = e if isinstance(e, dict) else {"text": str(e)}
        if str(e.get("id") or e.get("name") or e.get("campaign")) in seen:
            continue
        out.append(_alert(
            "budget_limited", "high", e.get("market"),
            f"Budget error: {e.get('name') or e.get('campaign') or e.get('text') or 'campaign'}",
            e.get("text") or e.get("hint") or "Raise the cap so tROAS governs; Google has no budget.",
            "paid", [{"section": "paid", "metric": "guardrail.budget_errors", "value": 1}], "google_budget",
            brand=_brand(e, e.get("name"), e.get("campaign"))))
    return out


def _rule_troas_suggestions(snaps, today):
    """paid: google.campaigns[].suggestion {action: lower|raise|hold|none, text, new_troas,
    blocked_until} with cooldown_until, inc_marginal, inc_gp3_per_10k, l7_cost_sek.
    Actionable today = not blocked and not in cooldown; one alert per market × action.
    Blocked ones are summarised in one low alert with the date they unblock."""
    p = snaps.get("paid") or {}
    tday = today.isoformat()
    groups: dict[tuple, list] = {}
    blocked: dict[str, int] = {}
    for c in _rows(p, "google.campaigns"):
        sg = c.get("suggestion") or {}
        act = sg.get("action")
        if act not in ("lower", "raise") or (_num(c.get("l7_cost_sek")) or 0) < RULES["suggestion_min_l7_sek"]:
            continue
        if act == "lower" and (_brand(c, c.get("name")), c.get("name")) in DELIBERATE_TROAS:
            continue
        until = max(str(sg.get("blocked_until") or ""), str(c.get("cooldown_until") or ""))
        if until and until > tday:
            blocked[until] = blocked.get(until, 0) + 1
            continue
        groups.setdefault((c.get("market") or "all", _brand(c, c.get("name")), act), []).append(c)
    out = []
    for (m, br, act), cs in groups.items():
        cs.sort(key=lambda c: -abs(_num(c.get("inc_gp3_per_10k")) or 0) * (_num(c.get("l7_cost_sek")) or 0))
        lst = "; ".join(f"{c.get('name')} {c.get('troas')} → {(c.get('suggestion') or {}).get('new_troas')}"
                        for c in cs[:4])
        up = act == "lower"
        out.append(_alert(
            "troas_" + act, "medium", m,
            f"{len(cs)} campaign{'s' if len(cs) > 1 else ''}: {act} tROAS "
            + ("(incremental marginal ROAS > 1: more spend adds GP3)" if up else
               "(the last krona loses GP3)"),
            f"Run apply_troas.py (dry run first): {lst}{' …' if len(cs) > 4 else ''}. "
            "Increments are model estimates with assumed incrementality factors.",
            "paid",
            [{"section": "paid", "metric": "campaigns", "value": len(cs)},
             {"section": "paid", "metric": "inc. GP3 per +10k spend, top campaign",
              "value": cs[0].get("inc_gp3_per_10k")}],
            "google_troas", brand=br))
    for until, n in sorted(blocked.items())[:2]:
        out.append(_alert(
            "troas_blocked", "low", "all",
            f"{n} tROAS suggestion{'s' if n > 1 else ''} wait for the cooldown ({until})",
            f"Re-check them on {until} in Paid media levers; then apply via apply_troas.py.",
            "paid", [{"section": "paid", "metric": "blocked suggestions", "value": n}], "google_troas"))
    return out


def _rule_meta_lines(snaps, today):
    """paid: meta.lines[] {market, type, suggestion.action up|test|down|none, inc_marginal,
    inc_gp3_per_10k, avg_gp2_roas_platform, lastclick_gp2_roas_market, mtd_spend, plan_month}.
    The approved Meta plan is a starting point, not a ceiling. Meta pixel ROAS runs
    4–10× last-click, so 'up' is a test candidate, never a sure thing."""
    p = snaps.get("paid") or {}
    up, down = {}, {}
    for l in _rows(p, "meta.lines"):
        act = (l.get("suggestion") or {}).get("action")
        if act in ("up", "test"):
            up.setdefault((l.get("market") or "all", _brand(l, l.get("campaign"))), []).append(l)
        elif act == "down":
            down.setdefault((l.get("market") or "all", _brand(l, l.get("campaign"))), []).append(l)
    out = []
    for (m, br), ls in up.items():
        ls.sort(key=lambda l: -(_num(l.get("inc_gp3_per_10k")) or 0))
        pix = [(_num(l.get("avg_gp2_roas_platform")), _num(l.get("lastclick_gp2_roas_market"))) for l in ls]
        ratio = next((a / b for a, b in pix if a and b), None)
        out.append(_alert(
            "meta_scale_test", "low", m,
            f"Meta {', '.join(str(l.get('type')) for l in ls)}: scale-up test candidate"
            f"{'s' if len(ls) > 1 else ''}",
            "Test +20 % for a week, not a plan change: Meta pixel ROAS is 4–10× last-click"
            + (f" (here {ratio:.1f}×)" if ratio else "") + " and incrementality is assumed, not measured. "
            "The 200k plan is a starting point, not a ceiling.",
            "paid", [{"section": "paid", "metric": f"Meta {l.get('type')} inc. marginal ROAS",
                      "value": l.get("inc_marginal")} for l in ls[:3]],
            "meta_allocation", brand=br))
    for (m, br), ls in down.items():
        out.append(_alert(
            "meta_scale_down", "medium", m,
            f"Meta {', '.join(str(l.get('type')) for l in ls)}: incremental marginal ROAS < 1",
            "Scale down: the extra krona doesn't pay back even on Meta's own (generous) numbers.",
            "paid", [{"section": "paid", "metric": f"Meta {l.get('type')} inc. marginal ROAS",
                      "value": l.get("inc_marginal")} for l in ls[:3]],
            "meta_allocation", brand=br))
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
            ("Last-click GP3 (GA4) undercounts Meta: check the line's incremental marginal ROAS in Paid "
             "media levers before cutting; cut what is < 1 there too." if ch == "meta" else
             "Cut the campaigns with incremental marginal ROAS < 1 (raise tROAS via apply_troas.py); "
             "keep what still adds GP3."),
            "paid", [{"section": "paid", "metric": "GP3 14 d", "value": round(g)},
                     {"section": "paid", "metric": "spend 14 d", "value": round(sp)}],
            "meta_allocation" if ch == "meta" else "google_budget", g, brand=_brand(r)))
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
        by_m.setdefault((r.get("market") or "all", _brand(r, r.get("campaign"), r.get("account"))), []).append(r)
    out = []
    for (m, br), rs in by_m.items():
        spend = sum(_num(r.get("spend")) or 0 for r in rs)
        nov = sum(_num(r.get("nov_spend")) or 0 for r in rs)
        names = "; ".join((r.get("name") or r.get("ad_name") or "?")[:48] for r in rs[:3])
        out.append(_alert(
            "kill_spending", "medium", m,
            f"{len(rs)} Kill creative{'s' if len(rs) > 1 else ''} still active ({_sek(spend)} in {win} d)",
            f"Pause in Meta Ads Manager and move the budget to Scale creatives: {names}{' …' if len(rs) > 3 else ''}",
            "creatives", [{"section": "creatives", "metric": f"spend on Kill creatives, {win} d", "value": round(spend)},
                          {"section": "creatives", "metric": "their November spend", "value": round(nov)}],
            "creative", None, brand=br))
    return out


def _rule_oos_spend(snaps, today):
    """offers: Google Shopping spend on days the SKU was actually out of stock.
    stock.ad_spend.<brand>.<m>.{oos_7d, total_7d, oos_today}, products[].ad_b.<brand>.ad7_oos.<m>
    (top items), oos_now[] {brand, m, title, spend_today, still_oos} (still serving today).
    Spend that sold the last units is NOT counted (offers' definition since 2026-11-04)."""
    o = snaps.get("offers") or {}
    spend = dig(o, "stock.ad_spend") or {}
    prods = _rows(o, "stock.products")
    now = _rows(o, "stock.oos_now")
    out = []
    for br in BRANDS:
        for m in bm.MARKETS:
            blk = dig(spend, f"{br}.{m}") or {}
            oos7, tot7 = _num(blk.get("oos_7d")), _num(blk.get("total_7d"))
            if oos7 is None or oos7 < RULES["oos_spend_sek"]:
                continue
            top = sorted(((_num(dig(r, f"ad_b.{br}.ad7_oos.{m}")) or 0, r) for r in prods),
                         key=lambda x: -x[0])
            names = "; ".join(f"{(r.get('title') or r.get('id') or '?')[:45]} ({_sek(v)})" for v, r in top[:3] if v > 0)
            live = [r for r in now if r.get("brand") == br and r.get("m") == m and r.get("still_oos")
                    and (_num(r.get("spend_today")) or 0) > 0]
            out.append(_alert(
                "oos_spend", "high" if oos7 >= 5 * RULES["oos_spend_sek"] else "medium", m,
                f"{_sek(oos7)} Shopping spend in 7 d on days the product was sold out"
                + (f" ({oos7 / tot7 * 100:.1f} % of spend)" if tot7 else ""),
                "Exclude sold-out variants from Shopping/PMax (feed label) until restocked"
                + (f"; {len(live)} still get clicks today" if live else "") + (f". Top: {names}" if names else "."),
                "offers", [{"section": "offers", "metric": "OOS-day spend, 7 d", "value": round(oos7)},
                           {"section": "offers", "metric": "Shopping spend, 7 d", "value": round(tot7 or 0)},
                           {"section": "offers", "metric": "still OOS with spend today", "value": len(live)}],
                "stock", -oos7, brand=br))
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
                                  "brand": _brand({"account": r.customer_name}),
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
                seas.append({"account": label, "cid": cid, "market": _market_of(label),
                             "brand": _brand({"account": label}), "name": s.name,
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
            "brand": _brand({"account": label}),
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
    {"id": "troas-raise", "verify": {"type": "troas_logged", "since": "2026-11-04", "tag": "black-month", "min": 1}, "due": "2026-11-04", "title": "Pre-BF tROAS raise (+10–15 %) applied via apply_troas.py",
     "detail": "Logged in target_changes. 14-day cooldown → those campaigns can move again from 18 Nov.",
},
    {"id": "ly-anomaly", "due": "2026-11-18", "state": "explained",
     "title": "14–20 Nov 2025 explained: Google spend doubled (0.86M → 1.85M) at flat CPC while demand rose",
     "detail": "Tracking was fine (conversions/Norce orders stable 0.77–0.87). 1.38 = last-click GP2 ÷ Google cost "
               "(1.21 the week before, 1.82 the week after). Marginal last-click ≈1.42 on the extra ~1M. At the "
               "18 Nov review: if 2026 spend surges like this, check marginal incremental ROAS before letting it run."},
    {"id": "troas-review", "verify": {"type": "troas_logged", "since": "2026-11-18"}, "due": "2026-11-18", "title": "tROAS review: back to normal before BF, or stay raised?",
     "detail": "Cooldown from the 4 Nov batch ends today. No positive BF seasonality adjustment is planned.",
     "auto": "cooldown"},
    {"id": "pb-review", "verify": {"type": "manual", "why": "a review: tick it when done"}, "due": "2026-11-18", "title": "Private-label search review (SE 600 %, NO/FI/DK 350 %)",
     "detail": "ps-search-*-pb-all: impression share, CPC, Kuling winter landing pages."},
    {"id": "postbf-soften", "verify": {"type": "seasonality_modifiers", "expect": [0.7, 0.85]}, "due": "2026-11-18", "title": "If tROAS stays raised through BF: soften Post-BF to −30 % / −15 %",
     "detail": "Edit the two seasonality adjustments in all nine accounts."},
    {"id": "bf-assets", "verify": {"type": "google_promotions", "covers": "2026-11-27"}, "due": "2026-11-23", "title": "BF offers in Google Ads: promotions, sitelinks, promo RSAs (SE/NO/FI/DK)",
     "detail": "Run the ads-assets routine from the Airtable calendar; promo_schedule script must be scheduled."},
    {"id": "sale-end", "verify": {"type": "manual", "why": "the campaign calendar (Airtable) is not readable from the job"}, "due": "2026-11-30", "title": "Confirm the sale end date in the campaign calendar",
     "detail": "Post-BF adjustments assume the sale ends on Cyber Monday 30 Nov; shift them if it runs longer."},
    {"id": "postbf-seasonality", "verify": {"type": "seasonality_present", "prefix": "post-bf", "accounts": 9}, "due": "2026-11-30",
     "title": "“Post-BF dag 1/2” (−45 % / −30 %) on 1–2 Dec in all nine accounts",
     "detail": "Search + Shopping, account-local 00:00–23:59. Created 2026-11-04."},
    {"id": "post-bf-troas", "verify": {"type": "troas_logged", "since": "2026-12-01"}, "due": "2026-12-03", "title": "After the sale: tROAS back to normal, score the month",
     "detail": "Score every accepted card; write up what worked for December."},
    # Meta action items (added 2026-11-04 at Patrik's request). Where the detail isn't
    # known yet, the item IS "the plan / creatives need to be put in place".
    {"id": "meta-setup-live", "verify": {"type": "meta_setup"}, "due": "2026-11-06", "lever": "meta_allocation", "brand": "babyshop",
     "title": "Confirm the approved Meta November setup is live",
     "detail": "Campaign-level budget, one ad set per country with min/max spend SE60/NO20/FI10/DK10, catalogue "
               "retargeting as one multi-country ad set; Kuling 60k / prospecting 20k / retargeting 120k. "
               "If not in place: the plan needs to be put in place."},
    {"id": "meta-naming", "verify": {"type": "naming_adoption", "threshold": 0.8}, "due": "2026-11-07", "lever": "creative", "brand": "babyshop",
     "title": "Use the creative naming convention on every new Meta ad",
     "detail": "Convention from the Creatives section, so angle / offer / format results can be read."},
    {"id": "meta-bw-plan", "verify": {"type": "manual", "why": "a plan document: tick it when agreed"}, "due": "2026-11-07", "lever": "meta_allocation", "brand": "babyshop", "owner": "TBD",
     "title": "Meta Black Week plan: offers per market, audiences, retargeting budget ramp",
     "detail": "Offers from the Airtable calendar, audiences, retargeting ramp into Black Week. "
               "Needs to be put in place; owner TBD."},
    {"id": "meta-bw-creative-plan", "verify": {"type": "manual", "why": "briefs and owners: tick it when agreed"}, "due": "2026-11-10", "lever": "creative", "brand": "babyshop",
     "title": "Black Week creative plan and briefs",
     "detail": "New creatives per market per week, angles and formats, production owner. Needs to be put in place."},
    {"id": "meta-singles-day", "verify": {"type": "meta_ads_named", "pattern": "singles|11[./]11", "since": "2026-11-01"}, "due": "2026-11-10", "lever": "creative", "brand": "babyshop",
     "title": "Singles Day (11 Nov) creatives live, if there is an offer", "detail": ""},
    {"id": "lekmer-meta-connector", "verify": {"type": "lekmer_meta"}, "due": "2026-11-10", "lever": "meta_allocation", "brand": "lekmer", "owner": "Patrik",
     "title": "Lekmer's Meta account (1465585123471722 “Lekmer ACTIVE”) into the warehouse connector",
     "detail": "So Lekmer creatives and spend show on this tab."},
    {"id": "meta-pixel-health", "verify": {"type": "manual", "why": "Events Manager diagnostics are not in our data"}, "due": "2026-11-13", "lever": "meta_allocation", "brand": "babyshop",
     "title": "Pixel / CAPI health check before Black Week",
     "detail": "Purchase event match quality, deduplication, catalogue sale prices (strikethrough) showing in DPA."},
    {"id": "meta-lift-test", "verify": {"type": "manual", "why": "a decision: tick it when made"}, "due": "2026-11-13", "lever": "meta_allocation", "brand": "babyshop",
     "title": "Decide on a Meta lift test or holdout",
     "detail": "To replace the 0.6 / 0.3 incrementality assumptions; can run in December if November is too risky."},
    {"id": "meta-bf-review", "verify": {"type": "meta_ads_named", "pattern": "black|\\bbf\\b|\\bbw\\b|cyber", "since": "2026-11-01", "approved": True}, "due": "2026-11-20", "lever": "creative", "brand": "babyshop",
     "title": "Black Friday / Black Week creatives uploaded and through Meta ad review",
     "detail": "Review delays peak in Black Week."},
    {"id": "meta-bw-budgets", "verify": {"type": "meta_budget_change", "since": "2026-11-20", "direction": "up"}, "due": "2026-11-24", "lever": "meta_allocation", "brand": "babyshop",
     "title": "Black Week budget increases scheduled and approved against the GP3 headroom",
     "detail": "Retargeting peak; size against Pacing's headroom per market."},
    {"id": "meta-daily-verdicts", "verify": {"type": "kill_paused", "min_spend": 500}, "due": "2026-11-23", "until": "2026-11-30", "lever": "creative", "brand": "babyshop",
     "title": "Daily 23–30 Nov: creative verdict check (Scale / Kill) and retargeting frequency",
     "detail": "Creatives section verdicts; pause Kill, feed Scale; watch frequency on retargeting."},
    {"id": "meta-bf-schedule", "verify": {"type": "all_of", "checks": [{"type": "meta_ads_named", "pattern": "black|\\bbf\\b|\\bbw\\b|cyber", "since": "2026-11-01"}, {"type": "kill_paused", "min_spend": 500}]}, "due": "2026-11-26", "lever": "creative", "brand": "babyshop",
     "title": "BF ads scheduled to start; evergreen ads with Kill verdicts paused", "detail": ""},
    {"id": "meta-post-cm", "verify": {"type": "meta_ads_named", "pattern": "black|\\bbf\\b|\\bbw\\b|cyber", "since": "2026-11-01", "inactive_after": "2026-12-01"}, "due": "2026-12-01", "lever": "creative", "brand": "babyshop",
     "title": "Switch off Black Week creatives and offers after Cyber Monday; Christmas creatives ready", "detail": ""},
]


def checklist(today: dt.date, troas_log: list[dict], seas: list[dict] | None, seas_known: bool,
              events: list[dict], vctx=None) -> list[dict]:
    """Items with state (done | verified | explained | today | overdue | open) and, when a
    verification context is given, `check` = {result, evidence, checked_at}. A manual
    tick always wins: a check can only add "verified", never undo a tick."""
    import bm_insights_verify as bv
    ticked = {}
    for e in sorted(events, key=lambda e: e.get("ts", "")):
        if e.get("kind") == "checklist" and e.get("ref"):
            ticked[e["ref"]] = e
    out = []
    for item in sorted(CHECKLIST, key=lambda i: i["due"]):
        it = dict(item)
        it.setdefault("brand", "all")
        it.setdefault("lever", "google_troas" if item["id"].startswith(("troas", "postbf", "post-bf", "ly-")) else "other")
        state, auto_note = None, None
        if vctx is not None and item.get("verify"):
            it["check"] = bv.run(vctx, item["verify"])
            if it["check"]["result"] == "done":
                state = "verified"
        if item.get("auto") == "cooldown":
            recent = sorted({g["date"] for g in troas_log})
            last = recent[-1] if recent else None
            if last:
                free = dt.date.fromisoformat(last) + dt.timedelta(days=14)
                auto_note = f"Last logged change {last}; cooldown ends {free.isoformat()}"
        t = ticked.get(item["id"])
        if t and t.get("detail") != "undo":
            state = "done"
            it["ticked_by"], it["ticked_at"] = t.get("by"), t.get("ts")
        if not state and item.get("until") and item["due"] <= today.isoformat() <= item["until"]:
            state = "today"                          # a daily item stays "today" through its window
        it["state"] = state or item.get("state") or ("overdue" if item.get("until", item["due"]) < today.isoformat() else
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

    troas_ok = ads_ok = meta_ok = True
    try:
        troas = troas_changes(LOG_FROM)
    except Exception as e:                           # noqa: BLE001
        troas, troas_ok = [], False
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
            "google_troas", brand=(gs[0]["brand"] if len({g["brand"] for g in gs}) == 1 else "all")))

    ads_events, seas, seas_known = [], None, False
    try:
        ads_events, seas, anotes = ads_change_events(LOG_FROM)
        seas_known = True
        notes += anotes
    except Exception as e:                           # noqa: BLE001
        ads_ok = False
        notes.append("Google Ads change history not loaded "
                     f"({type(e).__name__}: {str(e)[:140]}). The change log shows tROAS and manual entries only.")

    try:
        import bm_insights_meta
        meta_events, mnotes = bm_insights_meta.meta_activities(bm.MONTH_START)
        notes += mnotes
        meta_ok = not any("not loaded" in n for n in mnotes)
    except Exception as e:                           # noqa: BLE001 — fail soft
        meta_events, meta_ok = [], False
        notes.append(f"Meta activity log not loaded ({type(e).__name__}: {str(e)[:140]}).")

    events = (troas + ads_events + meta_events +
              [dict(e, source="manual") for e in manual if e.get("kind") != "checklist"])
    events.sort(key=lambda e: (e.get("date", ""), e.get("when") or e.get("ts") or ""), reverse=True)

    # Did the team do it? Checks for checklist items and accepted/done cards (read-only).
    import bm_insights_verify as bv
    vctx = bv.Ctx(today, snaps, troas if troas_ok else None, ads_events if ads_ok else None,
                  seas if seas_known else None, meta_events if meta_ok else None)
    for c in cards:
        spec = bv.card_spec(c)
        if spec and c.get("status") in ("accepted", "done", "measured"):
            c["verification"] = bv.run(vctx, spec)

    return {
        "today": today.isoformat(),
        "cards": cards,
        "stats": hit_rate(cards),
        "alerts": alerts,
        "rules": RULES,
        "events": events[:400],
        "seasonality": seas,
        "checklist": checklist(today, troas, seas, seas_known, manual, vctx),
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
    vf = sub.add_parser("verify", help="run every checklist and card check now (read-only)")
    vf.add_argument("--json", action="store_true")
    e = sub.add_parser("event"); e.add_argument("--date"); e.add_argument("--kind", default="other", choices=EVENT_KINDS)
    e.add_argument("--market", default="all"); e.add_argument("--brand", default="all", choices=BRAND_VALUES)
    e.add_argument("--title", required=True)
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
                print(f"{c['id']}  {c.get('created')}  {c.get('status'):9} {c.get('market'):4} {c.get('brand', 'all'):8} "
                      f"{c.get('lever'):16} {_sek(imp):>7}  {c.get('headline', '')[:70]}")
            print(json.dumps(hit_rate(cards)))
    elif args.cmd == "score":
        c = score_card(args.id, args.verdict, args.measured, args.method, args.by, args.detail)
        print(f"scored {c['id']}: {args.verdict}")
    elif args.cmd == "set-status":
        c = set_status(args.id, args.status, args.by, args.note)
        print(f"{c['id']} → {c['status']}")
    elif args.cmd == "verify":
        out = build(bm.make_ctx())
        rows = [{"kind": "checklist", "id": i["id"], "due": i["due"], "state": i["state"], "title": i["title"],
                 **(i.get("check") or {})} for i in out["checklist"]]
        rows += [{"kind": "card", "id": c["id"], "status": c.get("status"), "title": c.get("headline"),
                  **(c.get("verification") or {})} for c in out["cards"] if c.get("verification")]
        if args.json:
            print(json.dumps(rows, indent=1, ensure_ascii=False, default=str))
        else:
            for r in rows:
                print(f"{r['kind']:9} {r.get('due', r.get('status', '')):10} {r.get('state', ''):9} "
                      f"{r.get('result', '-'):13} {r['title'][:55]} | {r.get('evidence', '')[:90]}")
    elif args.cmd == "event":
        ev = add_event(vars(args) | {"source": "manual"})
        print(f"event {ev['id']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
