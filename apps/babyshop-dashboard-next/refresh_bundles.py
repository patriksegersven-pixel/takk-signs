#!/usr/bin/env python3
"""
Bundles snapshot — market-basket mining over Norce orders → Firestore.

Writes `funnel_cache/<workspace>__bundles`, the single document the Bundles
tab reads. Same client/auth wiring and the same
{data, fetched_at, expires_at, ttl_seconds, workspace} wrapper as
refresh_segments.py / refresh_customer_insights.py.

WHAT IT COMPUTES (last 365 days, per shop babyshop|lekmer)
  • basket composition: orders and AOV by number of DISTINCT products
  • product pairs bought in the same order, at PRODUCT-COLOR grain
    (COALESCE(products.VariantId, ProductId) — the variant parent groups the
    size ladder of one product+colorway, which is the grain a bundle is
    merchandised at)
  • per pair: co-purchase count, attach % both directions, lift, prices,
    product image (Channable feed via norce.sku_titles.image_link)
  • the attach gap: for every anchor with a statistically strong companion,
    how many anchor orders are missing it, valued at the companion's median
    unit price — the tab's headline opportunity number

RULES INHERITED FROM THE MARTS (norce_marts.sql — do not "fix" here)
  • SEK at today's rate (currency_rates view), ex-VAT, gross, no status filter
  • shipping line PartNo 1000014 excluded
  • order lines whose PartNo is missing from product_skus (deleted products)
    are excluded from pair mining — they cannot be named or merchandised

LIFT = P(A and B in one order) / (P(A)·P(B)) — how many times more often the
pair is bought together than independent purchasing would produce. <15 is
dominated by sibling/size-duplicate baskets (two sizes of one boot), which is
why the tab hides low-lift pairs by default rather than this job dropping
them: the threshold is a UI decision, MIN_TOGETHER here is the statistical
floor.

Run locally:  python3 refresh_bundles.py
              SKIP_FIRESTORE=1 BUNDLES_OUT=/tmp/bundles.json python3 refresh_bundles.py
"""
from __future__ import annotations
import datetime, json, os, re, time
from google.cloud import bigquery

BQ_PROJECT  = os.environ.get("NORCE_BQ_PROJECT", "project-a7ade44e-e7e3-4871-a83")
BQ_DATASET  = os.environ.get("NORCE_BQ_DATASET", "norce")
BQ_LOCATION = os.environ.get("BQ_LOCATION", "EU")

WORKSPACE  = os.environ.get("FUNNEL_WORKSPACE", "-Ln87GcdqU9CMJV6zMBY")
COLLECTION = "funnel_cache"
DOC_KEY    = "bundles"
TTL        = 30 * 24 * 3600
FIRESTORE_PROJECT = os.environ.get("FIRESTORE_PROJECT", "project-a7ade44e-e7e3-4871-a83")

WINDOW_DAYS  = int(os.environ.get("BUNDLES_WINDOW_DAYS", "365"))
MIN_TOGETHER = int(os.environ.get("BUNDLES_MIN_TOGETHER", "30"))
MIN_LIFT_STRONG = 5          # "proven companion" floor for the gap KPI
PAIR_LIMIT   = int(os.environ.get("BUNDLES_PAIR_LIMIT", "400"))   # per shop
GROSS_MARGIN = 0.44          # Segments tab, price-list costs (see that tab's caveats)

DOC_BUDGET_BYTES = 900_000

CAVEATS = [
    "co-purchase is correlation, not causation — the attach gap is the "
    "addressable pool, not a forecast; a live A/B on the site is the causal read",
    "pairs are strongly seasonal: a 12-month window mixes AW and SS sets, and "
    "winter gear dominates by volume",
    "products are at color grain (variant parent) — sizes of one product+print "
    "count as the same product",
    "lift under ~15 is mostly sibling/size-duplicate buying (two sizes of the "
    "same boot in one order), not a bundle — hidden by default in the tab",
    "images come from the current Channable feed, so discontinued products "
    "have none — which is fine, they cannot be bundled anyway",
    "all money is SEK at today's rate, ex-VAT, gross (marts rules); the gap "
    "value prices the missing companion at its median sold unit price",
    "today's attach already includes organic co-buying — uplift scenarios "
    "apply capture rates to the REMAINING gap only",
]

