#!/usr/bin/env python3
"""
Meta creatives snapshot — Bluebird warehouse (cross-project) → Firestore.

Writes the documents the "Meta creatives" tab reads. Same client/auth wiring
and the same {data, fetched_at, expires_at, ttl_seconds, workspace} wrapper as
refresh_bundles.py / refresh_segments.py.

MARKET × PERIOD FILTERS (v4)
  The tab is a nightly snapshot and must stay one Firestore read per view, so
  every combination the filter bar can select is PRECOMPUTED here rather than
  queried on demand:

    markets  all / SE / NO          periods  7 / 14 / 28 / 90 days

  12 documents:
    funnel_cache/<ws>__meta                  — the default (all, 7 d), kept at
                                               the legacy id for compatibility
    funnel_cache/<ws>__meta__<market>_<days> — the other 11

  BigQuery is queried ONCE, at ad-day grain, over the longest span any
  combination needs (90 d + its 90 d comparison = 180 days) plus the per-ad
  weekly history the fatigue baselines need. Every window is then summed in
  Python. Adding a period costs no extra query.

  Window semantics per period, so the 7-day preset behaves exactly as v3 did:
    7 d   current = L7,  leaderboards / rollups = L28
    14/28/90 d  current = leaderboards = rollups = the selected period
  The relevance floors scale with the window length (300 kr over 7 days is
  1 200 kr over 28), so "minimum spend" always means the same RATE of spend.

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
  # query the warehouse as the kuvio account, dump ALL 12 payloads to one file,
  # write nothing
  CLOUDSDK_CORE_ACCOUNT=patrik@kuvio.io META_AUTH=gcloud \\
    META_BQ_BILLING_PROJECT=claude-private-499703 \\
    SKIP_FIRESTORE=1 META_OUT=/tmp/meta.json python3 refresh_meta.py

  # write that bundle to Firestore as the gmail account, no BigQuery at all
  META_IN=/tmp/meta.json python3 refresh_meta.py
"""
from __future__ import annotations
import datetime, json, os, re, time
from google.cloud import bigquery

# The warehouse holding the Meta data (the NEW stack's project) …
DATA_PROJECT   = os.environ.get("META_DATA_PROJECT", "claude-private-499703")
MARTS_DATASET  = os.environ.get("META_MARTS_DATASET", "babyshop_marts")
STAGING_DATASET = os.environ.get("META_STAGING_DATASET", "babyshop_staging")
# The connector's landing dataset. Nothing is read from it in the normal case —
# the staging views already resolve against it — but it is where a newly loaded
# Meta field appears FIRST, before any dbt model exposes it (see preview_source).
RAW_DATASET    = os.environ.get("META_RAW_DATASET", "babyshop_raw")
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

# ── Image mirror ─────────────────────────────────────────────────────────────
# Meta hands out two kinds of picture and only one of them is worth showing:
#   • creative.thumbnail_url — 64x64, ALWAYS present, and the size is signed
#     into the URL (stp=…p64x64_q75…). Rewriting it to p640x640, or stripping
#     stp= altogether, returns "URL signature mismatch" 403. Verified again
#     2026-09-07: 64px really is all this URL will ever give.
#   • a full-size still — creative.image_url, or the video poster at
#     object_story_spec.video_data.image_url (1080x1920 on this account), or
#     link_data.picture. Present on some creatives, absent on many.
# Both expire ~4 days after minting, so neither can be linked from a snapshot
# that outlives a run. This job therefore MIRRORS the best available source
# into its own Firestore collection, one document per ad, and the tab reads
# GET /api/meta/image/<ad_id>. The snapshot itself stays small: it carries the
# source NAME, never the bytes.
IMAGES_COLLECTION  = "meta_creative_images"
IMAGE_TTL          = int(os.environ.get("META_IMAGE_TTL_DAYS", "30")) * 24 * 3600
IMAGE_LARGE_PX     = int(os.environ.get("META_IMAGE_LARGE_PX", "640"))
IMAGE_THUMB_PX     = int(os.environ.get("META_IMAGE_THUMB_PX", "192"))
IMAGE_QUALITY      = int(os.environ.get("META_IMAGE_QUALITY", "82"))
# A doc whose source is already this good and was fetched inside this many days
# is left alone, so the nightly run re-fetches almost nothing.
IMAGE_REFRESH_DAYS = int(os.environ.get("META_IMAGE_REFRESH_DAYS", "14"))
IMAGE_TIMEOUT      = float(os.environ.get("META_IMAGE_TIMEOUT", "10"))
IMAGE_WORKERS      = int(os.environ.get("META_IMAGE_WORKERS", "8"))
# 0 = every ad in the payload. Set META_IMAGE_ADS=15 for a local run so the
# mirror exercises the real code path without pulling ~90 images.
IMAGE_ADS_CAP      = int(os.environ.get("META_IMAGE_ADS", "0"))
# The production ceiling. Only ads that SPENT inside the longest window (90 d)
# are mirrored at all, and at most this many of them, highest 90-day spend
# first — an account that suddenly ships 2 000 creatives must not turn one
# nightly run into a 2 000-image download.
IMAGE_MAX          = int(os.environ.get("META_IMAGE_MAX", "300"))
SKIP_IMAGES        = os.environ.get("META_SKIP_IMAGES") == "1"
# Best first. A doc is re-fetched as soon as a BETTER source becomes available
# (a 'thumbnail' entry is replaced the day a poster or an HD thumbnail appears)
# and is NEVER overwritten by a worse one, however stale it is.
#
# thumbnail_hd is the same creative.thumbnail_url field, sized differently: the
# size is signed into the URL (stp=…p64x64… vs …p1080x1080…), so the rendition
# is knowable from the string without fetching it. The connector was switched
# to request thumbnail_width/height=1200 on the adcreatives edge, which turns
# most of this account's 64 px squares into 1080 px ones; a URL that still
# carries p64x64/s64x64 is genuinely all Meta will give for that ad.
IMAGE_SOURCE_RANK  = {"image_url": 5, "video_poster": 4, "thumbnail_hd": 3,
                      "link_picture": 2, "thumbnail": 1}
IMAGE_SOURCE_LABEL = {
    "image_url":    "full-size",
    "video_poster": "video poster",
    "thumbnail_hd": "creative thumbnail, full size",
    "link_picture": "link preview image",
    "thumbnail":    "64 px thumbnail — Meta does not expose a larger one for this ad",
}
# A thumbnail_url carrying one of these is the 64 px rendition. Anything else
# is whatever size the connector asked the adcreatives edge for.
THUMB_SMALL_RE = re.compile(r"[ps]64x64")
IMAGE_UA = ("Mozilla/5.0 (compatible; babyshop-dashboard meta-refresh; "
            "+https://github.com/patriksegersven-pixel/takk-signs)")

