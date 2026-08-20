#!/usr/bin/env python3
"""
Product season (Norce "Product Collection") snapshot — BigQuery → Firestore.

Writes `funnel_cache/<workspace>__product-seasons`, which funnel_client merges
into the /api/product-data payload so the products tab can render a Season
column. Same client/auth wiring and the same {data, fetched_at, expires_at,
ttl_seconds, workspace} wrapper as refresh_segments.py and refresh_voyado.py.

WHAT "SEASON" IS
  Norce calls it PRODUCT COLLECTION and stores it as a product FLAG in flag
  group 4 ('Product Collection', code productCollection) — not a field, not a
  parametric, not an entity. 113 flags: date-coded ones (SS21, AW22, HS24,
  Pre-SS25 …) plus lifecycle labels (CORE, NOS, Exited, DISCONTINUED, Paused,
  Sparepart, Vendor Exited/Replaced …). norce_sync.py lands the product links in
  `norce.product_flags`; norce_marts.sql resolves one season per product in
  `product_collections` and per SKU in `sku_collections`.

WHY THE SEASON IS ATTACHED AT BRAND / CATEGORY GRAIN
  The products tab has two tables — Category Performance and Brand Performance —
  and BOTH are pre-aggregated: one row is every product of that brand, or of that
  category. There is no product-grain row on the tab to hang a season on, and
  the Funnel→BigQuery export those tables are built from has no SKU column at
  all (its finest product identifier is an English feed title that matches
  Norce's variant+size name on only ~68% of product revenue — measured
  2026-08-18, too lossy to build on).

  So a row spans many collections and the column shows the DOMINANT one: the
  collection with the largest share of that brand's / that category's Norce
  merchandise revenue in the same 30-day window the tab's headline period uses.
  `share` and the top-3 `mix` ride along so the UI can qualify it rather than
  present a single label as the whole truth.

THE JOIN, AND ITS MEASURED MATCH RATE (2026-08-18, trailing 30 days)
  Funnel row → Norce is by NAME, because that is all both sides carry:
    • brand    LOWER(TRIM(kv_brand))      → dim_manufacturers.Name
              172/173 brands, 99.99% of Funnel product revenue
    • category LOWER(TRIM(Product_type_2)) → the L2 segment of
              dim_categories.DefaultFullName ("Ecom - <L1> - <L2> - <L3>")
              81/81 categories, 100.00% of Funnel product revenue
  A name that does not match simply gets no season — never a wrong one.

NUMBERS, AND WHAT THEY ARE NOT
  Everything here is NORCE merchandise revenue (SEK at today's rate, ex-VAT,
  gross, no status filtering — the norce_marts.sql header rules). It is used
  ONLY to rank collections within a brand/category. No amount from this file is
  ever displayed next to a Funnel money column; the payload carries shares, not
  kronor.

Run locally:
    python3 refresh_product_seasons.py
    SKIP_FIRESTORE=1 SEASONS_OUT=/tmp/seasons.json python3 refresh_product_seasons.py
"""
from __future__ import annotations
import datetime, json, os, time

from google.cloud import bigquery

BQ_PROJECT  = os.environ.get("NORCE_BQ_PROJECT", "project-a7ade44e-e7e3-4871-a83")
BQ_DATASET  = os.environ.get("NORCE_BQ_DATASET", "norce")
BQ_LOCATION = os.environ.get("BQ_LOCATION", "EU")

WORKSPACE  = os.environ.get("FUNNEL_WORKSPACE", "-Ln87GcdqU9CMJV6zMBY")
COLLECTION = "funnel_cache"
DOC_KEY    = "product-seasons"
TTL        = 30 * 24 * 3600
FIRESTORE_PROJECT = os.environ.get("FIRESTORE_PROJECT", "project-a7ade44e-e7e3-4871-a83")

# Same 30-day window bq_source.build_payloads() uses for the products tab, so
# the Season column describes exactly the slice the tab opens on. It does NOT
# follow the tab's period selector — a dominant season that flipped on every
# 7D/30D/90D toggle would be noise, and the payload is one snapshot. The UI
# tooltip says which window it is.
WINDOW_DAYS = int(os.environ.get("SEASON_WINDOW_DAYS", "30"))

# Top-N collections kept per row for the tooltip. Three is what fits on one line.
MIX_TOP_N = 3

# Firestore's hard limit is 1 MiB. ~190 brands + ~85 categories lands around
# 40 KB, so this is a tripwire for an unexpected explosion, not a real ceiling.
DOC_BUDGET_BYTES = 700_000

SOURCE = "norce-product-collection"


def F(v, nd=4):
    return None if v is None else round(float(v), nd)