DEFINITIONS = {
    "attach":    "orders containing both products ÷ orders containing the anchor "
                 "(the higher-volume product of the pair)",
    "lift":      "observed co-purchase rate ÷ the rate expected if the two "
                 "products were bought independently (P(A∩B) / P(A)·P(B))",
    "gap":       "anchor orders that did NOT include the pair's companion",
    "open_value":"gap orders × the companion's median unit price (SEK)",
}


def _credentials():
    """ADC in production (Cloud Run SA); gcloud user token as a local fallback."""
    try:
        import google.auth
        creds, _ = google.auth.default()
        return creds
    except Exception:
        import subprocess, google.oauth2.credentials

        class _GcloudToken(google.oauth2.credentials.Credentials):
            def refresh(self, request):  # noqa: ARG002
                self.token = subprocess.check_output(
                    ["gcloud", "auth", "print-access-token"]).decode().strip()
                self.expiry = datetime.datetime.utcnow() + datetime.timedelta(minutes=45)

        tok = subprocess.check_output(["gcloud", "auth", "print-access-token"]).decode().strip()
        c = _GcloudToken(tok)
        c.expiry = datetime.datetime.utcnow() + datetime.timedelta(minutes=45)
        return c


_client = None
def bq() -> bigquery.Client:
    global _client
    if _client is None:
        _client = bigquery.Client(project=BQ_PROJECT, location=BQ_LOCATION,
                                  credentials=_credentials())
    return _client


def q(sql: str) -> list[dict]:
    return [dict(r) for r in bq().query(sql).result()]


def I(v):
    return int(round(float(v or 0)))


def F(v, nd=2):
    return None if v is None else round(float(v), nd)


# ── Shared SQL fragments ─────────────────────────────────────────────────────
# One WITH-prefix reused by every query so the line/product rules can never
# drift apart between sections.
D = f"{BQ_PROJECT}.{BQ_DATASET}"
LINES_CTE = f"""
  lines AS (
    SELECT oi.OrderId,
           IF(STARTS_WITH(o.app_key,'lekmer'),'lekmer','babyshop') AS shop,
           COALESCE(p.VariantId, ps.ProductId) AS prod_key,
           oi.LineAmount * IFNULL(fx.to_sek,1)                          AS line_sek,
           oi.LineAmount * IFNULL(fx.to_sek,1) / NULLIF(oi.QtyOrdered,0) AS unit_sek
    FROM `{D}.order_items` oi
    JOIN `{D}.orders` o ON o.Id = oi.OrderId
    JOIN `{D}.product_skus` ps ON ps.PartNo = oi.PartNo
    JOIN `{D}.products` p ON p.Id = ps.ProductId
    LEFT JOIN `{D}.currency_rates` fx ON fx.currency_id = o.CurrencyId
    WHERE oi.PartNo != '1000014'
      AND o.OrderDate >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {WINDOW_DAYS} DAY)
  ),
  order_prods AS (SELECT OrderId, shop, prod_key FROM lines GROUP BY 1,2,3),
  prod_counts AS (SELECT shop, prod_key, COUNT(*) AS orders_with FROM order_prods GROUP BY 1,2),
  tot AS (SELECT shop, COUNT(DISTINCT OrderId) AS n_orders FROM order_prods GROUP BY 1)
"""


# ── Sections ─────────────────────────────────────────────────────────────────
def basket_composition() -> list[dict]:
    rows = q(f"""
      WITH {LINES_CTE},
      baskets AS (
        SELECT OrderId, ANY_VALUE(shop) AS shop,
               COUNT(DISTINCT prod_key) AS n_products, SUM(line_sek) AS order_sek
        FROM lines GROUP BY OrderId
      )
      SELECT shop,
             CASE WHEN n_products=1 THEN '1' WHEN n_products=2 THEN '2'
                  WHEN n_products=3 THEN '3' WHEN n_products BETWEEN 4 AND 5 THEN '4-5'
                  ELSE '6+' END AS bucket,
             MIN(n_products) AS ord_key,
             COUNT(*) AS orders, AVG(order_sek) AS aov, SUM(order_sek) AS revenue
      FROM baskets GROUP BY 1,2 ORDER BY shop, ord_key
    """)
    return [{"shop": r["shop"], "bucket": r["bucket"], "orders": I(r["orders"]),
             "aov": I(r["aov"]), "revenue_msek": F(r["revenue"] / 1e6)} for r in rows]


