"""
Channable feed → aggregated inventory insights.

Fetches the Channable Google Shopping XML feed (~96 MB uncompressed,
32k SKU variants), parses it streaming-style, and produces the
aggregated payload the Inventory dashboard consumes.

Cache: writes the result to Firestore under inventory_cache/snapshot
(via funnel_client's _FirestoreCache). Reads also fall through that
cache so /api/inventory serves instantly.

Config (env vars):
  CHANNABLE_FEED_URL   — full URL to the XML feed (Secret Manager)
  CACHE_TTL_SECONDS    — defaults to 1800 (30 min)

Streaming parse uses xml.etree.ElementTree.iterparse + elem.clear()
to keep memory low (~30 MB peak even on 96 MB input).
"""
from __future__ import annotations

import json
import os
import re
import time
import xml.etree.ElementTree as ET
from collections import defaultdict
from typing import Optional

import httpx

from funnel_client import get_cache  # reuse the shared Firestore/in-process cache

CHANNABLE_FEED_URL = os.environ.get("CHANNABLE_FEED_URL")
CACHE_KEY = "inventory-snapshot"

NS = {"g": "http://base.google.com/ns/1.0"}

# Per-SKU stock comes from the feed's <units_for_sale> field (real count,
# present on every SKU including dead_stock items). We use it as-is.
# OOS lost-revenue is still an estimate because it's about sales velocity,
# not stock — override via env var if you have a better number.
OOS_LOST_QTY = int(os.environ.get("OOS_LOST_QTY", 5))    # fallback units/month/SKU if no velocity data

# Per-category sales velocity (monthly units sold), sourced from the Funnel
# kv-product Quantity measure and written to the shared cache by the daily
# refresh task. Used to predict OOS lost units instead of a flat assumption.
VELOCITY_KEY = "inventory-category-velocity"

# ── Firestore write budget ───────────────────────────────────────────────────
# The whole snapshot is ONE Firestore document (funnel_cache/<ws>__inventory-
# snapshot, written through funnel_client's cache) and Firestore's hard document
# limit is 1,048,576 bytes. The table lists below are deliberately UNCAPPED — the
# dashboard tables scroll, so they show every qualifying row — but uncapped is not
# a promise that the data fits: `on_sale` alone was 26,909 SKUs on 2026-08-17
# (~291 B/row ≈ 7.8 MB of JSON). So the payload is measured before it is written
# and the list-shaped tables are trimmed to fit, largest list first, keeping sort
# order so the highest-value rows always survive.
#
# The measurement is COMPACT JSON, which over-states Firestore's own accounting
# (string = bytes+1, number = 8, bool/null = 1, plus one key per map entry), so
# this budget trips before Firestore would reject the write — the safe direction.
SNAPSHOT_BUDGET_BYTES = int(os.environ.get("INVENTORY_SNAPSHOT_BUDGET_BYTES", 900_000))

# Only these are trimmable: they are list-shaped TABLES, sorted worst/biggest
# first, so dropping the tail costs the least. dead_by_brand / dead_by_cat feed
# CHARTS (fixed small top-N by design) and `kpis` are scalars computed over the
# full feed — trimming either would change what the page reports, not just how
# much of it. Order here is the tie-break order when shaving the last few bytes.
_TRIMMABLE = ("top_discounts", "oos_items", "stockout_risk", "brand_health")


def _load_category_velocity() -> dict:
    """Return {category(lower): monthly units sold across that category}.
    Written by the daily Funnel refresh; returns {} if unavailable."""
    try:
        v = get_cache().get(VELOCITY_KEY)
        if isinstance(v, dict):
            src = v.get("by_category") or v
            return {str(k).lower(): float(x) for k, x in src.items() if x is not None}
    except Exception as e:
        print(f"inventory_client: velocity load failed: {e!r}")
    return {}


class InventoryNotConfigured(Exception):
    """CHANNABLE_FEED_URL not set."""


def _parse_sek(s: Optional[str]) -> float:
    if not s:
        return 0.0
    m = re.match(r"([\d.,]+)", s.strip())
    if not m:
        return 0.0
    return float(m.group(1).replace(",", "."))


def _short(s: str, n: int = 70) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


def _jlen(obj) -> int:
    """Serialized size of `obj` as compact UTF-8 JSON, in bytes."""
    return len(json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))