WINDOW_DAYS  = int(os.environ.get("META_WINDOW_DAYS", "7"))
LONG_DAYS    = int(os.environ.get("META_LONG_DAYS", "28"))
SETTLE_DAYS  = int(os.environ.get("META_SETTLE_DAYS", "2"))
MARKETS      = ["SE", "NO"]

# ── The filter grid ──────────────────────────────────────────────────────────
# Every combination is precomputed into its own Firestore document. "all" is
# SE+NO, i.e. exactly what v3 showed.
MARKET_OPTIONS = ["all"] + MARKETS
PERIOD_OPTIONS = [int(x) for x in
                  os.environ.get("META_PERIODS", "7,14,28,90").split(",") if x.strip()]
DEFAULT_MARKET = "all"
DEFAULT_DAYS   = WINDOW_DAYS
# The BigQuery scan has to cover the longest period AND its equally long
# comparison period: 90 + 90 = 180 days back from the settled end date.
MAX_DAYS       = max(PERIOD_OPTIONS)


def combo_key(market: str, days: int) -> str:
    """The Firestore document suffix for one combination.

    The default combination keeps the bare `__meta` id it has had since v1, so
    an older client (or a cached page) that asks for no filters still lands on
    a real document."""
    if market == DEFAULT_MARKET and days == DEFAULT_DAYS:
        return DOC_KEY
    return f"{DOC_KEY}__{market}_{days}"

# ── Relevance floors ─────────────────────────────────────────────────────────
# Nothing is ranked below these. They are shown in the UI rather than applied
# silently: a leaderboard whose floor the reader cannot see is a leaderboard
# the reader cannot argue with.
MIN_SPEND_L7  = float(os.environ.get("META_MIN_SPEND", "300"))     # L7 rankings
MIN_SPEND_L28 = float(os.environ.get("META_MIN_SPEND_L28", "1000"))  # L28 rankings


def floor_short(days: int) -> float:
    """The current-window floor, scaled to the selected period.

    A fixed 300 kr means "spent enough to be worth ranking" over 7 days and
    almost nothing over 90. Scaling keeps the floor a RATE of spend, so the
    top-5 tables mean the same thing at every period."""
    return round(MIN_SPEND_L7 * days / WINDOW_DAYS)


def floor_long(long_days: int) -> float:
    """Same, for the leaderboard / rollup window."""
    return round(MIN_SPEND_L28 * long_days / LONG_DAYS)

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
    "creative images are MIRRORED by this job — the Meta CDN URLs are signed "
    "and expire about 4 days after minting, so the tab serves its own copies "
    "from /api/meta/image/<ad_id> rather than hotlinking a URL that will die",
    "the source is the best rendition Meta exposes per ad, in order: the "
    "creative's full-size image_url, the video poster in "
    "object_story_spec.video_data.image_url, link_data.picture, then the 64x64 "
    "thumbnail_url. For page-post shares the 64px thumbnail is genuinely all "
    "there is: the size is signed into the URL, so asking for a larger "
    "rendition returns 403 — the lightbox says so per creative",
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
    "the market and period controls do not query anything: every combination "
    "is precomputed by this job into its own snapshot, so switching them reads "
    "one document and nothing is recalculated in the browser",
    "the relevance floors scale with the selected period — 300 kr over 7 days "
    "is 1 200 kr over 28 — so a ranking means the same thing at every period; "
    "the figure in force is printed under each table",
    "index columns in the effectiveness tables compare a group against the "
    "ACCOUNT AVERAGE for the same window and the same table, not against the "
    "ad's own history (that is what the fatigue indices do)",
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
    "top5":      "per market, ranked by lowest CPA over the selected window, "
                 "minimum " + str(int(MIN_SPEND_L7)) + " kr spend per 7 days of "
                 "window, DPA / catalog excluded",
    "leaderboards": "the long window (28 days on the 7-day preset, otherwise the "
                    "selected period), minimum " + str(int(MIN_SPEND_L28))
                    + " kr spend per 28 days of window, DPA / catalog excluded",
    "cpa_index_acct": "account average CPA ÷ the group's CPA over the same "
                      "window and the same table. Above 1.00 means the group "
                      "buys purchases more cheaply than the account does; the "
                      "column prints the deviation from 1.00 as a signed "
                      "percentage so every index reads 'better' upwards",
    "roas_index_acct": "the group's ROAS ÷ the account average ROAS, same window",
    "ctr_index_acct":  "the group's link CTR ÷ the account average link CTR",
    "hook_index_acct": "the group's hook rate ÷ the account average hook rate "
                       "over VIDEO ads only, on both sides of the ratio",
    "filters":   "market (all / SE / NO) and period (7 / 14 / 28 / 90 days) are "
                 "precomputed combinations, not live queries; the window shown "
                 "beside the controls is the exact date range in force",
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
# Raw only — read by preview_source() and by nothing else.
RAW_ADS = f"`{DATA_PROJECT}.{RAW_DATASET}.ads_native`"


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

# The per-day MEASURE projection. Dimensions are deliberately not here: they
# are constant per ad and would repeat ~30 times per ad across the 180-day
# scan, thumbnail URLs included.
DAY_SELECT = f"""
      i.date, i.ad_id,
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
      {ACT('comment')}                            AS comments
"""

# object_story_spec is the only place a full-size still lives for most of this
# account: image_url is populated on a handful of creatives, but every VIDEO
# creative carries a 1080x1920 poster at $.video_data.image_url. link_data.picture
# is the same idea for link ads. Page-post SHARE creatives carry an (almost)
# empty story spec and no image_hash URL at all — for those the 64px thumbnail
# is genuinely the only rendition the connector exposes.

# ── preview_shareable_link ───────────────────────────────────────────────────
# Meta's shareable ad preview (a facebook.com/ads/… URL that renders the ad as
# it appears in feed) is NOT in this feed today: neither stg_meta__ads nor the
# raw ads_native carries a column of that name — verified against
# INFORMATION_SCHEMA on 2026-09-08. Henrik has been asked for it; it needs a
# Facebook login to open, which is fine for the agency and the marketing team.
#
# Rather than hard-code its absence, the job LOOKS for it every run, in BOTH
# places it could appear, and passes it through the moment the connector starts
# loading it:
#   1. babyshop_staging.stg_meta__ads — where it belongs once the dbt model on
#      the new stack exposes it;
#   2. babyshop_raw.ads_native — where the connector lands it FIRST. Checking
#      raw too means the tab lights up on the next nightly run instead of
#      waiting on a dbt release on the other stack.
# Neither carries it today (verified against INFORMATION_SCHEMA 2026-09-08).
# A null `preview_url` is the only thing the tab sees until one of them does.
PREVIEW_COLUMN = "preview_shareable_link"
# (select expression, extra CTE text prepended to dims). None until probed.
_preview: tuple[str, str] | None = None


