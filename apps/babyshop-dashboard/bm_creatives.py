#!/usr/bin/env python3
"""
bm_creatives.py: the Black Month "Creatives" section.

Turns Meta ad-level data into today's decisions for the November creative push:

  scorecard   every ad that spent in November: metrics over the judging window,
              a fatigue read (last 3 spend days vs the ad's first 3) and a
              verdict Scale / Keep / Watch / Kill with the reason, actions first
  new         ads whose first spend day is in the last 7 days, early read
  tags        spend / ROAS by angle, offer, product, format and production,
              parsed from the proposed BM naming convention, falling back to
              what today's KiriMedia names carry; untagged share of spend
  dpa         DPA / catalog ads, scored apart (not creatives in the same sense)

Data: the same warehouse reads as the Meta creatives tab, imported from
refresh_meta.py (per_ad_day / ad_dims over stg_meta__ads_insights +
stg_meta__ads + stg_meta__ad_creatives in claude-private-499703), so market,
media type, concept tag and every ratio are defined exactly as on that tab.
Thumbnails are the same mirror, /api/meta/image/<ad_id>.

Locally the warehouse is only readable as the kuvio account:
  CLOUDSDK_CORE_ACCOUNT=patrik@kuvio.io META_AUTH=gcloud \\
  META_BQ_BILLING_PROJECT=claude-private-499703 \\
  python3 refresh_blackmonth.py --section creatives --dry-run --out bm-mock/creatives.json
"""
from __future__ import annotations

import datetime
import re

import bm_common as bm
import refresh_meta as rm

# ── Windows ──────────────────────────────────────────────────────────────────
# Judging uses SETTLED days only (Meta's 7d-click attribution keeps adding
# purchases for a couple of days), the same settle rule as the Meta tab.
SETTLE_DAYS = rm.SETTLE_DAYS          # 2
JUDGE_DAYS = 7                        # metrics + verdict window
TAG_DAYS = 28                         # tag rollups need more signal than a week
NEW_DAYS = 7                          # "new this week" = first spend in the last 7 days
FATIGUE_SPAN = 3                      # first 3 vs last 3 spend days

# ── Verdict thresholds (shown on the page verbatim via RULES) ────────────────
# The spend floor before an ad is judged at all: enough spend to EXPECT 3
# purchases at the market's average CPA, and never under 300 kr. SE (CPA ~17 kr)
# judges at 300 kr, FI (CPA ~137 kr) at ~410 kr.
SPEND_FLOOR_MIN = 300.0               # SEK in the judging window
FLOOR_CPA_X = 3.0                     # … or 3 × the market's CPA, whichever is higher
SCALE_ROAS_X = 1.30                   # ROAS ≥ 1.3 × market average …
SCALE_MIN_PURCHASES = 3               # … on at least 3 purchases
KILL_ROAS_X = 0.50                    # ROAS < 0.5 × market average
KILL_ZERO_CPA_X = 2.0                 # no purchase after 2 × the market's CPA in spend
WATCH_ROAS_X = 0.80                   # 0.5–0.8 × market average → watch
FATIGUE_CTR_IDX = 0.70                # last-3 link CTR < 70 % of first-3
FATIGUE_ROAS_IDX = 0.60               # last-3 ROAS < 60 % of first-3 (≥ 2 purchases in first 3)
FATIGUE_FREQ_X = 1.30                 # last-3 daily frequency ≥ 1.3 × first-3
FATIGUE_MIN_DAYS = 6                  # need 6 spend days for two disjoint 3-day blocks
FATIGUE_MIN_SPEND = 150.0             # each 3-day block needs this much spend to compare

