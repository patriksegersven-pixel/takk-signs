#!/usr/bin/env python3
"""
Meta creatives snapshot — Bluebird warehouse (cross-project) → Firestore.

Writes `funnel_cache/<workspace>__meta`, the single document the "Meta
creatives" tab reads. Same client/auth wiring and the same
{data, fetched_at, expires_at, ttl_seconds, workspace} wrapper as
refresh_bundles.py / refresh_segments.py.

WHERE THE DATA COMES FROM
  v1 read the pre-aggregated mart `babyshop_marts.agg_daily_kpis_by_ad`. v2
  reads one level lower, `babyshop_staging.stg_meta__ads_insights`, because the
  mart carries no per-ad video columns and no unnested engagement actions —
  hook rate, hold rate, saves and shares only exist in the `actions` JSON and
  the flattened `video_*` columns of the staging view.

    stg_meta__ads_insights   date × ad_id: spend, impressions, clicks, reach,
                             actions/action_values JSON, video_plays/thruplays/
                             p25..p100/avg_seconds
    stg_meta__ads            created_time, effective_status, creative_id
    stg_meta__ad_creatives   thumbnail_url, image_url, instagram_permalink_url,
                             title, body, video_id

  This is the NEW stack's warehouse read cross-project from the LEGACY
  dashboard. The BigQuery JOB is billed to project-a7ade44e (which already
  holds bigquery.jobUser for the runtime SA); the runtime SA needs
  roles/bigquery.dataViewer on babyshop_marts, babyshop_staging AND
  babyshop_raw (staging views resolve against raw with the caller's
  permissions) — see pipeline/setup-meta.sh for the exact grant.

DERIVATION RULES (verified against the KiriMedia mockup, 2026-08-27..09-02)
  • market comes from the AD NAME token (SWE/SE → SE, NOR/NO → NO), NOT from
    dim_campaign_market and not from the staging `market` column. One campaign
    ("KIR | SE+NO-BS-offers") carries separate SE and NO ads; the campaign-level
    map assigns all 5 651 kr of it to SE and silently moves a row out of the NO
    top-5. The staging `market` is only the fallback for names with no token.
  • media type is DERIVED from the ad name + campaign name. The creative field
    object_type only ever says SHARE or VIDEO here — videos posted as page-post
    shares land in SHARE, and DPA / carousel / slideshow are indistinguishable
    because the connector loads no asset_feed_spec and no product_set_id.
  • the concept tag (High End / UGC / In-house / Partnership) is likewise a
    naming convention, not a field. Untagged spend is reported as
    "Untagged / legacy" — a real monitor, not a rounding bucket.
  • the promo flag is a regex over ad name + campaign name + creative title +
    body (PROMO_PATTERNS), with PROMO_OVERRIDES for manual exceptions.
  • aggregate by ad_id, never by ad_name: the same ad_name runs in two adsets,
    and name-grain aggregation both distorts CPA and promotes the wrong rows
    past the spend floor.
  • every ratio is a ratio of sums. Nothing here averages a per-row ratio.

WINDOWS
  L7   ends META_SETTLE_DAYS (default 2) before today, so the last day in the
       window has had a full day to settle. Headline KPIs + the fatigue board's
       "current". Compared against the 7 days immediately before it.
  L28  the 28 days ending on the same day. Leaderboards and the "what works"
       rollups, which need more than a week of signal to rank on.
  weekly  per ad, 7-day buckets counted from that ad's FIRST SPEND DAY (not ISO
       weeks — an ad launched on a Thursday would otherwise get a 1-day week 0
       as its fatigue baseline). Up to WEEKS_KEPT buckets are kept for the
       sparkline; the baseline is picked from the full series.
  Pin the end with META_END_DATE=YYYY-MM-DD to reproduce a specific report.

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

# The warehouse holding the Meta data (the NEW stack's project) …
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
LONG_DAYS    = int(os.environ.get("META_LONG_DAYS", "28"))
SETTLE_DAYS  = int(os.environ.get("META_SETTLE_DAYS", "2"))
MARKETS      = ["SE", "NO"]

# ── Relevance floors ─────────────────────────────────────────────────────────
# Nothing is ranked below these. They are shown in the UI rather than applied
# silently: a leaderboard whose floor the reader cannot see is a leaderboard
# the reader cannot argue with.
MIN_SPEND_L7  = float(os.environ.get("META_MIN_SPEND", "300"))     # L7 rankings
MIN_SPEND_L28 = float(os.environ.get("META_MIN_SPEND_L28", "1000"))  # L28 rankings

TOP_N     = int(os.environ.get("META_TOP_N", "5"))
RECENT_N  = int(os.environ.get("META_RECENT_N", "5"))
LEADER_N  = int(os.environ.get("META_LEADER_N", "5"))

# ── Fatigue model ────────────────────────────────────────────────────────────
# Baseline = the ad's first FULL 7-day bucket (counted from its first spend
# day) that spent at least BASELINE_MIN_SPEND. "Full" matters: a bucket whose
# 7th day has not happened yet would compare a part-week against a whole one.
# Current = the L7 window. The indices are current ÷ baseline, so >1 is better
# for CTR and hook, and the CPA index is left uninverted (>1 = more expensive =
# worse) because that is how a reader reads a cost.
BASELINE_MIN_SPEND = float(os.environ.get("META_BASELINE_MIN_SPEND", "300"))
FRESH_DAYS      = int(os.environ.get("META_FRESH_DAYS", "14"))    # too new to judge
STABLE_INDEX    = float(os.environ.get("META_STABLE_INDEX", "0.85"))
EXHAUSTED_INDEX = float(os.environ.get("META_EXHAUSTED_INDEX", "0.70"))
EXHAUSTED_DAYS  = int(os.environ.get("META_EXHAUSTED_DAYS", "28"))  # days ACTIVE
FREQ_RISE       = float(os.environ.get("META_FREQ_RISE", "1.5"))
WEEKS_KEPT      = int(os.environ.get("META_WEEKS_KEPT", "12"))
RISING_DAYS     = int(os.environ.get("META_RISING_DAYS", "14"))

STATUS_FRESH     = "Fresh"
STATUS_STABLE    = "Stable"
STATUS_FATIGUING = "Fatiguing"
STATUS_EXHAUSTED = "Exhausted"
STATUS_NO_BASE   = "No baseline"
# Severity order — the fatigue board's default sort, worst money first.
STATUS_ORDER = [STATUS_EXHAUSTED, STATUS_FATIGUING, STATUS_NO_BASE,
                STATUS_FRESH, STATUS_STABLE]
# The two statuses that mean "this spend is buying less than it used to".
STATUS_AT_RISK = (STATUS_FATIGUING, STATUS_EXHAUSTED)

DOC_BUDGET_BYTES = 900_000

# ── Promo detection ──────────────────────────────────────────────────────────
# Matched case-insensitively against ad_name + campaign_name + creative title +
# creative body. Kept as a list so the pattern set is reviewable, and mirrored
# into the payload's definitions so the tab can state it.
#   \d+\s?%   "30%", "30 %"          rea/erbjudande = SE, tilbud = NO
PROMO_PATTERNS = [
    r"\d+\s?%", r"offer", r"promo", r"sale", r"\brea\b", r"erbjudande",
    r"tilbud", r"kampanj", r"-offers",
]
PROMO_REGEX = "(?i)(" + "|".join(PROMO_PATTERNS) + ")"
# Manual exceptions, ad_id → bool. The regex is deliberately broad (an ad named
# "…Wholesale…" matches "sale"), so a wrong call is corrected here rather than
# by weakening the pattern for everything.
PROMO_OVERRIDES: dict[str, bool] = {}

ADS_MANAGER_URL = ("https://adsmanager.facebook.com/adsmanager/manage/ads"
                   "?act={account_id}&selected_ad_ids={ad_id}")

CAVEATS = [
    "ROAS is Meta's platform-reported value on its default attribution window "
    "(7d click / 1d view) — it is context, not a GM-verified return, and it "
    "double-counts conversions the other channels also claim",
    "frequency is the average DAILY frequency (impressions ÷ summed daily "
    "reach): the feed carries reach at daily grain only, so a true 7-day "
    "frequency is not available — a rising daily figure is still the fatigue "
    "signal that matters",
    "link CTR uses the actions.link_click action, not the clicks column "
    "(which counts every click on the ad, including profile taps); the "
    "connector loads no inline_link_clicks column",
    "Meta's own quality / engagement / conversion rankings are not in the feed, "
    "and neither is preview_shareable_link — the preview links here are an Ads "
    "Manager deep link and, where one exists, the Instagram permalink",
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
    "staging campaign market — mixed SE+NO campaigns carry per-market ads and a "
    "campaign-level map puts them all in one country",
    "purchases are pixel website purchases at Meta's attribution, so they do "
    "not reconcile to Norce orders and are not comparable to the KV tab",
    "the window ends " + str(SETTLE_DAYS) + " days before today so attribution "
    "has settled; the last two days of spend are deliberately not shown",
]

DEFINITIONS = {
    "spend":     "Meta ad_spend, SEK, SE+NO ads only",
    "purchases": "attributed website purchases (pixel), Meta 7d click / 1d view; "
                 "action_type offsite_conversion.fb_pixel_purchase",
    "cpa":       "spend ÷ purchases (ratio of sums, never an average of per-row cpa)",
    "ctr":       "clicks ÷ impressions — ALL clicks, kept for continuity with v1",
    "link_ctr":  "actions.link_click ÷ impressions — the click that leaves Meta",
    "cpc":       "spend ÷ link clicks",
    "cpm":       "spend ÷ impressions × 1000",
    "roas":      "purchase_value ÷ spend, Meta platform-reported",
    "frequency": "average DAILY frequency — impressions ÷ the sum of daily "
                 "reach over the window (impression-weighted mean of Meta's "
                 "daily frequency). The feed has no period-level reach, so "
                 "this is not a 7-day frequency",
    "lpv":       "actions.landing_page_view — the page actually rendered",
    "lpv_rate":  "landing page views ÷ link clicks — click quality / page speed. "
                 "Meta attributes the two actions independently, so this can "
                 "read slightly above 100 %",
    "cost_per_lpv": "spend ÷ landing page views",
    "atc_rate":  "add_to_cart ÷ landing page views",
    "purchase_rate": "purchases ÷ landing page views",
    "hook_rate": "actions.video_view (Meta's 3-second video view) ÷ impressions. "
                 "video_plays (video_play_actions) counts every play START, "
                 "including sub-3s ones, so it is the denominator for hold and "
                 "completion but not the numerator for the hook",
    "hold_rate": "video_thruplays ÷ video_plays",
    "completion": "video_p100 (watched to the end) ÷ video_plays",
    "avg_seconds": "video_avg_seconds_watched weighted by video_plays",
    "social_per_1k": "(post_reaction + post [shares] + onsite_conversion.post_save "
                     "+ comment) per 1 000 impressions",
    "promo":     "regex over ad name + campaign name + creative title + body: "
                 + ", ".join(PROMO_PATTERNS) + " (case-insensitive), plus a "
                 "manual PROMO_OVERRIDES list",
    "uploaded":  "DATE(stg_meta__ads.created_time) — when the ad object was made, "
                 "which can be well before its first spend",
    "days_active": "days with spend > 0 between the ad's first spend day and the "
                   "end of the window — not calendar days since upload",
    "baseline":  "the ad's first FULL 7-day bucket (counted from its first spend "
                 "day) with at least " + str(int(BASELINE_MIN_SPEND)) + " kr spend",
    "ctr_index": "L7 link CTR ÷ baseline link CTR. >1 is better",
    "hook_index": "L7 hook rate ÷ baseline hook rate, video ads only. >1 is better",
    "cpa_index": "L7 CPA ÷ baseline CPA. NOT inverted — >1 means the ad got "
                 "more expensive, i.e. worse",
    "status":    "Fresh: fewer than " + str(FRESH_DAYS) + " days since first "
                 "spend, too new to judge. Stable: fatigue index ≥ "
                 + str(STABLE_INDEX) + " and frequency has not risen "
                 + str(FREQ_RISE) + "× vs baseline. Fatiguing: index between "
                 + str(EXHAUSTED_INDEX) + " and " + str(STABLE_INDEX)
                 + ", OR frequency risen ≥ " + str(FREQ_RISE) + "×. Exhausted: "
                 "index < " + str(EXHAUSTED_INDEX) + " and at least "
                 + str(EXHAUSTED_DAYS) + " days active. No baseline: the ad "
                 "never had a full week over " + str(int(BASELINE_MIN_SPEND))
                 + " kr, so there is nothing to compare against. The index is "
                 "the hook index for video ads and the link-CTR index otherwise",
    "top5":      "per market, ranked by lowest CPA over the L7 window, minimum "
                 + str(int(MIN_SPEND_L7)) + " kr spend, DPA / catalog excluded",
    "leaderboards": "L28, minimum " + str(int(MIN_SPEND_L28)) + " kr spend, "
                    "DPA / catalog excluded",
    "rising":    "first spend within the last " + str(RISING_DAYS) + " days, "
                 "ranked by L28 purchases",
    "recent":    "ads whose created_time falls in the L7 window and whose ad_name "
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


def _f(v) -> float:
    return float(v or 0)


# ── Shared SQL fragments ─────────────────────────────────────────────────────
INS  = f"`{DATA_PROJECT}.{STAGING_DATASET}.stg_meta__ads_insights`"
ADS  = f"`{DATA_PROJECT}.{STAGING_DATASET}.stg_meta__ads`"
CREA = f"`{DATA_PROJECT}.{STAGING_DATASET}.stg_meta__ad_creatives`"


def ACT(action_type: str, col: str = "i.actions") -> str:
    """Sum one action_type out of the actions / action_values JSON array.

    `actions` is JSON, not a repeated STRUCT, so this is JSON_QUERY_ARRAY +
    JSON_VALUE rather than UNNEST over a struct array. Written as a correlated
    subquery per metric: BigQuery evaluates them over the same tiny array and
    it keeps each metric's definition on one readable line."""
    return (f"(SELECT IFNULL(SUM(IF(JSON_VALUE(a,'$.action_type')='{action_type}',"
            f"CAST(JSON_VALUE(a,'$.value') AS FLOAT64),0)),0) "
            f"FROM UNNEST(JSON_QUERY_ARRAY({col})) a)")