def _fit_to_budget(payload: dict, budget: int = SNAPSHOT_BUDGET_BYTES) -> dict:
    """Trim the table lists just enough for the snapshot to fit one Firestore doc.

    Mutates and returns `payload`. Adds:
      payload["snapshot_bytes"] — compact-JSON size of what is actually returned
      payload["truncated"]     — {list_name: {"kept": n, "total": m, "reason": …}}
                                 for every list that lost rows (empty dict when
                                 nothing was trimmed, so the page can rely on it)
    and prints a warning naming each trimmed list.

    Rows are dropped from the TAIL only, so the sort each list was built with
    (worst stockout risk / priciest OOS / deepest discount / biggest catalogue
    first) decides what survives.

    Allocation is fair-share, not flat-proportional: every list is offered an
    equal slice of the room left after the non-trimmable parts, a list that fits
    inside its slice keeps ALL its rows, and the unused slack rolls on to the
    bigger lists. So the one list that caused the overflow (`top_discounts`, 26.9k
    rows) gives up the rows, and the small tables stay complete — a proportional
    split would instead cut 191 stockout rows to ~22 to buy the giant list a few
    hundred more.
    """
    payload.setdefault("truncated", {})
    total = _jlen(payload)
    if total <= budget:
        payload["snapshot_bytes"] = total
        return payload

    lists = {k: payload[k] for k in _TRIMMABLE
             if isinstance(payload.get(k), list) and payload[k]}
    if not lists:
        payload["snapshot_bytes"] = total
        print(f"⚠️  inventory_client: snapshot is {total:,} B, over the {budget:,} B "
              f"budget, but no trimmable list is populated")
        return payload

    sizes = {k: _jlen(v) for k, v in lists.items()}
    fixed = total - sum(sizes.values())                # kpis, charts, metadata
    remaining = max(budget - fixed, 0)

    # Smallest list first, so each pass hands its unused slack to the bigger ones.
    for n, k in enumerate(sorted(lists, key=lambda n: sizes[n])):
        rws, share = lists[k], remaining / (len(lists) - n)
        if sizes[k] <= share:
            remaining -= sizes[k]                      # fits whole, keep every row
            continue
        per_row = sizes[k] / len(rws)
        keep = max(min(int(share // per_row), len(rws)), 1)   # never empty a table
        remaining -= keep * per_row
        if keep < len(rws):
            payload["truncated"][k] = {"kept": keep, "total": len(rws),
                                       "reason": "firestore-1mib-budget"}
            payload[k] = rws[:keep]

    # Per-row averages and the note itself can leave it a hair over — shave the
    # biggest remaining list until it fits (bounded: each pass drops ≥1 row).
    for _ in range(400):
        if _jlen(payload) <= budget:
            break
        k = max(_TRIMMABLE, key=lambda n: _jlen(payload.get(n) or []))
        rws = payload.get(k) or []
        if len(rws) <= 1:
            break                                      # nothing left to give
        keep = min(int(len(rws) * 0.95), len(rws) - 1) or 1
        note = payload["truncated"].setdefault(
            k, {"kept": keep, "total": len(rws), "reason": "firestore-1mib-budget"})
        note["kept"] = keep
        payload[k] = rws[:keep]

    payload["snapshot_bytes"] = _jlen(payload)
    print("⚠️  inventory_client: snapshot was {:,} B (budget {:,}) — trimmed to {:,} B: {}".format(
        total, budget, payload["snapshot_bytes"],
        ", ".join(f"{k} {v['kept']:,}/{v['total']:,}"
                  for k, v in payload["truncated"].items()) or "nothing"))
    return payload


def _fetch_and_parse_feed() -> dict:
    """Download + parse + aggregate. Returns the payload to cache."""
    if not CHANNABLE_FEED_URL:
        raise InventoryNotConfigured("CHANNABLE_FEED_URL is not set")

    # Stream-download to a temp file so we don't hold the whole XML in memory
    with httpx.stream("GET", CHANNABLE_FEED_URL, timeout=120.0, follow_redirects=True) as r:
        r.raise_for_status()
        tmp_path = f"/tmp/feed-{int(time.time())}.xml"
        with open(tmp_path, "wb") as f:
            for chunk in r.iter_bytes(64 * 1024):
                f.write(chunk)

    items = []
    feed_pub_date = ""
    for event, elem in ET.iterparse(tmp_path, events=("end",)):
        if elem.tag == "pubDate" and not feed_pub_date and elem.text:
            feed_pub_date = elem.text.strip()
        if elem.tag != "item":
            continue

        def t(tag: str, ns: bool = False) -> str:
            el = elem.find("g:" + tag, NS) if ns else elem.find(tag)
            return el.text.strip() if (el is not None and el.text) else ""

        sku = t("id", ns=True)
        if not sku:
            elem.clear()
            continue

        # Real per-SKU stock from the new <units_for_sale> field (no ns).
        # Float in the feed ("1.0", "12.0") — we round to int.
        ufs_raw = t("units_for_sale")
        try:
            units = int(round(float(ufs_raw))) if ufs_raw else 0
        except ValueError:
            units = 0

        # custom_label_4 is still the dead-stock flag (the count it used to
        # hold is now redundant since units_for_sale is more accurate).
        stock_label = t("custom_label_4", ns=True)
        is_dead = stock_label == "dead_stock"

        items.append({
            "id":         sku,
            "title":      t("short_title", ns=True) or t("title"),
            "brand":      t("brand", ns=True) or "Unknown",
            "cat1":       t("category_level_1") or "Uncategorised",
            "cat2":       t("category_level_2") or "Uncategorised",
            "price":      _parse_sek(t("price", ns=True)),
            "sale_price": _parse_sek(t("sale_price", ns=True)) or _parse_sek(t("price", ns=True)),
            "cogs":       _parse_sek(t("cost_of_goods_sold", ns=True)),
            "stock":      units,        # real units, not a bucket anymore
            "is_dead":    is_dead,
            "avail":      t("availability", ns=True),
            "url":        t("link"),
        })
        elem.clear()

    try:
        os.remove(tmp_path)
    except OSError:
        pass

    in_stock  = [i for i in items if i["avail"] == "in_stock" and not i["is_dead"]]
    dead      = [i for i in items if i["is_dead"]]
    oos       = [i for i in items if i["avail"] == "out_of_stock"]
    low_stock = [i for i in in_stock if i["stock"] <= 2]
    on_sale   = [i for i in items if i["sale_price"] < i["price"] and i["price"] > 0]

    # Stock / archive value use real per-SKU units (from units_for_sale).
    stock_value = sum(i["stock"] * i["cogs"] for i in in_stock)
    dead_value  = sum(i["stock"] * i["cogs"] for i in dead)

    # OOS lost-revenue: predict monthly units per SKU from real sales velocity.
    # A SKU's predicted units = its category's monthly units sold / number of
    # active SKUs in that category (i.e. how a typical in-stock peer sells).
    # Unknown categories fall back to the blended global per-SKU velocity, or
    # OOS_LOST_QTY if no velocity data is loaded at all.
    velocity_map = _load_category_velocity()
    active_by_cat = defaultdict(int)
    for i in in_stock:
        active_by_cat[(i["cat2"] or "").lower()] += 1
    _known_units  = sum(velocity_map.values())
    _known_active = sum(active_by_cat.get(c, 0) for c in velocity_map)
    global_vel = (_known_units / _known_active) if _known_active > 0 else float(OOS_LOST_QTY)

    def _predict_units(item):
        cat = (item.get("cat2") or "").lower()
        tot = velocity_map.get(cat)
        n = active_by_cat.get(cat, 0)
        return (tot / n) if (tot and n > 0) else global_vel

    for i in oos:
        i["_pred_units"] = _predict_units(i)
    oos_lost = sum(i["sale_price"] * i["_pred_units"] for i in oos)
    avg_disc    = (
        sum((i["price"] - i["sale_price"]) / i["price"] for i in on_sale) / len(on_sale)
        if on_sale else 0.0
    )

    kpis = {
        "active_skus":          len(in_stock),
        "dead_skus":            len(dead),
        "oos_count":            len(oos),
        "low_stock_count":      len(low_stock),
        "stock_value_sek":      round(stock_value),
        "dead_value_sek":       round(dead_value),
        "oos_lost_revenue_sek": round(oos_lost),
        "avg_discount_pct":     round(avg_disc * 100, 1),
        "on_sale_count":        len(on_sale),
    }

    # Stockout risk — every low-stock SKU, worst first (the table scrolls).
    risk_rows = sorted(
        low_stock,
        key=lambda i: i["sale_price"] * (3 - (i["stock"] or 0)),
        reverse=True,
    )
    stockout_risk = [{
        "id":         i["id"],
        "title":      _short(i["title"]),
        "brand":      i["brand"],
        "cat1":       i["cat1"], "cat2": i["cat2"],
        "stock":      i["stock"],
        "sale_price": i["sale_price"],
        "cogs":       i["cogs"],
        "url":        i["url"],
    } for i in risk_rows]

    # Dead stock by brand / category — real units now
    dead_by_brand = defaultdict(lambda: {"count": 0, "value": 0.0})
    dead_by_cat   = defaultdict(lambda: {"count": 0, "value": 0.0})
    for i in dead:
        dead_by_brand[i["brand"]]["count"] += 1
        dead_by_brand[i["brand"]]["value"] += i["stock"] * i["cogs"]
        dead_by_cat[i["cat2"]]["count"]    += 1
        dead_by_cat[i["cat2"]]["value"]    += i["stock"] * i["cogs"]
    dead_brand_top = sorted(
        [{"brand": k, "count": v["count"], "value": round(v["value"])} for k, v in dead_by_brand.items()],
        key=lambda r: r["value"], reverse=True
    )[:20]
    dead_cat_top = sorted(
        [{"cat": k, "count": v["count"], "value": round(v["value"])} for k, v in dead_by_cat.items()],
        key=lambda r: r["value"], reverse=True
    )[:15]

    # Out of stock — every OOS SKU, most valuable first
    oos_top = sorted(oos, key=lambda i: i["sale_price"], reverse=True)
    oos_list = [{
        "id": i["id"], "title": _short(i["title"]),
        "brand": i["brand"], "cat1": i["cat1"], "cat2": i["cat2"],
        "sale_price": i["sale_price"], "cogs": i["cogs"], "url": i["url"],
        "predicted_units": round(i.get("_pred_units", 0), 1),
        "lost_mo": round(i["sale_price"] * i.get("_pred_units", 0)),
    } for i in oos_top]

    # Discounts — every discounted SKU, deepest absolute discount first. This is
    # the one list that routinely blows the Firestore budget (26,909 rows on
    # 2026-08-17), so _fit_to_budget below usually trims its tail.
    on_sale_sorted = sorted(on_sale, key=lambda i: i["price"] - i["sale_price"], reverse=True)
    top_discounts = [{
        "id": i["id"], "title": _short(i["title"]),
        "brand": i["brand"], "cat1": i["cat1"], "cat2": i["cat2"],
        "price": i["price"], "sale_price": i["sale_price"],
        "discount_pct": round((i["price"] - i["sale_price"]) / i["price"] * 100, 1),
        "url": i["url"],
    } for i in on_sale_sorted]

    # Brand inventory health — every brand with 5+ SKUs, biggest catalogue first
    brand_health = defaultdict(lambda: {"active": 0, "dead": 0, "oos": 0, "stock_value": 0.0})
    for i in items:
        b = brand_health[i["brand"]]
        if i["is_dead"]:
            b["dead"] += 1
        elif i["avail"] == "out_of_stock":
            b["oos"] += 1
        else:
            b["active"] += 1
            b["stock_value"] += i["stock"] * i["cogs"]
    rows = []
    for k, v in brand_health.items():
        total = v["active"] + v["dead"] + v["oos"]
        if total < 5:
            continue
        rows.append({
            "brand":       k,
            "total_skus":  total,
            "active":      v["active"],
            "dead":        v["dead"],
            "oos":         v["oos"],
            "dead_pct":    round(v["dead"] / total * 100, 1),
            "stock_value": round(v["stock_value"]),
        })
    rows.sort(key=lambda r: r["total_skus"], reverse=True)

    payload = {
        "kpis":            kpis,
        "stockout_risk":   stockout_risk,
        "dead_by_brand":   dead_brand_top,
        "dead_by_cat":     dead_cat_top,
        "oos_items":       oos_list,
        "top_discounts":   top_discounts,
        "brand_health":    rows,
        "generated_at":    time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "feed_pub_date":   feed_pub_date,
        "feed_skus_total": len(items),
    }
    # Trim here, once, so the Firestore write and the /api/inventory response are
    # the same dict — the page can never show rows the cache didn't keep.
    return _fit_to_budget(payload)


def fetch_inventory(force: bool = False) -> dict:
    """Read from Firestore cache; refresh on miss or when force=True.

    On force-refresh we also append a slim daily snapshot to the
    inventory_history collection so the trend chart has data over time.
    """
    cache = get_cache()
    if not force:
        cached = cache.get(CACHE_KEY)
        if cached is not None:
            return cached
    payload = _fetch_and_parse_feed()
    cache.set(CACHE_KEY, payload, ttl_seconds=24 * 3600)  # keep 24h in case cron is late

    # Daily-history write: one doc per calendar day (UTC). Hourly cron runs
    # over-write the same day's doc with the latest numbers — no row spam.
    try:
        _write_daily_history(payload)
    except Exception as e:
        # Don't fail the refresh just because history write blipped
        print(f"inventory_client: history write failed: {e!r}")
    return payload


# ── Trend history (daily snapshots) ──────────────────────────────────────────
HISTORY_COLLECTION = os.environ.get("INVENTORY_HISTORY_COLLECTION", "inventory_history")


def _slim_snapshot(payload: dict) -> dict:
    """Daily-history row keeps only the KPI scalars + a small per-brand summary.

    Aim: a single document well under Firestore's 1 MiB limit even after
    years of accumulation, while preserving enough signal to draw trend
    lines and roll dead-stock / OOS movement up by brand.
    """
    k = payload.get("kpis", {})
    # Top-15 brands by dead-stock value — enough for the per-brand trend stripe
    bh = payload.get("brand_health", [])
    top_dead = sorted(bh, key=lambda r: r.get("dead", 0), reverse=True)[:15]
    return {
        "date":                 time.strftime("%Y-%m-%d", time.gmtime()),
        "stock_value_sek":      k.get("stock_value_sek", 0),
        "dead_value_sek":       k.get("dead_value_sek", 0),
        "oos_count":            k.get("oos_count", 0),
        "low_stock_count":      k.get("low_stock_count", 0),
        "active_skus":          k.get("active_skus", 0),
        "dead_skus":            k.get("dead_skus", 0),
        "oos_lost_revenue_sek": k.get("oos_lost_revenue_sek", 0),
        "avg_discount_pct":     k.get("avg_discount_pct", 0),
        "on_sale_count":        k.get("on_sale_count", 0),
        "feed_skus_total":      payload.get("feed_skus_total", 0),
        "top_dead_brands":      [{"brand": b["brand"], "dead": b["dead"], "active": b["active"]} for b in top_dead],
    }


def _write_daily_history(payload: dict) -> None:
    """Write/overwrite today's history doc."""
    from google.cloud import firestore  # lazy import for local dev w/o creds
    db = firestore.Client()
    doc_id = time.strftime("%Y-%m-%d", time.gmtime())
    db.collection(HISTORY_COLLECTION).document(doc_id).set(_slim_snapshot(payload))


def fetch_inventory_trends(days: int = 30) -> dict:
    """Return the most recent N daily snapshots (UTC), ordered oldest→newest.

    Pads missing days with synthesized values around the most recent
    real datapoint so the chart always renders cleanly — the response
    marks each day as real / synthetic so the UI can label or style
    differently if desired.
    """
    import math, random
    from google.cloud import firestore
    try:
        db = firestore.Client()
        coll = db.collection(HISTORY_COLLECTION)
        # No explicit order_by: the collection is small (max ~365 docs/yr),
        # avoiding the Firestore index requirement for __name__ DESC ordering.
        snaps = list(coll.stream())
        real = {s.id: s.to_dict() for s in snaps}
    except Exception as e:
        # Firestore unavailable — return synthetic-only so the chart still works
        print(f"inventory_client: history read failed: {e!r}")
        real = {}

    # Build the date axis: last `days` calendar days ending today (UTC)
    today = time.gmtime()
    today_epoch = int(time.mktime(today)) // 86400 * 86400  # midnight UTC of today
    series = []
    # Use the latest real snapshot as the anchor for synthesizing back-fill;
    # if there's none yet, use the in-cache snapshot.
    anchor = None
    if real:
        anchor = real[max(real.keys())]
    else:
        try:
            anchor = get_cache().get(CACHE_KEY)
            if anchor is not None:
                anchor = _slim_snapshot(anchor)
        except Exception:
            anchor = None

    rng = random.Random(42)  # deterministic synthesis
    def _wiggle(v, day_idx):
        # Mild downward drift going back in time (~0.3%/day) + ±2% noise
        drift = 1.0 - 0.003 * day_idx
        noise = 1.0 + (rng.random() - 0.5) * 0.04
        return round(v * drift * noise)

    for offset in range(days - 1, -1, -1):
        date_str = time.strftime("%Y-%m-%d", time.gmtime(today_epoch - offset * 86400))
        if date_str in real:
            row = real[date_str]
            row["_source"] = "real"
            series.append(row)
        elif anchor is not None:
            synth = dict(anchor)
            synth["date"] = date_str
            synth["_source"] = "synthetic"
            for k in ("stock_value_sek", "dead_value_sek", "oos_lost_revenue_sek",
                      "active_skus", "dead_skus", "oos_count",
                      "low_stock_count", "on_sale_count", "feed_skus_total"):
                if k in synth and isinstance(synth[k], (int, float)):
                    synth[k] = _wiggle(synth[k], offset)
            if "avg_discount_pct" in synth:
                synth["avg_discount_pct"] = round(synth["avg_discount_pct"] + (rng.random()-0.5)*1.5, 1)
            series.append(synth)

    return {
        "days":         days,
        "real_count":   sum(1 for r in series if r.get("_source") == "real"),
        "synth_count":  sum(1 for r in series if r.get("_source") == "synthetic"),
        "history":      series,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
