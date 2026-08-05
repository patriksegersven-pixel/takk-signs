#!/usr/bin/env python3
"""
ROAS Impact monitor — BigQuery-backed refresh.

Tracks the impact of the Google Ads tROAS (profit-ROAS = GP2/Cost) increases made
on 2026-06-15 across 29 shopping/search campaigns (Babyshop SE/NO/FI/DK + ROW and
Lekmer SE). Forward-looking monitor: shows the pre-change baseline run-rate + the
projected trajectory now, and accrues daily actuals as days pass.

Mirrors refresh_from_bigquery.py: queries the Funnel→BigQuery export with the `bq`
CLI (active gcloud / ADC identity), aggregates in Python, and writes the snapshot
straight into the Firestore funnel_cache doc the page reads via /api/roas-impact
(get_cache().get("roas-impact")).

Run locally:        python3 refresh_roas_impact.py
As Cloud Run Job:   same entrypoint (uses the service-account ADC).

Measures (per the brief):
  Spend = SUM(Cost)            Revenue = SUM(kv_revenue)
  GP2   = SUM(kv_gp2)          GP3     = SUM(kv_gp2) + SUM(kv_gp3_fb)   (gp3_fb < 0)
  profit-ROAS = SUM(kv_gp2)/SUM(Cost)     rev-ROAS = SUM(kv_revenue)/SUM(Cost)

Babyshop and Lekmer share campaign names, so the set is split on shop_new.
"""
import json, subprocess, os, datetime, time

BQ_PROJECT        = os.environ.get("BQ_PROJECT", "babyshop-funnel-data")
TABLE             = "`babyshop-funnel-data.bs_funnel_export.funnel_data`"
FIRESTORE_PROJECT = os.environ.get("FIRESTORE_PROJECT", "project-a7ade44e-e7e3-4871-a83")
WORKSPACE         = os.environ.get("FUNNEL_WORKSPACE", "-Ln87GcdqU9CMJV6zMBY")
COLLECTION        = "funnel_cache"
CACHE_KEY         = "roas-impact"
TTL               = 30 * 24 * 3600
SOURCE            = "bigquery-export"

# ── Total-business view (substitution hypothesis: revenue lost on paid is partly
#    recovered via organic/direct rather than lost). Scope = the shops that own the
#    changed campaigns, all markets, all channels. ───────────────────────────────
TOTAL_SHOPS = ["Babyshop", "Lekmer"]
# Channel_Type_Level_2 buckets we treat as earned / unpaid (the substitution targets).
UNPAID_CHANNELS    = {"Organic Search", "Direct", "Email", "Referral",
                      "Social Organic", "Naver Organic"}
# Holding buckets that recent (un-attributed) conversions land in before being
# reassigned to a real channel. Their share of revenue is the attribution-maturity
# signal: while it's elevated, the per-channel split for those days isn't trustworthy.
AMBIGUOUS_CHANNELS = {"Other", "Kuvio"}
# A post-change day is "attribution-mature" once the ambiguous share falls back to
# roughly its normal level (baseline runs ~6–16%); only mature days feed the split.
CHANNEL_MATURE_MAX_AMBIG = 0.20
# Need at least this many mature post-change days before the per-channel "since change"
# column is meaningful — fewer than this is single-day noise (e.g. just the change day),
# so until then we show the baseline channel mix only and flag the split as maturing.
MIN_MATURE_DAYS = 3

# ── The change ───────────────────────────────────────────────────────────────
CHANGE_DATE  = datetime.date(2026, 6, 15)   # tROAS raised on 29 campaigns
SERIES_START = datetime.date(2026, 4, 1)    # daily series window start
BASELINE_DAYS = 28                          # pre-change run-rate window (4 weeks)

# Projection (held-revenue assumption from the brief)
PROJ_MONTHLY_CUT = 1_300_000   # ~ -1.3M kr/month total spend
PROJ_CUT_PCT     = 0.53        # ~53% of spend on these campaigns

