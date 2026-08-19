#!/usr/bin/env python3
"""
Share of Search — Google Ads Keyword Planner collector + snapshot reader.

Pulls monthly historical search volumes for a curated, per-market keyword set via
KeywordPlanIdeaService.GenerateKeywordHistoricalMetrics (the API behind the Keyword
Planner "historical metrics" screen the old manual Google-Sheet pull used), and merges
them into per-market Firestore history docs the dashboard tab reads.

Mirrors refresh_roas_sims.py: module-level config, a collect step, a Firestore writer,
a run lock, and a `__main__` block that can be run locally.

WHY MERGE, NEVER REPLACE
  The API returns a sliding ~4-year window of monthly volumes. Replacing the doc on
  each run would silently drop the oldest months forever; merging means history only
  ever grows. Months the API can no longer reach (before ~Sep 2022) come from the
  one-time SEED_HISTORY import of the old manual Sweden sheet and stay put.

CADENCE
  Monthly. Google finalises the previous month's Keyword Planner volumes in the second
  week of the month, so Cloud Scheduler fires /internal/refresh-sos on the 15th. Four
  API calls per run (one per market) — quota cost is negligible, but the run lock still
  guards against Scheduler retry overlap.

FIRESTORE (dedicated collections — never share one with another feature)
  sos_history/<MARKET>   one doc per market: {"series": {kw: {"YYYY-MM": vol}},
                         "meta": {kw: {...}}, ...}. Written with merge=True and NESTED
                         dicts (never dotted keys — see CLAUDE.md "Firestore").
  sos_config/config      USER-EDITABLE keyword set + market registry. seed_config()
                         creates it from DEFAULT_CONFIG on the first successful refresh
                         and never overwrites it — edits in the console survive deploys.
  sos_runs/<YYYY-MM-DD>  small per-run audit record (keyword/month counts + errors),
                         pruned past RETENTION_DAYS.
  sos_locks/refresh      self-expiring run lock, same shape as roas_sim_locks.

KEYWORD SET (curated 19 Aug 2026 with Patrik — see the Share of Search proposal artifact)
  group  brand          babyshop + lekmer (Lekmer is a Babyshop Group brand)
         private_label  kuling, stoy, buddy & hope, ng baby, carena, little jalo
         competitor     tiered: hyper (broad baby/kids specialist), core (premium /
                        functional kidswear — Babyshop's positioning), adjacent
                        (kid-scoped fast fashion, OFF by default in the headline
                        share), legacy (kept only for old-sheet history continuity)
         retail_brand   brands Babyshop sells; ambiguous names are kid-scoped in the
                        local language ("moncler barn") so adult volume can't inflate
         category       8 buckets x 2-3 canonical terms, localised per market
  One canonical term per concept: Keyword Planner folds plurals/misspellings/spacing
  variants into one volume, so tracking two variants double-counts.

CREDENTIALS
  The same five GOOGLE_ADS_* env vars refresh_roas_sims.py uses (wired from Secret
  Manager — see pipeline/PIPELINE.md). Requests run against each market's own child
  account under the MCC login-customer-id.
"""

from __future__ import annotations

import datetime as _dt
import os
import re
import time
from typing import Any

API_VERSION = "v25"          # google-ads 31.x default; keep in step with requirements.txt

FIRESTORE_PROJECT  = os.environ.get("FIRESTORE_PROJECT", "project-a7ade44e-e7e3-4871-a83")
HISTORY_COLLECTION = os.environ.get("SOS_HISTORY_COLLECTION", "sos_history")
CONFIG_COLLECTION  = os.environ.get("SOS_CONFIG_COLLECTION", "sos_config")
CONFIG_DOC         = "config"
RUNS_COLLECTION    = os.environ.get("SOS_RUNS_COLLECTION", "sos_runs")
LOCK_COLLECTION    = os.environ.get("SOS_LOCK_COLLECTION", "sos_locks")
LOCK_DOC           = "refresh"
LOCK_TTL_SECONDS   = int(os.environ.get("SOS_LOCK_TTL", "900"))    # 4 API calls — 15 min is generous
RETENTION_DAYS     = int(os.environ.get("SOS_RUNS_RETENTION_DAYS", "180"))