def _has_column(dataset: str, table: str, column: str) -> bool:
    return bool(q(f"""
      SELECT 1
      FROM `{DATA_PROJECT}.{dataset}.INFORMATION_SCHEMA.COLUMNS`
      WHERE table_name = '{table}' AND column_name = '{column}'
    """))


def preview_source() -> tuple[str, str]:
    """Where preview_url comes from this run: (expression, extra CTE).

    Probed once per process, and never fatal: if the probe itself fails the job
    carries on with no preview links rather than losing the whole snapshot over
    a cosmetic field.

    The raw branch has to de-duplicate. `ads_native` is Airbyte's landing
    table — one row per ad PER SYNC — so joining it straight onto dims would
    fan every ad out into as many rows as it has been synced, and silently
    multiply every creative in the payload. One newest non-null value per ad
    is taken instead."""
    global _preview
    if _preview is not None:
        return _preview
    _preview = ("CAST(NULL AS STRING)", "")
    try:
        if _has_column(STAGING_DATASET, "stg_meta__ads", PREVIEW_COLUMN):
            _preview = (f"a.{PREVIEW_COLUMN}", "")
            print(f"   preview links: stg_meta__ads.{PREVIEW_COLUMN} found",
                  flush=True)
        elif _has_column(RAW_DATASET, "ads_native", PREVIEW_COLUMN):
            _preview = ("p.preview_url", f"""
      prev AS (
        SELECT CAST(id AS STRING) AS ad_id,
               ARRAY_AGG({PREVIEW_COLUMN} IGNORE NULLS
                         ORDER BY _airbyte_extracted_at DESC
                         LIMIT 1)[SAFE_OFFSET(0)] AS preview_url
        FROM {RAW_ADS}
        GROUP BY id
      ),""")
            print(f"   preview links: ads_native.{PREVIEW_COLUMN} found in RAW "
                  "— staging model has not exposed it yet", flush=True)
        else:
            print(f"   preview links: no {PREVIEW_COLUMN} column in staging or "
                  "raw yet — preview_url stays null", flush=True)
    except Exception as e:
        print(f"   ! preview column probe failed ({type(e).__name__}: {e}) — "
              "preview_url stays null", flush=True)
    return _preview


def dims_cte() -> str:
    expr, extra = preview_source()
    # ON, not USING: `ad_id` also exists on the creatives side of the join, and
    # USING would be ambiguous the moment the raw branch is live.
    join = "\n        LEFT JOIN prev p ON p.ad_id = a.ad_id" if extra else ""
    return f"""{extra}
      dims AS (
        SELECT a.ad_id, DATE(a.created_time) AS uploaded, a.effective_status,
               c.thumbnail_url, c.image_url, c.instagram_permalink_url,
               {expr} AS preview_url,
               JSON_VALUE(c.object_story_spec, "$.video_data.image_url")
                 AS video_poster_url,
               JSON_VALUE(c.object_story_spec, "$.link_data.picture")
                 AS link_picture_url,
               c.title AS creative_title, c.body AS creative_body,
               c.video_id AS creative_video_id
        FROM {ADS} a
        LEFT JOIN {CREA} c USING (creative_id){join}
      )
"""

# The measures, summed identically wherever an aggregate is taken. v3 summed
# them in SQL (a `SUMS` fragment reused per period); v4 sums them in Python
# because there are now 12 windows over the same ad-day rows.
MEASURES = ["spend", "impressions", "clicks", "link_clicks", "lpv", "atc", "ic",
            "purchases", "purchase_value", "video_plays", "video_3s",
            "video_thruplays", "video_p100", "video_seconds", "reactions",
            "shares", "saves", "comments"]


# ── Queries ──────────────────────────────────────────────────────────────────
def per_ad_day(scan: dict) -> list[dict]:
    """Measures at ad × DAY grain over the whole 180-day scan.

    v3 asked BigQuery for three pre-summed periods. With 12 filter combinations
    that would be 12 queries (or one query with a 24-element period array); at
    this account's volume the whole ad-day matrix is ~8 000 rows, so it is
    cheaper and far clearer to pull the days once and sum every window in
    Python. Every combination is then guaranteed to be summing exactly the same
    numbers."""
    return q(f"""
      SELECT {DAY_SELECT}
      FROM {INS} i
      WHERE i.date BETWEEN @scan_from AND @to
    """, scan_from=scan["scan_from"], to=scan["to"])


def ad_dims(scan: dict) -> list[dict]:
    """One row per ad: name, market, tag, media type, promo flag, image URLs.

    Constant per ad, so it is joined in Python instead of riding along on every
    ad-day row. The DERIVE block is textually the same one v3 used, so the
    market / media-type / tag rules cannot drift."""
    return q(f"""
      WITH {dims_cte()},
      ins AS (
        SELECT i.date, i.ad_id, i.ad_name, i.campaign_name, i.market, i.account_id,
               x.uploaded, x.effective_status, x.thumbnail_url, x.image_url,
               x.video_poster_url, x.link_picture_url, x.instagram_permalink_url,
               x.preview_url, x.creative_title, x.creative_body, x.creative_video_id
        FROM {INS} i LEFT JOIN dims x USING (ad_id)
        WHERE i.date BETWEEN @scan_from AND @to
      ),
      d AS (SELECT *, {DERIVE} FROM ins)
      SELECT ad_id,
             ANY_VALUE(mk)                     AS mk,
             ANY_VALUE(ad_name)                AS ad_name,
             ANY_VALUE(campaign_name)          AS campaign_name,
             ANY_VALUE(account_id)             AS account_id,
             ANY_VALUE(tag)                    AS tag,
             ANY_VALUE(media_type)             AS media_type,
             ANY_VALUE(thumbnail_url)          AS thumbnail_url,
             ANY_VALUE(image_url)              AS image_url,
             ANY_VALUE(video_poster_url)       AS video_poster_url,
             ANY_VALUE(link_picture_url)       AS link_picture_url,
             ANY_VALUE(instagram_permalink_url) AS instagram_permalink_url,
             ANY_VALUE(preview_url)            AS preview_url,
             ANY_VALUE(uploaded)               AS uploaded,
             ANY_VALUE(effective_status)       AS effective_status,
             LOGICAL_OR(REGEXP_CONTAINS(
               CONCAT(IFNULL(ad_name,''), ' ', IFNULL(campaign_name,''), ' ',
                      IFNULL(creative_title,''), ' ', IFNULL(creative_body,'')),
               r'{PROMO_REGEX}'))              AS promo
      FROM d
      GROUP BY ad_id
    """, scan_from=scan["scan_from"], to=scan["to"])


