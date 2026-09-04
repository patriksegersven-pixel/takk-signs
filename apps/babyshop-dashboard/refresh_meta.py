#!/usr/bin/env python3
"""
Meta creatives snapshot — Bluebird warehouse (cross-project) → Firestore.

Writes `funnel_cache/<workspace>__meta`, the single document the "Meta
creatives" tab reads. Same client/auth wiring and the same
{data, fetched_at, expires_at, ttl_seconds, workspace} wrapper as
refresh_bundles.py / refresh_segments.py.

WHERE THE DATA COMES FROM
  `claude-private-499703.babyshop_marts.agg_daily_kpis_by_ad` — a
  purpose-built Meta mart at date × ad_id grain that already joins insights ×
  ads × creatives and pre-computes purchases / purchase_value out of the
  actions JSON. Plus `babyshop_staging.stg_meta__ads` for created_time (the
  "recently uploaded" panel).

  This is the NEW stack's warehouse read cross-project from the LEGACY
  dashboard. The BigQuery JOB is billed to project-a7ade44e (which already
  holds bigquery.jobUser for the runtime SA); the runtime SA only needs
  roles/bigquery.dataViewer on the two kuvio datasets — see
  pipeline/setup-meta.sh for the exact grant.

DERIVATION RULES (verified against the KiriMedia mockup, 2026-08-27..09-02)
  • market comes from the AD NAME token (SWE/SE → SE, NOR/NO → NO), NOT from
    dim_campaign_market and not from the mart's `market` column. One campaign
    ("KIR | SE+NO-BS-offers") carries separate SE and NO ads; the campaign-level
    map assigns all 5 651 kr of it to SE and silently moves a row out of the NO
    top-5. The mart's `market` is only the fallback for names with no token.
  • media type is DERIVED from the ad name + campaign name. The creative field
    `creative_type` (Meta object_type) only ever says SHARE or VIDEO here —
    videos posted as page-post shares land in SHARE, and DPA / carousel /
    slideshow are indistinguishable because the connector loads no
    asset_feed_spec and no product_set_id.
  • the concept tag (High End / UGC / In-house / Partnership) is likewise a
    naming convention, not a field. Untagged spend is reported as
    "Untagged / legacy" — a real monitor, not a rounding bucket.
  • aggregate by ad_id, never by ad_name: the same ad_name runs in two adsets,
    and name-grain aggregation both distorts CPA and promotes the wrong rows
    past the spend floor.
  • the mart's per-row ctr/cpc/cpm/cpa/roas columns are ratios and are NOT
    summable — every ratio here is recomputed as a ratio of sums.

WINDOW
  Ends META_SETTLE_DAYS (default 2) before today, so the last day in the
  window has had a full day to settle. 7 days back from there. Pin it with
  META_END_DATE=YYYY-MM-DD to reproduce a specific report.

Run locally:
  # query the warehouse as the kuvio account, dump the payload, write nothing
  CLOUDSDK_CORE_ACCOUNT=patrik@kuvio.io META_AUTH=gcloud \\
    META_BQ_BILLING_PROJECT=claude-private-499703 \\
    SKIP_FIRESTORE=1 META_OUT=/tmp/meta.json python3 refresh_meta.py

  # write that payload to Firestore as the gmail account, no BigQuery at all
  META_IN=/tmp/meta.json python3 refresh_meta.py
"""
from __future__ import annotations
import datetime, json, os, re, time
from google.cloud import bigquery

# The warehouse holding the Meta mart (the NEW stack's project) …
DATA_PROJECT   = os.environ.get("META_DATA_PROJECT", "claude-private-499703")
MARTS_DATASET  = os.environ.get("META_MARTS_DATASET", "babyshop_marts")
STAGING_DATASET = os.environ.get("META_STAGING_DATASET", "babyshop_staging")
# … and the project the query JOB is billed to. Defaults to this dashboard's
# own project: the runtime SA has jobUser there and needs only dataViewer on
# the datasets above. Override to the data project when running locally as a
# kuvio user who has no rights in project-a7ade44e.
BQ_BILLING_PROJECT = os.environ.get("META_BQ_BILLING_PROJECT",
                                    "project-a7ade44e-e7e3-4871-a83")