RUN_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\Z")
YM_RE       = re.compile(r"^\d{4}-\d{2}\Z")

ADS_ENV_KEYS = (
    "GOOGLE_ADS_DEVELOPER_TOKEN",
    "GOOGLE_ADS_CLIENT_ID",
    "GOOGLE_ADS_CLIENT_SECRET",
    "GOOGLE_ADS_REFRESH_TOKEN",
    "GOOGLE_ADS_LOGIN_CUSTOMER_ID",
)


class MissingCredentials(RuntimeError):
    """No Google Ads credentials in the environment — the collector cannot run."""


class RefreshInProgress(RuntimeError):
    """Another collection holds the run lock. Skipping is the correct response."""


# ════════════════════════════════════════════════════════════════════════════
#  Default config — seeded into sos_config/config on first run, edited there.
# ════════════════════════════════════════════════════════════════════════════

def _kw(kw: str, group: str, tier: str | None = None, bucket: str | None = None) -> dict:
    d: dict[str, Any] = {"kw": kw, "group": group}
    if tier:
        d["tier"] = tier
    if bucket:
        d["bucket"] = bucket
    return d


def _brands_and_labels() -> list[dict]:
    """Group brands + private labels — identical (brand names) in every market."""
    out = [_kw("babyshop", "brand"), _kw("lekmer", "brand")]
    for b in ("kuling", "stoy", "buddy & hope", "ng baby", "carena", "little jalo"):
        out.append(_kw(b, "private_label"))
    return out


def _competitors(hyper: list, core: list, adjacent: list, legacy: list = []) -> list[dict]:
    return ([_kw(k, "competitor", tier="hyper") for k in hyper]
            + [_kw(k, "competitor", tier="core") for k in core]
            + [_kw(k, "competitor", tier="adjacent") for k in adjacent]
            + [_kw(k, "competitor", tier="legacy") for k in legacy])


def _retail(*kws: str) -> list[dict]:
    return [_kw(k, "retail_brand") for k in kws]


def _categories(buckets: dict[str, list[str]]) -> list[dict]:
    return [_kw(k, "category", bucket=b) for b, kws in buckets.items() for k in kws]