def weekly(scan: dict) -> list[dict]:
    """Per-ad 7-day buckets counted from that ad's first spend day.

    Full history, not just the window: the fatigue baseline is the ad's first
    good week, which for a 200-day-old creative is 200 days ago. Restricted to
    ads that spent inside the LONGEST selectable window, because those are the
    only ads any combination's board can show."""
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
        WHERE date BETWEEN @long_from AND @to AND spend > 0
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
    """, to=scan["to"], long_from=scan["long_from"])


def upload_rows(scan: dict) -> list[dict]:
    """Ads created inside the longest window, each carrying the FIRST created
    date its ad_name ever had.

    v3 ran an anti-join against "any ad with this name created before @start",
    which is a different query for every period. `first_created` carries the
    same information for every period at once: an ad is new work in a window
    when it was created in that window AND no ad of that name existed before
    the window opened."""
    return q(f"""
      WITH first_by_name AS (
        SELECT ad_name, MIN(DATE(created_time)) AS first_created
        FROM {ADS} GROUP BY ad_name
      )
      SELECT a.ad_id, a.ad_name, DATE(a.created_time) AS uploaded,
             a.effective_status, f.first_created
      FROM {ADS} a JOIN first_by_name f USING (ad_name)
      WHERE DATE(a.created_time) BETWEEN @long_from AND @to
    """, long_from=scan["long_from"], to=scan["to"])


# ── Window aggregation (the old SQL `SUMS`, in Python) ───────────────────────
def _sum_days(days: list[dict]) -> dict:
    """Sum one ad's days into the bag of measures `_metrics` expects.

    `sum_reach` is the sum of DAILY reach, not a period reach — that is what
    makes `_freq` Meta's daily frequency averaged over the window, and it is
    the only reach figure the feed can support. `live_days` is COUNTIF(spend>0),
    the same definition v3's SQL used."""
    out = {m: 0.0 for m in MEASURES}
    reach = 0.0
    live = 0
    for r in days:
        for m in MEASURES:
            out[m] += _f(r.get(m))
        reach += _f(r.get("reach"))
        if _f(r.get("spend")) > 0:
            live += 1
    out["sum_reach"] = reach
    out["live_days"] = live
    return out


def _window_rows(by_ad: dict, dims: dict, d_from, d_to,
                 markets: list[str]) -> list[dict]:
    """Every ad with any activity in [d_from, d_to], as v3's per-period rows.

    Shape-compatible with what `per_ad` used to return for one period: the ad's
    dimensions plus its summed measures, so nothing downstream had to change.
    An ad with no row in the range is absent rather than zero, which is what
    the SQL GROUP BY did."""
    out = []
    for ad_id, days in by_ad.items():
        dim = dims.get(ad_id)
        if not dim or dim.get("mk") not in markets:
            continue
        sel = [r for r in days if d_from <= r["date"] <= d_to]
        if not sel:
            continue
        row = dict(dim)
        row.update(_sum_days(sel))
        row["ad_id"] = ad_id
        out.append(row)
    return out


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
        # Meta's shareable ad preview. Null until the connector loads
        # preview_shareable_link (see preview_expr) — the tab falls back to the
        # Ads Manager deep link, which is what it has always shown.
        "preview_url": r.get("preview_url"),
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
        "thumbnail_url": c.get("thumbnail_url"), "image_url": c.get("image_url"),
        "instagram_url": c.get("instagram_url"), "preview_url": c.get("preview_url"),
        "ads_manager_url": c.get("ads_manager_url"),
        "value": c.get(value_key), "spend": c["spend"], "purchases": c["purchases"],
    }


# ── Image mirror ─────────────────────────────────────────────────────────────
def _thumb_source(url: str) -> str:
    """`thumbnail` for a 64 px square, `thumbnail_hd` for anything larger.

    The rendition is encoded in the signed URL (stp=…p64x64… / …p1080x1080…),
    so this costs a regex and no fetch. Once Henrik's connector change lands —
    thumbnail_width/height=1200 on the adcreatives edge — most of this account
    stops being 64 px, and those ads must be re-mirrored rather than left on a
    stored 64 px copy."""
    return "thumbnail" if THUMB_SMALL_RE.search(url or "") else "thumbnail_hd"


def _best_image(r: dict) -> tuple[str | None, str | None]:
    """The best rendition Meta exposes for this ad, and what it is called.

    Order matters and is not cosmetic: image_url and the video poster are real
    full-size stills; a full-size thumbnail_url is nearly as good; link_data
    .picture is a feed-sized preview; and a 64 px thumbnail_url cannot be asked
    for any larger (rewriting the size in the URL returns a 403 signature
    mismatch). Candidates are ranked rather than tried in a fixed order,
    because whether thumbnail_url outranks link_picture depends on its size."""
    best_url, best_src, best_rank = None, None, 0
    for col, source in (("image_url", "image_url"),
                        ("video_poster_url", "video_poster"),
                        ("link_picture_url", "link_picture"),
                        ("thumbnail_url", None)):
        url = r.get(col)
        if not url:
            continue
        src = source or _thumb_source(str(url))
        rank = IMAGE_SOURCE_RANK.get(src, 0)
        if rank > best_rank:
            best_url, best_src, best_rank = str(url), src, rank
    return best_url, best_src


def _walk_creatives(node):
    """Every dict in the payload that names an ad. The payload repeats the same
    ad across the fatigue board, the leaderboards, the top-5 tables and the
    recent list, so annotating by walk beats threading a map through nine call
    sites."""
    if isinstance(node, dict):
        if node.get("ad_id"):
            yield node
        for v in node.values():
            yield from _walk_creatives(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_creatives(v)


def _image_jobs(dims: dict, spend_by_ad: dict) -> list[dict]:
    """One fetch job per ad that SPENT inside the longest window, best source
    first, highest spend first.

    v3 derived the list by walking the payload. With 12 payloads that would be
    12 walks over the same ads, and the union is anyway "every ad any
    combination can show" — which is exactly the set that spent in the 90-day
    window. Capped by META_IMAGE_MAX in production and by META_IMAGE_ADS for a
    local run, both spend-ordered so a 10-ad local run mirrors the rows a
    reviewer actually looks at."""
    jobs = []
    for ad_id, spend in spend_by_ad.items():
        if spend <= 0:
            continue
        row = dims.get(ad_id)
        if not row:
            continue
        url, source = _best_image(row)
        if url:
            jobs.append({"ad_id": ad_id, "url": url, "source": source,
                         "spend": spend})
    jobs.sort(key=lambda j: -_f(j["spend"]))
    cap = IMAGE_ADS_CAP if IMAGE_ADS_CAP > 0 else IMAGE_MAX
    return jobs[:cap] if cap > 0 else jobs


def _encode(raw: bytes) -> dict:
    """One fetched image → the two renditions a doc stores.

    Never upscales. A 64px thumbnail stays 64px: blowing it up to 640 in
    Pillow would cost 40x the bytes and show the reader exactly the same
    pixels the browser would have interpolated anyway."""
    import base64, io
    from PIL import Image

    im = Image.open(io.BytesIO(raw))
    im.load()
    if im.mode in ("RGBA", "LA", "P"):
        # JPEG has no alpha; composite onto white rather than letting Pillow
        # drop the channel and turn transparent logo backgrounds black.
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (255, 255, 255))
        bg.paste(im, mask=im.split()[-1])
        im = bg
    elif im.mode != "RGB":
        im = im.convert("RGB")

    w, h = im.size

    def jpeg(img) -> str:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=IMAGE_QUALITY, optimize=True)
        return base64.b64encode(buf.getvalue()).decode("ascii")

    large = im.copy()
    large.thumbnail((IMAGE_LARGE_PX, IMAGE_LARGE_PX), Image.LANCZOS)

    side = min(w, h)
    left, top = (w - side) // 2, (h - side) // 2
    thumb = im.crop((left, top, left + side, top + side))
    if side > IMAGE_THUMB_PX:
        thumb = thumb.resize((IMAGE_THUMB_PX, IMAGE_THUMB_PX), Image.LANCZOS)

    return {"thumb_b64": jpeg(thumb), "large_b64": jpeg(large), "w": w, "h": h}