# ── Changed campaign set (split by shop_new — names collide across shops) ──────
BABYSHOP_CAMPAIGNS = [
    "p-shopping-se-pb-generic", "p-shopping-se-rb-categories", "p-shopping-se-pb-product",
    "p-shopping-se-last-season", "p-shopping-se-brand", "ps-dsa-all",
    "ps-search-se-generic-hero-categories", "ps-dynamic-remarketing", "ps-search-se-generic-seasonal",
    "p-shopping-no-pb-generic", "p-shopping-no-rb-categories", "p-shopping-no-last-season",
    "p-shopping-no-pb-product", "p-shopping-no-brand", "ps-dsa-no",
    "p-shopping-fi-all-generic", "p-shopping-fi-rb-categories", "p-shopping-fi-last-season",
    "p-shopping-fi-brand",
    "p-shopping-dk-rb-categories", "p-shopping-dk-generic", "p-shopping-dk-pb-product",
    "p-shopping-dk-last-season", "p-shopping-dk-brand",
    "shopping-cz", "ps-dsa-is",
]
LEKMER_CAMPAIGNS = ["p-shopping-se-pb-product", "p-shopping-se-brand", "ps-dsa-se"]

# New tROAS target per campaign (account|campaign → target), for the per-group table
TARGETS = {
    ("Babyshop", "p-shopping-se-pb-generic"): 2.2, ("Babyshop", "p-shopping-se-rb-categories"): 2.4,
    ("Babyshop", "p-shopping-se-pb-product"): 6.0, ("Babyshop", "p-shopping-se-last-season"): 2.0,
    ("Babyshop", "p-shopping-se-brand"): 6.0, ("Babyshop", "ps-dsa-all"): 2.0,
    ("Babyshop", "ps-search-se-generic-hero-categories"): 2.0, ("Babyshop", "ps-dynamic-remarketing"): 2.0,
    ("Babyshop", "ps-search-se-generic-seasonal"): 1.8,
    ("Babyshop", "p-shopping-no-pb-generic"): 2.1, ("Babyshop", "p-shopping-no-rb-categories"): 2.0,
    ("Babyshop", "p-shopping-no-last-season"): 2.0, ("Babyshop", "p-shopping-no-pb-product"): 6.0,
    ("Babyshop", "p-shopping-no-brand"): 6.0, ("Babyshop", "ps-dsa-no"): 2.0,
    ("Babyshop", "p-shopping-fi-all-generic"): 2.0, ("Babyshop", "p-shopping-fi-rb-categories"): 3.0,
    ("Babyshop", "p-shopping-fi-last-season"): 2.0, ("Babyshop", "p-shopping-fi-brand"): 6.0,
    ("Babyshop", "p-shopping-dk-rb-categories"): 1.5, ("Babyshop", "p-shopping-dk-generic"): 2.2,
    ("Babyshop", "p-shopping-dk-pb-product"): 6.0, ("Babyshop", "p-shopping-dk-last-season"): 2.0,
    ("Babyshop", "p-shopping-dk-brand"): 6.0,
    ("Babyshop", "shopping-cz"): 2.0, ("Babyshop", "ps-dsa-is"): 2.0,
    ("Lekmer", "p-shopping-se-pb-product"): 6.0, ("Lekmer", "p-shopping-se-brand"): 6.0,
    ("Lekmer", "ps-dsa-se"): 2.0,
}

# Group definitions (key, label, target-display, revenue-risk?, note). Order = table order.
GROUP_META = [
    ("pb-product",    "PB-Product",            "6.0",     False, "Sole seller — minimal revenue risk"),
    ("brand",         "Brand",                 "6.0",     False, "Brand demand — minimal revenue risk"),
    ("pb-generic",    "PB-Generic / Generic",  "2.0–2.2", True,  "Incremental — revenue risk"),
    ("rb-categories", "RB-Categories",         "1.5–3.0", True,  "Incremental — revenue risk"),
    ("last-season",   "Last-Season",           "2.0",     False, "Clearance / seasonal"),
    ("search-dsa",    "Search / DSA",          "1.8–2.0", False, "Search, DSA & remarketing"),
]

def group_of(campaign: str) -> str:
    c = campaign
    if "pb-product" in c:     return "pb-product"
    if c.endswith("-brand"):  return "brand"
    if "rb-categories" in c:  return "rb-categories"
    if "last-season" in c:    return "last-season"
    if "generic" in c:        return "pb-generic"        # pb-generic, all-generic, dk-generic
    if c.startswith("ps-"):   return "search-dsa"        # dsa, search, dynamic-remarketing
    if c.startswith("shopping-"): return "pb-generic"    # shopping-cz (ROW generic)
    return "search-dsa"

