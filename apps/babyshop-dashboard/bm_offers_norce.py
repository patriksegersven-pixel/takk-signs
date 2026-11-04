#!/usr/bin/env python3
"""
bm_offers_norce.py: order lines WITH their full price, for the Black Month
offers section (bm_offers.py).

WHY THIS EXISTS
  norce_sync.py lands `order_items` without the line's list price, so a
  markdown is invisible there: a jacket sold at 345.60 looks exactly like a
  full-price one. Norce does carry it on the OrderItem:

      PriceStandard     the shop's own standard price, EX VAT (order currency)
      PriceRecommended  RRP incl. VAT (not used for depth; often above Standard)
      PriceSale         the unit price paid, ALREADY net of any code discount
      DiscountAmount    the code/promotion discount on the line (total, not unit)
      LineAmount        = PriceSale × QtyOrdered, ex VAT

  so   markdown depth = 1 − (PriceSale + DiscountAmount/Qty) / PriceStandard
       code depth     = DiscountAmount / (LineAmount + DiscountAmount)
  (audited on live SE orders 2026-11-04: 452.88 + 50.32 = 503.20 = 70 % of the
  719.20 standard price, then 10 % HAPPYBDAY on top).

  A handful of lines carry PriceStandard incl. VAT (it equals PriceRecommended
  to the öre); full_price_unit() divides those back out.

  This module keeps its own small table, `norce.bm_offer_lines`, synced
  incrementally on Created (prices on a placed order do not change; status
  changes are ignored, matching norce_sync's "all values gross" rule). Babyshop
  and Lekmer, SE/NO/FI/DK; a watermark per application. The right long-term home is norce_sync's ORDER_EXPAND
  (three more fields on order_items); until then this table stands in.

Reuses norce_sync's OAuth/paging/BigQuery plumbing; needs the same two env vars
(NORCE_CLIENT_ID, NORCE_CLIENT_SECRET).
"""
from __future__ import annotations

import datetime
import time

from google.cloud import bigquery

import norce_sync as ns

TABLE = "bm_offer_lines"
APPS = {f"{b}-{m.lower()}": (m, b) for b in ("babyshop", "lekmer") for m in ("SE", "NO", "FI", "DK")}
BACKFILL_FROM = datetime.date(2026, 9, 15)      # 14 days before the October offers
LAP = datetime.timedelta(hours=6)               # re-read the edge; MERGE makes it idempotent

S = bigquery.SchemaField
SCHEMA = [
    S("Id", "INT64", mode="REQUIRED"), S("OrderId", "INT64"), S("market", "STRING"),
    S("brand", "STRING"), S("app_key", "STRING"),
    S("OrderDate", "TIMESTAMP"), S("Created", "TIMESTAMP"), S("CurrencyId", "INT64"),
    S("PartNo", "STRING"), S("QtyOrdered", "FLOAT64"), S("PriceSale", "FLOAT64"),
    S("DiscountAmount", "FLOAT64"), S("LineAmount", "FLOAT64"), S("VatRate", "FLOAT64"),
    S("PriceStandard", "FLOAT64"), S("PriceRecommended", "FLOAT64"),
]
SELECT = "Id,OrderDate,Created,CurrencyId"
EXPAND = ("Items($select=Id,PartNo,QtyOrdered,PriceSale,DiscountAmount,LineAmount,"
          "VatRate,PriceStandard,PriceRecommended)")


def _f(v):
    return float(v) if v is not None else None


def _rows(o: dict, app_key: str) -> list[dict]:
    market, brand = APPS[app_key]
    return [{"Id": it["Id"], "OrderId": o["Id"], "market": market, "brand": brand, "app_key": app_key,
             "OrderDate": o.get("OrderDate"), "Created": o.get("Created"),
             "CurrencyId": o.get("CurrencyId"), "PartNo": it.get("PartNo"),
             "QtyOrdered": _f(it.get("QtyOrdered")), "PriceSale": _f(it.get("PriceSale")),
             "DiscountAmount": _f(it.get("DiscountAmount")), "LineAmount": _f(it.get("LineAmount")),
             "VatRate": _f(it.get("VatRate")), "PriceStandard": _f(it.get("PriceStandard")),
             "PriceRecommended": _f(it.get("PriceRecommended"))}
            for it in (o.get("Items") or [])]


def _ensure_table() -> None:
    t = bigquery.Table(ns.T(TABLE), schema=SCHEMA)
    t.time_partitioning = bigquery.TimePartitioning(field="OrderDate")
    t = ns.bq().create_table(t, exists_ok=True)
    have = {f.name for f in t.schema}
    if {"brand", "app_key"} - have:
        # v1 of this table was Babyshop-only and had neither column.
        t.schema = list(t.schema) + [f for f in SCHEMA if f.name not in have]
        ns.bq().update_table(t, ["schema"])
        ns.bq().query(f"UPDATE `{ns.T(TABLE)}` SET brand = 'babyshop', "
                      f"app_key = CONCAT('babyshop-', LOWER(market)) WHERE brand IS NULL").result()


def _merge(rows: list[dict]) -> int:
    if not rows:
        return 0
    rows = list({r["Id"]: r for r in rows}.values())
    stg = ns._load(rows, TABLE, SCHEMA)
    cols = [f.name for f in SCHEMA]
    ns.bq().query(
        f"MERGE `{ns.T(TABLE)}` t USING `{stg}` s ON t.Id = s.Id "
        f"WHEN MATCHED THEN UPDATE SET {', '.join(f'{c} = s.{c}' for c in cols if c != 'Id')} "
        f"WHEN NOT MATCHED THEN INSERT ({', '.join(cols)}) VALUES ({', '.join('s.' + c for c in cols)})"
    ).result()
    ns.bq().delete_table(stg, not_found_ok=True)
    return len(rows)


def _watermarks() -> dict[str, datetime.datetime]:
    return {r["a"]: r["w"] for r in ns.bq().query(
        f"SELECT app_key a, MAX(Created) w FROM `{ns.T(TABLE)}` GROUP BY a").result()}


def sync() -> dict:
    """Pull new orders' lines since the watermark. Returns a small log dict."""
    _ensure_table()
    wm = _watermarks()
    start = datetime.datetime.combine(BACKFILL_FROM, datetime.time(), datetime.timezone.utc)
    t0, n = time.time(), 0
    for key in APPS:
        app_id = ns.APPLICATIONS[key]
        since = (wm[key] - LAP) if wm.get(key) else start
        flt = f"ApplicationId eq {app_id} and Created ge {ns._odata_ts(since)}"
        buf = []
        for o in ns.query("Orders/Orders", app_id, select=SELECT, expand=EXPAND,
                          filter=flt, orderby="Id"):
            buf.extend(_rows(o, key))
            if len(buf) >= 20000:
                n += _merge(buf)
                buf = []
        n += _merge(buf)
    return {"lines": n, "seconds": round(time.time() - t0, 1)}


# SQL fragments for bm_offers.py. `l` is a bm_offer_lines row.
# Full unit price ex VAT; the incl-VAT outliers are divided back.
FULL_UNIT = ("IF(l.PriceRecommended IS NOT NULL AND l.VatRate > 0 "
             "AND ABS(l.PriceStandard - l.PriceRecommended) < 0.01, "
             "l.PriceStandard / (1 + l.VatRate / 100), l.PriceStandard)")


if __name__ == "__main__":
    print(sync())