DEFAULT_CONFIG: dict[str, Any] = {
    # Geo ids are Google geo target constants (ISO-3166 numeric + 2000):
    # SE 2752, NO 2578, DK 2208, FI 2246. No language filter on purpose — brand
    # terms are typed in any language and geo alone scopes the market.
    "markets": {
        "SE": {"customer_id": "4851485396", "geo": "2752", "label": "Sweden"},
        "NO": {"customer_id": "8623945183", "geo": "2578", "label": "Norway"},
        "DK": {"customer_id": "2054294342", "geo": "2208", "label": "Denmark"},
        "FI": {"customer_id": "6161399704", "geo": "2246", "label": "Finland"},
    },
    "keywords": {
        "SE": (_brands_and_labels()
               + _competitors(hyper=["jollyroom", "babysam", "babyland", "babyworld"],
                              core=["kidsbrandstore", "polarn o pyret"],
                              adjacent=["hm barn"],
                              legacy=["stor & liten", "babymarkt"])
               + _retail("mini rodini", "reima", "molo", "moncler barn",
                         "ralph lauren barn", "new balance barn", "gant barn", "ugg barn")
               + _categories({
                   # Granular buckets (19 Aug 2026): rainwear, winter coveralls,
                   # shell, balance bikes and kids' bikes are hero categories and
                   # get their own trend lines instead of blended outerwear/wheels.
                   "kidswear":         ["barnkläder", "babykläder"],
                   "rainwear":         ["regnkläder barn", "regnställ barn", "galonbyxor"],
                   "winter_coveralls": ["vinteroverall", "vinterkläder barn"],
                   "shell":            ["skalkläder barn", "skaloverall", "skaljacka barn"],
                   "footwear":         ["barnskor", "vinterskor barn", "gummistövlar barn"],
                   "strollers":        ["barnvagn", "resevagn", "syskonvagn"],
                   "car_seats":        ["bilbarnstol", "babyskydd", "bältesstol"],
                   "nursery":          ["spjälsäng", "skötbord", "åkpåse"],
                   "balance_bikes":    ["balanscykel", "springcykel"],
                   "kids_bikes":       ["barncykel", "trehjuling barn"],
                   "scooters":         ["sparkcykel barn"],
                   "uv_swim":          ["uv dräkt barn", "badblöja", "badkläder barn"],
               })),
        "NO": (_brands_and_labels()
               + _competitors(hyper=["jollyroom", "barnas hus", "babycare", "pinkorblue"],
                              core=["dressmykid", "polarn o pyret"],
                              adjacent=["hm barn"])
               # "molo" alone means pier/breakwater in Norwegian — scope is mandatory.
               + _retail("mini rodini", "reima", "molo barneklær", "moncler barn",
                         "ralph lauren barn", "new balance barn", "gant barn", "ugg barn")
               + _categories({
                   "kidswear":         ["barneklær", "babyklær"],
                   "rainwear":         ["regntøy barn", "regndress barn"],
                   "winter_coveralls": ["vinterdress barn", "parkdress barn"],
                   "shell":            ["skalldress barn", "skalljakke barn"],
                   "footwear":         ["barnesko", "vintersko barn", "gummistøvler barn"],
                   "strollers":        ["barnevogn", "sportsvogn", "søskenvogn"],
                   "car_seats":        ["bilstol barn", "barnesete bil", "beltestol"],
                   "nursery":          ["barneseng", "stellebord", "sovepose baby"],
                   "balance_bikes":    ["balansesykkel", "løpesykkel"],
                   "kids_bikes":       ["barnesykkel", "trehjulssykkel"],
                   "scooters":         ["sparkesykkel barn"],
                   "uv_swim":          ["uv drakt barn", "badebleie", "badetøy barn"],
               })),
        "DK": (_brands_and_labels()
               + _competitors(hyper=["babysam", "ønskebørn", "jollyroom"],
                              core=["luksusbaby", "kids world", "konges sløjd",
                                    "lirum larum leg"],
                              adjacent=["boozt børnetøj"])
               + _retail("mini rodini", "reima", "molo børnetøj", "moncler børn",
                         "ralph lauren børn", "new balance børn", "gant børn", "ugg børn")
               + _categories({
                   "kidswear":         ["børnetøj", "babytøj"],
                   "rainwear":         ["regntøj børn", "regnsæt børn"],
                   "winter_coveralls": ["flyverdragt", "termodragt"],
                   "shell":            ["skaljakke børn", "softshell dragt"],
                   "footwear":         ["børnesko", "vinterstøvler børn", "gummistøvler børn"],
                   "strollers":        ["barnevogn", "klapvogn", "søskendevogn"],
                   "car_seats":        ["autostol", "babyautostol", "selepude"],
                   "nursery":          ["tremmeseng", "puslebord", "sovepose baby"],
                   "balance_bikes":    ["løbecykel", "balancecykel"],
                   "kids_bikes":       ["børnecykel", "trehjulet cykel"],
                   "scooters":         ["løbehjul"],
                   "uv_swim":          ["uv dragt baby", "badebleer", "badetøj børn"],
               })),
        "FI": (_brands_and_labels()
               + _competitors(hyper=["jollyroom", "lastentarvike", "vauvan maailma",
                                     "nordbaby"],
                              core=["polarn o pyret", "name it"],
                              adjacent=["h&m lastenvaatteet"])
               + _retail("mini rodini", "reima", "molo lastenvaatteet",
                         "moncler lasten takki", "ralph lauren lastenvaatteet",
                         "new balance lasten kengät", "gant lastenvaatteet",
                         "ugg lasten kengät")
               + _categories({
                   "kidswear":         ["lastenvaatteet", "vauvanvaatteet"],
                   "rainwear":         ["lasten sadehaalari", "lasten sadevaatteet"],
                   "winter_coveralls": ["lasten talvihaalari", "lasten toppahaalari"],
                   "shell":            ["lasten kuoritakki", "lasten kuorihaalari"],
                   "footwear":         ["lasten kengät", "lasten talvikengät",
                                        "lasten kumisaappaat"],
                   "strollers":        ["lastenvaunut", "lastenrattaat", "yhdistelmävaunut"],
                   "car_seats":        ["turvaistuin", "turvakaukalo", "istuinkoroke"],
                   "nursery":          ["pinnasänky", "hoitopöytä", "vauvan makuupussi"],
                   "balance_bikes":    ["potkupyörä", "tasapainopyörä"],
                   "kids_bikes":       ["lasten polkupyörä", "kolmipyörä"],
                   "scooters":         ["potkulauta"],
                   "uv_swim":          ["uv-puku", "uimavaippa", "lasten uima-asu"],
               })),
    },
}