# ── BigQuery via the client library (works in Cloud Run, no `bq` CLI needed) ───
# Returns rows as dicts of stringified values to mirror `bq query --format=json`
# (the CLI emits every value as a string; downstream code wraps numerics in F()/I()).
_bq_client = None
def _bq():
    global _bq_client
    if _bq_client is None:
        from google.cloud import bigquery
        try:
            import google.auth
            creds, _ = google.auth.default()
        except Exception:
            import google.oauth2.credentials
            tok = subprocess.check_output(
                ["gcloud", "auth", "print-access-token"]).decode().strip()
            creds = google.oauth2.credentials.Credentials(tok)
        _bq_client = bigquery.Client(project=BQ_PROJECT, credentials=creds)
    return _bq_client

def bq(sql):
    rows = _bq().query(sql).result()
    return [{k: (None if v is None else str(v)) for k, v in dict(r).items()}
            for r in rows]

def F(v): return float(v or 0)
def I(v): return int(round(F(v)))

SV_MON = ["jan", "feb", "mar", "apr", "maj", "jun", "jul", "aug", "sep", "okt", "nov", "dec"]
def lbl(s, e):
    return (f"{s.day} {SV_MON[s.month-1]} – {e.day} {SV_MON[e.month-1]} {e.year}"
            if s.year == e.year else
            f"{s.day} {SV_MON[s.month-1]} {s.year} – {e.day} {SV_MON[e.month-1]} {e.year}")
def dm(iso): y, m, d = map(int, iso.split("-")); return f"{d}/{m}"


def _in(values): return ",".join("'" + v.replace("'", "") + "'" for v in values)