# One derivation block, textually reused by every query, so the market / media
# type / tag / promo rules can never drift apart between the KPI tiles, the
# fatigue board, the leaderboards and the rollups.
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

# The per-day projection every query starts from: the staging insights row
# joined to its ad and creative, with the actions JSON unnested into columns.
DAY_SELECT = f"""
      i.date, i.ad_id, i.ad_name, i.campaign_name, i.market, i.account_id,
      CAST(i.spend AS FLOAT64)                    AS spend,
      i.impressions, i.clicks,
      CAST(i.reach AS FLOAT64)                    AS reach,
      CAST(i.video_plays AS FLOAT64)              AS video_plays,
      CAST(i.video_thruplays AS FLOAT64)          AS video_thruplays,
      CAST(i.video_p100 AS FLOAT64)               AS video_p100,
      -- avg_seconds is a per-ROW average; weight it by plays so the aggregate
      -- is seconds ÷ plays and not an average of averages.
      CAST(i.video_avg_seconds_watched AS FLOAT64)
        * CAST(i.video_plays AS FLOAT64)          AS video_seconds,
      {ACT('link_click')}                         AS link_clicks,
      {ACT('landing_page_view')}                  AS lpv,
      {ACT('add_to_cart')}                        AS atc,
      {ACT('initiate_checkout')}                  AS ic,
      {ACT('offsite_conversion.fb_pixel_purchase')} AS purchases,
      {ACT('offsite_conversion.fb_pixel_purchase', 'i.action_values')} AS purchase_value,
      {ACT('video_view')}                         AS video_3s,
      {ACT('post_reaction')}                      AS reactions,
      {ACT('post')}                               AS shares,
      {ACT('onsite_conversion.post_save')}        AS saves,
      {ACT('comment')}                            AS comments,
      x.uploaded, x.effective_status, x.thumbnail_url, x.image_url,
      x.instagram_permalink_url, x.creative_title, x.creative_body,
      x.creative_video_id
"""