def _credentials():
    """ADC in production (Cloud Run SA); gcloud user token as a local fallback.
    Same shape as refresh_segments._credentials — every module wires its own."""
    try:
        import google.auth
        creds, _ = google.auth.default()
        return creds
    except Exception:
        import subprocess, google.oauth2.credentials

        class _GcloudToken(google.oauth2.credentials.Credentials):
            def refresh(self, request):  # noqa: ARG002 - signature fixed by google-auth
                self.token = subprocess.check_output(
                    ["gcloud", "auth", "print-access-token"]).decode().strip()
                # google-auth compares expiry against a NAIVE utcnow().
                self.expiry = datetime.datetime.utcnow() + datetime.timedelta(minutes=45)

        tok = subprocess.check_output(["gcloud", "auth", "print-access-token"]).decode().strip()
        c = _GcloudToken(tok)
        # Must be set here too: expiry=None reads as "never expires", so
        # google-auth would never call refresh() at all.
        c.expiry = datetime.datetime.utcnow() + datetime.timedelta(minutes=45)
        return c


_client = None
def bq():
    global _client
    if _client is None:
        _client = bigquery.Client(project=BQ_PROJECT, credentials=_credentials(),
                                  location=BQ_LOCATION)
    return _client


def D(name: str) -> str:
    return f"`{BQ_PROJECT}.{BQ_DATASET}.{name}`"


def _rows(sql, params=None):
    job = bq().query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params or []))
    return list(job.result())


def _window() -> tuple[datetime.date, datetime.date]:
    """30 days ending yesterday — bq_source.build_payloads()'s current period."""
    end = datetime.date.today() - datetime.timedelta(days=1)
    return end - datetime.timedelta(days=WINDOW_DAYS - 1), end


# The mart's fallback labels for a line whose SKU never resolved to a Norce
# product. They are not real brands/categories and must never be handed to the
# dashboard as one — but they ARE the coverage signal, so they are excluded from
# the output rather than filtered out of the query.
UNKNOWN_BRAND    = "(unknown brand)"
UNKNOWN_CATEGORY = "(uncategorised)"
UNKNOWN_SEASON   = "(unknown)"


def _dominant(mix: dict[str, float]) -> dict | None:
    """One brand's / one category's season mix -> the record the tab renders.

    The dominant season is chosen among KNOWN collections only. A SKU that never
    resolved to a product (a discontinued PartNo, a marketplace line) lands in
    the mart's '(unknown)' bucket; letting it win the vote would print
    "(unknown)" on the busiest rows. Instead it is excluded from the ranking and
    reported as `coverage` — the share of the row's Norce revenue that HAS a
    season, which is the honest confidence number for the label.

    Ties break on the season name so two runs over identical data agree.
    """
    total = sum(mix.values())
    known = {s: v for s, v in mix.items() if s != UNKNOWN_SEASON and v > 0}
    known_total = sum(known.values())
    if not known or known_total <= 0 or total <= 0:
        return None
    ranked = sorted(known.items(), key=lambda kv: (-kv[1], kv[0]))
    return {
        "season":   ranked[0][0],
        # Share OF THE SEASONED REVENUE, so 'AW25 62%' means 62% of what we
        # could attribute — not 62% diluted by the unattributable tail.
        "share":    F(ranked[0][1] / known_total, 3),
        "coverage": F(known_total / total, 3),
        "n":        len(known),
        # {"s": season, "v": share}, NOT [season, share]: Firestore rejects an
        # array whose elements are themselves arrays with "Property data
        # contains an invalid nested entity" (hit for real, 2026-08-18). The
        # dashboard reads .s / .v.
        "mix":      [{"s": s, "v": F(v / known_total, 3)} for s, v in ranked[:MIX_TOP_N]],
    }


