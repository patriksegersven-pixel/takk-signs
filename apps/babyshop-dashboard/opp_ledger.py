"""
Opportunity ledger — the single list of business-development opportunities.

Lens runs (opportunities/lenses/*.md, rules in opportunities/RULES.md) produce
candidate JSON files. This module is the ONLY writer: it validates a candidate,
sizes it with the shared GP2 model, drops what is under the floor, and upserts
it into Firestore keyed by a fingerprint of (lever, scope) — so the same insight
arriving twice updates one row instead of adding a second.

Firestore (legacy project, same database as funnel_cache):
  bizdev_opportunities/{id}   one opportunity (shape: normalise())
  bizdev_needs_data/{id}      hypotheses that cannot be tested with today's data

Run locally (Firestore is in the legacy project, so authenticate as that account):

  # validate + size + show what would change, write nothing
  python3 opp_ledger.py ingest /path/to/crm.json --dry-run

  META_AUTH=gcloud CLOUDSDK_CORE_ACCOUNT=patrik.segersven@gmail.com \\
      python3 opp_ledger.py ingest /path/to/crm.json

  python3 opp_ledger.py list
  python3 opp_ledger.py status <id> rejected --note "already live since 2025"
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import re
import sys

COLLECTION       = "bizdev_opportunities"
NEEDS_COLLECTION = "bizdev_needs_data"
FIRESTORE_PROJECT = os.environ.get("FIRESTORE_PROJECT", "project-a7ade44e-e7e3-4871-a83")

# ── The model's constants (mirrored in opportunities/RULES.md) ───────────────
FLOOR_SEK_MONTH = 10_000          # base-case incremental GP2, full rollout
TEST_MARKET     = "FI"

# driver → does flow_through apply? (margin/cost/recovered act on GP2 directly)
DRIVERS = {"traffic": True, "conversion": True, "aov": True, "repeat": True,
           "margin": False, "cost": False, "recovered": False}
CONFIDENCE   = {"measured": 0.8, "cross_market": 0.5, "benchmark": 0.3}
EFFORT_WEEKS = {"S": 1, "M": 3, "L": 8, "XL": 20}
OWNERS   = {"crm", "marketing", "merchandising", "buying", "pricing", "logistics",
            "tech", "customer_service"}
ROLLOUTS = {"fi_pilot", "ship_direct", "market_specific"}
LENSES   = {"crm", "demand", "operations", "offer", "conversion", "explorer", "seed"}
# new → accepted → piloting → shipped → measured, or rejected at any point.
STATUSES = ["new", "accepted", "piloting", "shipped", "measured", "rejected"]


# ── Fingerprint ──────────────────────────────────────────────────────────────
def _slug(s) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(s or "").lower()).strip("-")


def fingerprint(lever: str, scope: dict) -> str:
    """The de-duplication key: mechanism + markets + brand.

    Markets are sorted and ALL collapses to one token, so ["SE","NO"] and
    ["NO","SE"] are the same opportunity. Category and segment are left OUT on
    purpose: they are free text that two runs word differently ("first-time
    buyers" / "new customers"), which would split one opportunity into two. A
    genuinely different mechanism gets a different lever slug instead."""
    markets = sorted({str(m).upper() for m in (scope.get("markets") or ["ALL"])})
    if "ALL" in markets:
        markets = ["ALL"]
    parts = [_slug(lever), "-".join(m.lower() for m in markets)]
    if scope.get("brand"):
        parts.append(_slug(scope["brand"]))
    fid = "--".join(p for p in parts if p)
    if len(fid) > 120:      # keep it a legible Firestore id
        fid = fid[:100] + "-" + hashlib.sha1(fid.encode()).hexdigest()[:8]
    return fid


# ── Sizing ───────────────────────────────────────────────────────────────────
def size(s: dict) -> dict:
    """Incremental GP2 per month from one driver applied to a queried baseline."""
    driver = s["driver"]
    ft = float(s.get("flow_through", 1.0)) if DRIVERS[driver] else 1.0
    base = float(s["baseline_month_sek"])
    fi   = float(s.get("fi_baseline_month_sek") or 0)
    conf = CONFIDENCE[s["evidence_grade"]]
    gp2 = {k: round(base * float(s[f"uplift_{k}"]) * ft) for k in ("low", "base", "high")}
    return {
        "gp2_month_low": gp2["low"], "gp2_month_base": gp2["base"], "gp2_month_high": gp2["high"],
        "gp2_year_base": gp2["base"] * 12,
        "gp2_month_base_fi": round(fi * float(s["uplift_base"]) * ft),
        "confidence": conf,
        "gp2_month_risk_adjusted": round(gp2["base"] * conf),
    }


def _require(cond: bool, msg: str):
    if not cond:
        raise ValueError(msg)


def normalise(c: dict, lens: str, run_date: str) -> dict:
    """Validate one candidate and return the ledger row. Raises ValueError."""
    for k in ("lever", "scope", "title", "hypothesis", "action", "evidence", "sizing",
              "effort", "owner", "rollout"):
        _require(c.get(k) not in (None, "", []), f"missing {k}")
    s = c["sizing"]
    _require(s.get("driver") in DRIVERS, f"unknown driver {s.get('driver')!r}")
    _require(s.get("evidence_grade") in CONFIDENCE, f"unknown evidence_grade {s.get('evidence_grade')!r}")
    for k in ("baseline_month_sek", "uplift_low", "uplift_base", "uplift_high"):
        _require(isinstance(s.get(k), (int, float)), f"sizing.{k} must be a number")
    _require(s["baseline_month_sek"] > 0, "sizing.baseline_month_sek must be > 0")
    _require(0 <= s["uplift_low"] <= s["uplift_base"] <= s["uplift_high"],
             "uplifts must satisfy 0 <= low <= base <= high")
    _require(c["effort"] in EFFORT_WEEKS, f"unknown effort {c['effort']!r}")
    _require(c["owner"] in OWNERS, f"unknown owner {c['owner']!r}")
    _require(c["rollout"] in ROLLOUTS, f"unknown rollout {c['rollout']!r}")
    _require(lens in LENSES, f"unknown lens {lens!r}")
    _require(all(e.get("claim") for e in c["evidence"]), "every evidence item needs a claim")
    if c["rollout"] == "fi_pilot":
        _require(c.get("pilot") and c["pilot"].get("metric"), "fi_pilot needs pilot.metric")

    scope = {"markets": sorted({str(m).upper() for m in (c["scope"].get("markets") or ["ALL"])}),
             "category": c["scope"].get("category"), "brand": c["scope"].get("brand"),
             "segment": c["scope"].get("segment")}
    sized = size(s)
    weeks = EFFORT_WEEKS[c["effort"]]
    return {
        "id": fingerprint(c["lever"], scope),
        "lever": _slug(c["lever"]), "scope": scope, "lens": lens,
        "title": c["title"].strip(), "hypothesis": c["hypothesis"].strip(),
        "action": c["action"].strip(),
        "evidence": [{"claim": e["claim"], "sql": e.get("sql"), "n": e.get("n")} for e in c["evidence"]],
        "replication": c.get("replication"),
        "sizing": {k: s.get(k) for k in (
            "driver", "baseline_month_sek", "baseline_note", "fi_baseline_month_sek",
            "uplift_low", "uplift_base", "uplift_high", "uplift_rationale",
            "flow_through", "evidence_grade")},
        **sized,
        "effort": c["effort"], "effort_weeks": weeks, "owner": c["owner"],
        # The ranking number: risk-adjusted monthly GP2 per person-week of effort.
        "score": round(sized["gp2_month_risk_adjusted"] / weeks),
        "rollout": c["rollout"], "pilot": c.get("pilot"), "risks": c.get("risks"),
        "curator_note": c.get("curator_note"),
        "last_run": run_date,
    }


# ── Firestore ────────────────────────────────────────────────────────────────
def _db():
    from google.cloud import firestore
    import refresh_meta as _rm      # the one ADC / gcloud-token fallback in this app
    return firestore.Client(project=FIRESTORE_PROJECT, credentials=_rm._credentials())


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def ingest(bundle: dict, db=None, dry_run: bool = False) -> dict:
    """Upsert one lens run. Returns a report of what happened to every candidate."""
    lens, run_date = bundle["lens"], bundle.get("run_date") or datetime.date.today().isoformat()
    report = {"added": [], "updated": [], "below_floor": [], "invalid": [],
              "kept_rejected": [], "same_lever": [], "needs_data": 0}
    rows = []
    for c in bundle.get("candidates", []):
        try:
            row = normalise(c, lens, run_date)
        except (ValueError, KeyError, TypeError) as e:
            report["invalid"].append({"title": c.get("title"), "error": str(e)})
            continue
        if row["gp2_month_base"] < FLOOR_SEK_MONTH:
            report["below_floor"].append({"id": row["id"], "gp2_month_base": row["gp2_month_base"]})
            continue
        rows.append(row)

    existing = {}
    if db is not None:
        existing = {d.id: d.to_dict() for d in db.collection(COLLECTION).stream()}

    for row in rows:
        old = existing.get(row["id"])
        # Same mechanism under a different scope is the commonest near-duplicate
        # (e.g. one lens says ALL, another says FI). Not merged automatically —
        # surfaced for the curator.
        twins = [i for i, o in existing.items() if o.get("lever") == row["lever"] and i != row["id"]]
        twins += [r["id"] for r in rows if r["lever"] == row["lever"] and r["id"] != row["id"]]
        if twins:
            report["same_lever"].append({"id": row["id"], "others": sorted(set(twins))})

        if old and old.get("status") == "rejected":
            report["kept_rejected"].append(row["id"])     # a rejection sticks
            continue
        if old:
            row.update(status=old.get("status", "new"), status_note=old.get("status_note"),
                       status_history=old.get("status_history", []),
                       first_seen=old.get("first_seen", run_date),
                       times_seen=int(old.get("times_seen", 1)) + (old.get("last_run") != run_date),
                       lenses=sorted(set(old.get("lenses", [])) | {lens}))
            report["updated"].append(row["id"])
        else:
            row.update(status="new", status_note=None, status_history=[],
                       first_seen=run_date, times_seen=1, lenses=[lens])
            report["added"].append(row["id"])
        row["updated_at"] = _now()
        if not dry_run and db is not None:
            db.collection(COLLECTION).document(row["id"]).set(row)
        existing[row["id"]] = row

    for n in bundle.get("needs_data", []):
        if not n.get("hypothesis"):
            continue
        nid = _slug(n["hypothesis"])[:100]
        report["needs_data"] += 1
        if not dry_run and db is not None:
            db.collection(NEEDS_COLLECTION).document(nid).set(
                {"id": nid, "lens": lens, "last_run": run_date, **n, "updated_at": _now()})
    report["rows"] = rows
    return report


def set_status(db, opp_id: str, new_status: str, note: str | None = None, by: str = "") -> dict:
    """Move an opportunity through its lifecycle; the trail is kept on the row."""
    if new_status not in STATUSES:
        raise ValueError(f"unknown status {new_status!r}; one of {STATUSES}")
    ref = db.collection(COLLECTION).document(opp_id)
    snap = ref.get()
    if not snap.exists:
        raise KeyError(opp_id)
    row = snap.to_dict()
    hist = row.get("status_history", [])
    hist.append({"from": row.get("status"), "to": new_status, "note": note, "by": by, "at": _now()})
    ref.update({"status": new_status, "status_note": note, "status_history": hist,
                "updated_at": _now()})
    return {**row, "status": new_status, "status_note": note, "status_history": hist}


def payload(db) -> dict:
    """What GET /api/opportunities returns: the ranked list plus the model."""
    items = [d.to_dict() for d in db.collection(COLLECTION).stream()]
    items.sort(key=lambda r: -r.get("score", 0))
    needs = [d.to_dict() for d in db.collection(NEEDS_COLLECTION).stream()]
    needs.sort(key=lambda r: (r.get("lens", ""), r.get("id", "")))
    return {
        "items": items, "needs_data": needs,
        "model": {"floor_sek_month": FLOOR_SEK_MONTH, "test_market": TEST_MARKET,
                  "confidence": CONFIDENCE, "effort_weeks": EFFORT_WEEKS, "statuses": STATUSES},
        "generated_at": _now(),
    }


# ── CLI ──────────────────────────────────────────────────────────────────────
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ingest"); p.add_argument("path"); p.add_argument("--dry-run", action="store_true")
    sub.add_parser("list")
    p = sub.add_parser("status"); p.add_argument("id"); p.add_argument("status"); p.add_argument("--note")
    a = ap.parse_args(argv)

    if a.cmd == "ingest":
        with open(a.path) as f:
            bundle = json.load(f)
        # A dry run still reads the ledger when it can, so the report shows
        # added vs updated truthfully; with no credentials it validates only.
        try:
            db = _db()
        except Exception as e:
            if not a.dry_run:
                raise
            print(f"(no Firestore: {type(e).__name__} — validating only)")
            db = None
        r = ingest(bundle, db=db, dry_run=a.dry_run)
        for row in sorted(r["rows"], key=lambda x: -x["score"]):
            print(f"  {row['score']:>7,}  {row['gp2_month_base']:>8,} kr/mo  {row['effort']:<2} "
                  f"{row['sizing']['evidence_grade']:<12} {row['id']}")
        for k in ("added", "updated", "below_floor", "invalid", "kept_rejected", "same_lever"):
            if r[k]:
                print(f"{k}: {json.dumps(r[k], ensure_ascii=False)}")
        print(f"needs_data: {r['needs_data']}" + ("   (dry run — nothing written)" if a.dry_run else ""))
        return 1 if r["invalid"] else 0
    if a.cmd == "list":
        for row in payload(_db())["items"]:
            print(f"  {row['score']:>7,}  {row['gp2_month_base']:>8,} kr/mo  {row['status']:<9} {row['id']}")
        return 0
    if a.cmd == "status":
        set_status(_db(), a.id, a.status, a.note, by="cli")
        print(f"{a.id} → {a.status}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