DIMS_CTE = f"""
      dims AS (
        SELECT a.ad_id, DATE(a.created_time) AS uploaded, a.effective_status,
               c.thumbnail_url, c.image_url, c.instagram_permalink_url,
               c.title AS creative_title, c.body AS creative_body,
               c.video_id AS creative_video_id
        FROM {ADS} a
        LEFT JOIN {CREA} c USING (creative_id)
      )
"""

# The measures, summed identically wherever an aggregate is taken.
SUMS = """
             SUM(spend)          AS spend,
             SUM(impressions)    AS impressions,
             SUM(clicks)         AS clicks,
             SUM(link_clicks)    AS link_clicks,
             SUM(lpv)            AS lpv,
             SUM(atc)            AS atc,
             SUM(ic)             AS ic,
             SUM(purchases)      AS purchases,
             SUM(purchase_value) AS purchase_value,
             SUM(video_plays)    AS video_plays,
             SUM(video_3s)       AS video_3s,
             SUM(video_thruplays) AS video_thruplays,
             SUM(video_p100)     AS video_p100,
             SUM(video_seconds)  AS video_seconds,
             SUM(reactions)      AS reactions,
             SUM(shares)         AS shares,
             SUM(saves)          AS saves,
             SUM(comments)       AS comments,
             -- daily reach is not summable into period reach; summing it
             -- instead gives Meta's DAILY frequency averaged over the period.
             SUM(NULLIF(reach, 0)) AS sum_reach,
             COUNTIF(spend > 0)  AS live_days
"""