BQ_LOCATION = os.environ.get("META_BQ_LOCATION", "EU")

WORKSPACE  = os.environ.get("FUNNEL_WORKSPACE", "-Ln87GcdqU9CMJV6zMBY")
COLLECTION = "funnel_cache"
DOC_KEY    = "meta"
TTL        = 30 * 24 * 3600
FIRESTORE_PROJECT = os.environ.get("FIRESTORE_PROJECT", "project-a7ade44e-e7e3-4871-a83")

WINDOW_DAYS  = int(os.environ.get("META_WINDOW_DAYS", "7"))
SETTLE_DAYS  = int(os.environ.get("META_SETTLE_DAYS", "2"))
MIN_SPEND    = float(os.environ.get("META_MIN_SPEND", "300"))   # top-5 relevance floor
TOP_N        = int(os.environ.get("META_TOP_N", "5"))
RECENT_N     = int(os.environ.get("META_RECENT_N", "5"))
MARKETS      = ["SE", "NO"]

DOC_BUDGET_BYTES = 900_000

CAVEATS = [
    "ROAS is Meta's platform-reported value on its default attribution window "
    "(7d click / 1d view) — it is context, not a GM-verified return, and it "
    "double-counts conversions the other channels also claim",
    "creative thumbnails are signed Meta CDN URLs that expire about 4 days "
    "after they are minted, which is why this job runs daily — a snapshot "
    "older than ~3 days renders broken images even though the numbers are fine",
    "those thumbnails are 64x64: the requested size is signed into the URL, so "
    "asking for a larger rendition returns 403, and creative_image_url (a real "
    "full-size still) is populated on well under 5% of rows",
    "concept tags (High End / UGC / In-house / Partnership) come from the "
    "agency's ad-naming convention, not from a field; anything unrecognised is "
    "reported as 'Untagged / legacy' rather than hidden",
    "media type is derived the same way — the creative object_type only "
    "distinguishes SHARE from VIDEO, so carousel, slideshow and DPA are read "
    "off the ad and campaign names",
    "market is parsed from the ad name (SWE/SE, NOR/NO), falling back to the "
    "mart's campaign market — mixed SE+NO campaigns carry per-market ads and a "
    "campaign-level map puts them all in one country",
    "purchases are pixel website purchases at Meta's attribution, so they do "
    "not reconcile to Norce orders and are not comparable to the KV tab",
    "the window ends " + str(SETTLE_DAYS) + " days before today so attribution "
    "has settled; the last two days of spend are deliberately not shown",
]

DEFINITIONS = {
    "spend":     "Meta ad_spend, SEK, SE+NO ads only",
    "purchases": "attributed website purchases (pixel), Meta 7d click / 1d view",
    "cpa":       "spend ÷ purchases (ratio of sums, not an average of the mart's per-row cpa)",
    "ctr":       "clicks ÷ impressions — all clicks, since the connector loads no "
                 "inline_link_clicks column; used as a hook/interest proxy",
    "cpm":       "spend ÷ impressions × 1000",
    "roas":      "purchase_value ÷ spend, Meta platform-reported",
    "top5":      "per market, ranked by lowest CPA, minimum "
                 + str(int(MIN_SPEND)) + " kr spend, DPA / catalog excluded",
    "recent":    "ads whose created_time falls in the window and whose ad_name "
                 "did not already exist on an older ad (so re-uploads of an old "
                 "concept into a new adset do not crowd out genuinely new work)",
}


def _credentials():
    """ADC in production (Cloud Run SA); gcloud user token as a local fallback.

    META_AUTH=gcloud forces the subprocess path: locally the ADC file belongs
    to one account and the warehouse read has to run as the other, and
    CLOUDSDK_CORE_ACCOUNT only steers the CLI, never ADC."""
    import subprocess, google.oauth2.credentials

    class _GcloudToken(google.oauth2.credentials.Credentials):
        def refresh(self, request):  # noqa: ARG002
            self.token = subprocess.check_output(
                ["gcloud", "auth", "print-access-token"]).decode().strip()
            self.expiry = datetime.datetime.utcnow() + datetime.timedelta(minutes=45)

    if os.environ.get("META_AUTH") != "gcloud":
        try:
            import google.auth
            creds, _ = google.auth.default()
            return creds
        except Exception:
            pass

    tok = subprocess.check_output(["gcloud", "auth", "print-access-token"]).decode().strip()
    c = _GcloudToken(tok)
    c.expiry = datetime.datetime.utcnow() + datetime.timedelta(minutes=45)
    return c