def category_pairs() -> list[dict]:
    rows = q(f"""
      WITH {LINES_CTE},
      pcat AS (
        SELECT COALESCE(p.VariantId, ps.ProductId) AS prod_key,
               ANY_VALUE(SPLIT(dc.DefaultFullName, ' - ')[SAFE_OFFSET(1)]) AS seg,
               ANY_VALUE(ARRAY_REVERSE(SPLIT(dc.DefaultFullName, ' - '))[SAFE_OFFSET(0)]) AS leaf
        FROM `{D}.product_skus` ps
        JOIN `{D}.products` p ON p.Id = ps.ProductId
        JOIN `{D}.product_categories` pc ON pc.ProductId = p.Id
        JOIN `{D}.dim_categories` dc ON dc.Id = pc.CategoryId
        GROUP BY 1
      ),
      ocat AS (
        SELECT op.OrderId, op.shop, c.leaf
        FROM order_prods op JOIN pcat c ON c.prod_key = op.prod_key
        WHERE c.leaf IS NOT NULL
        GROUP BY 1,2,3
      )
      SELECT a.shop, a.leaf AS cat_a, b.leaf AS cat_b, COUNT(*) AS orders_together
      FROM ocat a JOIN ocat b ON a.OrderId = b.OrderId AND a.shop = b.shop AND a.leaf < b.leaf
      GROUP BY 1,2,3
      QUALIFY ROW_NUMBER() OVER (PARTITION BY a.shop ORDER BY COUNT(*) DESC) <= 12
      ORDER BY shop, orders_together DESC
    """)
    return [{"shop": r["shop"], "a": r["cat_a"], "b": r["cat_b"],
             "orders": I(r["orders_together"])} for r in rows]


_SIZE_RE  = re.compile(r'(\d+(?:/\d+)?\s*(?:cm|EU)\b|\d+\s*-\s*\d+\s*(?:Y|M|Months?|Years?)\b|\d+"|One Size)', re.I)
_NOISE_RE = re.compile(r'\b(unisex|flicka|flickor|pojkar|pojke|lila|svart|grön|rosa|vit|beige|brun|blå|wool|shell|rain)\b', re.I)

def _clean_name(name: str | None, brand: str | None) -> str:
    """Feed/variant titles carry size + gender suffixes ("Levi Vintermössa
    Always Black 52 cm Svart unisex"); at color grain those are noise."""
    n = name or ""
    n = _SIZE_RE.sub(" ", n)
    n = _NOISE_RE.sub(" ", n)
    if brand:
        n = re.sub(r'\b' + re.escape(brand) + r'\b', " ", n, flags=re.I)
    return re.sub(r"\s{2,}", " ", n).strip(" -,·") or (name or "")