MEASURES = ["spend", "impressions", "clicks", "link_clicks", "lpv", "atc", "ic",
            "purchases", "purchase_value", "video_plays", "video_3s",
            "video_thruplays", "video_p100", "video_seconds", "reactions",
            "shares", "saves", "comments"]


# ── Queries ──────────────────────────────────────────────────────────────────
def per_ad(win: dict) -> list[dict]:
    """One pass over all three periods at ad_id grain.

    An ad-day belongs to more than one period (the previous L7 sits inside
    L28), so the periods are a cross-joined array rather than a CASE — a CASE
    would silently assign each day to exactly one bucket."""
    return q(f"""
      WITH {DIMS_CTE},
      ins AS (
        SELECT {DAY_SELECT}
        FROM {INS} i LEFT JOIN dims x USING (ad_id)
        WHERE i.date BETWEEN @l28_from AND @to
      ),
      d AS (SELECT *, {DERIVE} FROM ins),
      p AS (
        SELECT d.*, period FROM d, UNNEST([
          IF(d.date BETWEEN @start     AND @to,   'cur',  NULL),
          IF(d.date BETWEEN @prev_from AND @prev_to, 'prev', NULL),
          IF(d.date BETWEEN @l28_from  AND @to,   'l28',  NULL)]) AS period
        WHERE period IS NOT NULL
      )
      SELECT period, ad_id,
             ANY_VALUE(mk)                     AS mk,
             ANY_VALUE(ad_name)                AS ad_name,
             ANY_VALUE(campaign_name)          AS campaign_name,
             ANY_VALUE(account_id)             AS account_id,
             ANY_VALUE(tag)                    AS tag,
             ANY_VALUE(media_type)             AS media_type,
             ANY_VALUE(thumbnail_url)          AS thumbnail_url,
             ANY_VALUE(image_url)              AS image_url,
             ANY_VALUE(instagram_permalink_url) AS instagram_permalink_url,
             ANY_VALUE(uploaded)               AS uploaded,
             ANY_VALUE(effective_status)       AS effective_status,
             LOGICAL_OR(REGEXP_CONTAINS(
               CONCAT(IFNULL(ad_name,''), ' ', IFNULL(campaign_name,''), ' ',
                      IFNULL(creative_title,''), ' ', IFNULL(creative_body,'')),
               r'{PROMO_REGEX}'))              AS promo,
             {SUMS}
      FROM p
      GROUP BY period, ad_id
    """, start=win["from"], to=win["to"], prev_from=win["prev_from"],
         prev_to=win["prev_to"], l28_from=win["l28_from"])


def weekly(win: dict) -> list[dict]:
    """Per-ad 7-day buckets counted from that ad's first spend day.

    Full history, not just the window: the fatigue baseline is the ad's first
    good week, which for a 200-day-old creative is 200 days ago. Restricted to
    ads that actually spent something in L28 so the scan stays small."""
    return q(f"""
      WITH hist AS (
        SELECT i.date, i.ad_id,
               CAST(i.spend AS FLOAT64) AS spend, i.impressions,
               CAST(i.reach AS FLOAT64) AS reach,
               CAST(i.video_plays AS FLOAT64) AS video_plays,
               {ACT('link_click')}      AS link_clicks,
               {ACT('video_view')}      AS video_3s,
               {ACT('offsite_conversion.fb_pixel_purchase')} AS purchases
        FROM {INS} i
        WHERE i.date <= @to
      ),
      live AS (
        SELECT DISTINCT ad_id FROM hist
        WHERE date BETWEEN @l28_from AND @to AND spend > 0
      ),
      h AS (SELECT hist.* FROM hist JOIN live USING (ad_id)),
      fs AS (
        SELECT ad_id, MIN(IF(spend > 0, date, NULL)) AS first_day
        FROM h GROUP BY ad_id
      )
      SELECT h.ad_id, fs.first_day,
             DIV(DATE_DIFF(h.date, fs.first_day, DAY), 7)          AS week,
             SUM(h.spend)        AS spend,
             SUM(h.impressions)  AS impressions,
             SUM(h.link_clicks)  AS link_clicks,
             SUM(h.video_3s)     AS video_3s,
             SUM(h.video_plays)  AS video_plays,
             SUM(h.purchases)    AS purchases,
             SUM(NULLIF(h.reach, 0)) AS sum_reach,
             COUNTIF(h.spend > 0)    AS live_days
      FROM h JOIN fs USING (ad_id)
      WHERE h.date >= fs.first_day
      GROUP BY h.ad_id, fs.first_day, week
      ORDER BY h.ad_id, week
    """, to=win["to"], l28_from=win["l28_from"])


def recent_ids(win: dict) -> list[dict]:
    """Ads created inside the L7 window, minus re-uploads.

    The `prior` anti-join is what makes this panel mean "new work": without it
    four re-uploads of last month's concepts into the new -shoes adset crowd
    the genuinely new batch out of the top 5."""
    return q(f"""
      WITH prior AS (
        SELECT DISTINCT ad_name FROM {ADS} WHERE DATE(created_time) < @start
      )
      SELECT ad_id, ad_name, DATE(created_time) AS uploaded, effective_status
      FROM {ADS}
      WHERE DATE(created_time) BETWEEN @start AND @to
        AND ad_name NOT IN (SELECT ad_name FROM prior)
    """, start=win["from"], to=win["to"])


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


