#!/usr/bin/env python3
"""
ROAS Simulations — Google Ads API collector + snapshot reader.

Replaces the Google-Sheets pipeline (`pipeline/gp3-simulations.js` → Google Sheet →
`pipeline/webapp.gs`) with a direct Google Ads API integration. Both paths COEXIST:
this module writes its own Firestore collections and serves the SAME JSON payload
shape webapp.gs produced, so `babyshop-roas-simulations.html` works unchanged.

Mirrors refresh_roas_impact.py: module-level config, a build/collect step, a
Firestore writer, and a `__main__` block that can be run locally.

WHAT IS COLLECTED (per child account, mirroring gp3-simulations.js exactly)
  Raw      Target-ROAS bid simulations. One row per simulated target-ROAS point.
           Campaign level (campaign_simulation) and — when INCLUDE_PORTFOLIO is on —
           portfolio level (bidding_strategy_simulation). Point metrics come from
           TargetRoasSimulationPoint: biddable_conversions, biddable_conversions_value,
           clicks, cost_micros, impressions, top_slot_impressions.
  Shares   Last-7-days metrics.search_impression_share / search_top_impression_share /
           search_absolute_top_impression_share per ENABLED campaign. A metric the API
           does not report stays None and is served as BLANK — never 0. The dashboard
           reads 0 and blank alike as "no data" and falls back to the flat class factor,
           so a fabricated 0 would claim "no auction headroom left".
  Actuals  What each simulated entity ACTUALLY did over THAT ENTITY'S OWN simulation
           window, under both attribution schemes:
             click time      metrics.conversions / metrics.conversions_value
             conversion time metrics.conversions_by_conversion_date /
                             metrics.conversions_value_by_conversion_date
           Cost is identical under both, so it is carried once. Rows with no spend in
           the window are dropped (ROAS is undefined there); a genuine 0 is preserved
           as 0, and "not reported" stays None → blank. Opposite rule to Shares, on
           purpose: here 0 IS a measurement.

FIRESTORE (dedicated collections — never share one with another feature)
  roas_sim_snapshots/<YYYY-MM-DD>   one daily snapshot, idempotent per day (a re-run
                                    overwrites the same doc), pruned at RETENTION_DAYS.
  roas_sim_config/config            USER-EDITABLE config that used to live in the
                                    sheet's Config tab: per-account conversion-value →
                                    GP2 multipliers and the incrementality factors /
                                    share floors+caps. Seeded with the sheet defaults
                                    on first run if absent. See EDITING THE CONFIG.

  Row grids are stored JSON-encoded (`rows_json` / `shares_json` / `actuals_json`)
  because Firestore cannot store an array inside an array. ~600 simulation points +
  ~70 shares + ~60 actuals per day is ~120 KB of JSON, comfortably under the 1 MiB
  document limit; DATASET_BUDGET_BYTES trims in the Ads script's value order
  (actuals → shares → simulations) if a run ever gets close.

CREDENTIALS (env vars, wired from Secret Manager — see pipeline/PIPELINE.md)
  GOOGLE_ADS_DEVELOPER_TOKEN     MCC → Tools → API Center (Basic access)
  GOOGLE_ADS_CLIENT_ID           OAuth desktop-app client
  GOOGLE_ADS_CLIENT_SECRET
  GOOGLE_ADS_REFRESH_TOKEN       minted once with the google-ads oauth2 flow
  GOOGLE_ADS_LOGIN_CUSTOMER_ID   the MCC customer id, digits only

  Missing any of them raises MissingCredentials — /internal/refresh-roas-sims turns
  that into a clear 503 and /api/roas-sims keeps serving whatever snapshots exist.

EDITING THE CONFIG WITHOUT A DEPLOY
  Firestore console → collection `roas_sim_config` → document `config`, or the REST
  API with a gcloud token (CLAUDE.md "Operational"):

    TOKEN=$(gcloud auth print-access-token)
    PROJ=project-a7ade44e-e7e3-4871-a83
    # read
    curl -sH "Authorization: Bearer $TOKEN" \
      "https://firestore.googleapis.com/v1/projects/$PROJ/databases/(default)/documents/roas_sim_config/config"
    # patch one factor (brand class default 0.20 -> 0.15)
    curl -sX PATCH -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
      "https://firestore.googleapis.com/v1/projects/$PROJ/databases/(default)/documents/roas_sim_config/config?updateMask.fieldPaths=incrementality.classes.brand" \
      -d '{"fields":{"incrementality":{"mapValue":{"fields":{"classes":{"mapValue":{"fields":{"brand":{"doubleValue":0.15}}}}}}}}}'

  Values are forgiving in exactly the way the sheet was: 0.2, "0,2", 20 and "20%" all
  parse to 0.20. Overrides are a list of {pattern, factor} maps.

Run locally:   python3 refresh_roas_sims.py            (writes Firestore)
               SKIP_FIRESTORE=1 ROAS_SIMS_OUT=/tmp/s.json python3 refresh_roas_sims.py
"""
from __future__ import annotations

import datetime as _dt
import json
import os
import re
import time
from typing import Any, Iterable

# ── Firestore ────────────────────────────────────────────────────────────────
FIRESTORE_PROJECT   = os.environ.get("FIRESTORE_PROJECT") or os.environ.get("GCP_PROJECT")
SNAPSHOT_COLLECTION = os.environ.get("ROAS_SIMS_COLLECTION", "roas_sim_snapshots")
CONFIG_COLLECTION   = os.environ.get("ROAS_SIMS_CONFIG_COLLECTION", "roas_sim_config")
CONFIG_DOC          = "config"

# History retention, mirroring the Ads script's LOOKBACK_PRUNE_DAYS.
RETENTION_DAYS = int(os.environ.get("ROAS_SIMS_RETENTION_DAYS", "90"))

# Hard cap on rows served in one payload, mirroring MAX_ROWS in webapp.gs.
MAX_ROWS = 20000

# Leave headroom under Firestore's 1 MiB document limit for the JSON row grids.
DATASET_BUDGET_BYTES = 800_000

SOURCE      = "google-ads-api"
API_VERSION = "v25"          # google-ads 31.x default; see requirements.txt

