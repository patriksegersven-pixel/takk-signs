#!/usr/bin/env python3
"""
Shipping cost tab — order-level lookups for /api/shipping/orders.

The tab's summary (insights, monthly/daily series, market and market × method
rows) is ONE Firestore snapshot, `funnel_cache/<workspace>__shipping`, read by
/api/shipping. Only the order table needs a live query, because 280k orders do
not fit in a snapshot: it reads `shipping_cost.order_costs` in this project.

Both are written by the shipping-cost model (babyshop-shipping-model, outside
this repo) after each weekly recalibration: one row per Norce order with the
carrier and method from the checkout choice, basket weight, the estimated
outbound freight in SEK (calibrated against each carrier's invoices in
Business Central) and, where the carrier's invoice lines carry the order
number (Bring, Posti, Airmee), what the carrier actually billed.

OLD-STACK module: reads only this project's own BigQuery.
"""
from __future__ import annotations

import csv
import io
import os
import re
from datetime import date

from google.cloud import bigquery

import bq_source

PROJECT = os.environ.get("SHIPPING_BQ_PROJECT", "project-a7ade44e-e7e3-4871-a83")
LOCATION = os.environ.get("SHIPPING_BQ_LOCATION", "EU")
TABLE = f"`{PROJECT}.shipping_cost.order_costs`"

COLUMNS = ["order_no", "order_date", "project", "country", "carrier", "method", "basket_kg", "items",
           "cost_sek", "source", "invoice_amount_sek", "calibration_factor", "revenue_sek"]
SORTABLE = {"order_no", "order_date", "project", "country", "carrier", "method", "basket_kg", "items",
            "cost_sek", "invoice_amount_sek", "revenue_sek"}
PAGE_MAX = 500
EXPORT_MAX = 100_000

_client = None


def _bq() -> bigquery.Client:
    global _client
    if _client is None:
        _client = bigquery.Client(project=PROJECT, location=LOCATION, credentials=bq_source._credentials())
    return _client


def _list(v: str | None) -> list[str]:
    return [x.strip() for x in (v or "").split(",") if x.strip()][:100]


def _date(v: str | None) -> date | None:
    if not v:
        return None
    try:
        return date.fromisoformat(v.strip())
    except ValueError:
        raise ValueError(f"bad date {v!r}, expected YYYY-MM-DD")


def _num(v: str | None) -> float | None:
    if v in (None, ""):
        return None
    try:
        return float(v)
    except ValueError:
        raise ValueError(f"bad number {v!r}")


def _where(p: dict) -> tuple[str, list]:
    """WHERE clause + query parameters from the request's filters. Every value
    is a bound parameter; column names come from fixed strings only."""
    conds, params = [], []
    d_from, d_to = _date(p.get("from")), _date(p.get("to"))
    if d_from:
        conds.append("order_date >= @d_from"); params.append(bigquery.ScalarQueryParameter("d_from", "DATE", d_from))
    if d_to:
        conds.append("order_date <= @d_to"); params.append(bigquery.ScalarQueryParameter("d_to", "DATE", d_to))
    for key, col in (("project", "project"), ("country", "country"), ("carrier", "carrier"),
                     ("method", "method"), ("source", "source")):
        vals = _list(p.get(key))
        if vals:
            conds.append(f"{col} IN UNNEST(@{key})")
            params.append(bigquery.ArrayQueryParameter(key, "STRING", vals))
    q = (p.get("q") or "").strip()
    if q:
        if not re.fullmatch(r"[0-9 ,]{1,2000}", q):
            raise ValueError("order search takes order numbers (digits), comma-separated")
        nums = [x for x in re.split(r"[ ,]+", q) if x]
        if len(nums) == 1:
            conds.append("STARTS_WITH(order_no, @q)"); params.append(bigquery.ScalarQueryParameter("q", "STRING", nums[0]))
        else:
            conds.append("order_no IN UNNEST(@qs)"); params.append(bigquery.ArrayQueryParameter("qs", "STRING", nums[:500]))
    for key, op in (("min_cost", ">="), ("max_cost", "<=")):
        v = _num(p.get(key))
        if v is not None:
            conds.append(f"cost_sek {op} @{key}"); params.append(bigquery.ScalarQueryParameter(key, "FLOAT64", v))
    for key, op in (("min_kg", ">="), ("max_kg", "<=")):
        v = _num(p.get(key))
        if v is not None:
            conds.append(f"basket_kg {op} @{key}"); params.append(bigquery.ScalarQueryParameter(key, "FLOAT64", v))
    return ("WHERE " + " AND ".join(conds)) if conds else "", params


def query_orders(p: dict) -> dict:
    """One page of orders plus totals over the whole filtered set."""
    where, params = _where(p)
    sort = p.get("sort") if p.get("sort") in SORTABLE else "order_date"
    direction = "ASC" if (p.get("dir") or "").lower() == "asc" else "DESC"
    limit = max(1, min(int(p.get("limit") or 100), PAGE_MAX))
    offset = max(0, int(p.get("offset") or 0))
    cfg = bigquery.QueryJobConfig(query_parameters=params)
    tot = list(_bq().query(f"""
        SELECT COUNT(*) n, SUM(cost_sek) cost, SUM(basket_kg) kg, SUM(revenue_sek) rev,
               COUNTIF(source = 'carrier invoice') invoiced, MIN(order_date) d_min, MAX(order_date) d_max
        FROM {TABLE} {where}""", job_config=cfg).result())[0]
    rows = _bq().query(f"""
        SELECT {", ".join(COLUMNS)} FROM {TABLE} {where}
        ORDER BY {sort} {direction}, order_no {direction}
        LIMIT {limit} OFFSET {offset}""", job_config=cfg).result()
    return {
        "total": {"orders": tot.n, "cost_sek": round(tot.cost or 0, 2), "basket_kg": round(tot.kg or 0, 1),
                  "revenue_sek": round(tot.rev or 0, 2), "invoiced_orders": tot.invoiced,
                  "from": tot.d_min.isoformat() if tot.d_min else None, "to": tot.d_max.isoformat() if tot.d_max else None},
        "columns": COLUMNS, "limit": limit, "offset": offset, "sort": sort, "dir": direction.lower(),
        "rows": [[(r[c].isoformat() if isinstance(r[c], date) else r[c]) for c in COLUMNS] for r in rows],
    }


def export_orders_csv(p: dict) -> str:
    """The filtered set as CSV (capped at EXPORT_MAX rows)."""
    where, params = _where(p)
    rows = _bq().query(f"SELECT {', '.join(COLUMNS)} FROM {TABLE} {where} ORDER BY order_date DESC, order_no DESC LIMIT {EXPORT_MAX}",
                       job_config=bigquery.QueryJobConfig(query_parameters=params)).result()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(COLUMNS)
    for r in rows:
        w.writerow([r[c] for c in COLUMNS])
    return buf.getvalue()