def mirror_images(jobs: list[dict]) -> tuple[dict, dict]:
    """Fetch, resize and store one document per ad in `meta_creative_images`.

    Returns (present, stats). `present` maps ad_id → {source, has_large} for
    every ad the collection now serves, including the ones this run skipped
    because a good-enough document was already there.

    Nothing in here may fail the job. A dead CDN URL, a truncated JPEG or a
    Firestore hiccup costs one thumbnail, not a snapshot: the tab falls back to
    the hotlinked URL and then to a placeholder."""
    import concurrent.futures
    from google.cloud import firestore

    present: dict[str, dict] = {}
    stats = {"considered": len(jobs), "fetched": 0, "skipped": 0, "failed": 0,
             "bytes": 0, "by_source": {}}
    if not jobs:
        return present, stats

    try:
        import requests  # noqa: F401
        from PIL import Image  # noqa: F401
    except Exception as e:
        print(f"   ! image mirror disabled — {type(e).__name__}: {e}", flush=True)
        stats["failed"] = len(jobs)
        return present, stats

    db = firestore.Client(project=FIRESTORE_PROJECT, credentials=_credentials())
    col = db.collection(IMAGES_COLLECTION)
    doc_id = lambda ad_id: f"{WORKSPACE}__{ad_id}"          # noqa: E731
    now = time.time()

    # Read the existing docs BY ID. Never a collection scan: this collection
    # holds base64 image bytes, so listing it would pull megabytes per run.
    existing: dict[str, dict] = {}
    try:
        refs = [col.document(doc_id(j["ad_id"])) for j in jobs]
        for snap in db.get_all(refs):
            if snap.exists:
                d = snap.to_dict() or {}
                # The bytes are not needed here, only the provenance and the
                # size — `to_dict()` still pulls them, which is why this read
                # is by id over the job list and never a collection scan.
                existing[snap.id] = {"source": d.get("source"),
                                     "w": d.get("w"), "h": d.get("h"),
                                     "fetched_at": _f(d.get("fetched_at"))}
    except Exception as e:
        print(f"   ! could not read {IMAGES_COLLECTION}: {type(e).__name__}: {e}",
              flush=True)

    def _keep(job: dict, ex: dict) -> None:
        """Advertise the stored document without re-fetching it."""
        stats["skipped"] += 1
        present[job["ad_id"]] = {"source": ex.get("source"),
                                 "has_large": ex.get("source") != "thumbnail",
                                 "w": ex.get("w"), "h": ex.get("h")}

    todo = []
    for j in jobs:
        ex = existing.get(doc_id(j["ad_id"]))
        if ex:
            stored = IMAGE_SOURCE_RANK.get(ex.get("source"), 0)
            avail = IMAGE_SOURCE_RANK.get(j["source"], 0)
            # A better source appeared (a poster, or a 64 px thumbnail that is
            # now served at 1080) — re-fetch NOW, not in 14 days. This is the
            # whole point of ranking the sources.
            if avail > stored:
                todo.append(j)
                continue
            # Never trade a good rendition for a worse one. If all Meta offers
            # today is thinner than what is already stored, keep the stored
            # copy however old it is — a stale 1080 px poster beats a fresh
            # 64 px square, and the URL it was fetched from is long dead anyway.
            if avail < stored:
                _keep(j, ex)
                continue
            if (now - ex["fetched_at"]) < IMAGE_REFRESH_DAYS * 86400:
                _keep(j, ex)
                continue
        todo.append(j)

    def one(j: dict) -> dict | None:
        import requests
        try:
            r = requests.get(j["url"], timeout=IMAGE_TIMEOUT,
                             headers={"User-Agent": IMAGE_UA})
            r.raise_for_status()
            enc = _encode(r.content)
            col.document(doc_id(j["ad_id"])).set({
                **enc,
                "ad_id": j["ad_id"],
                "source": j["source"],
                "fetched_at": now,
                "expires_at": now + IMAGE_TTL,
                "workspace": WORKSPACE,
            })
            return {"ad_id": j["ad_id"], "source": j["source"],
                    "w": enc["w"], "h": enc["h"],
                    "bytes": len(enc["thumb_b64"]) + len(enc["large_b64"])}
        except Exception as e:
            print(f"   ! image {j['ad_id']} ({j['source']}): "
                  f"{type(e).__name__}: {e}", flush=True)
            return None

    if todo:
        with concurrent.futures.ThreadPoolExecutor(max_workers=IMAGE_WORKERS) as ex:
            for res in ex.map(one, todo):
                if res is None:
                    stats["failed"] += 1
                    continue
                stats["fetched"] += 1
                stats["bytes"] += res["bytes"]
                present[res["ad_id"]] = {
                    "source": res["source"],
                    "has_large": res["source"] != "thumbnail",
                    "w": res["w"], "h": res["h"]}

    for v in present.values():
        stats["by_source"][v["source"]] = stats["by_source"].get(v["source"], 0) + 1
    return present, stats