# ════════════════════════════════════════════════════════════════════════════
#  Seed history — the old manual Sweden sheet reaches back to Sep 2020; the API
#  window starts ~Sep 2022. These pre-window months are merged on every run
#  (idempotent, disjoint from anything the API returns) so they survive forever.
# ════════════════════════════════════════════════════════════════════════════

def _series(start_ym: str, values: list[int]) -> dict[str, int]:
    y, m = int(start_ym[:4]), int(start_ym[5:7])
    out = {}
    for v in values:
        out[f"{y:04d}-{m:02d}"] = v
        m += 1
        if m > 12:
            m, y = 1, y + 1
    return out


SEED_HISTORY: dict[str, dict[str, dict[str, int]]] = {
    "SE": {
        "babyshop":     _series("2020-09", [27100, 33100, 60500, 33100, 27100, 27100,
                                            33100, 27100, 27100, 22200, 14800, 27100,
                                            22200, 22200, 40500, 27100, 22200, 18100,
                                            22200, 22200, 22200, 18100, 14800, 22200]),
        "jollyroom":    _series("2020-09", [165000, 201000, 368000, 246000, 165000, 135000,
                                            165000, 165000, 165000, 135000, 135000, 165000,
                                            165000, 201000, 301000, 246000, 165000, 135000,
                                            135000, 135000, 135000, 135000, 110000, 135000]),
        "babyworld":    _series("2020-09", [22200, 22200, 40500, 27100, 27100, 22200,
                                            27100, 27100, 27100, 27100, 27100, 33100,
                                            33100, 27100, 40500, 33100, 27100, 22200,
                                            27100, 27100, 33100, 27100, 27100, 27100]),
        "babymarkt":    _series("2020-09", [70, 50, 70, 110, 90, 50,
                                            70, 70, 70, 70, 1600, 2400,
                                            2900, 2900, 4400, 3600, 3600, 2900,
                                            3600, 2900, 3600, 2900, 2900, 3600]),
        "stor & liten": _series("2020-09", [590, 590, 1900, 1600, 480, 480,
                                            590, 390, 390, 390, 390, 390,
                                            390, 720, 1300, 1300, 480, 320,
                                            320, 320, 320, 260, 210, 210]),
    },
}


# ════════════════════════════════════════════════════════════════════════════
#  Clients (same resolution as refresh_roas_sims.py — copied, not reinvented)
# ════════════════════════════════════════════════════════════════════════════

def missing_credentials() -> list[str]:
    return [k for k in ADS_ENV_KEYS if not os.environ.get(k, "").strip()]


def ads_client():
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
        "login_customer_id": re.sub(r"\D", "", os.environ["GOOGLE_ADS_LOGIN_CUSTOMER_ID"]),
        "use_proto_plus": True,
    }, version=API_VERSION)


_db_client = None


def _firestore():
    """Firestore client with the refresh_roas_sims local-dev fallback (memoised)."""
    global _db_client
    if _db_client is not None:
        return _db_client
    from google.cloud import firestore   # lazy import
    try:
        import google.auth
        creds, project = google.auth.default()
    except Exception:
        import subprocess
        import google.oauth2.credentials

        class _GcloudToken(google.oauth2.credentials.Credentials):
            def refresh(self, request):  # noqa: ARG002 - signature fixed by google-auth
                self.token = subprocess.check_output(
                    ["gcloud", "auth", "print-access-token"]).decode().strip()
                self.expiry = _dt.datetime.utcnow() + _dt.timedelta(minutes=45)

        tok = subprocess.check_output(["gcloud", "auth", "print-access-token"]).decode().strip()
        creds, project = _GcloudToken(tok), None
        creds.expiry = _dt.datetime.utcnow() + _dt.timedelta(minutes=45)
    _db_client = firestore.Client(project=FIRESTORE_PROJECT or project, credentials=creds)
    return _db_client