_client = None
def bq() -> bigquery.Client:
    global _client
    if _client is None:
        _client = bigquery.Client(project=BQ_BILLING_PROJECT, location=BQ_LOCATION,
                                  credentials=_credentials())
    return _client


def q(sql: str, **params) -> list[dict]:
    cfg = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter(k, "DATE", v) for k, v in params.items()])
    return [dict(r) for r in bq().query(sql, job_config=cfg).result()]


def I(v):
    return int(round(float(v or 0)))


def F(v, nd=2):
    return None if v is None else round(float(v), nd)


def div(a, b, scale=1.0, nd=2):
    a, b = float(a or 0), float(b or 0)
    return None if b == 0 else round(a / b * scale, nd)


# ── Shared SQL fragments ─────────────────────────────────────────────────────
MART    = f"`{DATA_PROJECT}.{MARTS_DATASET}.agg_daily_kpis_by_ad`"
STG_ADS = f"`{DATA_PROJECT}.{STAGING_DATASET}.stg_meta__ads`"

# One derivation block, textually reused by every query, so the market / media
# type / tag rules can never drift apart between the KPI tiles, the donuts and
# the tables.
DERIVE = r"""
    CASE
      WHEN REGEXP_CONTAINS(ad_name, r'(?i)(^|[ \-])(SWE|SE)([ \-]|$)') THEN 'SE'
      WHEN REGEXP_CONTAINS(ad_name, r'(?i)(^|[ \-])(NOR|NO)([ \-]|$)') THEN 'NO'
      ELSE market END                                    AS mk,
    CASE
      WHEN UPPER(ad_name) LIKE 'DPA%'
        OR UPPER(campaign_name) LIKE '%CATALOG%'  THEN 'DPA / catalog'
      WHEN UPPER(ad_name) LIKE '%SLIDESHOW%'      THEN 'Slideshow'
      WHEN UPPER(ad_name) LIKE '%CAROUSEL%'       THEN 'Carousel'
      WHEN UPPER(ad_name) LIKE '%VIDEO%'          THEN 'Video'
      WHEN UPPER(ad_name) LIKE '%STILL IMG%'
        OR UPPER(ad_name) LIKE '%IMG%'            THEN 'Image (still)'
      WHEN creative_video_id IS NOT NULL          THEN 'Video'
      ELSE 'Image (still)' END                            AS media_type,
    CASE
      WHEN UPPER(ad_name) LIKE 'DPA%'
        OR UPPER(campaign_name) LIKE '%CATALOG%'  THEN 'DPA / catalog'
      WHEN UPPER(ad_name) LIKE '%HIGH END%'       THEN 'High End'
      WHEN UPPER(ad_name) LIKE '%UGC%'            THEN 'UGC'
      WHEN UPPER(ad_name) LIKE '%INHOUSE%'
        OR UPPER(ad_name) LIKE '%IN-HOUSE%'       THEN 'In-house production'
      WHEN UPPER(ad_name) LIKE '%PARTNERSHIP%'    THEN 'Partnership'
      ELSE 'Untagged / legacy' END                        AS tag
"""