# ── Google Ads ───────────────────────────────────────────────────────────────
# Child accounts, from CONFIG.ACCOUNT_IDS in pipeline/gp3-simulations.js. `currency`
# is the documented account currency and is only a FALLBACK — the live
# customer.currency_code wins whenever the account query succeeds.
ACCOUNTS = [
    {"cid": "4851485396", "label": "Babyshop SE",  "currency": "SEK"},   # 485-148-5396
    {"cid": "8623945183", "label": "Babyshop NO",  "currency": "NOK"},   # 862-394-5183
    {"cid": "8308232278", "label": "Lekmer NO",    "currency": "NOK"},   # 830-823-2278
    {"cid": "7780114635", "label": "Lekmer SE",    "currency": "SEK"},   # 778-011-4635
    {"cid": "6161399704", "label": "Babyshop FI",  "currency": "EUR"},   # 616-139-9704
    {"cid": "5541487401", "label": "Babyshop ROW", "currency": "SEK"},   # 554-148-7401
    {"cid": "2756397225", "label": "Lekmer DK",    "currency": "DKK"},   # 275-639-7225
    {"cid": "2054294342", "label": "Babyshop DK",  "currency": "SEK"},   # 205-429-4342
]

def _flag(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in ("1", "true", "yes", "on")

# Mirrors CONFIG.INCLUDE_CAMPAIGNS / INCLUDE_PORTFOLIO in gp3-simulations.js. Campaign
# level is the real source for these accounts (validated live, Aug 2026 — portfolio
# simulations return nothing for SE / Lekmer NO / Lekmer DK). Portfolio level is OFF by
# default because its campaigns already appear in campaign_simulation and including both
# double-counts the same auction traffic in a cross-strategy total.
INCLUDE_CAMPAIGNS = _flag("ROAS_SIMS_INCLUDE_CAMPAIGNS", True)
INCLUDE_PORTFOLIO = _flag("ROAS_SIMS_INCLUDE_PORTFOLIO", False)
COLLECT_SHARES    = _flag("ROAS_SIMS_COLLECT_SHARES", True)
COLLECT_ACTUALS   = _flag("ROAS_SIMS_COLLECT_ACTUALS", True)

ADS_ENV_KEYS = (
    "GOOGLE_ADS_DEVELOPER_TOKEN",
    "GOOGLE_ADS_CLIENT_ID",
    "GOOGLE_ADS_CLIENT_SECRET",
    "GOOGLE_ADS_REFRESH_TOKEN",
    "GOOGLE_ADS_LOGIN_CUSTOMER_ID",
)

# ── Column grids (must stay identical to HEADERS / SHARE_HEADERS / ACTUAL_HEADERS
#    in pipeline/gp3-simulations.js — the dashboard maps them by header name) ────
RAW_COLUMNS = [
    "Customer Name", "Bidding Strategy Name", "Current Target Roas", "Bidding Strategy Id",
    "Start Date", "End Date", "TARGET ROAS", "Conversions", "Conversions Value", "Clicks",
    "Cost Micros", "Impressions", "Top Slot Impressions", "Current Campaign Strategy",
    "Currency", "Run Date",
]
SHARE_COLUMNS = [
    "Customer Name", "Campaign Id", "Campaign Name", "Search IS", "Top IS", "Abs Top IS",
    "Currency", "Run Date",
]
ACTUAL_COLUMNS = [
    "Customer Name", "Bidding Strategy Id", "Bidding Strategy Name", "Level",
    "Start Date", "End Date", "Cost Micros", "Conversions", "Conversions Value",
    "Conv Time Conversions", "Conv Time Conversions Value", "Currency", "Run Date",
]

# Columns webapp.gs hands to the client as NUMBERS (RAW_NUMERIC_HEADERS /
# ACTUAL_NUMERIC_HEADERS there). Raw uses toMetricOrZero — a blank becomes 0, because an
# unset Current Target Roas has always reached the dashboard as 0 and parseRoas() depends
# on it. Actuals uses toMetric — a blank stays blank, because a real 0 is a measurement.
RAW_NUMERIC_COLUMNS = ["Current Target Roas", "TARGET ROAS", "Conversions", "Conversions Value",
                       "Clicks", "Cost Micros", "Impressions", "Top Slot Impressions"]
ACTUAL_NUMERIC_COLUMNS = ["Cost Micros", "Conversions", "Conversions Value",
                          "Conv Time Conversions", "Conv Time Conversions Value"]
SHARE_METRIC_COLUMNS = ["Search IS", "Top IS", "Abs Top IS"]

SIM_NAME_IDX  = RAW_COLUMNS.index("Bidding Strategy Name")
SIM_ID_IDX    = RAW_COLUMNS.index("Bidding Strategy Id")
SIM_START_IDX = RAW_COLUMNS.index("Start Date")
SIM_END_IDX   = RAW_COLUMNS.index("End Date")

RUN_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# ── Config defaults — the values setupConfigTab() seeded into the sheet's Config tab.
#    Keep in sync with DEFAULT_INCREMENTALITY / DEFAULT_SHARE_WEIGHTS in webapp.gs and
#    INCREMENTALITY_DEFAULTS in babyshop-roas-simulations.html. ────────────────────
DEFAULT_CONFIG: dict[str, Any] = {
    # Conversion value in these accounts IS gross profit, so the multiplier stays 1.0.
    # Override per account only when an account reports revenue instead — then its gross
    # margin is the multiplier.
    "valueToGp2Multipliers": {},
    "defaultValueToGp2Multiplier": 1.0,
    "incrementality": {
        "classes": {"brand": 0.20, "private-label": 0.50, "generic": 1.00},
        "overrides": [],   # [{"pattern": "p-shopping-se-pb-product", "factor": 0.65}]
        "shareWeights": {
            "brand": {"floor": 0.10, "cap": 0.60},
            "private-label": {"floor": 0.40, "cap": 1.00},
        },
    },
}


class MissingCredentials(RuntimeError):
    """No Google Ads credentials in the environment — the collector cannot run."""


# ════════════════════════════════════════════════════════════════════════════
#  Parsing helpers — Python ports of the toMetric / toShare / toFactor family in
#  pipeline/webapp.gs, so the served payload is byte-for-byte the same shape.
# ════════════════════════════════════════════════════════════════════════════

def _metric(v: Any) -> Any:
    """One numeric cell. Unreadable, None or blank becomes '' (webapp.gs toMetric)."""
    if isinstance(v, bool):
        return ""
    if isinstance(v, (int, float)):
        f = float(v)
        return v if (f == f and f not in (float("inf"), float("-inf"))) else ""
    s = str(v).strip() if v is not None else ""
    if not s:
        return ""
    is_percent = "%" in s
    s = s.replace("%", "")
    s = re.sub(r"\s", "", s)
    commas, dots = s.count(","), s.count(".")
    if commas and dots:
        # whichever separator comes last is the decimal mark
        s = (s.replace(".", "").replace(",", ".") if s.rindex(",") > s.rindex(".")
             else s.replace(",", ""))
    elif commas == 1:
        s = s.replace(",", ".")     # "45,125" is forty-five point one two five
    elif commas > 1:
        s = s.replace(",", "")      # repeated commas can only be grouping
    try:
        n = float(s)
    except ValueError:
        return ""
    if n != n or n in (float("inf"), float("-inf")):
        return ""
    return n / 100 if is_percent else n


def _metric_or_zero(v: Any) -> Any:
    """toMetric() with the Raw tab's blank convention: an empty cell is 0 there."""
    n = _metric(v)
    return 0 if n == "" else n


def _share(v: Any) -> Any:
    """
    An impression share as a fraction in (0,1], or '' when there is no data.

    A literal 0 is normalised to blank on purpose: downstream 0 and blank mean the same
    thing (no data → flat class factor), and deriving headroom h = 1 - 0 from an absent
    metric would claim maximum headroom for a campaign we simply cannot see.
    """
    if isinstance(v, bool):
        return ""
    if isinstance(v, (int, float)):
        f = float(v)
        if f != f or f <= 0:
            return ""
        return min(f, 1.0)
    s = str(v).strip() if v is not None else ""
    if not s:
        return ""
    is_percent = "%" in s
    try:
        n = float(re.sub(r"\s", "", s.replace("%", "")).replace(",", "."))
    except ValueError:
        return ""
    if n != n or n <= 0:
        return ""
    if is_percent or n > 1:
        n = n / 100
    return 1.0 if n > 1 else n


def _to_multiplier(v: Any) -> float | None:
    """"1"/"1.0"/"100%" -> 1.0; "30"/"30%"/"0.3"/"0,3" -> 0.30. None when unreadable."""
    n = _metric(v)
    if n == "" or not isinstance(n, (int, float)) or n <= 0:
        return None
    n = float(n)
    if n > 1:
        n = n / 100
    return None if n > 1 else n


def _to_factor(v: Any) -> float | None:
    """Same forgiving notation as the multiplier, but 0 is allowed (assumed non-causal)."""
    n = _metric(v)
    if n == "" or not isinstance(n, (int, float)) or n < 0:
        return None
    n = float(n)
    if n > 1:
        n = n / 100
    return None if n > 1 else n


def _safe_name(v: Any) -> str:
    """
    Flatten an entity name to one line, mirroring safeName() in gp3-simulations.js
    character for character (the ASCII separators are in the class because the sheet
    pipeline stripped them) — so a campaign name reaching the dashboard from the API
    path is spelled exactly as the sheet path spelled it, and the two stay joinable on
    (account, name).
    """
    return re.sub(r"[,\r\n\u001d\u001e\u001f]+", ";", "" if v is None else str(v))


def _error_text(e: BaseException) -> str:
    """
    Readable text for an exception, including the Google Ads failure detail.

    GoogleAdsException defines no __str__ and never calls super().__init__(), so its
    default text is the raw (error, call, failure, request_id) argument tuple — which
    does carry the failure, but as an unreadable blob. Pull the error codes and messages
    out explicitly instead. _is_unsupported_field_error() matches on this text, so it
    has to keep carrying the API's field-error vocabulary.
    """
    errors = getattr(getattr(e, "failure", None), "errors", None)
    if errors:
        parts = []
        for err in errors:
            code = " ".join(str(getattr(err, "error_code", "")).split())
            parts.append(": ".join(x for x in (code, getattr(err, "message", "")) if x))
        rid = getattr(e, "request_id", "")
        return (f"{type(e).__name__}({'; '.join(parts)})"
                + (f" request_id={rid}" if rid else ""))
    return f"{type(e).__name__}: {e}"


def _roas_or_none(v: Any) -> float | None:
    """A ROAS target of None, '' or 0 all mean 'not set here'."""
    if v is None or v == "":
        return None
    try:
        n = float(v)
    except (TypeError, ValueError):
        return None
    return None if (n != n or n == 0) else n


def _iso_or_blank(v: Any) -> str:
    """Only a literal yyyy-MM-dd survives — these dates are interpolated into GAQL."""
    m = RUN_DATE_RE.match(str(v).strip() if v is not None else "")
    return m.group(0) if m else ""


def _has(msg: Any, field: str) -> bool:
    """Proto presence check. Optional scalars read back as 0/0.0 when never set."""
    try:
        return msg._pb.HasField(field)
    except Exception:
        return False


def _opt(msg: Any, field: str) -> Any:
    """Value of an optional scalar, or None when the API did not report it."""
    return getattr(msg, field) if _has(msg, field) else None


# ════════════════════════════════════════════════════════════════════════════
#  Google Ads client
# ════════════════════════════════════════════════════════════════════════════

def missing_credentials() -> list[str]:
    return [k for k in ADS_ENV_KEYS if not (os.environ.get(k) or "").strip()]


def ads_client():
    """
    Build a GoogleAdsClient from the five env vars. Raises MissingCredentials with the
    exact names still unset, so the operator gets a usable error instead of a stack trace.
    """
    missing = missing_credentials()
    if missing:
        raise MissingCredentials(
            "Google Ads credentials not configured — missing " + ", ".join(missing) +
            ". See pipeline/PIPELINE.md ('One-time setup')."
        )
    from google.ads.googleads.client import GoogleAdsClient   # lazy: import cost only on use

    return GoogleAdsClient.load_from_dict({
        "developer_token": os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"].strip(),
        "client_id": os.environ["GOOGLE_ADS_CLIENT_ID"].strip(),
        "client_secret": os.environ["GOOGLE_ADS_CLIENT_SECRET"].strip(),
        "refresh_token": os.environ["GOOGLE_ADS_REFRESH_TOKEN"].strip(),
        # The MCC id. Digits only — dashes are a UI convention, not part of the id.
        "login_customer_id": re.sub(r"\D", "", os.environ["GOOGLE_ADS_LOGIN_CUSTOMER_ID"]),
        "use_proto_plus": True,
    }, version=API_VERSION)


def _search(service, cid: str, query: str) -> Iterable[Any]:
    """Stream one GAQL query and yield GoogleAdsRow objects."""
    for batch in service.search_stream(customer_id=cid, query=query):
        for row in batch.results:
            yield row


# ════════════════════════════════════════════════════════════════════════════
#  Collectors — one per dataset, mirroring gp3-simulations.js
# ════════════════════════════════════════════════════════════════════════════

def _account_info(service, cid: str, fallback: dict) -> dict:
    """
    Customer name + currency. `descriptive_name` is what AdsApp.currentAccount().getName()
    returned, so the "Customer Name" join key matches the sheet pipeline's spelling.
    """
    info = {"name": fallback["label"], "currency": fallback["currency"]}
    for row in _search(service, cid,
                       "SELECT customer.id, customer.descriptive_name, customer.currency_code "
                       "FROM customer LIMIT 1"):
        c = row.customer
        if c.descriptive_name:
            info["name"] = _safe_name(c.descriptive_name)
        if c.currency_code:
            info["currency"] = c.currency_code
        break
    return info


def _strategy_map(service, cid: str) -> dict[str, dict]:
    """
    id -> {name, type, target} for every portfolio bidding strategy in the account.

    Queried resource-locally and joined in Python rather than selecting bidding_strategy.*
    from bidding_strategy_simulation: one query serves BOTH the portfolio-level simulation
    rows and the campaign-level "current target lives on the strategy" fallback.
    """
    out: dict[str, dict] = {}
    query = (
        "SELECT bidding_strategy.id, bidding_strategy.name, bidding_strategy.type, "
        "bidding_strategy.target_roas.target_roas, "
        "bidding_strategy.maximize_conversion_value.target_roas "
        "FROM bidding_strategy"
    )
    for row in _search(service, cid, query):
        s = row.bidding_strategy
        target = _roas_or_none(s.target_roas.target_roas)
        if target is None:
            target = _roas_or_none(s.maximize_conversion_value.target_roas)
        out[str(s.id)] = {
            "name": _safe_name(s.name),
            "type": s.type_.name if s.type_ else "",
            "target": target,
        }
    return out


def _campaign_map(service, cid: str, strategies: dict[str, dict]) -> dict[str, dict]:
    """
    id -> {name, type, target} for every campaign, with the current Target ROAS resolved
    exactly the way gp3-simulations.js resolves it (validated live, Aug 2026: an unset
    target arrives as 0, never null, so 0 always means "look at the next source"):
      1. the campaign's own tROAS
      2. the campaign's Maximize Conversion Value target
      3. the portfolio strategy the campaign bids through
    """
    out: dict[str, dict] = {}
    query = (
        "SELECT campaign.id, campaign.name, campaign.bidding_strategy, "
        "campaign.bidding_strategy_type, campaign.target_roas.target_roas, "
        "campaign.maximize_conversion_value.target_roas "
        "FROM campaign"
    )
    for row in _search(service, cid, query):
        c = row.campaign
        target = _roas_or_none(c.target_roas.target_roas)
        if target is None:
            target = _roas_or_none(c.maximize_conversion_value.target_roas)
        if target is None and c.bidding_strategy:
            strat = strategies.get(str(c.bidding_strategy).split("/")[-1])
            if strat:
                target = _roas_or_none(strat.get("target"))
        out[str(c.id)] = {
            "name": _safe_name(c.name),
            "type": c.bidding_strategy_type.name if c.bidding_strategy_type else "",
            "target": target,
        }
    return out


def _point_row(customer: dict, entity: dict, point: Any, run_date: str) -> list:
    """One Raw row per simulation point, in RAW_COLUMNS order (buildRow in the Ads script).

    Point metrics are protobuf scalars: cost_micros / clicks / impressions /
    top_slot_impressions are int64 (micros stay integral micros — the dashboard divides by
    1e6), biddable_conversions / biddable_conversions_value are doubles. target_roas is
    presence-checked because an absent target is '' on the Raw tab, not 0.
    """
    return [
        customer["name"],
        _safe_name(entity["name"]),
        "" if entity["target"] is None else float(entity["target"]),
        str(entity["id"] or ""),
        entity["start"] or "",
        entity["end"] or "",
        float(point.target_roas) if _has(point, "target_roas") else "",
        float(point.biddable_conversions),
        float(point.biddable_conversions_value),
        int(point.clicks),
        int(point.cost_micros),
        int(point.impressions),
        int(point.top_slot_impressions),
        entity["bidding_type"] or "",
        customer["currency"],
        run_date,
    ]


def _campaign_simulations(service, cid: str, customer: dict, campaigns: dict,
                          run_date: str) -> list[list]:
    """Target-ROAS simulations at campaign level — the main source for these accounts."""
    out: list[list] = []
    query = (
        "SELECT campaign_simulation.campaign_id, campaign_simulation.start_date, "
        "campaign_simulation.end_date, campaign_simulation.type, "
        "campaign_simulation.target_roas_point_list.points "
        "FROM campaign_simulation "
        "WHERE campaign_simulation.type = 'TARGET_ROAS'"
    )
    for row in _search(service, cid, query):
        sim = row.campaign_simulation
        points = sim.target_roas_point_list.points
        if not points:
            continue
        camp = campaigns.get(str(sim.campaign_id), {})
        entity = {
            "name": camp.get("name") or str(sim.campaign_id),
            "id": sim.campaign_id,
            "target": camp.get("target"),
            "bidding_type": camp.get("type", ""),
            "start": sim.start_date,
            "end": sim.end_date,
        }
        for p in points:
            out.append(_point_row(customer, entity, p, run_date))
    return out


def _portfolio_simulations(service, cid: str, customer: dict, strategies: dict,
                           run_date: str) -> list[list]:
    """Target-ROAS simulations for portfolio (shared) bidding strategies."""
    out: list[list] = []
    query = (
        "SELECT bidding_strategy_simulation.bidding_strategy_id, "
        "bidding_strategy_simulation.start_date, bidding_strategy_simulation.end_date, "
        "bidding_strategy_simulation.type, "
        "bidding_strategy_simulation.target_roas_point_list.points "
        "FROM bidding_strategy_simulation "
        "WHERE bidding_strategy_simulation.type = 'TARGET_ROAS'"
    )
    for row in _search(service, cid, query):
        sim = row.bidding_strategy_simulation
        points = sim.target_roas_point_list.points
        if not points:
            continue
        strat = strategies.get(str(sim.bidding_strategy_id), {})
        entity = {
            "name": strat.get("name") or str(sim.bidding_strategy_id),
            "id": sim.bidding_strategy_id,
            "target": strat.get("target"),
            "bidding_type": strat.get("type", ""),
            "start": sim.start_date,
            "end": sim.end_date,
        }
        for p in points:
            out.append(_point_row(customer, entity, p, run_date))
    return out


def _campaign_shares(service, cid: str, customer: dict, run_date: str) -> list[list]:
    """
    Last-7-days impression share per ENABLED campaign — the auction-headroom input behind
    the dashboard's dynamic incrementality factors.

    Google reports these metrics BUCKETED at the extremes ("<10%" arrives as 0.0999,
    anything above 90% as a value just over 0.9). They are taken at face value; the
    bucketing is documented, not smoothed away.

    A campaign type that reports no impression share at all (Performance Max, Display,
    video) yields ABSENT metrics, written as None → blank. Never 0.
    """
    out: list[list] = []
    query = (
        "SELECT campaign.id, campaign.name, metrics.search_impression_share, "
        "metrics.search_top_impression_share, metrics.search_absolute_top_impression_share "
        "FROM campaign "
        "WHERE segments.date DURING LAST_7_DAYS AND campaign.status = 'ENABLED'"
    )
    for row in _search(service, cid, query):
        m = row.metrics
        search  = _opt(m, "search_impression_share")
        top     = _opt(m, "search_top_impression_share")
        abs_top = _opt(m, "search_absolute_top_impression_share")
        if search is None and top is None and abs_top is None:
            continue                                   # nothing worth a row
        out.append([
            customer["name"], str(row.campaign.id), _safe_name(row.campaign.name),
            search, top, abs_top, customer["currency"], run_date,
        ])
    return out


def _simulation_windows(rows: list[list]) -> list[dict]:
    """
    Group already-built simulation rows by their simulation window, so one query covers
    every entity Google simulated over it. A blank start/end means the simulation carried
    no window — those fall back to LAST_7_DAYS downstream.
    """
    by_window: dict[str, dict] = {}
    order: list[str] = []
    for r in rows:
        ident = str(r[SIM_ID_IDX] or "").strip()
        if not ident:
            continue                                   # nothing to join an actuals row to
        start, end = _iso_or_blank(r[SIM_START_IDX]), _iso_or_blank(r[SIM_END_IDX])
        key = start + "|" + end
        if key not in by_window:
            by_window[key] = {"start": start, "end": end, "ids": {}}
            order.append(key)
        by_window[key]["ids"][ident] = str(r[SIM_NAME_IDX] or "")
    return [by_window[k] for k in order]


ACTUAL_SOURCES = {
    "campaign":  {"level": "campaign",  "resource": "campaign",         "attr": "campaign"},
    "portfolio": {"level": "portfolio", "resource": "bidding_strategy", "attr": "bidding_strategy"},
}

# Error vocabulary that means "this resource will not SERVE that field", as opposed to a
# transient failure. Deliberately narrow (isUnsupportedFieldError in the Ads script):
# treating an unrecognised error as transient is the safe side, because dropping the
# conversion-time metrics on a timeout would replace a visible gap with a half-populated
# column that looks fine.
_UNSUPPORTED_MARKERS = (
    "conversion_date", "unrecognized_field", "unrecognized field", "unknown field",
    "prohibited_field", "prohibited field", "field_error", "not supported", "unsupported",
    "cannot be selected", "cannot be used", "selected together", "not selectable",
)


def _is_unsupported_field_error(e: BaseException) -> bool:
    msg = _error_text(e).lower()
    return any(marker in msg for marker in _UNSUPPORTED_MARKERS)


def _query_actuals(service, cid: str, customer: dict, win: dict, source: dict,
                   run_date: str, with_conv_time: bool) -> list[list]:
    """
    One row per simulated entity for one window, carrying cost plus BOTH attribution
    schemes. segments.date is FILTERED but not SELECTED, so the API aggregates the whole
    window into one row per entity.

    Every entity in the account comes back; rows whose id was not simulated are dropped
    client-side (a GAQL id list long enough for a large account is worse than a filter), as
    are rows with no spend in the window — ROAS is undefined there.
    """
    wanted = win["ids"]
    if not wanted:
        return []

    # Dates reach GAQL only after _iso_or_blank() has validated them as yyyy-MM-dd.
    rng = (f"segments.date BETWEEN '{win['start']}' AND '{win['end']}'"
           if win["start"] and win["end"] else "segments.date DURING LAST_7_DAYS")
    conv_time = (", metrics.conversions_by_conversion_date, "
                 "metrics.conversions_value_by_conversion_date" if with_conv_time else "")
    query = (
        f"SELECT {source['resource']}.id, {source['resource']}.name, metrics.cost_micros, "
        f"metrics.conversions, metrics.conversions_value{conv_time} "
        f"FROM {source['resource']} WHERE {rng}"
    )

    out: list[list] = []
    for row in _search(service, cid, query):
        ent = getattr(row, source["attr"])
        ident = str(ent.id or "")
        if not ident or ident not in wanted:
            continue
        m = row.metrics
        cost = _opt(m, "cost_micros")
        if not (cost is not None and cost > 0):
            continue                                   # no spend: ROAS is undefined
        out.append([
            customer["name"], ident, _safe_name(wanted[ident] or ent.name), source["level"],
            win["start"], win["end"], int(cost),
            _opt(m, "conversions"), _opt(m, "conversions_value"),
            _opt(m, "conversions_by_conversion_date"),
            _opt(m, "conversions_value_by_conversion_date"),
            customer["currency"], run_date,
        ])
    return out


def _window_actuals(service, cid: str, customer: dict, win: dict, source: dict,
                    run_date: str, warnings: list[str]) -> list[list]:
    """
    One window's actuals, with the only two failure modes worth telling apart:

      the resource will not SERVE the conversion-time metrics
          Retry this window without them. Click-time actuals still arrive, the
          conversion-time cells stay BLANK and the dashboard shows a dash. Degrade,
          never vanish.
      anything else (timeout, quota, internal error)
          Skip THIS WINDOW only and log it.
    """
    label = (f"{source['level']} "
             f"{win['start'] + '..' + win['end'] if win['start'] and win['end'] else 'last 7 days'}")
    try:
        return _query_actuals(service, cid, customer, win, source, run_date, True)
    except Exception as e:
        if not _is_unsupported_field_error(e):
            warnings.append(f"actuals {label} skipped: {_error_text(e)}")
            return []
        warnings.append(f"actuals {label}: conversion-time metrics unavailable, "
                        f"retrying click-time only ({_error_text(e)})")
    try:
        return _query_actuals(service, cid, customer, win, source, run_date, False)
    except Exception as e2:
        warnings.append(f"actuals {label}: failed even without the conversion-time "
                        f"metrics ({_error_text(e2)})")
        return []


def collect_account(service, account: dict, run_date: str) -> dict:
    """
    Everything one child account contributes. Simulations are the primary payload: a
    failure there is the account's failure. Shares and actuals are SECONDARY — each has
    its own guard, because losing them only costs a factor refinement or two display
    columns, never the run.
    """
    cid = account["cid"]
    warnings: list[str] = []
    customer = _account_info(service, cid, account)

    strategies = _strategy_map(service, cid)
    rows: list[list] = []
    plans: list[dict] = []

    if INCLUDE_PORTFOLIO:
        prows = _portfolio_simulations(service, cid, customer, strategies, run_date)
        rows += prows
        plans.append({"source": ACTUAL_SOURCES["portfolio"], "windows": _simulation_windows(prows)})
    if INCLUDE_CAMPAIGNS:
        campaigns = _campaign_map(service, cid, strategies)
        crows = _campaign_simulations(service, cid, customer, campaigns, run_date)
        rows += crows
        plans.append({"source": ACTUAL_SOURCES["campaign"], "windows": _simulation_windows(crows)})

    shares: list[list] = []
    if COLLECT_SHARES:
        try:
            shares = _campaign_shares(service, cid, customer, run_date)
        except Exception as e:
            warnings.append(f"impression shares: {_error_text(e)}")

    actuals: list[list] = []
    if COLLECT_ACTUALS:
        try:
            for plan in plans:
                for win in plan["windows"]:
                    actuals += _window_actuals(service, cid, customer, win, plan["source"],
                                               run_date, warnings)
        except Exception as e:
            warnings.append(f"actuals: {_error_text(e)}")

    return {
        "cid": cid, "name": customer["name"], "currency": customer["currency"],
        "status": "ok", "rows": rows, "shares": shares, "actuals": actuals,
        "warnings": warnings,
    }


# ════════════════════════════════════════════════════════════════════════════
#  Snapshot build + write
# ════════════════════════════════════════════════════════════════════════════

def _json_bytes(rows: list) -> tuple[str, int]:
    s = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
    return s, len(s.encode("utf-8"))


def build_snapshot(run_date: str | None = None) -> dict:
    """
    Collect every configured account and assemble one daily snapshot document.

    Per-account failures are captured as status="error" rather than raised, so one dead
    account can never lose the other seven. The run is `ok` when at least one account
    returned.
    """
    run_date = run_date or _dt.date.today().isoformat()
    if not RUN_DATE_RE.match(run_date):
        raise ValueError(f"run_date must be YYYY-MM-DD, got {run_date!r}")

    client = ads_client()
    service = client.get_service("GoogleAdsService")

    rows: list[list] = []
    shares: list[list] = []
    actuals: list[list] = []
    accounts: list[dict] = []

    for account in ACCOUNTS:
        try:
            res = collect_account(service, account, run_date)
        except Exception as e:
            accounts.append({
                "cid": account["cid"], "name": account["label"],
                "currency": account["currency"], "status": "error",
                "error": _error_text(e)[:1500],
                "sim_rows": 0, "share_rows": 0, "actual_rows": 0, "warnings": [],
            })
            continue
        rows += res["rows"]
        shares += res["shares"]
        actuals += res["actuals"]
        accounts.append({
            "cid": res["cid"], "name": res["name"], "currency": res["currency"],
            "status": "ok", "error": "",
            "sim_rows": len(res["rows"]), "share_rows": len(res["shares"]),
            "actual_rows": len(res["actuals"]),
            "warnings": [w[:500] for w in res["warnings"][:20]],
        })

    # Fit the three grids inside the Firestore document budget, trimming in the Ads
    # script's value order: actuals are display-only, shares change which incrementality
    # factor a recommendation was read off, and the simulations ARE the run.
    dropped: list[str] = []
    rows_json, n_rows = _json_bytes(rows)
    shares_json, n_shares = _json_bytes(shares)
    actuals_json, n_actuals = _json_bytes(actuals)

    if n_rows + n_shares + n_actuals > DATASET_BUDGET_BYTES and actuals:
        actuals = []
        actuals_json, n_actuals = _json_bytes(actuals)
        dropped.append("actuals")
    if n_rows + n_shares + n_actuals > DATASET_BUDGET_BYTES and shares:
        shares = []
        shares_json, n_shares = _json_bytes(shares)
        dropped.append("shares")
    if n_rows > DATASET_BUDGET_BYTES:
        # Last resort: drop the oldest complete rows until the grid fits. Trimming on a row
        # boundary matters — half a simulation curve is worse than a missing one.
        while rows and n_rows > DATASET_BUDGET_BYTES:
            rows = rows[max(1, len(rows) // 10):]
            rows_json, n_rows = _json_bytes(rows)
        dropped.append("simulation-rows-trimmed")

    ok_accounts = [a for a in accounts if a["status"] == "ok"]
    return {
        "run_date": run_date,
        "generated_at_iso": _dt.datetime.now(_dt.timezone.utc)
                              .replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "source": SOURCE,
        "api_version": API_VERSION,
        "ok": bool(ok_accounts),
        "include_campaigns": INCLUDE_CAMPAIGNS,
        "include_portfolio": INCLUDE_PORTFOLIO,
        "columns": {"raw": RAW_COLUMNS, "shares": SHARE_COLUMNS, "actuals": ACTUAL_COLUMNS},
        "counts": {"raw": len(rows), "shares": len(shares), "actuals": len(actuals)},
        # Firestore cannot nest an array inside an array, so the row grids ride as JSON.
        "rows_json": rows_json,
        "shares_json": shares_json,
        "actuals_json": actuals_json,
        "accounts": accounts,
        "dropped_datasets": dropped,
    }


def _firestore():
    """Firestore client, resolving credentials the way refresh_roas_impact.py does."""
    from google.cloud import firestore   # lazy import (local dev w/o lib still imports)
    try:
        import google.auth
        creds, project = google.auth.default()
    except Exception:
        import subprocess
        import google.oauth2.credentials
        tok = subprocess.check_output(["gcloud", "auth", "print-access-token"]).decode().strip()
        creds, project = google.oauth2.credentials.Credentials(tok), None
    return firestore.Client(project=FIRESTORE_PROJECT or project, credentials=creds)


def write_snapshot(snapshot: dict, db=None) -> dict:
    """
    Write one snapshot to roas_sim_snapshots/<run_date> and prune past RETENTION_DAYS.

    Idempotent per day by construction: the document id IS the run date, so a re-run
    overwrites (set() without merge — a shrinking dataset must not leave stale keys behind).
    """
    from google.cloud import firestore
    db = db or _firestore()
    doc = dict(snapshot)
    doc["generated_at"] = firestore.SERVER_TIMESTAMP
    db.collection(SNAPSHOT_COLLECTION).document(snapshot["run_date"]).set(doc)
    pruned = prune_snapshots(snapshot["run_date"], db=db)
    return {"written": snapshot["run_date"], "pruned": pruned}


def prune_snapshots(run_date: str, db=None) -> list[str]:
    """
    Drop snapshots older than RETENTION_DAYS.

    Document ids are ISO dates, so a lexical comparison is a chronological one and
    list_documents() needs no index — deliberately no where+order_by anywhere in this
    module (CLAUDE.md "Query patterns").
    """
    db = db or _firestore()
    cutoff = (_dt.date.fromisoformat(run_date) - _dt.timedelta(days=RETENTION_DAYS)).isoformat()
    dropped = []
    for ref in db.collection(SNAPSHOT_COLLECTION).list_documents():
        if RUN_DATE_RE.match(ref.id) and ref.id < cutoff:
            ref.delete()
            dropped.append(ref.id)
    return dropped


def refresh(run_date: str | None = None, db=None) -> dict:
    """Collect + persist. The entry point /internal/refresh-roas-sims calls."""
    started = time.time()
    snapshot = build_snapshot(run_date)
    written = {"written": None, "pruned": []}
    if not os.environ.get("SKIP_FIRESTORE"):
        written = write_snapshot(snapshot, db=db)
    return {
        "status": "ok" if snapshot["ok"] else "error",
        "run_date": snapshot["run_date"],
        "counts": snapshot["counts"],
        "accounts": [{k: a[k] for k in ("cid", "name", "status", "error", "sim_rows",
                                        "share_rows", "actual_rows", "warnings")}
                     for a in snapshot["accounts"]],
        "dropped_datasets": snapshot["dropped_datasets"],
        "seconds": round(time.time() - started, 1),
        **written,
    }


# ════════════════════════════════════════════════════════════════════════════
#  Config document — what used to be the sheet's Config tab
# ════════════════════════════════════════════════════════════════════════════

def _clean_config(raw: dict | None) -> dict:
    """
    Merge a stored config doc over DEFAULT_CONFIG, parsing every number the forgiving way
    the sheet did ("0,2", "20" and "20%" all mean 0.20). An unreadable value falls back to
    the default rather than poisoning the payload, exactly like a comment row in the tab.
    """
    out = json.loads(json.dumps(DEFAULT_CONFIG))     # deep copy of plain JSON data
    if not isinstance(raw, dict):
        return out

    mults = raw.get("valueToGp2Multipliers")
    if isinstance(mults, dict):
        for name, v in mults.items():
            m = _to_multiplier(v)
            if m is not None and str(name).strip():
                out["valueToGp2Multipliers"][str(name).strip()] = m
    dflt = _to_multiplier(raw.get("defaultValueToGp2Multiplier"))
    if dflt is not None:
        out["defaultValueToGp2Multiplier"] = dflt

    inc = raw.get("incrementality")
    if isinstance(inc, dict):
        classes = inc.get("classes")
        if isinstance(classes, dict):
            for cls, v in classes.items():
                f = _to_factor(v)
                if f is not None and cls in out["incrementality"]["classes"]:
                    out["incrementality"]["classes"][cls] = f
        weights = inc.get("shareWeights")
        if isinstance(weights, dict):
            for cls, bounds in weights.items():
                if cls not in out["incrementality"]["shareWeights"] or not isinstance(bounds, dict):
                    continue
                for bound in ("floor", "cap"):
                    f = _to_factor(bounds.get(bound))
                    if f is not None:
                        out["incrementality"]["shareWeights"][cls][bound] = f
        overrides = inc.get("overrides")
        if isinstance(overrides, list):
            for o in overrides:
                if not isinstance(o, dict):
                    continue
                pattern = str(o.get("pattern", "")).strip()
                f = _to_factor(o.get("factor"))
                if pattern and f is not None:
                    out["incrementality"]["overrides"].append({"pattern": pattern, "factor": f})
    return out


def load_config(db=None, seed: bool = False) -> dict:
    """
    Read roas_sim_config/config, merged over the built-in defaults.

    `seed=True` writes the defaults back when the document is absent, so the operator has
    something concrete to edit in the Firestore console. Never overwrites an existing doc.
    A Firestore failure degrades to the defaults — the dashboard has the same numbers baked
    in, so a missing config block has never been an error.
    """
    try:
        db = db or _firestore()
        ref = db.collection(CONFIG_COLLECTION).document(CONFIG_DOC)
        snap = ref.get()
        if not snap.exists:
            if seed:
                ref.set(json.loads(json.dumps(DEFAULT_CONFIG)))
            return _clean_config(None)
        return _clean_config(snap.to_dict())
    except Exception:
        return _clean_config(None)


# ════════════════════════════════════════════════════════════════════════════
#  Read path — assemble the payload shape pipeline/webapp.gs served
# ════════════════════════════════════════════════════════════════════════════

def _column_index(columns: list[str], name: str) -> int:
    want = re.sub(r"[^a-z0-9]", "", name.lower())
    for i, c in enumerate(columns):
        if re.sub(r"[^a-z0-9]", "", str(c).lower()) == want:
            return i
    return -1


def _load_json_rows(doc: dict, key: str, width: int) -> list[list]:
    """
    Decode one stored grid, padding or trimming every row to exactly `width`.

    Same guarantee decodeRows() gave in gp3-simulations.js: one malformed row must not be
    able to abort the whole read, and no downstream index can ever run off the end.
    """
    raw = doc.get(key)
    if not raw:
        return []
    try:
        rows = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(rows, list):
        return []
    out = []
    for r in rows:
        if not isinstance(r, list):
            continue
        out.append(list(r[:width]) + [""] * max(0, width - len(r)))
    return out


def latest_snapshots(limit: int, db=None) -> list[dict]:
    """
    The `limit` most recent snapshot documents, oldest first.

    Document ids are ISO dates, so list_documents() + a Python-side sort gives
    chronological order with NO index and no where+order_by (CLAUDE.md "Query patterns").
    The selected refs are then fetched in one get_all() batch.
    """
    db = db or _firestore()
    ids = sorted(ref.id for ref in db.collection(SNAPSHOT_COLLECTION).list_documents()
                 if RUN_DATE_RE.match(ref.id))
    if limit > 0:
        ids = ids[-limit:]
    if not ids:
        return []
    col = db.collection(SNAPSHOT_COLLECTION)
    docs = [s.to_dict() for s in db.get_all([col.document(i) for i in ids]) if s.exists]
    docs = [d for d in docs if isinstance(d, dict) and d.get("run_date")]
    docs.sort(key=lambda d: d["run_date"])
    return docs


def build_payload(runs: int = 0, account: str = "", db=None) -> dict:
    """
    Serve the SAME JSON payload doGet() in pipeline/webapp.gs returned, so the dashboard's
    normalize path (normalizePayload / normalizeShares / normalizeActuals) runs unchanged:

      { generatedAt, source, spreadsheet, config, columns, rows, rowCount, runDates,
        truncated, shares: {columns, rows, runDates}|null,
                   actuals: {columns, rows, runDates}|null }

    `runs`    keep only the N most recent run dates (0 = all). Applies to all three grids,
              with the SAME asymmetry webapp.gs had: without it `shares` carries the latest
              run date ONLY — auction position is a statement about now, and a factor
              derived from three-week-old share data would be worse than the flat prior it
              replaces — while `actuals` carries every run present, because an actuals row
              is joined to the simulation snapshot of its own run.
    `account` exact Customer Name filter, applied to all three grids.

    An empty collection returns the `no-snapshots` shape: an `error` string (so the
    dashboard falls back to its cached/demo payload exactly as it does for a dead endpoint)
    plus `status: "no-snapshots"`, which is what tells a caller this is NOT an auth failure.
    """
    db = db or _firestore()
    # One snapshot per day, so the document budget IS the run budget — but a day on which
    # the requested account errored carries no rows for it and would still burn a slot, so
    # fetch a few spare days and let the run-date limiter below do the real narrowing.
    want_docs = RETENTION_DAYS + 1 if runs <= 0 else min(runs + 10, RETENTION_DAYS + 1)
    docs = latest_snapshots(want_docs, db=db)
    config = load_config(db=db)
    generated_at = (_dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)
                    .isoformat().replace("+00:00", "Z"))

    if not docs:
        return {
            "generatedAt": generated_at,
            "source": SOURCE,
            "spreadsheet": SNAPSHOT_COLLECTION,
            "status": "no-snapshots",
            "error": ("No ROAS simulation snapshots yet — run POST /internal/refresh-roas-sims "
                      "once the Google Ads credentials are in place."),
            "config": config,
            "columns": RAW_COLUMNS, "rows": [], "rowCount": 0, "runDates": [],
            "truncated": False, "shares": None, "actuals": None,
        }

    # The newest snapshot names the grids. A doc written by an older build (or a hand-edit)
    # that lost the block falls back to this module's headers rather than serving nothing.
    columns = docs[-1].get("columns")
    columns = columns if isinstance(columns, dict) else {}
    raw_cols    = list(columns.get("raw") or RAW_COLUMNS)
    share_cols  = list(columns.get("shares") or SHARE_COLUMNS)
    actual_cols = list(columns.get("actuals") or ACTUAL_COLUMNS)

    def _collect(key: str, cols: list[str]) -> list[list]:
        out: list[list] = []
        acct_idx = _column_index(cols, "Customer Name")
        wanted = str(account).strip()
        for d in docs:
            for r in _load_json_rows(d, key, len(cols)):
                if wanted and acct_idx >= 0 and str(r[acct_idx]).strip() != wanted:
                    continue
                out.append(r)
        return out

    rows    = _collect("rows_json", raw_cols)
    shares  = _collect("shares_json", share_cols)
    actuals = _collect("actuals_json", actual_cols)

    def _run_dates(grid: list[list], cols: list[str]) -> tuple[list[str], int]:
        idx = _column_index(cols, "Run Date")
        if idx < 0:
            return [], idx
        return sorted({str(r[idx]) for r in grid if r[idx]}), idx

    def _limit_runs(grid: list[list], cols: list[str], limit: int) -> tuple[list[list], list[str]]:
        dates, idx = _run_dates(grid, cols)
        if idx < 0 or limit <= 0 or len(dates) <= limit:
            return grid, dates
        keep = set(dates[-limit:])
        return [r for r in grid if str(r[idx]) in keep], dates[-limit:]

    rows, run_dates       = _limit_runs(rows, raw_cols, runs)
    # Shares default to the latest run date only; `runs=N` widens it exactly like Raw.
    shares, share_dates   = _limit_runs(shares, share_cols, runs if runs > 0 else 1)
    actuals, actual_dates = _limit_runs(actuals, actual_cols, runs)

    # Cap by WHOLE runs, oldest first, so a partial run can never skew a trend point and
    # runDates always describes exactly the rows returned (readRaw() in webapp.gs).
    truncated = False
    run_idx = _column_index(raw_cols, "Run Date")
    if run_idx >= 0:
        while len(rows) > MAX_ROWS and len(run_dates) > 1:
            drop = run_dates.pop(0)
            rows = [r for r in rows if str(r[run_idx]) != drop]
            truncated = True
    if len(rows) > MAX_ROWS:
        rows, truncated = rows[-MAX_ROWS:], True
    if len(shares) > MAX_ROWS:
        shares = shares[-MAX_ROWS:]
    if len(actuals) > MAX_ROWS:
        actuals = actuals[-MAX_ROWS:]

    def _normalise(grid: list[list], cols: list[str], numeric: list[str], blank_is_zero: bool):
        idxs = [i for i in (_column_index(cols, h) for h in numeric) if i >= 0]
        fn = _metric_or_zero if blank_is_zero else _metric
        for r in grid:
            for i in idxs:
                r[i] = fn(r[i])
        return grid

    rows = _normalise(rows, raw_cols, RAW_NUMERIC_COLUMNS, True)
    actuals = _normalise(actuals, actual_cols, ACTUAL_NUMERIC_COLUMNS, False)
    share_idxs = [i for i in (_column_index(share_cols, h) for h in SHARE_METRIC_COLUMNS) if i >= 0]
    for r in shares:
        for i in share_idxs:
            r[i] = _share(r[i])

    return {
        "generatedAt": generated_at,
        "source": SOURCE,
        "spreadsheet": SNAPSHOT_COLLECTION,
        "status": "ok",
        "config": config,
        "columns": raw_cols,
        "rows": rows,
        "rowCount": len(rows),
        "runDates": run_dates,
        "truncated": truncated,
        # Absent is a NORMAL state on both, never an error: no shares means every campaign
        # keeps its flat class factor, no actuals means the actual-ROAS columns are not
        # rendered at all.
        "shares": {"columns": share_cols, "rows": shares, "runDates": share_dates}
                  if shares else None,
        "actuals": {"columns": actual_cols, "rows": actuals, "runDates": actual_dates}
                   if actuals else None,
    }


# ════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    out = refresh()
    print(f"ROAS sims · run {out['run_date']} · {out['counts']['raw']} simulation point(s), "
          f"{out['counts']['shares']} share row(s), {out['counts']['actuals']} actual row(s) "
          f"in {out['seconds']}s")
    for a in out["accounts"]:
        line = f"  {a['name']:<16} {a['status']:<5} sims={a['sim_rows']:<5} " \
               f"shares={a['share_rows']:<4} actuals={a['actual_rows']}"
        print(line + (f"  ERROR {a['error']}" if a["error"] else ""))
        for w in a["warnings"]:
            print(f"      warn: {w}")
    if out.get("pruned"):
        print(f"  pruned {len(out['pruned'])} snapshot(s) past {RETENTION_DAYS} days")
    dest = os.environ.get("ROAS_SIMS_OUT")
    if dest:
        with open(dest, "w") as f:
            json.dump(build_payload(runs=30), f, ensure_ascii=False)
        print(f"  wrote {dest}")