# ════════════════════════════════════════════════════════════════════════════
#  Lock + config seeding (same contract as refresh_roas_sims)
# ════════════════════════════════════════════════════════════════════════════

def acquire_lock(db, holder: str, ttl: int = LOCK_TTL_SECONDS) -> dict:
    ref = db.collection(LOCK_COLLECTION).document(LOCK_DOC)
    now = time.time()
    try:
        snap = ref.get()
    except Exception:
        return {"locked": False, "reason": "lock-unavailable"}

    if snap.exists:
        held = snap.to_dict() or {}
        expires = float(held.get("expires_at") or 0)
        if expires > now:
            raise RefreshInProgress(
                f"a refresh started at {held.get('acquired_at_iso', '?')} by "
                f"{held.get('holder', '?')} still holds the lock until "
                f"{_dt.datetime.fromtimestamp(expires, _dt.timezone.utc).isoformat()}"
            )
        ref.delete()          # stale (a container died mid-run) — reclaim it

    payload = {
        "holder": holder,
        "acquired_at": now,
        "acquired_at_iso": _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)
                             .isoformat().replace("+00:00", "Z"),
        "expires_at": now + ttl,
        "ttl_seconds": ttl,
    }
    try:
        ref.create(payload)
    except Exception as e:
        raise RefreshInProgress(f"lost the race for the refresh lock ({e})")
    return {"locked": True, "holder": holder}


def release_lock(db, holder: str) -> None:
    try:
        ref = db.collection(LOCK_COLLECTION).document(LOCK_DOC)
        snap = ref.get()
        if snap.exists and (snap.to_dict() or {}).get("holder") == holder:
            ref.delete()
    except Exception:
        pass


def seed_config(db) -> bool:
    """Create sos_config/config from DEFAULT_CONFIG if absent. Never overwrites —
    operator edits in the Firestore console survive every deploy and run."""
    try:
        ref = db.collection(CONFIG_COLLECTION).document(CONFIG_DOC)
        if ref.get().exists:
            return False
        import json
        ref.set(json.loads(json.dumps(DEFAULT_CONFIG)))
        return True
    except Exception:
        return False


def load_config(db) -> dict:
    """The stored config, or DEFAULT_CONFIG before the first refresh has seeded it."""
    try:
        snap = db.collection(CONFIG_COLLECTION).document(CONFIG_DOC).get()
        if snap.exists:
            cfg = snap.to_dict() or {}
            if cfg.get("markets") and cfg.get("keywords"):
                return cfg
    except Exception as e:
        print(f"WARN sos load_config: {type(e).__name__}: {e}", flush=True)
    return DEFAULT_CONFIG


# ════════════════════════════════════════════════════════════════════════════
#  Collector
# ════════════════════════════════════════════════════════════════════════════