def build_payload():
    # data_end = latest date present in the table. The export lags ~1 day, so the latest
    # day is partial (spend lands before revenue/GP2 is attributed). Mirror the KV refresh
    # convention (CUR_END = TODAY-1): treat the latest day as partial and accrue actuals
    # only through the last COMPLETE day, so day-1 lag doesn't show as a fake GP3 plunge.
    data_end = datetime.date.fromisoformat(bq(f"SELECT CAST(MAX(Date) AS STRING) d FROM {TABLE}")[0]["d"])
    complete_end = min(data_end, datetime.date.today() - datetime.timedelta(days=1))
    series_end   = min(data_end, max(complete_end, CHANGE_DATE))   # keep the change day on-chart

    base_end   = CHANGE_DATE - datetime.timedelta(days=1)
    base_start = base_end - datetime.timedelta(days=BASELINE_DAYS - 1)

    where_set = (
        f"((shop_new='Babyshop' AND Campaign IN ({_in(BABYSHOP_CAMPAIGNS)})) "
        f"OR (shop_new='Lekmer' AND Campaign IN ({_in(LEKMER_CAMPAIGNS)})))"
    )

    # Daily per campaign across the whole window (covers chart series + baseline + since-change)
    rows = bq(
        f"SELECT CAST(Date AS STRING) d, shop_new, Campaign, "
        f"SUM(Cost) cost, SUM(kv_revenue) rev, SUM(kv_gp2) gp2, SUM(kv_gp3_fb) gp3fb "
        f"FROM {TABLE} WHERE Date BETWEEN DATE '{SERIES_START}' AND DATE '{data_end}' "
        f"AND {where_set} GROUP BY d, shop_new, Campaign ORDER BY d")

    # ── Daily totals for the whole changed set ─────────────────────────────────
    daily = {}
    for r in rows:
        e = daily.setdefault(r["d"], {"spend": 0.0, "rev": 0.0, "gp2": 0.0, "gp3fb": 0.0})
        e["spend"] += F(r["cost"]); e["rev"] += F(r["rev"]); e["gp2"] += F(r["gp2"]); e["gp3fb"] += F(r["gp3fb"])

    daily_series = []
    d = SERIES_START
    while d <= series_end:
        iso = d.isoformat()
        partial = d > complete_end                          # latest (lagging) day → null on chart
        e = daily.get(iso, {"spend": 0, "rev": 0, "gp2": 0, "gp3fb": 0})
        daily_series.append({
            "iso": iso, "d": dm(iso), "post": d >= CHANGE_DATE, "partial": partial,
            "spend": None if partial else I(e["spend"]),
            "rev":   None if partial else I(e["rev"]),
            "gp2":   None if partial else I(e["gp2"]),
            "gp3":   None if partial else I(e["gp2"] + e["gp3fb"]),
        })
        d += datetime.timedelta(days=1)

    # ── Baseline run-rate (per day) over the 28d pre-change window ──────────────
    bwin = [x for x in daily_series if base_start.isoformat() <= x["iso"] <= base_end.isoformat()]
    nb = max(len(bwin), 1)
    base = {
        "spend_day": sum(x["spend"] for x in bwin) / nb,
        "rev_day":   sum(x["rev"]   for x in bwin) / nb,
        "gp2_day":   sum(x["gp2"]   for x in bwin) / nb,
        "gp3_day":   sum(x["gp3"]   for x in bwin) / nb,
    }

    # ── Since-change actuals (COMPLETE post-change days only) ──────────────────
    post = [x for x in daily_series if x["post"] and not x["partial"]]
    days_elapsed = len(post)
    proj_spend_day = base["spend_day"] * (1 - PROJ_CUT_PCT)

    kpis = {
        "days_elapsed": days_elapsed,
        "spend":   {"actual": sum(x["spend"] for x in post),
                    "projected": proj_spend_day * days_elapsed,
                    "baseline":  base["spend_day"] * days_elapsed},
        "revenue": {"actual": sum(x["rev"] for x in post),
                    "baseline": base["rev_day"] * days_elapsed},
        "gp3":     {"actual": sum(x["gp3"] for x in post),
                    "baseline": base["gp3_day"] * days_elapsed},
    }

    # ── Cumulative-savings tracker (actual saved vs projection trajectory) ──────
    # Forward-looking: projection spans a 30-day horizon so the target path shows
    # immediately; actuals accrue on top (null beyond the elapsed complete days).
    HORIZON = 30
    cum, post_cum = 0.0, []
    for x in post:
        cum += base["spend_day"] - x["spend"]
        post_cum.append(cum)
    savings = [{"day": i, "label": f"Day {i}",
                "projected": I(PROJ_MONTHLY_CUT / 30 * i),
                "actual": I(post_cum[i - 1]) if i <= len(post_cum) else None}
               for i in range(1, HORIZON + 1)]

    # ── Per-group aggregation (baseline window vs since-change) ─────────────────
    def blank(): return {"b_cost": 0.0, "b_gp2": 0.0, "p_cost": 0.0, "p_gp2": 0.0, "p_rev": 0.0}
    g = {k: blank() for k, *_ in GROUP_META}
    bs, be = base_start.isoformat(), base_end.isoformat()
    ce, ple = CHANGE_DATE.isoformat(), complete_end.isoformat()
    for r in rows:
        grp = group_of(r["Campaign"]); iso = r["d"]
        if grp not in g:  # safety
            continue
        if bs <= iso <= be:
            g[grp]["b_cost"] += F(r["cost"]); g[grp]["b_gp2"] += F(r["gp2"])
        if ce <= iso <= ple:   # complete post-change days only
            g[grp]["p_cost"] += F(r["cost"]); g[grp]["p_gp2"] += F(r["gp2"]); g[grp]["p_rev"] += F(r["rev"])

    groups = []
    for key, label, target, risk, note in GROUP_META:
        v = g[key]
        base_day = v["b_cost"] / nb
        act_day  = (v["p_cost"] / days_elapsed) if days_elapsed else None   # None until a complete day
        groups.append({
            "key": key, "label": label, "target": target, "risk": risk, "note": note,
            "baseline_spend_mo": base_day * 30,           # baseline monthly run-rate
            "actual_spend": v["p_cost"],                   # since change (cumulative)
            "actual_spend_day": act_day,
            "pct_change": ((act_day - base_day) / base_day) if (act_day is not None and base_day) else None,
            "proas_pre":  (v["b_gp2"] / v["b_cost"]) if v["b_cost"] else None,
            "proas_post": (v["p_gp2"] / v["p_cost"]) if v["p_cost"] else None,
        })

    # ── Total business across ALL channels (substitution hypothesis) ───────────
    # The KPIs/series above are siloed to the changed campaigns. This block tracks
    # the WHOLE Babyshop+Lekmer business across every channel, to test whether the
    # paid pullback is recovered via organic/direct rather than lost outright.
    trows = bq(
        f"SELECT CAST(Date AS STRING) d, Channel_Type_Level_2 ch, "
        f"SUM(kv_revenue) rev, SUM(kv_gp2) gp2, SUM(kv_gp3_fb) gp3fb, SUM(Cost) cost "
        f"FROM {TABLE} WHERE Date BETWEEN DATE '{SERIES_START}' AND DATE '{data_end}' "
        f"AND shop_new IN ({_in(TOTAL_SHOPS)}) GROUP BY d, ch")

    tdaily = {}     # iso -> day totals incl. ambiguous-bucket revenue (maturity signal)
    chan_day = {}   # iso -> {channel -> {rev}}
    for r in trows:
        iso = r["d"]; ch = r["ch"] or "Other"; rev = F(r["rev"])
        e = tdaily.setdefault(iso, {"rev": 0.0, "gp2": 0.0, "gp3fb": 0.0, "cost": 0.0, "amb": 0.0})
        e["rev"] += rev; e["gp2"] += F(r["gp2"]); e["gp3fb"] += F(r["gp3fb"]); e["cost"] += F(r["cost"])
        if ch in AMBIGUOUS_CHANNELS:
            e["amb"] += rev
        chan_day.setdefault(iso, {})[ch] = chan_day.get(iso, {}).get(ch, 0.0) + rev

    # Daily total series (mirrors `daily`'s partial-day convention)
    tot_series = []
    d = SERIES_START
    while d <= series_end:
        iso = d.isoformat(); partial = d > complete_end
        e = tdaily.get(iso, {"rev": 0, "gp2": 0, "gp3fb": 0, "cost": 0, "amb": 0})
        tot_series.append({
            "iso": iso, "d": dm(iso), "post": d >= CHANGE_DATE, "partial": partial,
            "rev":   None if partial else I(e["rev"]),
            "gp3":   None if partial else I(e["gp2"] + e["gp3fb"]),
            "spend": None if partial else I(e["cost"]),
        })
        d += datetime.timedelta(days=1)

    # Baseline vs since-change run-rate over complete days
    def _accum(lo, hi):
        a = {"rev": 0.0, "gp3": 0.0, "spend": 0.0, "days": 0}
        for x in tot_series:
            if x["partial"] or not (lo <= x["iso"] <= hi):
                continue
            a["rev"] += x["rev"]; a["gp3"] += x["gp3"]; a["spend"] += x["spend"]; a["days"] += 1
        return a
    tb = _accum(base_start.isoformat(), base_end.isoformat())
    tp = _accum(CHANGE_DATE.isoformat(), complete_end.isoformat())

    def _day(a, k): return (a[k] / a["days"]) if a["days"] else 0.0
    def _pct(b, p): return ((p - b) / b) if b else None
    tb_rev, tb_gp3, tb_spend = _day(tb, "rev"), _day(tb, "gp3"), _day(tb, "spend")
    tp_rev, tp_gp3, tp_spend = _day(tp, "rev"), _day(tp, "gp3"), _day(tp, "spend")

    # Attribution maturity per post-change complete day (ambiguous-bucket share)
    mature_post, amb_daily = [], []
    for x in tot_series:
        if not x["post"] or x["partial"]:
            continue
        e = tdaily.get(x["iso"], {})
        amb_pct = (e.get("amb", 0) / e["rev"]) if e.get("rev") else None
        is_mature = amb_pct is not None and amb_pct <= CHANNEL_MATURE_MAX_AMBIG
        if is_mature:
            mature_post.append(x["iso"])
        amb_daily.append({"iso": x["iso"], "d": x["d"],
                          "amb_pct": round(amb_pct, 3) if amb_pct is not None else None,
                          "mature": is_mature})

    # Per-channel revenue/day: baseline (all complete baseline days) vs post (mature days only)
    def _classify(ch):
        if ch in AMBIGUOUS_CHANNELS: return "unattributed"
        if ch in UNPAID_CHANNELS:    return "unpaid"
        return "paid"
    base_isos = [x["iso"] for x in tot_series
                 if not x["partial"] and base_start.isoformat() <= x["iso"] <= base_end.isoformat()]
    nb_t, nmp = max(len(base_isos), 1), len(mature_post)
    chan = {}
    for iso in base_isos:
        for ch, rev in chan_day.get(iso, {}).items():
            chan.setdefault(ch, {"b": 0.0, "p": 0.0})["b"] += rev
    for iso in mature_post:
        for ch, rev in chan_day.get(iso, {}).items():
            chan.setdefault(ch, {"b": 0.0, "p": 0.0})["p"] += rev
    channels = []
    for ch, v in chan.items():
        b_day = v["b"] / nb_t
        p_day = (v["p"] / nmp) if nmp else None
        channels.append({
            "channel": ch, "group": _classify(ch),
            "base_rev_day": I(b_day),
            "post_rev_day": (I(p_day) if p_day is not None else None),
            "delta_pct": (_pct(b_day, p_day) if (p_day is not None and b_day) else None),
        })
    channels.sort(key=lambda r: r["base_rev_day"], reverse=True)

    total = {
        "scope": "Babyshop + Lekmer · all markets · all channels",
        "baseline_window": lbl(base_start, base_end),
        "baseline": {"rev_day": I(tb_rev), "gp3_day": I(tb_gp3), "spend_day": I(tb_spend),
                     "days": tb["days"], "margin": (tb_gp3 / tb_rev) if tb_rev else None},
        "post": {"rev_day": I(tp_rev), "gp3_day": I(tp_gp3), "spend_day": I(tp_spend),
                 "days": tp["days"], "margin": (tp_gp3 / tp_rev) if tp_rev else None},
        "delta": {"rev": _pct(tb_rev, tp_rev), "gp3": _pct(tb_gp3, tp_gp3),
                  "spend": _pct(tb_spend, tp_spend)},
        "daily": tot_series,
        "channels": channels,
        "maturity": {"threshold_pct": CHANNEL_MATURE_MAX_AMBIG,
                     "min_days": MIN_MATURE_DAYS,
                     "post_days_total": tp["days"], "post_days_mature": nmp,
                     "mature": nmp >= MIN_MATURE_DAYS, "daily": amb_daily},
    }

    return {
        "change_date": CHANGE_DATE.isoformat(),
        "data_end": data_end.isoformat(),
        "complete_end": complete_end.isoformat(),
        "currency": "SEK",
        "source": SOURCE,
        "n_campaigns": len(BABYSHOP_CAMPAIGNS) + len(LEKMER_CAMPAIGNS),
        "projection": {"monthly_cut": PROJ_MONTHLY_CUT, "cut_pct": PROJ_CUT_PCT},
        "baseline": {
            "window": lbl(base_start, base_end), "days": nb,
            "spend_day": I(base["spend_day"]), "rev_day": I(base["rev_day"]),
            "gp2_day": I(base["gp2_day"]), "gp3_day": I(base["gp3_day"]),
            "spend_mo": I(base["spend_day"] * 30), "rev_mo": I(base["rev_day"] * 30),
            "gp3_mo": I(base["gp3_day"] * 30),
            "proj_spend_day": I(proj_spend_day), "proj_spend_mo": I(proj_spend_day * 30),
        },
        "kpis": kpis,
        "daily": daily_series,
        "savings": savings,
        "groups": groups,
        "total": total,
    }