def _metrics(a: dict) -> dict:
    """Every ratio this tab shows, computed once from a bag of sums.

    `a` is any aggregate — one ad, one market, one tag, the whole account. The
    inputs are sums; nothing here averages a ratio."""
    impr  = _f(a.get("impressions"))
    lc    = _f(a.get("link_clicks"))
    lpv   = _f(a.get("lpv"))
    plays = _f(a.get("video_plays"))
    spend = _f(a.get("spend"))
    social = (_f(a.get("reactions")) + _f(a.get("shares"))
              + _f(a.get("saves")) + _f(a.get("comments")))
    return {
        "spend": F(spend),
        "impressions": I(impr),
        "clicks": I(a.get("clicks")),
        "link_clicks": I(lc),
        "lpv": I(lpv),
        "add_to_cart": I(a.get("atc")),
        "initiate_checkout": I(a.get("ic")),
        "purchases": I(a.get("purchases")),
        "purchase_value": F(a.get("purchase_value")),
        "ctr": div(a.get("clicks"), impr, 100.0),
        "link_ctr": div(lc, impr, 100.0),
        "cpm": div(spend, impr, 1000.0),
        "cpc": div(spend, lc),
        "cpa": div(spend, a.get("purchases")),
        "roas": div(a.get("purchase_value"), spend),
        "lpv_rate": div(lpv, lc, 100.0),
        "cost_per_lpv": div(spend, lpv),
        "atc_rate": div(a.get("atc"), lpv, 100.0),
        "purchase_rate": div(a.get("purchases"), lpv, 100.0),
        "video_plays": I(plays),
        "hook_rate": div(a.get("video_3s"), impr, 100.0),
        "hold_rate": div(a.get("video_thruplays"), plays, 100.0),
        "completion": div(a.get("video_p100"), plays, 100.0),
        "avg_seconds": div(a.get("video_seconds"), plays, 1.0, 1),
        "reactions": I(a.get("reactions")),
        "shares": I(a.get("shares")),
        "saves": I(a.get("saves")),
        "comments": I(a.get("comments")),
        "social_per_1k": div(social, impr, 1000.0),
        "saves_shares_per_1k": div(_f(a.get("saves")) + _f(a.get("shares")),
                                   impr, 1000.0),
    }


def _sum_rows(rows: list[dict]) -> dict:
    """Add up the raw measures of many ads. Reach is deliberately absent: an
    average of daily reach across different ads means nothing, so the frequency
    proxy exists per ad only."""
    out = {m: sum(_f(r.get(m)) for r in rows) for m in MEASURES}
    out["ads"] = len(rows)
    return out


def _freq(row: dict) -> float | None:
    """Average DAILY frequency — impressions ÷ sum of daily reach, i.e. the
    impression-weighted mean of Meta's own daily frequency. Period-level
    (7-day) frequency is not in the feed."""
    return div(row.get("impressions"), row.get("sum_reach"), 1.0, 2)


def _is_video(row: dict) -> bool:
    return _f(row.get("video_plays")) > 0


def _hook_kpi(rows: list[dict]) -> float | None:
    """Blended hook rate over VIDEO ads only. Dividing 3-second views by the
    impressions of still images too would just measure the video share."""
    vids = [r for r in rows if _is_video(r)]
    if not vids:
        return None
    return div(sum(_f(r.get("video_3s")) for r in vids),
               sum(_f(r.get("impressions")) for r in vids), 100.0)


def _kpis(rows: list[dict]) -> dict:
    k = _metrics(_sum_rows(rows))
    k["hook_rate"] = _hook_kpi(rows)   # video-ad denominator, see _hook_kpi
    k["ads"] = len(rows)
    return k


# Lower is better for CPA, CPM and cost per LPV; for everything else more is
# better. The tab only needs to know which way to colour the delta pill.
_LOWER_IS_BETTER = {"cpa", "cpm", "cpc", "cost_per_lpv"}
_DELTA_KEYS = ("spend", "purchases", "cpa", "ctr", "cpm", "roas",
               "link_ctr", "hook_rate", "lpv_rate")

def _with_deltas(cur: dict, prev: dict) -> dict:
    out = dict(cur)
    out["prev"] = prev
    out["delta_pct"] = {
        k: div((cur.get(k) or 0) - (prev.get(k) or 0), prev.get(k), 100.0, 1)
        for k in _DELTA_KEYS
    }
    out["lower_is_better"] = sorted(_LOWER_IS_BETTER)
    return out


def _mix(rows: list[dict], key: str) -> list[dict]:
    total = sum(_f(r.get("spend")) for r in rows)
    buckets: dict[str, float] = {}
    for r in rows:
        buckets[r[key]] = buckets.get(r[key], 0.0) + _f(r.get("spend"))
    return [{"label": k, "spend": F(v), "pct": div(v, total, 100.0, 1)}
            for k, v in sorted(buckets.items(), key=lambda kv: -kv[1])]


def _promo(row: dict) -> bool:
    """SQL flags it, PROMO_OVERRIDES has the last word."""
    ov = PROMO_OVERRIDES.get(str(row.get("ad_id")))
    return bool(row.get("promo")) if ov is None else bool(ov)


def _creative(r: dict, account_id: str, **extra) -> dict:
    """The one creative shape the tab renders, wherever a row comes from."""
    ad_id = str(r["ad_id"])
    uploaded = r.get("uploaded")
    out = {
        "ad_id":    ad_id,
        "ad_name":  r.get("ad_name"),
        "display_name": _display_name(r.get("ad_name")),
        "tag":      r.get("tag"),
        "media_type": r.get("media_type"),
        "market":   r.get("mk"),
        "promo":    _promo(r),
        "uploaded": uploaded.isoformat() if hasattr(uploaded, "isoformat") else uploaded,
        "effective_status": r.get("effective_status"),
        "thumbnail_url": r.get("thumbnail_url"),
        # Left null when Meta has no full-size still (the overwhelming
        # majority, and every video): the page falls back to the thumbnail, so
        # copying the URL twice per row would only inflate the document.
        "image_url": r.get("image_url"),
        "instagram_url": r.get("instagram_permalink_url"),
        "ads_manager_url": ADS_MANAGER_URL.format(account_id=account_id, ad_id=ad_id),
    }
    out.update(_metrics(r))
    out.update(extra)
    return out