def collect_market(client, market: str, mcfg: dict, keywords: list[dict]) -> dict:
    """
    One GenerateKeywordHistoricalMetrics call for one market.

    Returns {"series": {kw: {"YYYY-MM": vol}}, "meta": {kw: {...}}}. Keys are the
    RESPONSE text (the API lowercases and canonicalises), mapped back to the config
    spelling where they differ, so config remains the single naming authority.
    """
    service = client.get_service("KeywordPlanIdeaService")
    request = client.get_type("GenerateKeywordHistoricalMetricsRequest")
    request.customer_id = re.sub(r"\D", "", mcfg["customer_id"])
    request.keywords.extend([k["kw"] for k in keywords])
    request.geo_target_constants.append(f"geoTargetConstants/{mcfg['geo']}")
    request.keyword_plan_network = (
        client.enums.KeywordPlanNetworkEnum.GOOGLE_SEARCH)
    request.historical_metrics_options.include_average_cpc = True

    # Without an explicit range the API returns only the last 12 months; the point
    # of this collector is the full window, so ask for ~4 years (47 months, one
    # inside the documented 4-year boundary) up to the last complete month.
    # MonthOfYear is enum-offset: calendar month + 1.
    today = _dt.date.today()
    end = today.replace(day=1) - _dt.timedelta(days=1)            # last complete month
    start_idx = end.year * 12 + (end.month - 1) - 46              # 47 months inclusive
    rng = request.historical_metrics_options.year_month_range
    rng.start.year, rng.start.month = start_idx // 12, (start_idx % 12 + 1) + 1
    rng.end.year, rng.end.month = end.year, end.month + 1

    response = service.generate_keyword_historical_metrics(request=request)

    # The API canonicalises keyword text (lowercase, collapsed whitespace — and for
    # some terms punctuation); map the canonical form back to the exact config
    # spelling so doc keys stay stable. The loose (alphanumeric-only) map is the
    # fallback for punctuation drift ("stor & liten" → "stor liten").
    canon = {re.sub(r"\s+", " ", k["kw"].lower()).strip(): k["kw"] for k in keywords}
    loose = {re.sub(r"[^a-z0-9åäöøæé]+", "", k["kw"].lower()): k["kw"] for k in keywords}

    series: dict[str, dict[str, int]] = {}
    meta: dict[str, dict[str, Any]] = {}
    for res in response.results:
        text = re.sub(r"\s+", " ", res.text.lower()).strip()
        kw = canon.get(text) or loose.get(re.sub(r"[^a-z0-9åäöøæé]+", "", text)) or res.text
        m = res.keyword_metrics
        months: dict[str, int] = {}
        for mv in m.monthly_search_volumes:
            # MonthOfYear is enum-offset: JANUARY=2 … DECEMBER=13 (0/1 are
            # UNSPECIFIED/UNKNOWN), so the calendar month is enum − 1.
            month_num = int(mv.month) - 1
            if 1 <= month_num <= 12 and mv.year:
                months[f"{int(mv.year):04d}-{month_num:02d}"] = int(mv.monthly_searches or 0)
        series[kw] = months
        meta[kw] = {
            "avg_monthly": int(m.avg_monthly_searches or 0),
            "competition": m.competition.name if m.competition else "UNSPECIFIED",
            "bid_low": round((m.low_top_of_page_bid_micros or 0) / 1e6, 4),
            "bid_high": round((m.high_top_of_page_bid_micros or 0) / 1e6, 4),
            "avg_cpc": round((m.average_cpc_micros or 0) / 1e6, 4),
        }

    # A keyword the API returned nothing for still deserves an (empty) entry, so the
    # tab can show "no volume data" instead of silently omitting a configured term.
    for k in keywords:
        series.setdefault(k["kw"], {})
        meta.setdefault(k["kw"], {"avg_monthly": 0, "competition": "UNSPECIFIED",
                                  "bid_low": 0, "bid_high": 0, "avg_cpc": 0})
    return {"series": series, "meta": meta}


def write_history(db, market: str, label: str, collected: dict) -> dict:
    """
    Merge one market's collected months into sos_history/<MARKET>.

    merge=True with nested dicts deep-merges the {kw: {month: vol}} maps: new months
    are added, restated months are updated, and months outside the API window (the
    sheet seed, and eventually every month older than ~4 years) are left untouched.
    """
    from google.cloud import firestore

    series = {kw: dict(months) for kw, months in collected["series"].items()}
    for kw, months in SEED_HISTORY.get(market, {}).items():
        tgt = series.setdefault(kw, {})
        for ym, vol in months.items():
            tgt.setdefault(ym, vol)   # API wins where windows ever overlap

    doc = {
        "market": market,
        "label": label,
        "series": series,
        "meta": collected["meta"],
        "updated_at": firestore.SERVER_TIMESTAMP,
    }
    db.collection(HISTORY_COLLECTION).document(market).set(doc, merge=True)
    n_months = sum(len(m) for m in series.values())
    return {"keywords": len(series), "keyword_months": n_months}


def prune_runs(db, run_date: str) -> list[str]:
    cutoff = (_dt.date.fromisoformat(run_date)
              - _dt.timedelta(days=RETENTION_DAYS)).isoformat()
    dropped = []
    for ref in db.collection(RUNS_COLLECTION).list_documents():
        if RUN_DATE_RE.match(ref.id) and ref.id < cutoff:
            ref.delete()
            dropped.append(ref.id)
    return dropped