# ── Sections ─────────────────────────────────────────────────────────────────
def per_ad(win: dict) -> list[dict]:
    """One pass over both windows at ad_id grain. Everything except the
    "recently uploaded" panel is folded out of this in Python — a couple of
    hundred rows, and one cross-project scan instead of five."""
    return q(f"""
      WITH base AS (
        SELECT
          CASE WHEN date BETWEEN @start  AND @end  THEN 'cur'
               WHEN date BETWEEN @pstart AND @pend THEN 'prev' END AS period,
          ad_id, ad_name, campaign_name, market, creative_video_id,
          creative_thumbnail_url, creative_image_url,
          ad_spend, impressions, clicks, purchases, purchase_value
        FROM {MART}
        WHERE date BETWEEN @pstart AND @end
      ),
      d AS (SELECT *, {DERIVE} FROM base WHERE period IS NOT NULL)
      SELECT period, mk, ad_id,
             ANY_VALUE(ad_name)                AS ad_name,
             ANY_VALUE(tag)                    AS tag,
             ANY_VALUE(media_type)             AS media_type,
             ANY_VALUE(creative_thumbnail_url) AS thumbnail_url,
             ANY_VALUE(creative_image_url)     AS image_url,
             SUM(ad_spend)       AS spend,
             SUM(impressions)    AS impressions,
             SUM(clicks)         AS clicks,
             SUM(purchases)      AS purchases,
             SUM(purchase_value) AS purchase_value
      FROM d
      GROUP BY period, mk, ad_id
    """, start=win["from"], end=win["to"], pstart=win["prev_from"], pend=win["prev_to"])


def recent_ads(win: dict) -> list[dict]:
    """Ads created inside the window that spent something.

    The `prior` anti-join is what makes this panel mean "new work": without it
    four re-uploads of last month's concepts into the new -shoes adset crowd
    the genuinely new batch out of the top 5."""
    rows = q(f"""
      WITH prior AS (
        SELECT DISTINCT ad_name FROM {STG_ADS} WHERE DATE(created_time) < @start
      ),
      base AS (
        SELECT ad_id, ad_name, campaign_name, market, creative_video_id,
               creative_thumbnail_url, creative_image_url,
               ad_spend, purchases, purchase_value
        FROM {MART}
        WHERE date BETWEEN @start AND @end
      ),
      d AS (SELECT *, {DERIVE} FROM base),
      k AS (
        SELECT ad_id,
               ANY_VALUE(mk)                     AS mk,
               ANY_VALUE(tag)                    AS tag,
               ANY_VALUE(media_type)             AS media_type,
               ANY_VALUE(creative_thumbnail_url) AS thumbnail_url,
               ANY_VALUE(creative_image_url)     AS image_url,
               SUM(ad_spend)       AS spend,
               SUM(purchases)      AS purchases,
               SUM(purchase_value) AS purchase_value
        FROM d GROUP BY ad_id
      )
      SELECT a.ad_id, a.ad_name, DATE(a.created_time) AS uploaded,
             a.effective_status, k.mk, k.tag, k.media_type,
             k.thumbnail_url, k.image_url, k.spend, k.purchases, k.purchase_value
      FROM {STG_ADS} a
      JOIN k USING (ad_id)
      WHERE DATE(a.created_time) BETWEEN @start AND @end
        AND a.ad_name NOT IN (SELECT ad_name FROM prior)
        AND k.spend > 0
      ORDER BY k.purchases DESC, k.spend ASC
      LIMIT {RECENT_N}
    """, start=win["from"], end=win["to"])
    return [_creative(r, uploaded=r["uploaded"].isoformat(),
                      status=r["effective_status"]) for r in rows]


# ── Assembly ─────────────────────────────────────────────────────────────────
# A KiriMedia ad name is a slug, not a label:
#   "Product - SWE - 1 - Kuling - Logo+copy - UGC - VIDEO - 2026-08-14"
# Everything a reader wants sits in the middle: brand, then the concept. The
# head is objective + market + a serial, the tail is the tag, the media type
# and the upload date — all of which the tab already shows as their own column
# or chip, so repeating them in the name is noise.
_NAME_HEAD = {"PRODUCT", "NEWS", "OFFER", "BRAND", "PRODUCTNEWS", "DPA",
              "CATALOG", "RETARGETING", "PROSPECTING", "TOF", "MOF", "BOF",
              "SWE", "SE", "NOR", "NO", "FIN", "FI", "DK", "DNK"}
_NAME_TAIL = {"UGC", "HIGHEND", "INHOUSEPROD", "INHOUSE", "PARTNERSHIP",
              "VIDEO", "IMG", "STILLIMG", "CAROUSEL", "SLIDESHOW", "STATIC",
              "GIF", "DPA"}