def _slim(c: dict, value_key: str) -> dict:
    """Leaderboard cards show a thumbnail, a name, one number and the spend
    behind it — carrying the full 40-field creative five times over in eight
    cards would triple the document for nothing."""
    return {
        "ad_id": c["ad_id"], "ad_name": c["ad_name"],
        "display_name": c["display_name"], "tag": c["tag"],
        "media_type": c["media_type"], "market": c["market"], "promo": c["promo"],
        "thumbnail_url": c["thumbnail_url"], "image_url": c["image_url"],
        "instagram_url": c["instagram_url"], "ads_manager_url": c["ads_manager_url"],
        "value": c.get(value_key), "spend": c["spend"], "purchases": c["purchases"],
    }


# ── Fatigue ──────────────────────────────────────────────────────────────────
def _index(cur, base) -> float | None:
    """current ÷ baseline, but only when BOTH sides exist.

    div() treats a missing numerator as zero, which for an index would print a
    total collapse for an ad that simply had no purchases this week — an
    undefined index has to stay undefined."""
    if cur is None or base in (None, 0):
        return None
    return round(float(cur) / float(base), 3)


def _week_metrics(w: dict) -> dict:
    return {
        "week": int(w["week"]),
        "spend": F(w.get("spend")),
        "link_ctr": div(w.get("link_clicks"), w.get("impressions"), 100.0),
        "hook_rate": div(w.get("video_3s"), w.get("impressions"), 100.0),
        "cpa": div(w.get("spend"), w.get("purchases")),
        "freq": div(w.get("impressions"), w.get("sum_reach"), 1.0, 2),
    }


def _baseline(weeks: list[dict], end: datetime.date) -> dict | None:
    """The ad's first FULL week over the spend floor. Full matters: week 0 of an
    ad launched three days ago is a three-day week, and comparing a seven-day
    current window against it would invent a collapse. "Full" is decided here
    rather than in SQL because the bucket boundary is a function of the ad's
    own first spend day, not of the calendar."""
    for w in weeks:
        last_day = w["first_day"] + datetime.timedelta(days=int(w["week"]) * 7 + 6)
        if last_day <= end and _f(w.get("spend")) >= BASELINE_MIN_SPEND:
            return w
    return None


def _classify(index: float | None, freq_ratio: float | None,
              age_days: int, days_active: int) -> str:
    if age_days < FRESH_DAYS:
        return STATUS_FRESH
    if index is None:
        return STATUS_NO_BASE
    if index < EXHAUSTED_INDEX and days_active >= EXHAUSTED_DAYS:
        return STATUS_EXHAUSTED
    if freq_ratio is not None and freq_ratio >= FREQ_RISE:
        return STATUS_FATIGUING
    if index >= STABLE_INDEX:
        return STATUS_STABLE
    return STATUS_FATIGUING


def _fatigue_row(cur: dict, weeks: list[dict], win: dict, account_id: str) -> dict:
    """One ad's fatigue verdict, plus the weekly series the sparkline draws."""
    end = datetime.date.fromisoformat(win["to"])
    first = weeks[0]["first_day"] if weeks else None
    age_days = (end - first).days + 1 if first else 0
    days_active = sum(int(w.get("live_days") or 0) for w in weeks)

    base = _baseline(weeks, end)
    bm = _week_metrics(base) if base else None
    cm = _metrics(cur)
    cur_freq = _freq(cur)
    video = _is_video(cur)

    ctr_index  = _index(cm["link_ctr"], bm["link_ctr"]) if bm else None
    hook_index = _index(cm["hook_rate"], bm["hook_rate"]) if bm and video else None
    cpa_index  = _index(cm["cpa"], bm["cpa"]) if bm else None
    freq_ratio = (round(cur_freq / bm["freq"], 2)
                  if bm and bm.get("freq") and cur_freq else None)

    # Video is judged on the hook (does the thumbstop still work), everything
    # else on the link CTR. Fall back to the CTR index when a video ad has no
    # usable hook baseline.
    index = hook_index if hook_index is not None else ctr_index
    status = _classify(index, freq_ratio, age_days, days_active)

    series = [_week_metrics(w) for w in weeks if _f(w.get("impressions")) > 0]
    # Long-running creatives blow the document budget; keep the most recent
    # WEEKS_KEPT buckets — the baseline is already resolved above, so trimming
    # the head costs the sparkline's left edge and nothing else.
    trimmed = len(series) > WEEKS_KEPT
    series = series[-WEEKS_KEPT:]

    return _creative(
        cur, account_id,
        first_spend=first.isoformat() if first else None,
        days_active=days_active,
        days_since_first_spend=age_days,
        days_since_upload=((end - cur["uploaded"]).days
                           if cur.get("uploaded") else None),
        frequency=cur_freq,
        is_video=video,
        status=status,
        status_rank=STATUS_ORDER.index(status),
        baseline_week=(int(base["week"]) if base else None),
        baseline=bm,
        ctr_index=ctr_index,
        hook_index=hook_index,
        cpa_index=cpa_index,
        freq_ratio=freq_ratio,
        weeks=series,
        weeks_trimmed=trimmed,
    )


# What a fatigue-board row keeps. The full creative shape carries every ratio
# this job can compute; at ~80 rows that tripled the document for columns the
# board does not render and the CSV export cannot reach.
BOARD_KEYS = (
    "ad_id", "ad_name", "display_name", "tag", "media_type", "market", "promo",
    "uploaded", "effective_status", "thumbnail_url", "image_url",
    "instagram_url", "ads_manager_url",
    "spend", "impressions", "purchases", "link_ctr", "cpa", "roas",
    "lpv_rate", "hook_rate", "hold_rate", "saves_shares_per_1k",
    "frequency", "is_video", "status", "status_rank",
    "first_spend", "days_active", "days_since_first_spend", "days_since_upload",
    "ctr_index", "hook_index", "cpa_index", "freq_ratio",
    "baseline", "baseline_week", "weeks", "weeks_trimmed",
)