RULES = [
    {"verdict": "Kill", "rule": f"Judged (spend in the judging window ≥ the market's floor) and ROAS "
                                f"< {KILL_ROAS_X:.1f}× the market's average, or no purchase after "
                                f"{KILL_ZERO_CPA_X:.0f}× the market's CPA in spend."},
    {"verdict": "Scale", "rule": f"Judged, ROAS ≥ {SCALE_ROAS_X:.1f}× the market's average on "
                                 f"≥ {SCALE_MIN_PURCHASES} purchases, and not fatiguing."},
    {"verdict": "Watch", "rule": f"Below the market's spend floor (still learning), fatiguing, or ROAS "
                                 f"{KILL_ROAS_X:.1f}–{WATCH_ROAS_X:.1f}× the market's average."},
    {"verdict": "Keep", "rule": "Judged and none of the above: performing around the market average."},
    {"verdict": "Fatigue", "rule": f"Needs ≥ {FATIGUE_MIN_DAYS} spend days. Last 3 spend days vs the ad's "
                                   f"first 3: link CTR < {FATIGUE_CTR_IDX:.0%} of start, or ROAS "
                                   f"< {FATIGUE_ROAS_IDX:.0%} of start, or daily frequency up "
                                   f"≥ {FATIGUE_FREQ_X:.1f}×."},
    {"verdict": "Spend floor", "rule": f"Spend needed before an ad is judged: {FLOOR_CPA_X:.0f}× the market's "
                                       f"CPA (enough to expect {FLOOR_CPA_X:.0f} purchases), minimum "
                                       f"{SPEND_FLOOR_MIN:.0f} kr, over the {JUDGE_DAYS}-day judging window."},
    {"verdict": "Market average", "rule": f"ROAS and CPA of all non-DPA ads in the same market over the same "
                                          f"{JUDGE_DAYS}-day judging window."},
]
# Today's actions first: what to change before what to leave alone.
ACTION_ORDER = {"Kill": 0, "Scale": 1, "Watch": 2, "Keep": 3}

# ── Proposed naming convention ───────────────────────────────────────────────
CONVENTION = {
    "pattern": "BM | <market> | <type> | <angle> | <offer> | <product> | <format> | <production> | v<n>",
    "example": "BM | SE | PRO | Gift guide | 20% off | Kuling overall | Video | UGC | v2",
    "separator": " | ",
    "fields": [
        {"key": "market", "label": "Market", "values": "SE, NO, FI, DK (one ad = one market)"},
        {"key": "type", "label": "Campaign type", "values": "PRO (prospecting), RET (retargeting), KUL (Kuling), BRP (brand partner)"},
        {"key": "angle", "label": "Angle", "values": "free text from a short list: Gift guide, Deal, Education, Social proof, Unboxing, Product hero, Winter ready, Urgency"},
        {"key": "offer", "label": "Offer", "values": "the offer as shown: 20% off, 3 for 2, Free shipping, No offer"},
        {"key": "product", "label": "Product / category", "values": "brand or category in focus: Kuling overall, Strollers, Shoes, Mixed"},
        {"key": "format", "label": "Format", "values": "Video, Still, Carousel, Slideshow"},
        {"key": "production", "label": "Production", "values": "UGC, High end, In-house, Partnership"},
        {"key": "version", "label": "Version", "values": "v1, v2 … — bump on any change to the asset"},
    ],
    "notes": "Ads that keep today's KiriMedia slugs are parsed best-effort (objective, brand, concept, "
             "format and any % / sale wording) and counted as untagged. The prefix 'BM |' is what marks an "
             "ad as tagged; DPA / catalog ads do not need it.",
}
# ── Brands ───────────────────────────────────────────────────────────────────
# Which Meta ad account belongs to which store. Only Babyshop's account is in
# the warehouse today (verified 2026-11-04: stg_meta__ads_insights, the mart
# agg_daily_kpis_by_ad and raw ads_insights_native all carry the one account,
# client_site = 'babyshop'; no ad or campaign name mentions Lekmer, and the
# Funnel export's Lekmer "Paid Social (Meta)" rows carry no cost). Add Lekmer's
# account id here the day the connector loads it; nothing else needs to change.
BRANDS = ["babyshop", "lekmer"]
BRAND_BY_ACCOUNT = {"2927326560764919": "babyshop"}
BRAND_LABEL = {"babyshop": "Babyshop", "lekmer": "Lekmer"}


def brand_of(d: dict) -> str:
    if re.search(r"(?i)lekmer", f"{d.get('ad_name') or ''} {d.get('campaign_name') or ''}"):
        return "lekmer"
    return BRAND_BY_ACCOUNT.get(str(d.get("account_id") or ""), "babyshop")


TYPE_CODES = {"PRO": "Prospecting", "RET": "Retargeting", "KUL": "Kuling", "BRP": "Brand partner"}