def _display_name(ad_name: str | None) -> str:
    raw = (ad_name or "").strip()
    parts = [p.strip() for p in raw.split(" - ") if p.strip()]
    if len(parts) < 3:
        return raw
    key = lambda p: re.sub(r"[^A-Za-z]", "", p).upper()
    i = 0
    while i < len(parts) and (key(parts[i]) in _NAME_HEAD or not key(parts[i])):
        i += 1
    body = []
    for p in parts[i:]:
        # the tail begins at the first tag / media token or the upload date
        if key(p) in _NAME_TAIL or re.match(r"^\d{4}-\d{2}", p):
            break
        body.append(p)
    if not body:
        return raw
    return body[0] if len(body) == 1 else f"{body[0]} — " + ", ".join(body[1:])


def _creative(r: dict, **extra) -> dict:
    """The one creative shape the tab renders, wherever a row comes from."""
    out = {
        "ad_id":    str(r["ad_id"]),
        "ad_name":  r["ad_name"],
        "display_name": _display_name(r["ad_name"]),
        "tag":      r["tag"],
        "media_type": r.get("media_type"),
        "market":   r.get("mk"),
        "spend":    F(r["spend"], 2),
        "purchases": I(r["purchases"]),
        "cpa":      div(r["spend"], r["purchases"]),
        "roas":     div(r["purchase_value"], r["spend"]),
        "thumbnail_url": r.get("thumbnail_url"),
        # Videos carry no still image_url; the lightbox falls back to the
        # thumbnail, which is the only rendition Meta exposes for them.
        "image_url": r.get("image_url") or r.get("thumbnail_url"),
    }
    out.update(extra)
    return out


def _kpis(rows: list[dict]) -> dict:
    spend = sum(float(r["spend"] or 0) for r in rows)
    impr  = sum(I(r["impressions"]) for r in rows)
    clicks = sum(I(r["clicks"]) for r in rows)
    purch = sum(I(r["purchases"]) for r in rows)
    value = sum(float(r["purchase_value"] or 0) for r in rows)
    return {
        "spend": F(spend), "impressions": impr, "clicks": clicks,
        "purchases": purch, "purchase_value": F(value),
        "cpa": div(spend, purch),
        "ctr": div(clicks, impr, 100.0),
        "cpm": div(spend, impr, 1000.0),
        "roas": div(value, spend),
    }


# Lower is better for CPA and CPM; for everything else more is better. The tab
# only needs to know which way to colour the delta pill.
_LOWER_IS_BETTER = {"cpa", "cpm"}

def _with_deltas(cur: dict, prev: dict) -> dict:
    out = dict(cur)
    out["prev"] = prev
    out["delta_pct"] = {
        k: div((cur.get(k) or 0) - (prev.get(k) or 0), prev.get(k), 100.0, 1)
        for k in ("spend", "purchases", "cpa", "ctr", "cpm", "roas")
    }
    out["lower_is_better"] = sorted(_LOWER_IS_BETTER)
    return out


def _mix(rows: list[dict], key: str) -> list[dict]:
    total = sum(float(r["spend"] or 0) for r in rows)
    buckets: dict[str, float] = {}
    for r in rows:
        buckets[r[key]] = buckets.get(r[key], 0.0) + float(r["spend"] or 0)
    return [{"label": k, "spend": F(v), "pct": div(v, total, 100.0, 1)}
            for k, v in sorted(buckets.items(), key=lambda kv: -kv[1])]


def _window() -> dict:
    end = os.environ.get("META_END_DATE")
    end_d = (datetime.date.fromisoformat(end) if end
             else datetime.date.today() - datetime.timedelta(days=SETTLE_DAYS))
    start_d = end_d - datetime.timedelta(days=WINDOW_DAYS - 1)
    return {
        "from": start_d.isoformat(), "to": end_d.isoformat(),
        "prev_from": (start_d - datetime.timedelta(days=WINDOW_DAYS)).isoformat(),
        "prev_to":   (end_d - datetime.timedelta(days=WINDOW_DAYS)).isoformat(),
        "days": WINDOW_DAYS,
    }