def _fatigue(cur_rows: list[dict], weeks_by_ad: dict, win: dict,
             account_id: str) -> dict:
    rows = [{k: c[k] for k in BOARD_KEYS if k in c}
            for c in (_fatigue_row(r, weeks_by_ad.get(str(r["ad_id"]), []),
                                   win, account_id)
                      for r in cur_rows if _f(r.get("spend")) > 0)]
    # Default order = worst money first: status severity, then spend.
    rows.sort(key=lambda c: (c["status_rank"], -_f(c["spend"])))

    total = sum(_f(c["spend"]) for c in rows)
    by_status = []
    for s in STATUS_ORDER:
        grp = [c for c in rows if c["status"] == s]
        if not grp:
            continue
        sp = sum(_f(c["spend"]) for c in grp)
        by_status.append({"status": s, "ads": len(grp), "spend": F(sp),
                          "pct": div(sp, total, 100.0, 1)})
    at_risk = sum(_f(c["spend"]) for c in rows if c["status"] in STATUS_AT_RISK)
    return {
        "rows": rows,
        "by_status": by_status,
        "total_spend": F(total),
        "at_risk_spend": F(at_risk),
        "at_risk_pct": div(at_risk, total, 100.0, 1),
        "thresholds": {
            "baseline_min_spend": BASELINE_MIN_SPEND,
            "fresh_days": FRESH_DAYS,
            "stable_index": STABLE_INDEX,
            "exhausted_index": EXHAUSTED_INDEX,
            "exhausted_days": EXHAUSTED_DAYS,
            "freq_rise": FREQ_RISE,
            "weeks_kept": WEEKS_KEPT,
        },
    }


# ── Leaderboards ─────────────────────────────────────────────────────────────
# (payload key, card title, metric, direction, extra row filter)
LEADERBOARDS = [
    ("hook_rate",  "Best hook rate",      "hook_rate",  "desc", "video"),
    ("hold_rate",  "Best hold rate",      "hold_rate",  "desc", "video"),
    ("link_ctr",   "Best link CTR",       "link_ctr",   "desc", None),
    ("cpa",        "Best CPA",            "cpa",        "asc",  "purchases"),
    ("lpv_rate",   "Best click quality",  "lpv_rate",   "desc", None),
    ("social",     "Most saved + shared", "saves_shares_per_1k", "desc", None),
]


def _leaderboards(l28: list[dict], fatigue_rows: list[dict],
                  account_id: str) -> dict:
    elig = [r for r in l28
            if r["media_type"] != "DPA / catalog"
            and _f(r.get("spend")) >= MIN_SPEND_L28]
    cards: dict[str, dict] = {}
    for key, title, metric, direction, need in LEADERBOARDS:
        pool = elig
        if need == "video":
            pool = [r for r in pool if _is_video(r)]
        elif need == "purchases":
            pool = [r for r in pool if _f(r.get("purchases")) > 0]
        ranked = [_creative(r, account_id) for r in pool]
        ranked = [c for c in ranked if c.get(metric) is not None]
        ranked.sort(key=lambda c: c[metric], reverse=(direction == "desc"))
        cards[key] = {
            "title": title, "metric": metric, "direction": direction,
            "rows": [_slim(c, metric) for c in ranked[:LEADER_N]],
        }

    # Rising — new work that is already converting. Ranked on purchases, not on
    # a rate: a three-day-old ad's rates are noise, its orders are not.
    by_id = {c["ad_id"]: c for c in fatigue_rows}
    rising = []
    # No spend floor here: a three-day-old ad has not had time to spend 1 000 kr.
    for r in l28:
        c = by_id.get(str(r["ad_id"]))
        if not c or c.get("days_since_first_spend") is None:
            continue
        if c["days_since_first_spend"] > RISING_DAYS:
            continue
        m = _creative(r, account_id)
        m["days_since_first_spend"] = c["days_since_first_spend"]
        rising.append(m)
    rising.sort(key=lambda c: (-_f(c["purchases"]), _f(c["spend"])))
    cards["rising"] = {
        "title": "Rising", "metric": "purchases", "direction": "desc",
        "rows": [dict(_slim(c, "purchases"),
                      days_since_first_spend=c["days_since_first_spend"])
                 for c in rising[:LEADER_N]],
    }

    # Fatiguing now — the money to move, ranked by L7 spend.
    at_risk = [c for c in fatigue_rows if c["status"] in STATUS_AT_RISK]
    at_risk.sort(key=lambda c: -_f(c["spend"]))
    cards["fatiguing_now"] = {
        "title": "Fatiguing now", "metric": "spend", "direction": "desc",
        "rows": [dict(_slim(c, "spend"), status=c["status"],
                      ctr_index=c["ctr_index"], hook_index=c["hook_index"])
                 for c in at_risk[:LEADER_N]],
    }
    return cards


# ── Rollups ──────────────────────────────────────────────────────────────────
def _rollup(rows: list[dict], keyfn, label="label") -> list[dict]:
    """Ratio-of-sums by any grouping, with the ad count so a reader can see
    when a row is one creative pretending to be a pattern."""
    total = sum(_f(r.get("spend")) for r in rows)
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(keyfn(r), []).append(r)
    out = []
    for k, grp in groups.items():
        m = _metrics(_sum_rows(grp))
        m[label] = k
        m["ads"] = len(grp)
        m["share"] = div(m["spend"], total, 100.0, 1)
        m["hook_rate"] = _hook_kpi(grp)
        out.append(m)
    out.sort(key=lambda r: -_f(r["spend"]))
    return out


def _rollups(l28: list[dict]) -> dict:
    body = [r for r in l28 if r["media_type"] != "DPA / catalog"]
    crossed = _rollup(body, lambda r: f"{r['tag']} · {r['media_type']}")
    # A crossed table is only worth showing while its cells hold real spend;
    # below the L28 floor it is a list of one-ad curiosities.
    crossed = [r for r in crossed if _f(r["spend"]) >= MIN_SPEND_L28]
    return {
        "by_tag": _rollup(l28, lambda r: r["tag"]),
        "by_media_type": _rollup(l28, lambda r: r["media_type"]),
        "by_promo": _rollup(l28, lambda r: "Promo" if _promo(r) else "Evergreen"),
        "by_tag_media": crossed,
    }