def refresh(run_date: str | None = None, db=None) -> dict:
    """
    Collect + persist all markets. The entry point /internal/refresh-sos calls.

    Per-market failures are recorded and do not abort the run; the status is "ok"
    when at least one market succeeded, "error" when all failed.
    """
    run_date = run_date or _dt.date.today().isoformat()
    if not RUN_DATE_RE.match(run_date):
        raise ValueError("run_date must be YYYY-MM-DD")

    client = ads_client()                       # raises MissingCredentials first
    db = db or _firestore()
    holder = f"refresh-sos-{os.getpid()}-{int(time.time())}"
    lock = acquire_lock(db, holder)             # raises RefreshInProgress -> HTTP 409

    t0 = time.time()
    markets_out: dict[str, Any] = {}
    errors: dict[str, str] = {}
    try:
        cfg = load_config(db)
        for i, (mk, mcfg) in enumerate(sorted(cfg["markets"].items())):
            if i:
                time.sleep(1.0)                 # planning services are ~1 QPS
            keywords = cfg["keywords"].get(mk) or []
            if not keywords:
                errors[mk] = "no keywords configured"
                continue
            try:
                collected = collect_market(client, mk, mcfg, keywords)
                markets_out[mk] = write_history(db, mk, mcfg.get("label", mk), collected)
            except Exception as e:
                errors[mk] = f"{type(e).__name__}: {e}"
                print(f"ERROR sos collect {mk}: {errors[mk]}", flush=True)

        seeded = seed_config(db)
        from google.cloud import firestore
        db.collection(RUNS_COLLECTION).document(run_date).set({
            "run_date": run_date,
            "generated_at": firestore.SERVER_TIMESTAMP,
            "elapsed_s": round(time.time() - t0, 1),
            "markets": markets_out,
            "errors": errors,
            "config_seeded": seeded,
        })
        pruned = prune_runs(db, run_date)
    finally:
        if lock.get("locked"):
            release_lock(db, holder)

    status = "ok" if markets_out else "error"
    return {"status": status, "run_date": run_date, "markets": markets_out,
            "errors": errors, "pruned_runs": pruned,
            "elapsed_s": round(time.time() - t0, 1)}


# ════════════════════════════════════════════════════════════════════════════
#  Reader — what /api/share-of-search serves
# ════════════════════════════════════════════════════════════════════════════

def read_payload(db=None) -> dict:
    """
    Full payload for the tab: config + every market's merged history. Share math
    happens client-side (the adjacent-tier toggle needs the raw series anyway).
    """
    db = db or _firestore()
    cfg = load_config(db)
    markets: dict[str, Any] = {}
    generated_at = None
    for snap in db.collection(HISTORY_COLLECTION).stream():
        d = snap.to_dict() or {}
        upd = d.get("updated_at")
        upd_iso = upd.isoformat() if hasattr(upd, "isoformat") else None
        if upd_iso and (generated_at is None or upd_iso > generated_at):
            generated_at = upd_iso
        markets[snap.id] = {
            "label": d.get("label", snap.id),
            "series": d.get("series", {}),
            "meta": d.get("meta", {}),
            "updated_at": upd_iso,
        }
    return {
        "generated_at": generated_at,
        "config": {"markets": cfg.get("markets", {}), "keywords": cfg.get("keywords", {})},
        "markets": markets,
    }


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Share of Search collector (local run)")
    ap.add_argument("--read-only", action="store_true",
                    help="skip collection; print the served payload")
    ap.add_argument("--out", help="write the payload JSON here")
    args = ap.parse_args()

    if not args.read_only:
        out = refresh()
        print(json.dumps({k: v for k, v in out.items() if k != "markets"} |
                         {"markets": {m: s for m, s in out["markets"].items()}},
                         indent=2, ensure_ascii=False))
    payload = read_payload()
    n = sum(len(m["series"]) for m in payload["markets"].values())
    print(f"payload: {len(payload['markets'])} markets · {n} keyword series · "
          f"generated_at {payload['generated_at']}")
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        print(f"wrote {args.out} ({os.path.getsize(args.out):,} bytes)")