TAG_DIMS = [("angle", "Angle"), ("offer", "Offer"), ("product", "Product"),
            ("format", "Format"), ("production", "Production")]

_ANGLE_WORDS = [  # legacy names: specific angle words beat the objective head
    (r"social\s*pro", "Social proof"), (r"educat", "Education"), (r"unbox", "Unboxing"),
    (r"\btips?\b", "Tips"), (r"voice\s*over", "Voice-over"), (r"style\s*guide|styled with", "Styling"),
    (r"announce", "Announcement"), (r"birthday", "Birthday offer"), (r"save more|buy more", "Buy more save more"),
]
_OBJECTIVES = {"PRODUCT": "Product", "BRAND": "Brand", "CATEGORY": "Category",
               "NEWS": "News", "OFFER": "Offer", "SALE": "Sale"}
_BRANDS = ["Kuling", "Babyshop", "Joolz", "Stoy", "Bugaboo", "Cybex", "Stokke"]


def _parse_bm(name: str) -> dict | None:
    parts = [p.strip() for p in (name or "").split("|")]
    if len(parts) < 8 or parts[0].upper() != "BM":
        return None
    f = dict(zip(["market", "type", "angle", "offer", "product", "format", "production", "version"],
                 parts[1:9] + [""] * (9 - len(parts))))
    f["type"] = TYPE_CODES.get(f["type"].upper(), f["type"])
    return f


def _offer(text: str) -> str:
    m = re.search(r"(\d+)\s?%", text)
    if m:
        return f"{m.group(1)}% off"
    if re.search(r"(?i)save more|buy more", text):
        return "Buy more save more"
    if re.search(r"(?i)\bsale\b|\brea\b|tilbud|discou?n?t", text):
        return "Sale"
    return "No offer"


def _parse_legacy(r: dict) -> dict:
    name = r.get("ad_name") or ""
    head = re.split(r"[ ,\-]", name.strip(), maxsplit=1)[0].upper()
    angle = next((lab for pat, lab in _ANGLE_WORDS if re.search(pat, name, re.I)),
                 _OBJECTIVES.get(head, "Unknown"))
    product = next((b for b in _BRANDS if re.search(rf"\b{b}\b", name + " " + (r.get("campaign_name") or ""), re.I)),
                   "Unknown")
    fmt = {"Image (still)": "Still"}.get(r.get("media_type"), r.get("media_type") or "Unknown")
    prod = r.get("tag") or "Untagged / legacy"
    return {"angle": angle, "offer": _offer(name), "product": product, "format": fmt,
            "production": "Unknown" if prod == "Untagged / legacy" else prod}


_HEAD_WORDS = set(_OBJECTIVES) | {"DPA", "CATALOG", "SWE", "SE", "NOR", "NO", "FIN", "FI",
                                   "DK", "DNK", "EU", "COPY"}
_TAIL_WORDS = {"UGC", "HIGHEND", "INHOUSEPROD", "INHOUSE", "PARTNERSHIP", "VIDEO", "IMG",
               "STILLIMG", "CAROUSEL", "SLIDESHOW", "STATIC", "PDPIMG", "BRANDASSET"}


def short_name(name: str) -> str:
    """Today's KiriMedia slug minus what the row already shows as columns
    (objective, market, serial, concept, format, date, 'Copy'):
    'Product - SWE - 1 - Kuling - Shoes - … - UGC - Still IMG - 2026-10-15'
    → 'Kuling · Shoes · …'. A BM-convention name keeps angle / offer / product."""
    bmt = _parse_bm(name)
    if bmt:
        return " · ".join(x for x in (bmt["angle"], bmt["offer"], bmt["product"], bmt["version"]) if x)
    parts = [p.strip() for p in re.split(r"\s+-\s*|\s*-\s+", name or "") if p.strip()]
    key = lambda p: re.sub(r"[^A-Za-z]", "", p).upper()
    keep = []
    for p in parts:
        k = key(p)
        words = {key(w) for w in p.split(",")}
        if (not k or k in _TAIL_WORDS or re.match(r"^\d{4}-\d{2}", p) or re.match(r"(?i)^copy( \d+)?$", p)
                or words <= _HEAD_WORDS):
            continue
        keep.append(p.strip('" '))
    return " · ".join(keep) or (name or "")