def annotate_images(payload: dict, present: dict) -> None:
    """Stamp `image_source` / `has_large` onto every creative the mirror serves.

    Deliberately the LAST step before the Firestore write: an ad only advertises
    a mirrored image once the bytes are actually stored, so a failed fetch
    leaves the tab on its hotlink → placeholder fallback instead of pointing at
    a 404."""
    for c in _walk_creatives(payload):
        got = present.get(str(c["ad_id"]))
        if got:
            c["image_source"] = got["source"]
            c["has_large"] = got["has_large"]
            # The SOURCE pixel size, so the lightbox can say "1080 × 1080" and
            # a 64 px creative is visibly Meta's limit rather than our mirror's.
            if got.get("w"):
                c["image_w"] = got.get("w")
                c["image_h"] = got.get("h")
    payload.setdefault("sources", {})["image_labels"] = IMAGE_SOURCE_LABEL
    payload["sources"]["images_mirrored"] = len(present)


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
    "instagram_url", "preview_url", "ads_manager_url",
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
                  account_id: str, min_spend_long: float) -> dict:
    elig = [r for r in l28
            if r["media_type"] != "DPA / catalog"
            and _f(r.get("spend")) >= min_spend_long]
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
# Index columns compare a GROUP against the ACCOUNT AVERAGE over the same
# window and the same table. This is a different question from the fatigue
# indices on the creatives board, which compare an ad against its OWN baseline
# week — the two are deliberately named apart in the payload (`idx_*` here,
# `*_index` there) so nothing downstream can confuse them.
#
# Every index is oriented so that ABOVE 1.00 IS BETTER, including CPA, which is
# therefore account ÷ group rather than group ÷ account. Without that the tab
# would show four columns where three read upwards and one downwards, and the
# colour of a cell would depend on which column it sat in.
IDX_SPECS = [
    ("idx_cpa",      "cpa",      True),
    ("idx_roas",     "roas",     False),
    ("idx_link_ctr", "link_ctr", False),
    ("idx_hook",     "hook_rate", False),
]


def _index_vs(group_v, ref_v, lower_is_better: bool) -> float | None:
    """One index, or None when either side is missing or zero.

    A zero denominator is undefined, not infinite: a group with no purchases
    has no CPA to compare, and printing a 0.00× there would read as "free"."""
    if group_v is None or ref_v is None:
        return None
    g, r = float(group_v), float(ref_v)
    if g == 0 or r == 0:
        return None
    return round((r / g) if lower_is_better else (g / r), 3)


def _reference(rows: list[dict]) -> dict:
    """The account average for one table: the same ratio-of-sums over every row
    the table was built from, so the indices and the spend shares agree on what
    100 % is."""
    m = _metrics(_sum_rows(rows))
    m["hook_rate"] = _hook_kpi(rows)   # video-ad denominator, as everywhere
    m["ads"] = len(rows)
    return m


def _rollup(rows: list[dict], keyfn, label="label") -> tuple[list[dict], dict]:
    """Ratio-of-sums by any grouping, with the ad count so a reader can see
    when a row is one creative pretending to be a pattern, and an index against
    the account average for the same window.

    Returns (rows, reference) — the reference is what the bar charts draw their
    dashed average line at."""
    ref = _reference(rows)
    total = _f(ref.get("spend"))
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
        for field, metric, lower in IDX_SPECS:
            m[field] = _index_vs(m.get(metric), ref.get(metric), lower)
        out.append(m)
    out.sort(key=lambda r: -_f(r["spend"]))
    return out, ref


def _rollups(long_rows: list[dict], min_spend_long: float) -> dict:
    body = [r for r in long_rows if r["media_type"] != "DPA / catalog"]
    crossed, crossed_ref = _rollup(body, lambda r: f"{r['tag']} · {r['media_type']}")
    # A crossed table is only worth showing while its cells hold real spend;
    # below the long-window floor it is a list of one-ad curiosities. The
    # reference stays the FULL body, so a cell's index still answers "against
    # the account", not "against the cells that survived the floor".
    crossed = [r for r in crossed if _f(r["spend"]) >= min_spend_long]
    by_tag, tag_ref = _rollup(long_rows, lambda r: r["tag"])
    by_media, media_ref = _rollup(long_rows, lambda r: r["media_type"])
    by_promo, promo_ref = _rollup(
        long_rows, lambda r: "Promo" if _promo(r) else "Evergreen")
    return {
        "by_tag": by_tag,
        "by_media_type": by_media,
        "by_promo": by_promo,
        "by_tag_media": crossed,
        # The account average each table's indices are measured against, and
        # the level the charts draw their dashed line at.
        "averages": {"by_tag": tag_ref, "by_media_type": media_ref,
                     "by_promo": promo_ref, "by_tag_media": crossed_ref},
        "index_metrics": [{"field": f, "metric": m, "lower_is_better": low}
                          for f, m, low in IDX_SPECS],
    }


def _end_date() -> datetime.date:
    """The last SETTLED day. Pin it with META_END_DATE=YYYY-MM-DD to reproduce
    a specific report."""
    end = os.environ.get("META_END_DATE")
    return (datetime.date.fromisoformat(end) if end
            else datetime.date.today() - datetime.timedelta(days=SETTLE_DAYS))


def _window(days: int) -> dict:
    """One period's dates.

    `l28_from` / `l28_to` keep their v1 names but now mean "the LONG window",
    which is 28 days on the 7-day preset (exactly what v3 did) and the selected
    period on every other. Keeping the key names means the payload shape is
    identical across all 12 combinations and across versions."""
    end_d = _end_date()
    start_d = end_d - datetime.timedelta(days=days - 1)
    long_days = LONG_DAYS if days == DEFAULT_DAYS else days
    return {
        "from": start_d.isoformat(), "to": end_d.isoformat(),
        "prev_from": (start_d - datetime.timedelta(days=days)).isoformat(),
        "prev_to":   (end_d - datetime.timedelta(days=days)).isoformat(),
        "l28_from":  (end_d - datetime.timedelta(days=long_days - 1)).isoformat(),
        "l28_to":    end_d.isoformat(),
        "days": days,
        "long_days": long_days,
    }


def _scan_window() -> dict:
    """The single BigQuery scan every combination is summed out of.

    Back to the start of the longest period's COMPARISON period — 90 days of
    window plus 90 days of previous is 180 days — plus the long-window start
    the weekly history and the upload list are restricted to."""
    end_d = _end_date()
    return {
        "to": end_d.isoformat(),
        "scan_from": (end_d - datetime.timedelta(days=2 * MAX_DAYS - 1)).isoformat(),
        "long_from": (end_d - datetime.timedelta(days=MAX_DAYS - 1)).isoformat(),
    }


