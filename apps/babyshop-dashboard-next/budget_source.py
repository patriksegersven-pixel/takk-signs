#!/usr/bin/env python3
"""
Budget / forecast source for the Babyshop dashboard.

Parses Finance's "Rullande forecast · P&L" workbook export into a compact
monthly JSON — one entry per P&L line, 12 months (Jan→Dec) + full year — and
writes it to Firestore doc `funnel_cache/<WORKSPACE>__budget-2026`, which the
dashboard reads via /api/budget.

The forecast is a *budget*, not live data: it changes when Finance revises the
plan, not daily. So the flow is two-step and container-safe:

    1.  Locally, once per revision:  python3 budget_source.py path/to/Forecast.xlsx
        → parses the xlsx, writes budget_2026.json (committed to the repo).
    2.  The daily Cloud Run job (bq_source.main) calls push_budget(), which reads
        the committed budget_2026.json and writes it to Firestore. No xlsx is
        needed in the container, and the doc self-heals on every refresh.

Only two P&L lines reconcile with the dashboard's Funnel/BigQuery data and are
tracked live against plan (revenue, marketing spend); the rest are served as a
context plan table. See ``LINES`` and the revenue calibration note below.
"""
from __future__ import annotations

import datetime
import json
import os

# Reuse the exact Firestore wiring the rest of the dashboard uses.
from bq_source import COLLECTION, FIRESTORE_PROJECT, TTL, WORKSPACE, _credentials

HERE = os.path.dirname(os.path.abspath(__file__))
BUDGET_JSON = os.path.join(HERE, "budget_2026.json")
YEAR = 2026

# ── P&L line map ──────────────────────────────────────────────────────────────
# The workbook is the "Rullande forecast · P&L" export from Finance's app
# (sheet "P&L": column A = line label, B..M = Jan..Dec, N = FY). Rows move
# between exports, so lines are located by label, not by row number.
#
# key -> (workbook labels summed into the line, human label, group, is_cost)
# Sub-total lines the dashboard tracks are exact workbook rows; the two
# bracket lines the old layout carried as one row are sums of workbook rows:
#   direct_var  = Net Shipping + Total Fulfillment + Transaction fees (= GP1 − GP2)
#   interest_da = Amortization & Depreciation + Total Financial Items (= EBITDA − EBT)
LINES = [
    ("gross_sales", ["Gross Sales"],           "Gross sales",           "sales",  False),
    ("returns",     ["Returns"],               "Sales returns",         "sales",  True),
    ("net_sales",   ["Net Sales"],             "Net sales",             "sales",  False),
    ("cogs",        ["Total COGS"],            "Cost of goods sold",    "cogs",   True),
    ("gp1",         ["Gross profit 1"],        "Gross profit 1",        "profit", False),
    ("direct_var",  ["Net Shipping", "Total Fulfillment", "Transaction fees"],
                                               "Direct variable costs", "cost",   True),
    ("gp2",         ["GP2"],                   "Gross profit 2",        "profit", False),
    ("marketing",   ["Total Marketing"],       "Marketing costs",       "cost",   True),
    ("gp3",         ["GP3"],                   "Gross profit 3",        "profit", False),
    ("opex",        ["Total Overhead"],        "Operating expenses",    "cost",   True),
    ("other_income",["Other Income"],          "Other income",          "other",  False),
    ("ebitda",      ["EBITDA"],                "EBITDA",                "profit", False),
    ("interest_da", ["Amortization & Depreciation", "Total Financial Items"],
                                               "Interest, D&A",         "cost",   True),
    ("ebt",         ["EBT"],                   "EBT",                   "profit", False),
    ("eat",         ["Net Income"],            "Earnings after tax",    "profit", False),
]
SHEET = "P&L"
HEADER_ROW = 5                    # A5 = "SEK", B5..M5 = "jan · prognos" …, N5 = "FY"
MONTH_COLS = list(range(2, 14))   # B(2) .. M(13)  → Jan..Dec
FY_COL = 14                       # N
# Identities the workbook must satisfy; a failed check means a row was
# renamed/moved and the label map above needs a look.
IDENTITIES = [("gp1", "direct_var", "gp2"), ("ebitda", "interest_da", "ebt")]


def _num(v):
    if v is None:
        return None
    try:
        return round(float(v))
    except (TypeError, ValueError):
        return None