def campaign_type(campaign: str, ad_name: str) -> str:
    c = (campaign or "").upper()
    if "CATALOG" in c or (ad_name or "").upper().startswith("DPA"):
        return "DPA / catalog"
    if "RETARG" in c or "-RET" in c:
        return "Retargeting"
    if "MKTCONTRIB" in c or " X " in c:
        return "Brand partner"
    if "OFFER" in c:
        return "Offers"
    if "STOY" in c:
        return "Stoy"
    if "KULING" in c:
        return "Kuling"
    if "PROSP" in c:
        return "Prospecting"
    return "Other"


# ── Helpers ──────────────────────────────────────────────────────────────────
def _block(days: list[dict]) -> dict:
    s = rm._sum_days(days)
    m = rm._metrics(s)
    return {"spend": m["spend"], "link_ctr": m["link_ctr"], "roas": m["roas"],
            "purchases": m["purchases"], "freq": rm._freq(s), "hook_rate": m["hook_rate"]}


def _fatigue(spend_days: list[dict]) -> dict:
    """First 3 spend days of the ad's life vs its last 3 settled spend days."""
    if len(spend_days) < FATIGUE_MIN_DAYS:
        return {"status": "n/a", "reason": f"{len(spend_days)} spend days — too few to compare"}
    a, b = _block(spend_days[:FATIGUE_SPAN]), _block(spend_days[-FATIGUE_SPAN:])
    if (a["spend"] or 0) < FATIGUE_MIN_SPEND or (b["spend"] or 0) < FATIGUE_MIN_SPEND:
        return {"status": "n/a", "first": a, "last": b, "reason": "too little spend in a 3-day block"}
    idx = lambda k: rm.div(b[k], a[k]) if a[k] and b[k] is not None else None
    ctr_i, roas_i, freq_x = idx("link_ctr"), idx("roas"), idx("freq")
    if (a["purchases"] or 0) < 2:
        roas_i = None                 # a 3-day ROAS on 0–1 purchases is noise
    why = []
    if ctr_i is not None and ctr_i < FATIGUE_CTR_IDX:
        why.append(f"CTR {ctr_i:.0%} of start")
    if roas_i is not None and roas_i < FATIGUE_ROAS_IDX:
        why.append(f"ROAS {roas_i:.0%} of start")
    if freq_x is not None and freq_x >= FATIGUE_FREQ_X:
        why.append(f"frequency {freq_x:.1f}×")
    return {"status": "fatiguing" if why else "ok", "ctr_idx": ctr_i, "roas_idx": roas_i,
            "freq_x": freq_x, "first": a, "last": b, "reason": ", ".join(why)}


def _verdict(c: dict, ref: dict) -> tuple[str, str]:
    spend, roas, buys = c["spend"] or 0, c["roas"], c["purchases"] or 0
    avg_roas, avg_cpa, floor = ref.get("roas"), ref.get("cpa"), ref["floor"]
    fat = c["fatigue"]["status"] == "fatiguing"
    if spend < floor:
        return "Watch", f"learning: {spend:.0f} of {floor:.0f} kr floor in {JUDGE_DAYS} d"
    rel = rm.div(roas, avg_roas) if roas is not None and avg_roas else None
    rel_s = f"ROAS {rel:.2f}× market" if rel is not None else "no market ROAS"
    if buys == 0 and avg_cpa and spend >= KILL_ZERO_CPA_X * avg_cpa:
        return "Kill", f"0 purchases on {spend:.0f} kr (market CPA {avg_cpa:.0f} kr)"
    if rel is not None and rel < KILL_ROAS_X:
        return "Kill", rel_s + (f", {c['fatigue']['reason']}" if fat else "")
    if fat:
        return "Watch", f"fatiguing: {c['fatigue']['reason']} ({rel_s})"
    if rel is not None and rel >= SCALE_ROAS_X and buys >= SCALE_MIN_PURCHASES:
        return "Scale", f"{rel_s} on {buys} purchases"
    if rel is not None and rel < WATCH_ROAS_X:
        return "Watch", rel_s + " — below par"
    return "Keep", rel_s