def build_one(market: str, days: int, by_ad: dict, dims: dict,
              weeks_by_ad: dict, uploads: list[dict], account_id: str,
              generated_at: str) -> dict:
    """One combination's payload. Identical in shape to every other."""
    win = _window(days)
    markets = MARKETS if market == "all" else [market]
    min_spend = floor_short(days)
    min_spend_long = floor_long(win["long_days"])

    d = lambda s: datetime.date.fromisoformat(s)   # noqa: E731
    cur = _window_rows(by_ad, dims, d(win["from"]), d(win["to"]), markets)
    prev = _window_rows(by_ad, dims, d(win["prev_from"]), d(win["prev_to"]), markets)
    # On the 7-day preset the long window is a genuinely different range; on
    # every other period it IS the current window, so it is not re-summed.
    long_rows = (cur if win["long_days"] == days
                 else _window_rows(by_ad, dims, d(win["l28_from"]),
                                   d(win["l28_to"]), markets))

    kpis = {"combined": _with_deltas(_kpis(cur), _kpis(prev))}
    for m in markets:
        kpis[m] = _with_deltas(_kpis([r for r in cur if r["mk"] == m]),
                               _kpis([r for r in prev if r["mk"] == m]))

    fatigue = _fatigue(cur, weeks_by_ad, win, account_id)
    leaderboards = _leaderboards(long_rows, fatigue["rows"], account_id,
                                 min_spend_long)
    rollups = _rollups(long_rows, min_spend_long)

    # Top 5 per market: lowest CPA over a spend floor, DPA excluded — a
    # catalogue ad is not a creative anyone can iterate on. Only the selected
    # market(s) are filled; the tab hides the other card rather than showing an
    # empty one, so an SE view never implies "no NO ad qualified".
    top: dict[str, list[dict]] = {}
    for m in markets:
        elig = [r for r in cur
                if r["mk"] == m
                and r["media_type"] != "DPA / catalog"
                and _f(r.get("spend")) >= min_spend
                and I(r.get("purchases")) > 0]
        elig.sort(key=lambda r: _f(r["spend"]) / I(r["purchases"]))
        top[m] = [_creative(r, account_id) for r in elig[:TOP_N]]

    # Recently uploaded: created in the window, a genuinely new concept
    # (`first_created` proves no older ad carried the name), and it delivered.
    fat_by_id = {c["ad_id"]: c for c in fatigue["rows"]}
    cur_by_id = {str(r["ad_id"]): r for r in cur}
    w_from, w_to = d(win["from"]), d(win["to"])
    recent = []
    for a in uploads:
        up = a.get("uploaded")
        if not (up and w_from <= up <= w_to):
            continue
        if a.get("first_created") and a["first_created"] < w_from:
            continue                      # a re-upload of an older concept
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

    return {
        "generated_at": generated_at,
        "sources": {
            "insights": f"{DATA_PROJECT}.{STAGING_DATASET}.stg_meta__ads_insights",
            "ads":      f"{DATA_PROJECT}.{STAGING_DATASET}.stg_meta__ads",
            "creatives": f"{DATA_PROJECT}.{STAGING_DATASET}.stg_meta__ad_creatives",
            "marts": f"{DATA_PROJECT}.{MARTS_DATASET}.agg_daily_kpis_by_ad",
            "account_id": account_id,
            "account_label": "Babyshop SE — New",
            "attribution": "7d_click,1d_view",
            "markets": markets,
            "all_markets": MARKETS,
            "min_spend": min_spend,
            "min_spend_l28": min_spend_long,
            "images": "mirrored into Firestore by this job and served from "
                      "/api/meta/image/<ad_id>; the Meta CDN URLs they are "
                      "fetched from are signed and expire ~4 days after minting",
            "instagram_coverage": {
                "ads": ig_ads, "ads_total": len(cur),
                "ads_pct": div(ig_ads, len(cur), 100.0, 1),
                "spend_pct": div(ig_spend, sum(_f(r["spend"]) for r in cur), 100.0, 1),
            },
        },
        "filters": {
            "market": market,
            "days": days,
            "available_markets": MARKET_OPTIONS,
            "available_days": PERIOD_OPTIONS,
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


def build_all() -> dict:
    """Every combination, out of one BigQuery scan.

    Returns {"combos": {"<market>_<days>": payload}, "_image_jobs": [...]}.
    META_OUT writes this bundle whole so a META_IN replay can produce all 12
    documents from one file."""
    scan = _scan_window()
    generated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()

    dims = {str(r["ad_id"]): r for r in ad_dims(scan)}
    by_ad: dict[str, list[dict]] = {}
    for r in per_ad_day(scan):
        by_ad.setdefault(str(r["ad_id"]), []).append(r)
    weeks_by_ad: dict[str, list[dict]] = {}
    for w in weekly(scan):
        weeks_by_ad.setdefault(str(w["ad_id"]), []).append(w)
    uploads = upload_rows(scan)
    account_id = next((str(r["account_id"]) for r in dims.values()
                       if r.get("account_id")), "")
    print(f"   scan {scan['scan_from']}..{scan['to']}: {len(dims)} ads, "
          f"{sum(len(v) for v in by_ad.values()):,} ad-days, "
          f"{len(weeks_by_ad)} weekly series, {len(uploads)} uploads",
          flush=True)

    combos = {}
    for market in MARKET_OPTIONS:
        for days in PERIOD_OPTIONS:
            combos[f"{market}_{days}"] = build_one(
                market, days, by_ad, dims, weeks_by_ad, uploads, account_id,
                generated_at)

    # The fetch list for the image mirror, built from the ads that SPENT in the
    # longest window — the union of every combination's creatives. Carried on
    # the bundle (and stripped before the Firestore write) because the two
    # halves of a local run are two processes authenticating as two different
    # accounts: the URLs are only knowable in the BigQuery half and only usable
    # in the Firestore half.
    long_from = datetime.date.fromisoformat(scan["long_from"])
    spend_by_ad: dict[str, float] = {}
    for ad_id, days_rows in by_ad.items():
        spend_by_ad[ad_id] = sum(_f(r.get("spend")) for r in days_rows
                                 if r["date"] >= long_from)
    return {"combos": combos, "_image_jobs": _image_jobs(dims, spend_by_ad)}


# ── Document size ────────────────────────────────────────────────────────────
# 12 documents now, and the 90-day ones carry every ad that spent in three
# months rather than in one week. Trimming is ordered cheapest-loss-first and
# every step is recorded in the payload, because a table that silently drops
# rows is worse than a small one that says it did.
def _payload_bytes(payload: dict) -> int:
    return len(json.dumps(payload, ensure_ascii=False))


WEEKLY_SERIES_KEEP = int(os.environ.get("META_WEEKLY_ROWS", "150"))


def _fit(payload: dict, label: str) -> tuple[int, list[str]]:
    """Bring one payload under the budget, or raise.

    Steps, in order:
      1. drop creatives-table rows under 1 kr of spend (a row that spent 4 öre
         is a rounding artefact, not a creative);
      2. drop the weekly sparkline series outside the top WEEKLY_SERIES_KEEP
         rows by spend — the status chip and every index survive, only the
         sparkline goes, and only on rows nobody scrolls to.
    The leaderboards are already capped at META_LEADER_N (5) and the weekly
    series at META_WEEKS_KEPT (12) by construction."""
    trimmed: list[str] = []
    n = _payload_bytes(payload)
    if n <= DOC_BUDGET_BYTES:
        return n, trimmed

    fat = payload.get("fatigue") or {}
    rows = fat.get("rows") or []
    kept = [r for r in rows if _f(r.get("spend")) >= 1.0]
    if len(kept) < len(rows):
        fat["rows"] = kept
        trimmed.append(f"{len(rows) - len(kept)} creatives under 1 kr spend")
        rows = kept
        n = _payload_bytes(payload)
        if n <= DOC_BUDGET_BYTES:
            return n, trimmed

    ranked = sorted(rows, key=lambda r: -_f(r.get("spend")))
    dropped = 0
    for r in ranked[WEEKLY_SERIES_KEEP:]:
        if r.get("weeks"):
            r["weeks"] = []
            r["weeks_trimmed"] = True
            dropped += 1
    if dropped:
        trimmed.append(f"weekly series on {dropped} rows outside the top "
                       f"{WEEKLY_SERIES_KEEP} by spend")
        n = _payload_bytes(payload)

    if n > DOC_BUDGET_BYTES:
        big = sorted(((len(json.dumps(v, ensure_ascii=False)), k)
                      for k, v in payload.items()), reverse=True)[:3]
        raise RuntimeError(
            f"Meta payload {label} is {n:,} B, over the {DOC_BUDGET_BYTES:,} B "
            "budget after trimming. Biggest sections: "
            + ", ".join(f"{k} {s:,} B" for s, k in big)
            + ". Lower META_WEEKS_KEPT / META_LEADER_N / META_TOP_N / "
              "META_WEEKLY_ROWS — do not just raise the budget.")
    return n, trimmed


def write_firestore(combos: dict) -> dict:
    """Every combination as its own document. Returns {key: bytes}."""
    from google.cloud import firestore

    db = firestore.Client(project=FIRESTORE_PROJECT, credentials=_credentials())
    sizes: dict[str, int] = {}
    for combo, payload in combos.items():
        market, days = combo.rsplit("_", 1)
        key = combo_key(market, int(days))
        n, trimmed = _fit(payload, combo)
        if trimmed:
            payload["sources"]["trimmed"] = trimmed
            n = _payload_bytes(payload)
        doc_id = f"{WORKSPACE}__{key}"
        db.collection(COLLECTION).document(doc_id).set({
            "data": payload, "fetched_at": firestore.SERVER_TIMESTAMP,
            "expires_at": time.time() + TTL, "ttl_seconds": TTL,
            "workspace": WORKSPACE})
        sizes[key] = n
    return sizes


def main():
    t0 = time.time()
    # META_IN replays a bundle produced by an earlier META_OUT run. The two
    # halves of this job authenticate to different projects as different
    # accounts, and locally one process cannot be both — this splits them.
    src = os.environ.get("META_IN")
    if src:
        with open(src, encoding="utf-8") as fh:
            bundle = json.load(fh)
        # A v3 file is a single payload, not a bundle. Read it as the default
        # combination so an old dump still replays.
        if "combos" not in bundle:
            bundle = {"combos": {f"{DEFAULT_MARKET}_{DEFAULT_DAYS}": bundle},
                      "_image_jobs": bundle.pop("_image_jobs", [])}
        print(f"   loaded {src} ({os.path.getsize(src):,} bytes, "
              f"{len(bundle['combos'])} combinations) — BigQuery skipped")
    else:
        bundle = build_all()

    out = os.environ.get("META_OUT")
    if out:
        # Written WITH _image_jobs so a META_IN replay can still mirror.
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(bundle, fh, ensure_ascii=False)
        print(f"   wrote {out} ({os.path.getsize(out):,} bytes)")

    combos = bundle["combos"]
    jobs = bundle.pop("_image_jobs", None) or []
    skip_fs = bool(os.environ.get("SKIP_FIRESTORE"))
    img = {"considered": len(jobs), "fetched": 0, "skipped": 0, "failed": 0,
           "bytes": 0, "by_source": {}}
    if jobs and not SKIP_IMAGES and not skip_fs:
        present, img = mirror_images(jobs)
        # Every combination shows the same ads, so every one is annotated.
        for p in combos.values():
            annotate_images(p, present)
    elif jobs:
        print(f"   images skipped ({len(jobs)} candidates) — "
              f"{'META_SKIP_IMAGES' if SKIP_IMAGES else 'SKIP_FIRESTORE'}")

    default = combos[f"{DEFAULT_MARKET}_{DEFAULT_DAYS}"]
    if skip_fs:
        where = "(skipped)"
        sizes = {}
        for combo, p in combos.items():
            market, days = combo.rsplit("_", 1)
            n, trimmed = _fit(p, combo)
            if trimmed:
                p["sources"]["trimmed"] = trimmed
                n = _payload_bytes(p)
            sizes[combo_key(market, int(days))] = n
    else:
        sizes = write_firestore(combos)
        where = f"{COLLECTION}/{WORKSPACE}__{DOC_KEY} +{len(sizes) - 1}"

    k = default["kpis"]["combined"]
    w = default["window"]
    fat = default.get("fatigue", {})
    dist = " / ".join(f"{b['status']} {b['ads']}" for b in fat.get("by_status", []))
    isrc = " / ".join(f"{s} {n}" for s, n in sorted(img["by_source"].items()))
    print(f"✓ Meta refresh · {where} · default {w['from']}..{w['to']} · "
          f"spend {k['spend']:,.0f} kr / {k['purchases']} purchases / CPA {k['cpa']} / "
          f"ROAS {k['roas']}x · link CTR {k['link_ctr']}% · "
          f"fatigue {len(fat.get('rows', []))} ads [{dist}], "
          f"{fat.get('at_risk_pct')}% of spend at risk · "
          f"images {img['fetched']} fetched / {img['skipped']} cached / "
          f"{img['failed']} failed of {img['considered']}"
          + (f" [{isrc}]" if isrc else "")
          + f", {img['bytes']/1024:,.0f} KiB b64 · "
          f"{len(sizes)} docs, {max(sizes.values()):,} B max "
          f"({max(sizes, key=sizes.get)}) · {time.time()-t0:.1f}s")
    for key in sorted(sizes, key=lambda x: -sizes[x]):
        tr = combos_trimmed(combos, key)
        print(f"     {key:<22} {sizes[key]:>9,} B" + (f"  · trimmed {tr}" if tr else ""))


def combos_trimmed(combos: dict, key: str) -> str:
    for combo, p in combos.items():
        market, days = combo.rsplit("_", 1)
        if combo_key(market, int(days)) == key:
            return "; ".join((p.get("sources") or {}).get("trimmed") or [])
    return ""


if __name__ == "__main__":
    main()