def write_firestore(payload):
    from google.cloud import firestore  # lazy import (local dev w/o lib still builds payload)
    try:
        import google.auth
        creds, _ = google.auth.default()
    except Exception:
        import google.oauth2.credentials, subprocess as _sp
        tok = _sp.check_output(["gcloud", "auth", "print-access-token"]).decode().strip()
        creds = google.oauth2.credentials.Credentials(tok)
    db = firestore.Client(project=FIRESTORE_PROJECT, credentials=creds)
    db.collection(COLLECTION).document(f"{WORKSPACE}__{CACHE_KEY}").set({
        "data": payload, "fetched_at": firestore.SERVER_TIMESTAMP,
        "expires_at": time.time() + TTL, "ttl_seconds": TTL, "workspace": WORKSPACE,
    })


if __name__ == "__main__":
    p = build_payload()
    print(f"ROAS Impact · change {p['change_date']} · data→{p['data_end']} · "
          f"{p['n_campaigns']} campaigns · baseline spend {p['baseline']['spend_mo']:,}/mo "
          f"→ projected {p['baseline']['proj_spend_mo']:,}/mo · days since change "
          f"{p['kpis']['days_elapsed']}")
    out = os.environ.get("ROAS_OUT")
    if out:
        with open(out, "w") as f:
            json.dump(p, f, ensure_ascii=False)
        print(f"  wrote {out}")
    if not os.environ.get("SKIP_FIRESTORE"):
        write_firestore(p)
        print("  ✓ wrote Firestore funnel_cache/" + f"{WORKSPACE}__{CACHE_KEY}")