def mine_pairs() -> list[dict]:
    rows = q(f"""
      WITH {LINES_CTE},
      pinfo AS (
        SELECT shop, prod_key, APPROX_QUANTILES(unit_sek, 2)[OFFSET(1)] AS med_price
        FROM lines GROUP BY 1,2
      ),
      names AS (
        SELECT COALESCE(p.VariantId, ps.ProductId) AS prod_key,
               ANY_VALUE(COALESCE(st.title, v.DefaultName, p.DefaultName)) AS name,
               ANY_VALUE(m.Name) AS brand,
               ANY_VALUE(SPLIT(dc.DefaultFullName, ' - ')[SAFE_OFFSET(1)]) AS category,
               ARRAY_AGG(st.image_link IGNORE NULLS LIMIT 1) AS imgs
        FROM `{D}.product_skus` ps
        JOIN `{D}.products` p ON p.Id = ps.ProductId
        LEFT JOIN `{D}.variants` v ON v.Id = p.VariantId
        LEFT JOIN `{D}.sku_titles` st ON st.PartNo = ps.PartNo
        LEFT JOIN `{D}.dim_manufacturers` m ON m.ManufacturerId = p.ManufacturerId
        LEFT JOIN `{D}.product_categories` pc ON pc.ProductId = p.Id
        LEFT JOIN `{D}.dim_categories` dc ON dc.Id = pc.CategoryId
        GROUP BY 1
      ),
      pairs AS (
        SELECT a.shop, a.prod_key AS pa, b.prod_key AS pb, COUNT(*) AS together
        FROM order_prods a
        JOIN order_prods b ON a.OrderId = b.OrderId AND a.prod_key < b.prod_key
        GROUP BY 1,2,3
        HAVING together >= {MIN_TOGETHER}
      )
      SELECT pr.shop, pr.pa, pr.pb, pr.together,
             ca.orders_with AS oa, cb.orders_with AS ob,
             SAFE_DIVIDE(pr.together * t.n_orders,
                         ca.orders_with * cb.orders_with) AS lift,
             na.name AS name_a, na.brand AS brand_a, na.category AS cat_a,
             IF(ARRAY_LENGTH(na.imgs) > 0, na.imgs[OFFSET(0)], NULL) AS img_a,
             nb.name AS name_b, nb.brand AS brand_b, nb.category AS cat_b,
             IF(ARRAY_LENGTH(nb.imgs) > 0, nb.imgs[OFFSET(0)], NULL) AS img_b,
             pia.med_price AS price_a, pib.med_price AS price_b
      FROM pairs pr
      JOIN prod_counts ca ON ca.shop = pr.shop AND ca.prod_key = pr.pa
      JOIN prod_counts cb ON cb.shop = pr.shop AND cb.prod_key = pr.pb
      JOIN tot t ON t.shop = pr.shop
      JOIN names na ON na.prod_key = pr.pa
      JOIN names nb ON nb.prod_key = pr.pb
      JOIN pinfo pia ON pia.shop = pr.shop AND pia.prod_key = pr.pa
      JOIN pinfo pib ON pib.shop = pr.shop AND pib.prod_key = pr.pb
      QUALIFY ROW_NUMBER() OVER (PARTITION BY pr.shop ORDER BY pr.together DESC) <= {PAIR_LIMIT}
      ORDER BY pr.shop, pr.together DESC
    """)
    out = []
    for r in rows:
        # anchor = the higher-volume product; the tab reads attach anchor→companion
        a_first = I(r["oa"]) >= I(r["ob"])
        anc, cmp_ = ("a", "b") if a_first else ("b", "a")
        anchor_orders = I(r["oa"]) if a_first else I(r["ob"])
        comp_price = F(r[f"price_{cmp_}"], 0) or 0
        gap = anchor_orders - I(r["together"])
        out.append({
            "shop": r["shop"],
            "anchor": {"name": _clean_name(r[f"name_{anc}"], r[f"brand_{anc}"]),
                       "brand": r[f"brand_{anc}"], "cat": r[f"cat_{anc}"],
                       "img": r[f"img_{anc}"], "orders": anchor_orders,
                       "price": F(r[f"price_{anc}"], 0)},
            "comp":   {"name": _clean_name(r[f"name_{cmp_}"], r[f"brand_{cmp_}"]),
                       "brand": r[f"brand_{cmp_}"], "cat": r[f"cat_{cmp_}"],
                       "img": r[f"img_{cmp_}"], "orders": I(r["ob"]) if a_first else I(r["oa"]),
                       "price": comp_price},
            "together": I(r["together"]),
            "attach": F(100.0 * I(r["together"]) / anchor_orders, 1),
            "lift": F(r["lift"], 1),
            "gap": gap,
            "gap_ksek": I(gap * comp_price / 1000),
        })
    return out


def attach_gap_kpis() -> list[dict]:
    """Per shop: the headline gap numbers over ALL strong pairs (not just the
    PAIR_LIMIT the doc carries) — the KPI must not shrink when the list is
    capped."""
    return q(f"""
      WITH {LINES_CTE},
      pinfo AS (
        SELECT shop, prod_key, APPROX_QUANTILES(unit_sek, 2)[OFFSET(1)] AS med_price
        FROM lines GROUP BY 1,2
      ),
      pairs AS (
        SELECT a.shop, a.prod_key AS pa, b.prod_key AS pb, COUNT(*) AS together
        FROM order_prods a
        JOIN order_prods b ON a.OrderId = b.OrderId AND a.prod_key < b.prod_key
        GROUP BY 1,2,3
        HAVING together >= {MIN_TOGETHER}
      ),
      strong AS (
        SELECT pr.*, ca.orders_with AS oa, cb.orders_with AS ob
        FROM pairs pr
        JOIN prod_counts ca ON ca.shop = pr.shop AND ca.prod_key = pr.pa
        JOIN prod_counts cb ON cb.shop = pr.shop AND cb.prod_key = pr.pb
        JOIN tot t ON t.shop = pr.shop
        WHERE SAFE_DIVIDE(pr.together * t.n_orders,
                          ca.orders_with * cb.orders_with) >= {MIN_LIFT_STRONG}
      ),
      sym AS (
        SELECT shop, pa AS anchor, pb AS comp, together, oa AS anchor_orders FROM strong
        UNION ALL
        SELECT shop, pb, pa, together, ob FROM strong
      ),
      best AS (
        SELECT shop, anchor, ANY_VALUE(anchor_orders) AS aord,
               ARRAY_AGG(STRUCT(comp, together) ORDER BY together DESC LIMIT 1)[OFFSET(0)] AS b
        FROM sym GROUP BY 1,2
      )
      SELECT best.shop,
             COUNT(*)                                   AS anchors,
             SUM(best.aord)                             AS anchor_orders,
             SUM(best.b.together)                       AS attached,
             SUM(best.aord - best.b.together)           AS gap_orders,
             SUM((best.aord - best.b.together) * pp.med_price) AS gap_value_sek
      FROM best
      JOIN pinfo pp ON pp.shop = best.shop AND pp.prod_key = best.b.comp
      GROUP BY 1
    """)