def _window() -> dict:
    end = os.environ.get("META_END_DATE")
    end_d = (datetime.date.fromisoformat(end) if end
             else datetime.date.today() - datetime.timedelta(days=SETTLE_DAYS))
    start_d = end_d - datetime.timedelta(days=WINDOW_DAYS - 1)
    return {
        "from": start_d.isoformat(), "to": end_d.isoformat(),
        "prev_from": (start_d - datetime.timedelta(days=WINDOW_DAYS)).isoformat(),
        "prev_to":   (end_d - datetime.timedelta(days=WINDOW_DAYS)).isoformat(),
        "l28_from":  (end_d - datetime.timedelta(days=LONG_DAYS - 1)).isoformat(),
        "l28_to":    end_d.isoformat(),
        "days": WINDOW_DAYS,
        "long_days": LONG_DAYS,
    }


def build_payload() -> dict:
    win = _window()
    rows = per_ad(win)

    def period(p):
        return [r for r in rows if r["period"] == p and r["mk"] in MARKETS]

    cur, prev, l28 = period("cur"), period("prev"), period("l28")
    account_id = next((str(r["account_id"]) for r in rows if r.get("account_id")), "")

    kpis = {"combined": _with_deltas(_kpis(cur), _kpis(prev))}
    for m in MARKETS:
        kpis[m] = _with_deltas(_kpis([r for r in cur if r["mk"] == m]),
                               _kpis([r for r in prev if r["mk"] == m]))

    weeks_by_ad: dict[str, list[dict]] = {}
    for w in weekly(win):
        weeks_by_ad.setdefault(str(w["ad_id"]), []).append(w)

    fatigue = _fatigue(cur, weeks_by_ad, win, account_id)
    leaderboards = _leaderboards(l28, fatigue["rows"], account_id)
    rollups = _rollups(l28)

    # Top 5 per market: lowest CPA over a spend floor, DPA excluded — a
    # catalogue ad is not a creative anyone can iterate on.
    top: dict[str, list[dict]] = {}
    for m in MARKETS:
        elig = [r for r in cur
                if r["mk"] == m
                and r["media_type"] != "DPA / catalog"
                and _f(r.get("spend")) >= MIN_SPEND_L7
                and I(r.get("purchases")) > 0]
        elig.sort(key=lambda r: _f(r["spend"]) / I(r["purchases"]))
        top[m] = [_creative(r, account_id) for r in elig[:TOP_N]]

    # Recently uploaded: created in the window, a genuinely new concept, and it
    # delivered. Metrics come from the same `cur` rows everything else uses.
    fat_by_id = {c["ad_id"]: c for c in fatigue["rows"]}
    cur_by_id = {str(r["ad_id"]): r for r in cur}
    recent = []
    for a in recent_ids(win):
        r = cur_by_id.get(str(a["ad_id"]))
        if not r or _f(r.get("spend")) <= 0:
            continue
        c = _creative(r, account_id)
        f = fat_by_id.get(c["ad_id"], {})
        c["first_spend"] = f.get("first_spend")
        c["days_active"] = f.get("days_active")
        recent.append(c)
    recent.sort(key=lambda c: (-_f(c["purchases"]), _f(c["spend"])))
    recent = recent[:RECENT_N]

    ig_ads = sum(1 for r in cur if r.get("instagram_permalink_url"))
    ig_spend = sum(_f(r["spend"]) for r in cur if r.get("instagram_permalink_url"))

    payload = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "sources": {
            "insights": f"{DATA_PROJECT}.{STAGING_DATASET}.stg_meta__ads_insights",
            "ads":      f"{DATA_PROJECT}.{STAGING_DATASET}.stg_meta__ads",
            "creatives": f"{DATA_PROJECT}.{STAGING_DATASET}.stg_meta__ad_creatives",
            "marts": f"{DATA_PROJECT}.{MARTS_DATASET}.agg_daily_kpis_by_ad",
            "account_id": account_id,
            "account_label": "Babyshop SE — New",
            "attribution": "7d_click,1d_view",
            "markets": MARKETS,
            "min_spend": MIN_SPEND_L7,
            "min_spend_l28": MIN_SPEND_L28,
            "images": "Meta CDN signed URLs from the creative record "
                      "(expire ~4 days after minting)",
            "instagram_coverage": {
                "ads": ig_ads, "ads_total": len(cur),
                "ads_pct": div(ig_ads, len(cur), 100.0, 1),
                "spend_pct": div(ig_spend, sum(_f(r["spend"]) for r in cur), 100.0, 1),
            },
        },
        "window": win,
        "kpis": kpis,
        "media_types": _mix(cur, "media_type"),
        "media_formats": _mix(cur, "tag"),
        "fatigue": fatigue,
        "leaderboards": leaderboards,
        "rollups": rollups,
        "top_creatives": top,
        "recent": recent,
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
            + ". Lower META_WEEKS_KEPT / META_LEADER_N / META_TOP_N — do not "
              "just raise the budget.")
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
    fat = p.get("fatigue", {})
    dist = " / ".join(f"{b['status']} {b['ads']}" for b in fat.get("by_status", []))
    print(f"✓ Meta refresh · {where} · {w['from']}..{w['to']} · "
          f"spend {k['spend']:,.0f} kr / {k['purchases']} purchases / CPA {k['cpa']} / "
          f"ROAS {k['roas']}x · link CTR {k['link_ctr']}% · "
          f"fatigue {len(fat.get('rows', []))} ads [{dist}], "
          f"{fat.get('at_risk_pct')}% of spend at risk · "
          f"{len(json.dumps(p, ensure_ascii=False)):,} B · {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