def build_payload() -> dict:
    cs, ce = _window()

    # ONE scan of season_sales at its finest useful grain; every number below is
    # folded out of it in Python. The alternative — a query per dimension plus
    # one for the vocabulary plus one for the counts — re-joins 1.4M order lines
    # four times for the same answer. A few tens of thousands of cells is well
    # within what a job should pull.
    cells = _rows(
        f"SELECT brand, category, season, SUM(revenue) AS rev "
        f"FROM {D('season_sales')} WHERE order_date BETWEEN @cs AND @ce "
        f"GROUP BY brand, category, season HAVING SUM(revenue) > 0",
        [bigquery.ScalarQueryParameter("cs", "DATE", cs),
         bigquery.ScalarQueryParameter("ce", "DATE", ce)])

    by_brand: dict[str, dict[str, float]] = {}
    by_cat:   dict[str, dict[str, float]] = {}
    by_season: dict[str, float] = {}
    for r in cells:
        rev = float(r["rev"] or 0)
        season = r["season"] or UNKNOWN_SEASON
        by_season[season] = by_season.get(season, 0.0) + rev
        b, c = r["brand"], r["category"]
        # Each order line lands in exactly one (brand, category, season) cell, so
        # folding the same cell into both dimensions double-counts nothing.
        if b and b != UNKNOWN_BRAND:
            d = by_brand.setdefault(b, {}); d[season] = d.get(season, 0.0) + rev
        if c and c != UNKNOWN_CATEGORY:
            d = by_cat.setdefault(c, {}); d[season] = d.get(season, 0.0) + rev

    brands     = {k: v for k, v in ((k, _dominant(m)) for k, m in by_brand.items()) if v}
    categories = {k: v for k, v in ((k, _dominant(m)) for k, m in by_cat.items()) if v}

    # Workspace-wide season vocabulary, ranked by revenue. Drives nothing on its
    # own — it is what lets a UI order a Season list sensibly and what makes a
    # "why is this season missing" question answerable.
    grand = sum(by_season.values()) or 1.0
    seasons = [{"season": s, "share": F(v / grand, 4)}
               for s, v in sorted(by_season.items(), key=lambda kv: (-kv[1], kv[0]))]

    # Catalogue-side coverage: how much of the product master carries a
    # collection at all. Independent of what sold, so it is the number to look
    # at when the sync looks wrong.
    cov = _rows(
        f"SELECT (SELECT COUNT(*) FROM {D('products')}) AS products, "
        f"       (SELECT COUNT(*) FROM {D('product_collections')}) AS with_collection, "
        f"       (SELECT COUNT(*) FROM {D('product_collections')} WHERE collection_count > 1) "
        f"           AS multi_collection")[0]

    # Denominators for the coverage line: brands/categories that actually SOLD in
    # the window, so "172 of 173" is a real ratio and not brands-with-a-season
    # over itself.
    sold = {"brands": len(by_brand), "categories": len(by_cat)}

    return {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "source": SOURCE,
        "window": {"start": cs.isoformat(), "end": ce.isoformat(), "days": WINDOW_DAYS},
        "seasons": seasons,
        "brands": brands,
        "categories": categories,
        "coverage": {
            "products_total":        int(cov["products"] or 0),
            "products_with_season":  int(cov["with_collection"] or 0),
            "products_multi_season": int(cov["multi_collection"] or 0),
            "brands_total":          sold["brands"],
            "brands_with_season":    len(brands),
            "categories_total":      sold["categories"],
            "categories_with_season": len(categories),
        },
        "notes": [
            "season = Norce 'Product Collection' (product flag group 4): SS/AW/HS "
            "date codes plus lifecycle labels (CORE, NOS, Exited, DISCONTINUED …)",
            "a brand or category spans many collections — the value shown is the "
            f"DOMINANT one over {WINDOW_DAYS} days ({cs.isoformat()} → {ce.isoformat()}), "
            "by Norce merchandise revenue; 'share' is its share of that row's "
            "seasonable revenue and 'coverage' how much of the row could be attributed",
            "the window is fixed and does NOT follow the tab's period selector",
            "products with more than one collection resolve to the newest date-coded "
            "one, falling back to the alphabetically first lifecycle label",
            "attached to Funnel rows by NAME: brand → Norce manufacturer (99.99% of "
            "Funnel product revenue), category → Norce category level 2 (100.00%)",
        ],
    }


def _check_size(payload: dict) -> int:
    n = len(json.dumps(payload, ensure_ascii=False))
    if n > DOC_BUDGET_BYTES:
        big = sorted(((len(json.dumps(v, ensure_ascii=False)), k)
                      for k, v in payload.items()), reverse=True)[:3]
        raise RuntimeError(
            f"Product-seasons payload is {n:,} B, over the {DOC_BUDGET_BYTES:,} B "
            "budget. Biggest sections: "
            + ", ".join(f"{k} {s:,} B" for s, k in big)
            + ". Trim the mix or the vocabulary — do not just raise the budget.")
    return n


def write_firestore(payload: dict) -> str:
    from google.cloud import firestore
    _check_size(payload)
    db = firestore.Client(project=FIRESTORE_PROJECT, credentials=_credentials())
    doc_id = f"{WORKSPACE}__{DOC_KEY}"
    db.collection(COLLECTION).document(doc_id).set({
        "data": payload, "fetched_at": firestore.SERVER_TIMESTAMP,
        "expires_at": time.time() + TTL, "ttl_seconds": TTL, "workspace": WORKSPACE})
    return f"{COLLECTION}/{doc_id}"


def main():
    t0 = time.time()
    p = build_payload()
    out = os.environ.get("SEASONS_OUT")
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(p, fh, ensure_ascii=False, indent=1)
        print(f"   wrote {out} ({os.path.getsize(out):,} bytes)")
    where = "(skipped)" if os.environ.get("SKIP_FIRESTORE") else write_firestore(p)
    c, w = p["coverage"], p["window"]
    print(f"✓ Product seasons · {where} · {w['start']}→{w['end']} · "
          f"{len(p['seasons'])} collections sold · "
          f"brands {c['brands_with_season']}/{c['brands_total']} · "
          f"categories {c['categories_with_season']}/{c['categories_total']} · "
          f"catalogue {c['products_with_season']:,}/{c['products_total']:,} "
          f"({c['products_multi_season']:,} multi) · "
          f"{len(json.dumps(p, ensure_ascii=False)):,} B · {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