# ── Assembly ─────────────────────────────────────────────────────────────────
def build_payload() -> dict:
    baskets = basket_composition()
    pairs = mine_pairs()
    gaps = attach_gap_kpis()
    cats = category_pairs()

    shops = {}
    for g in gaps:
        s = g["shop"]
        srows = [b for b in baskets if b["shop"] == s]
        orders = sum(b["orders"] for b in srows)
        revenue = sum(b["revenue_msek"] or 0 for b in srows)
        single = next((b for b in srows if b["bucket"] == "1"), None)
        two = next((b for b in srows if b["bucket"] == "2"), None)
        gap_value_m = F((g["gap_value_sek"] or 0) / 1e6)
        aov_step = (two["aov"] - single["aov"]) if (single and two) else None
        single_orders = single["orders"] if single else 0
        shops[s] = {
            "orders": orders,
            "revenue_msek": F(revenue),
            "single_share": F(100.0 * single_orders / orders, 1) if orders else None,
            "aov_step": aov_step,
            "anchors": I(g["anchors"]),
            "avg_attach": F(100.0 * I(g["attached"]) / I(g["anchor_orders"]), 1),
            "gap_orders": I(g["gap_orders"]),
            "gap_value_msek": gap_value_m,
            # every pp of single→multi shift, valued at the observed AOV step
            "pp_value_msek": F(orders / 100.0 * (aov_step or 0) / 1e6)
                             if (orders and aov_step) else None,
            # string keys: Firestore rejects non-string map keys (json.dump
            # would silently stringify them, Firestore raises instead)
            "uplift_msek": {str(p): F((gap_value_m or 0) * p / 100.0, 1) for p in (5, 10, 15)},
            "uplift_gp_msek": {str(p): F((gap_value_m or 0) * p / 100.0 * GROSS_MARGIN, 1)
                               for p in (5, 10, 15)},
        }

    return {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "sources": {
            "norce": {"dataset": f"{BQ_PROJECT}.{BQ_DATASET}"},
            "window_days": WINDOW_DAYS,
            "min_together": MIN_TOGETHER,
            "min_lift_strong": MIN_LIFT_STRONG,
            "gross_margin": GROSS_MARGIN,
            "images": "Channable feed via norce.sku_titles.image_link "
                      "(current catalogue only)",
        },
        "shops": shops,
        "baskets": baskets,
        "category_pairs": cats,
        "pairs": pairs,
        "caveats": CAVEATS,
        "definitions": DEFINITIONS,
    }


def _check_size(payload: dict) -> int:
    n = len(json.dumps(payload, ensure_ascii=False))
    if n > DOC_BUDGET_BYTES:
        big = sorted(((len(json.dumps(v, ensure_ascii=False)), k)
                      for k, v in payload.items()), reverse=True)[:3]
        raise RuntimeError(
            f"Bundles payload is {n:,} B, over the {DOC_BUDGET_BYTES:,} B budget. "
            "Biggest sections: " + ", ".join(f"{k} {s:,} B" for s, k in big)
            + ". Lower BUNDLES_PAIR_LIMIT — do not just raise the budget.")
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
    out = os.environ.get("BUNDLES_OUT")
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(p, fh, ensure_ascii=False)
        print(f"   wrote {out} ({os.path.getsize(out):,} bytes)")
    where = "(skipped)" if os.environ.get("SKIP_FIRESTORE") else write_firestore(p)
    bs = p["shops"].get("babyshop", {})
    with_img = sum(1 for pr in p["pairs"]
                   if pr["anchor"].get("img") and pr["comp"].get("img"))
    print(f"✓ Bundles refresh · {where} · {len(p['pairs'])} pairs "
          f"({with_img} with both images) · "
          f"babyshop gap {bs.get('gap_value_msek')} MSEK over {bs.get('anchors')} anchors · "
          f"{len(json.dumps(p, ensure_ascii=False)):,} B · {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