def build_payload() -> dict:
    win = _window()
    rows = per_ad(win)

    cur  = [r for r in rows if r["period"] == "cur"  and r["mk"] in MARKETS]
    prev = [r for r in rows if r["period"] == "prev" and r["mk"] in MARKETS]

    kpis = {"combined": _with_deltas(_kpis(cur), _kpis(prev))}
    for m in MARKETS:
        kpis[m] = _with_deltas(_kpis([r for r in cur if r["mk"] == m]),
                               _kpis([r for r in prev if r["mk"] == m]))

    # Top 5 per market: lowest CPA over a spend floor, DPA excluded — a
    # catalogue ad is not a creative anyone can iterate on.
    top: dict[str, list[dict]] = {}
    for m in MARKETS:
        elig = [r for r in cur
                if r["mk"] == m
                and r["media_type"] != "DPA / catalog"
                and float(r["spend"] or 0) >= MIN_SPEND
                and I(r["purchases"]) > 0]
        elig.sort(key=lambda r: float(r["spend"]) / I(r["purchases"]))
        top[m] = [_creative(r) for r in elig[:TOP_N]]

    payload = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "sources": {
            "marts": f"{DATA_PROJECT}.{MARTS_DATASET}.agg_daily_kpis_by_ad",
            "staging": f"{DATA_PROJECT}.{STAGING_DATASET}.stg_meta__ads",
            "account_id": "2927326560764919",
            "account_label": "Babyshop SE — New",
            "attribution": "7d_click,1d_view",
            "markets": MARKETS,
            "min_spend": MIN_SPEND,
            "images": "Meta CDN signed URLs from the creative record "
                      "(expire ~4 days after minting)",
        },
        "window": win,
        "kpis": kpis,
        "media_types": _mix(cur, "media_type"),
        "media_formats": _mix(cur, "tag"),
        "top_creatives": top,
        "recent": recent_ads(win),
        "caveats": CAVEATS,
        "definitions": DEFINITIONS,
    }
    return payload


def _check_size(payload: dict) -> int:
    n = len(json.dumps(payload, ensure_ascii=False))
    if n > DOC_BUDGET_BYTES:
        big = sorted(((len(json.dumps(v, ensure_ascii=False)), k)
                      for k, v in payload.items()), reverse=True)[:3]
        raise RuntimeError(
            f"Meta payload is {n:,} B, over the {DOC_BUDGET_BYTES:,} B budget. "
            "Biggest sections: " + ", ".join(f"{k} {s:,} B" for s, k in big)
            + ". Lower META_TOP_N / META_RECENT_N — do not just raise the budget.")
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
    # META_IN replays a payload produced by an earlier META_OUT run. The two
    # halves of this job authenticate to different projects as different
    # accounts, and locally one process cannot be both — this splits them.
    src = os.environ.get("META_IN")
    if src:
        with open(src, encoding="utf-8") as fh:
            p = json.load(fh)
        print(f"   loaded {src} ({os.path.getsize(src):,} bytes) — BigQuery skipped")
    else:
        p = build_payload()

    out = os.environ.get("META_OUT")
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(p, fh, ensure_ascii=False)
        print(f"   wrote {out} ({os.path.getsize(out):,} bytes)")

    where = "(skipped)" if os.environ.get("SKIP_FIRESTORE") else write_firestore(p)
    k = p["kpis"]["combined"]
    w = p["window"]
    with_thumb = sum(1 for c in p["top_creatives"]["SE"] + p["top_creatives"]["NO"]
                     + p["recent"] if c.get("thumbnail_url"))
    print(f"✓ Meta refresh · {where} · {w['from']}..{w['to']} · "
          f"spend {k['spend']:,.0f} kr / {k['purchases']} purchases / CPA {k['cpa']} / "
          f"ROAS {k['roas']}x · "
          f"top {len(p['top_creatives']['SE'])}+{len(p['top_creatives']['NO'])}, "
          f"{len(p['recent'])} new ({with_thumb} with thumbnails) · "
          f"{len(json.dumps(p, ensure_ascii=False)):,} B · {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