def _hook(rows: list[dict]) -> float | None:
    """Hook rate over VIDEO-format ads only. Stills and catalog ads report a
    handful of video plays (rm._is_video counts them), which would print a
    meaningless ~0 % hook; the format comes from the name / creative video id."""
    vids = [r for r in rows if r.get("format") == "Video"]
    imp = sum(rm._f(r.get("impressions")) for r in vids)
    return rm.div(sum(rm._f(r.get("video_3s")) for r in vids), imp, 100.0) if imp else None


def _agg(rows: list[dict]) -> dict:
    m = rm._metrics(rm._sum_rows(rows))
    return {"spend": m["spend"], "purchases": m["purchases"], "purchase_value": m["purchase_value"],
            "roas": m["roas"], "cpa": m["cpa"], "link_ctr": m["link_ctr"],
            "hook_rate": _hook(rows), "ads": len(rows)}


def _r(v, nd=2):
    return None if v is None else round(v, nd)


# ── Build ────────────────────────────────────────────────────────────────────
def build(ctx: dict) -> dict:
    today = ctx["today"]
    last_day = today - datetime.timedelta(days=1)                 # last (near-)complete day
    end = today - datetime.timedelta(days=SETTLE_DAYS)            # last settled day
    j_from = end - datetime.timedelta(days=JUDGE_DAYS - 1)
    t_from = end - datetime.timedelta(days=TAG_DAYS - 1)
    new_from = last_day - datetime.timedelta(days=NEW_DAYS - 1)
    nov_from = bm.MONTH_START

    # Full history: the fatigue baseline is the ad's first 3 spend days, which
    # for an evergreen creative can be months back. ~25k ad-days in total.
    scan = {"scan_from": rm.DATA_START, "to": last_day.isoformat(),
            "long_from": t_from.isoformat()}
    dims = {str(r["ad_id"]): r for r in rm.ad_dims(scan)}
    by_ad: dict[str, list[dict]] = {}
    for r in rm.per_ad_day(scan):
        by_ad.setdefault(str(r["ad_id"]), []).append(r)
    account_id = next((str(d["account_id"]) for d in dims.values() if d.get("account_id")), "")

    def window(days, d0, d1):
        return [r for r in days if d0 <= r["date"] <= d1]

    # Per-ad facts over the windows we need.
    ads = []
    for ad_id, days in by_ad.items():
        d = dims.get(ad_id)
        if not d or d.get("mk") not in bm.MARKETS:
            continue
        days.sort(key=lambda r: r["date"])
        spend_days = [r for r in days if rm._f(r.get("spend")) > 0]
        if not spend_days:
            continue
        nov = window(days, nov_from, last_day)
        tag_win = window(days, t_from, end)
        if not any(rm._f(r.get("spend")) > 0 for r in nov) and not tag_win:
            continue
        bm_tags = _parse_bm(d.get("ad_name"))
        ctype = campaign_type(d.get("campaign_name"), d.get("ad_name"))
        is_dpa = d.get("media_type") == "DPA / catalog" or ctype == "DPA / catalog"
        tags_ = bm_tags or _parse_legacy(d)
        if is_dpa:
            tags_["format"] = "Catalog"
        if bm_tags and bm_tags.get("type"):
            ctype = ctype if is_dpa else bm_tags["type"]
        ads.append({"ad_id": ad_id, "dim": d, "brand": brand_of(d), "days": days, "spend_days": spend_days,
                    "nov": nov, "tag_win": tag_win, "dpa": is_dpa, "ctype": ctype,
                    "tags": tags_, "tagged": bool(bm_tags)})

    # Market reference over the judging window: non-DPA ads only.
    judge_rows = {}
    for a in ads:
        sel = window(a["days"], j_from, end)
        if sel:
            judge_rows[a["ad_id"]] = {**rm._sum_days(sel), "format": a["tags"]["format"]}
    # Per brand AND market: the stores' economics differ, so an ad is judged
    # against its own store's market. ref['all'] blends both and is shown only.
    in_scope = lambda a, br, mk: ((br == "all" or a["brand"] == br)
                                  and (mk == "all" or a["dim"]["mk"] == mk))
    ref = {}
    for br in ["all"] + BRANDS:
        ref[br] = {}
        for mk in ["all"] + bm.MARKETS:
            rows = [judge_rows[a["ad_id"]] for a in ads if not a["dpa"] and a["ad_id"] in judge_rows
                    and in_scope(a, br, mk)]
            m = rm._metrics(rm._sum_rows(rows)) if rows else {}
            r_ = {"roas": m.get("roas"), "cpa": m.get("cpa"), "link_ctr": m.get("link_ctr"),
                  "spend": m.get("spend"), "hook_rate": _hook(rows)}
            r_["floor"] = round(max(SPEND_FLOOR_MIN, FLOOR_CPA_X * (r_["cpa"] or 0)))
            ref[br][mk] = r_

    # Scorecard: every ad with November spend.
    cards, dpa_cards = [], []
    for a in ads:
        nov_spend = sum(rm._f(r.get("spend")) for r in a["nov"])
        if nov_spend <= 0:
            continue
        d = a["dim"]
        jr = judge_rows.get(a["ad_id"]) or rm._sum_days([])
        m = rm._metrics(jr)
        settled = [r for r in a["spend_days"] if r["date"] <= end]
        first = a["spend_days"][0]["date"]
        live_days = len(a["spend_days"])            # days with spend, not calendar days
        is_video = a["tags"]["format"] == "Video"
        c = {
            "ad_id": a["ad_id"], "name": short_name(d.get("ad_name")), "ad_name": d.get("ad_name"),
            "campaign": d.get("campaign_name"), "brand": a["brand"], "market": d["mk"], "type": a["ctype"],
            "format": a["tags"]["format"], "status": d.get("effective_status"),
            "active": d.get("effective_status") == "ACTIVE",
            "first_spend": first.isoformat(), "days_live": live_days,
            "nov_spend": round(nov_spend),
            "spend": m["spend"], "impressions": m["impressions"], "purchases": m["purchases"],
            "purchase_value": m["purchase_value"], "roas": m["roas"], "cpa": m["cpa"],
            "ctr": m["link_ctr"], "cpc": m["cpc"], "cpm": m["cpm"],
            "hook_rate": m["hook_rate"] if is_video and m["impressions"] else None,
            "hold_rate": m["hold_rate"] if is_video else None,
            "freq": rm._freq(jr),
            "fatigue": _fatigue(settled),
            "tagged": a["tagged"], "tags": a["tags"],
            "ads_manager_url": rm.ADS_MANAGER_URL.format(account_id=account_id, ad_id=a["ad_id"]),
        }
        mref = ref[a["brand"]][d["mk"]]
        c["verdict"], c["reason"] = _verdict(c, mref)
        if not c["active"]:
            c["reason"] += f" · already off ({(d.get('effective_status') or '').lower().replace('_', ' ')})"
        c["rel_roas"] = rm.div(c["roas"], mref["roas"]) if c["roas"] is not None and mref["roas"] else None
        c["new"] = first >= new_from
        (dpa_cards if a["dpa"] else cards).append(c)

    def sort_key(c):
        # live ads with an action first; within a verdict, the most money first
        return (not c["active"], ACTION_ORDER[c["verdict"]],
                c["verdict"] == "Watch" and c["fatigue"]["status"] != "fatiguing",
                -(c["spend"] or 0), -c["nov_spend"])
    cards.sort(key=sort_key)
    dpa_cards.sort(key=sort_key)

    # Results by tag over the tag window, per market filter value.
    tag_rows = []
    for a in ads:
        if a["dpa"] or not a["tag_win"]:
            continue
        s = rm._sum_days(a["tag_win"])
        if s["spend"] <= 0:
            continue
        s.update({"mk": a["dim"]["mk"], "brand": a["brand"], "tagged": a["tagged"], **{k: a["tags"][k] for k, _ in TAG_DIMS}})
        tag_rows.append(s)
    tags = {br: {} for br in ["all"] + BRANDS}
    for br, mk in [(b, m) for b in ["all"] + BRANDS for m in ["all"] + bm.MARKETS]:
        rows = [r for r in tag_rows if (br == "all" or r["brand"] == br) and (mk == "all" or r["mk"] == mk)]
        tot = sum(r["spend"] for r in rows)
        mref = _agg(rows) if rows else {}
        out = {"total_spend": round(tot), "roas": mref.get("roas"),
               "untagged_share": rm.div(sum(r["spend"] for r in rows if not r["tagged"]), tot, 1.0, 3),
               "dims": {}}
        for k, _ in TAG_DIMS:
            groups: dict[str, list] = {}
            for r in rows:
                groups.setdefault(r[k], []).append(r)
            lst = []
            for val, g in groups.items():
                gg = _agg(g)
                gg.update({"value": val, "share": rm.div(gg["spend"], tot, 1.0, 3),
                           "roas_idx": rm.div(gg["roas"], mref.get("roas")) if gg["roas"] is not None else None})
                lst.append(gg)
            lst.sort(key=lambda x: -(x["spend"] or 0))
            out["dims"][k] = lst
        tags[br][mk] = out

    # Market reference also on the page.
    for per_mk in ref.values():
        for v in per_mk.values():
            for k in v:
                v[k] = _r(v[k])

    # Which brands the warehouse actually has; the page says so for the rest.
    seen = {a["brand"] for a in ads}
    brands = {b: {"label": BRAND_LABEL[b], "available": b in seen,
                  "accounts": sorted(k for k, v in BRAND_BY_ACCOUNT.items() if v == b)} for b in BRANDS}
    missing = [BRAND_LABEL[b] for b in BRANDS if b not in seen]

    notes = [
        f"Metrics and verdicts use the {JUDGE_DAYS} settled days {j_from.isoformat()} – {end.isoformat()}; "
        f"the last {SETTLE_DAYS} days are left out because Meta keeps attributing purchases to them.",
        "ROAS is Meta's platform-reported purchase value ÷ spend (7d click / 1d view), revenue not profit. "
        "Use it to rank creatives against each other, not as business ROAS.",
        "Hook rate = 3-second video views ÷ impressions, video-format ads only (format from the ad name / "
        "creative video id; stills and catalog ads that log a few autoplays are left out). Hold rate = "
        "ThruPlays ÷ plays.",
        "Frequency is the average DAILY frequency (impressions ÷ summed daily reach); the feed has no "
        "period-level reach.",
        "Market comes from the ad name (SWE/NOR/FIN/DK), as on the Meta creatives tab. Ads named for 'EU' "
        "fall back to the campaign's market.",
        f"Results by tag cover {TAG_DAYS} settled days ({t_from.isoformat()} – {end.isoformat()}) so a tag "
        "has enough spend behind it. Untagged ads are parsed from today's names and counted as untagged.",
        "Verdicts never act on anything: this page is read-only. Pause or scale in Ads Manager.",
        "Each ad is judged against its own store's market average (Babyshop SE vs Babyshop SE, Lekmer SE vs "
        "Lekmer SE); 'All' brands only blends them for display.",
    ]
    if missing:
        notes.insert(0, f"{' and '.join(missing)} Meta ads are not in the warehouse: the Meta connector loads "
                        "one ad account (Babyshop, 2927326560764919) and no ad or campaign names mention "
                        f"{' or '.join(missing)}. {' and '.join(missing)} creatives appear here once their "
                        "ad account is added to the connector.")
    return {
        "windows": {"judge": {"from": j_from.isoformat(), "to": end.isoformat(), "days": JUDGE_DAYS},
                    "tags": {"from": t_from.isoformat(), "to": end.isoformat(), "days": TAG_DAYS},
                    "new": {"from": new_from.isoformat(), "to": last_day.isoformat(), "days": NEW_DAYS},
                    "november": {"from": nov_from.isoformat(), "to": last_day.isoformat()}},
        "rules": RULES,
        "thresholds": {"spend_floor_min": SPEND_FLOOR_MIN, "floor_cpa_x": FLOOR_CPA_X, "scale_roas_x": SCALE_ROAS_X,
                       "kill_roas_x": KILL_ROAS_X, "watch_roas_x": WATCH_ROAS_X},
        "brands": brands,
        "market_ref": ref,
        "creatives": cards,
        "dpa": dpa_cards,
        "tags": tags,
        "tag_dims": [{"key": k, "label": lab} for k, lab in TAG_DIMS],
        "convention": CONVENTION,
        "image_url": "/api/meta/image/{ad_id}",
        "notes": notes,
    }
