#!/usr/bin/env python3
"""
bm_common.py: shared constants and plumbing for the Black Month tab.

The Black Month tab (babyshop-blackmonth.html) is an operational war-room page
for November 2026. It is assembled from independent SECTIONS. Each section is
one Python module `bm_<key>.py` exposing

    build(ctx: dict) -> dict          # JSON-serialisable payload

and one browser module `bm-<key>.js` that renders that payload. The job
`refresh_blackmonth.py` calls every section's build() and writes each payload
to its own Firestore document, `funnel_cache/<workspace>__bm__<key>`; the
service serves it at GET /api/blackmonth/<key>. A failing section never blocks
the others. See BLACKMONTH.md for the full contract.

Everything that more than one section needs to agree on lives here: the
campaign period, the markets, the approved Meta plan, the forecast lines.
"""
from __future__ import annotations

import datetime
import json
import os
import time
from pathlib import Path
from zoneinfo import ZoneInfo

HERE = Path(__file__).parent

# ── Where things live ────────────────────────────────────────────────────────
GCP_PROJECT = os.environ.get("GCP_PROJECT", "project-a7ade44e-e7e3-4871-a83")
WORKSPACE = os.environ.get("FUNNEL_WORKSPACE", "-Ln87GcdqU9CMJV6zMBY")
COLLECTION = "funnel_cache"
DOC_PREFIX = "bm__"                    # doc id: <WORKSPACE>__bm__<section>
TTL = 30 * 24 * 3600
TZ = ZoneInfo("Europe/Stockholm")

# Section keys, in page order. A key here with no bm_<key>.py yet is skipped by
# the job and rendered as "not built yet" by the page.
SECTIONS = ["pacing", "paid", "creatives", "offers", "insights"]

# ── The campaign period ──────────────────────────────────────────────────────
YEAR, MONTH = 2026, 11
MONTH_START = datetime.date(2026, 11, 1)
MONTH_END = datetime.date(2026, 11, 30)
BLACK_FRIDAY = datetime.date(2026, 11, 27)
CYBER_MONDAY = datetime.date(2026, 11, 30)
BLACK_WEEK = (datetime.date(2026, 11, 23), CYBER_MONDAY)
# Same events last year, for event-aligned comparisons.
BLACK_FRIDAY_LY = datetime.date(2025, 11, 28)
CYBER_MONDAY_LY = datetime.date(2025, 12, 1)

MARKETS = ["SE", "NO", "FI", "DK"]     # core Nordic markets; ROW shown as one bucket

# ── Approved Meta plan, November 2026 (SEK) ──────────────────────────────────
# Approved by Babyshop 2026-11-04 as proposed in September.
META_PLAN_MONTH = 200_000
META_PLAN_BY_TYPE = {"kuling": 60_000, "prospecting": 20_000, "retargeting": 120_000}
META_PLAN_MARKET_SHARE = {"SE": 0.60, "NO": 0.20, "FI": 0.10, "DK": 0.10}

# ── Spend philosophy (decided with Babyshop 2026-11-04) ──────────────────────
# Google has NO budget: it is performance/incrementality driven and optimised to
# maximise GP3. The forecast's COS (marketing ÷ gross sales = 10.5 %) is a
# derived envelope built on assumed GM1/GM2, not a cap: the binding constraint
# is the forecast's GP3. Margin above forecast is room to spend more — on Google
# AND Meta — wherever the extra krona still adds incremental GP3. The Meta plan
# above is the approved starting point, not a ceiling.
COS_TARGET = 0.105                     # forecast marketing ÷ gross sales (5.54M ÷ 52.9M)

# Incrementality: share of platform-reported value that is truly incremental.
# ASSUMPTIONS until Babyshop has test results — show them on the page as such.
INCREMENTALITY = {
    "google_brand_search": 0.30,
    "google_generic_search": 0.80,
    "google_shopping_pmax": 0.70,
    "meta_prospecting": 0.60,
    "meta_kuling": 0.60,
    "meta_retargeting": 0.30,
}

# DEPRECATED: Google has no budget. Kept only as an "expected spend" reference
# (+20 % Black Week assumption) until bm_paid/bm_insights stop reading it; never
# judge Google spend against it.
GOOGLE_PLAN_MONTH = 3_100_000
GOOGLE_PLAN_MARKET_SHARE = {"SE": 0.54, "NO": 0.28, "FI": 0.08, "DK": 0.07, "ROW": 0.03}


def budget_lines() -> dict:
    """Finance's rolling forecast (budget_2026.json), November value per line,
    e.g. {'gross_sales': 52914713, 'gp1': ..., 'gp2': ..., 'marketing': ...}.
    Costs are positive magnitudes. Global only: no market split in the file."""
    b = json.loads((HERE / "budget_2026.json").read_text())
    return {k: v["monthly"][MONTH - 1] for k, v in b["lines"].items()}


def now() -> datetime.datetime:
    return datetime.datetime.now(TZ)


def today() -> datetime.date:
    return now().date()


def month_days() -> list[datetime.date]:
    return [MONTH_START + datetime.timedelta(days=i)
            for i in range((MONTH_END - MONTH_START).days + 1)]


def make_ctx() -> dict:
    """The ctx every build() receives. Sections may add their own lookups but
    must not depend on another section's output (sections run independently;
    only `insights` reads the others, from Firestore)."""
    return {
        "today": today(),
        "generated_at": now().isoformat(timespec="seconds"),
        "budget": budget_lines(),
    }


def envelope(section: str, payload: dict) -> dict:
    """Common wrapper every stored payload carries."""
    return {"section": section, "generated_at": now().isoformat(timespec="seconds"),
            "period": {"from": MONTH_START.isoformat(), "to": MONTH_END.isoformat()},
            **payload}


def write_section(section: str, payload: dict) -> None:
    from google.cloud import firestore
    db = firestore.Client(project=GCP_PROJECT)
    db.collection(COLLECTION).document(f"{WORKSPACE}__{DOC_PREFIX}{section}").set({
        "data": payload, "created_at": time.time(),
        "expires_at": time.time() + TTL, "ttl_seconds": TTL, "workspace": WORKSPACE})


def read_section(section: str) -> dict | None:
    """Read another section's stored payload (used by `insights`)."""
    from google.cloud import firestore
    db = firestore.Client(project=GCP_PROJECT)
    snap = db.collection(COLLECTION).document(f"{WORKSPACE}__{DOC_PREFIX}{section}").get()
    return (snap.to_dict() or {}).get("data") if snap.exists else None