def _month_status(header) -> str:
    h = (str(header or "")).lower()
    return "Actual" if ("utfall" in h or "actual" in h) else "Forecast"


def parse_xlsx(path: str) -> dict:
    """Parse the forecast workbook into the budget dict."""
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[SHEET]

    # Label -> row (first occurrence). "Cost of goods sold" appears twice
    # (section header + line); we only address sub-totals, so first-hit is fine.
    rows = {}
    for r in range(1, ws.max_row + 1):
        lbl = ws.cell(row=r, column=1).value
        if isinstance(lbl, str) and lbl.strip() and lbl.strip() not in rows:
            rows[lbl.strip()] = r

    month_status = [_month_status(ws.cell(row=HEADER_ROW, column=c).value)
                    for c in MONTH_COLS]

    def _row_vals(label):
        r = rows.get(label)
        if r is None:
            raise KeyError(f"P&L line {label!r} not found in {os.path.basename(path)}")
        return ([_num(ws.cell(row=r, column=c).value) for c in MONTH_COLS],
                _num(ws.cell(row=r, column=FY_COL).value))

    lines = {}
    for key, labels, label, group, is_cost in LINES:
        monthly = [0] * 12
        fy = 0
        for lb in labels:
            m, f = _row_vals(lb)
            monthly = [a + (b or 0) for a, b in zip(monthly, m)]
            fy += (f or 0)
        # Store costs as positive magnitudes (the sheet keeps them negative);
        # `is_cost` tells the UI how to render them.
        if is_cost:
            monthly = [-v for v in monthly]
            fy = -fy
        lines[key] = {
            "label": label, "group": group, "is_cost": is_cost,
            "monthly": monthly, "fy": fy,
        }

    for top, cost, bottom in IDENTITIES:
        for i in range(13):
            pick = (lambda k: lines[k]["fy"]) if i == 12 else (lambda k: lines[k]["monthly"][i])
            diff = pick(top) - pick(cost) - pick(bottom)
            if abs(diff) > 2:   # rounding only
                raise ValueError(f"{top} − {cost} ≠ {bottom} (col {i}, off by {diff}); "
                                 f"check the LINES label map")

    return {
        "year": YEAR,
        "currency": "SEK",
        "month_status": month_status,          # 12 × "Actual" | "Forecast"
        "line_order": [k for k, *_ in LINES],
        "lines": lines,
        "source_file": os.path.basename(path),
        "exported": (str(ws.cell(row=2, column=1).value or "")[:40]),
        "note": "Rullande P&L forecast. Costs stored as positive magnitudes.",
    }


def load_budget_json() -> dict:
    with open(BUDGET_JSON, encoding="utf-8") as f:
        return json.load(f)


def push_budget(budget: dict | None = None) -> None:
    """Write the budget doc to Firestore (called by the daily job)."""
    from google.cloud import firestore
    budget = budget or load_budget_json()
    db = firestore.Client(project=FIRESTORE_PROJECT, credentials=_credentials())
    now = datetime.datetime.now(datetime.timezone.utc).timestamp()
    db.collection(COLLECTION).document(f"{WORKSPACE}__budget-2026").set({
        "data": budget,
        "fetched_at": firestore.SERVER_TIMESTAMP,
        "expires_at": now + TTL,
        "ttl_seconds": TTL,
        "workspace": WORKSPACE,
    })
    print(f"✓ budget-2026 written to Firestore ({len(budget['lines'])} lines)")


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2 or sys.argv[1].startswith("--"):
        sys.exit("usage: budget_source.py <rullande-forecast.xlsx> [--no-push]")
    src = sys.argv[1]
    budget = parse_xlsx(src)
    with open(BUDGET_JSON, "w", encoding="utf-8") as f:
        json.dump(budget, f, ensure_ascii=False, indent=2)
    print(f"✓ parsed {src}")
    print(f"✓ wrote {BUDGET_JSON}")
    # Quick sanity dump.
    for k in ("gross_sales", "net_sales", "marketing", "gp3", "ebitda"):
        ln = budget["lines"][k]
        print(f"  {ln['label']:<22} Jan={ln['monthly'][0]:>13,} FY={ln['fy']:>14,}")
    # Push to Firestore too when creds are available (local convenience).
    if "--no-push" not in sys.argv:
        try:
            push_budget(budget)
        except Exception as e:  # noqa: BLE001
            print(f"⚠️  Firestore push skipped ({e!r}). JSON is written; the daily "
                  f"job will push it.")
