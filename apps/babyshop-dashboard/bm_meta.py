#!/usr/bin/env python3
"""
bm_meta.py: "Meta · scale / hold / cut" section of the Black Month tab.

Three levels a buyer can actually move:
  1. market
  2. market × tactic: new customers (Meta's "prospecting" segment) vs existing
     customers (Meta's "existing customer" segment = retargeting/retention)
  3. market × tactic × format (catalog/DPA or not) × offer vs evergreen × line
     (Kuling / Babyshop / STOY) — matches the ad sets you would scale or pause

Every row gets a verdict (Scale / Hold / Cut / too little data) from:
  1. incremental GP3 per krona (marginal), as a range:
       low  = Norce orders from Meta ad clicks (GA4-matched), new vs returning by
              Norce's own new-customer flag
       high = Meta's own purchases (7-day click / 1-day view) for the segment
     value = new orders × (first-order GP2 + 12-month repeat GP2) × inc_new
           + returning orders × GP2 × inc_existing
     marginal = Δvalue ÷ Δspend last 7 days vs the 7 before (demand-normalised by
                the market's site orders) when spend moved ≥ 20 %, else
                average × 0.75; never above the average, never below 0
  2. frequency gates (7-day, straight from Meta): new > 3 blocks Scale,
     existing > 5 forces Cut
  3. cost-rise gate: CPM and Meta CPA both up > 15 % while spend rose → one step down
  4. market GP3 headroom from the pacing section: negative headroom blocks Scale
  5. data gate: < 3 000 SEK spend or < 10 Norce orders in 7 days → no verdict
The verdict is "confident" only when the low and high ends agree.

Sources (read-only):
  Meta Graph API insights for Babyshop's account 2927326560764919, breakdown
  user_segment_key (new / existing) — the warehouse feed does not carry it.
  Token: env META_ADS_ACCESS_TOKEN (same as bm_insights_meta).
  claude-private-499703.babyshop_marts.fct_order_ga4_match (Norce orders ↔ GA4
  session source/campaign) and fct_customer_orders (12-month repeat value).
  Margins (GP2 rate) and headroom: this run's pacing payload.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from collections import defaultdict

import bm_common as bm
from bm_insights_meta import GRAPH, _get, market_of

ACCOUNT = "2927326560764919"                 # Babyshop SE - New (the only one spending)
IDLE_ACCOUNTS = {"686612050420009": "Kuling", "1465585123471722": "Lekmer"}
WH = "`claude-private-499703.babyshop_marts.{}`"
MKTS = bm.MARKETS + ["ROW"]

T = {                                        # verdict thresholds (agreed 2026-11-11)
    "scale": 1.2, "cut": 0.8,                # incremental GP3 per krona (1.0 = break-even)
    "freq_new": 3.0, "freq_existing": 5.0,   # 7-day frequency
    "cost_rise": 0.15,                       # CPM and CPA both up more than this
    "min_spend": 3000, "min_orders": 10,     # 7-day data gate
    "spend_move": 0.20,                      # spend change needed to read a slope
    "step_up": 0.20, "step_down": 0.25,      # recommended budget step
}
MARGINAL_HAIRCUT = 0.75                      # marginal ≈ 0.75 × average when spend did not move
INC = {"new": bm.INCREMENTALITY["meta_prospecting"], "existing": bm.INCREMENTALITY["meta_retargeting"]}
REPEAT_CAP = 20_000                          # SEK of repeat revenue per customer (B2B-ish outliers)
SEG_OF = {"prospecting": "new", "existing": "existing", "engaged": "new", "unknown": "new"}
SEG_LABEL = {"new": "New customers (prospecting)", "existing": "Existing customers (retargeting)"}
VERDICTS = ("scale", "hold", "cut")

META_SRC_RX = r"^(facebook|instagram|meta|fb|ig)$"
META_MED_RX = r"^(cpc|ppc|paid|paid_social|paidsocial)"


# ════════════════════════════════════════════════════════════════════════════
#  Meta Graph API
# ════════════════════════════════════════════════════════════════════════════

def _insights(token: str, *, level: str, breakdowns: str, fields: str,
              since: dt.date, until: dt.date, daily: bool = False) -> list[dict]:
    url = f"{GRAPH}/act_{ACCOUNT}/insights"
    params = {"level": level, "breakdowns": breakdowns, "fields": fields, "limit": 500,
              "time_range": json.dumps({"since": since.isoformat(), "until": until.isoformat()})}
    if daily:
        params["time_increment"] = 1
    out = []
    for _ in range(60):
        d = _get(url, params, token)
        out.extend(d.get("data") or [])
        url, params = (d.get("paging") or {}).get("next"), None
        if not url:
            break
    return out


def _act(lst, kind: str) -> float:
    for a in lst or []:
        if a.get("action_type") == kind:
            return float(a.get("value") or 0)
    return 0.0


def _spend_only(token: str, acct: str, since: dt.date, until: dt.date) -> float:
    d = _get(f"{GRAPH}/act_{acct}/insights", {
        "level": "account", "fields": "spend",
        "time_range": json.dumps({"since": since.isoformat(), "until": until.isoformat()})}, token)
    return sum(float(r.get("spend") or 0) for r in d.get("data") or [])


# ════════════════════════════════════════════════════════════════════════════
#  Classification
# ════════════════════════════════════════════════════════════════════════════

def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def classify(campaign: str, adset: str) -> dict:
    n = f"{campaign} {adset}".lower()
    fmt = "dpa" if re.search(r"catalog|dpa", n) else "creative"
    offer = "offer" if re.search(r"offer", n) else "evergreen"
    line = "kuling" if "kuling" in n else "stoy" if "stoy" in n else "babyshop"
    return {"fmt": fmt, "offer": offer, "line": line}


FMT_LABEL = {"dpa": "Catalog / DPA", "creative": "Creative ads"}
OFFER_LABEL = {"offer": "Offer", "evergreen": "Evergreen"}
LINE_LABEL = {"kuling": "Kuling", "stoy": "STOY", "babyshop": "Babyshop"}


def _n(x) -> str:
    """12 345 (space thousands, as on the page)."""
    return f"{x:,.0f}".replace(",", " ")


def _mk(m: str | None) -> str:
    m = (m or "").upper()
    return m if m in bm.MARKETS else "ROW"


# ════════════════════════════════════════════════════════════════════════════
#  Warehouse
# ════════════════════════════════════════════════════════════════════════════

def _norce_meta(d0: dt.date, d1: dt.date) -> tuple[list[dict], list[dict]]:
    """Orders whose GA4 session came from a paid Meta click, per day × market ×
    utm campaign × new flag; plus site-wide order counts per day × market (all
    statuses, and matched only) for the demand index and the match-rate gross-up."""
    import refresh_meta as rm
    meta = rm.q(f"""
        SELECT CAST(date AS STRING) d, market, session_campaign camp, is_new_customer nw,
               COUNT(*) orders, SUM(order_revenue_gross) rev
        FROM {WH.format('fct_order_ga4_match')}
        WHERE date BETWEEN @s AND @e AND client_site = 'babyshop' AND match_status = 'matched'
          AND REGEXP_CONTAINS(LOWER(session_source), r'{META_SRC_RX}')
          AND REGEXP_CONTAINS(LOWER(session_medium), r'{META_MED_RX}')
        GROUP BY d, market, camp, nw""", s=d0, e=d1)
    site = rm.q(f"""
        SELECT CAST(date AS STRING) d, market,
               COUNTIF(match_status IN ('matched', 'order_only', 'settling')) all_orders,
               COUNTIF(match_status = 'matched') matched
        FROM {WH.format('fct_order_ga4_match')}
        WHERE date BETWEEN @s AND @e AND client_site = 'babyshop'
        GROUP BY d, market""", s=d0, e=d1)
    return meta, site


def _repeat_value() -> dict:
    """12-month repeat revenue per new customer, per market: customers whose
    first order (no order in that market the year before) fell Nov 2024–Oct 2025,
    so every one has a full year observed. Per-customer repeat revenue capped."""
    import refresh_meta as rm
    rows = rm.q(f"""
        WITH o AS (
          SELECT market, customer_key, order_date, revenue_gross, is_new_365
          FROM {WH.format('fct_customer_orders')}
          WHERE client_site = 'babyshop' AND customer_key IS NOT NULL AND revenue_gross > 0),
        f AS (SELECT market, customer_key, order_date d0, revenue_gross r0 FROM o
              WHERE is_new_365 AND order_date BETWEEN @s AND @e),
        x AS (SELECT f.market, f.customer_key, f.d0, f.r0,
                     LEAST(IFNULL(SUM(o.revenue_gross), 0), {REPEAT_CAP}) rep
              FROM f LEFT JOIN o ON o.customer_key = f.customer_key AND o.market = f.market
                   AND o.order_date > f.d0 AND o.order_date <= DATE_ADD(f.d0, INTERVAL 365 DAY)
              GROUP BY 1, 2, 3, 4)
        SELECT market, COUNT(*) n, AVG(r0) first_aov, AVG(rep) repeat_rev, AVG(IF(rep > 0, 1, 0)) repeat_rate
        FROM x GROUP BY market""", s=dt.date(2024, 11, 1), e=dt.date(2025, 10, 31))
    out, tot = {}, {"n": 0, "rev": 0.0, "rate": 0.0}
    for r in rows:
        m = _mk(r["market"])
        n = int(r["n"] or 0)
        cur = out.setdefault(m, {"n": 0, "rev": 0.0, "rate": 0.0})
        cur["n"] += n; cur["rev"] += float(r["repeat_rev"] or 0) * n; cur["rate"] += float(r["repeat_rate"] or 0) * n
        tot["n"] += n; tot["rev"] += float(r["repeat_rev"] or 0) * n; tot["rate"] += float(r["repeat_rate"] or 0) * n
    res = {m: {"customers": v["n"], "repeat_rev": round(v["rev"] / v["n"]) if v["n"] else 0,
               "repeat_rate": round(v["rate"] / v["n"], 3) if v["n"] else 0} for m, v in out.items()}
    res["all"] = {"customers": tot["n"], "repeat_rev": round(tot["rev"] / tot["n"]) if tot["n"] else 0,
                  "repeat_rate": round(tot["rate"] / tot["n"], 3) if tot["n"] else 0}
    return res


# ════════════════════════════════════════════════════════════════════════════
#  Build
# ════════════════════════════════════════════════════════════════════════════

def _pacing(ctx: dict) -> dict:
    p = (ctx.get("built") or {}).get("pacing")
    if p:
        return p
    try:
        return bm.read_section("pacing") or {}
    except Exception:                                    # noqa: BLE001
        return {}


def _windows(today: dt.date) -> tuple[dt.date, dict]:
    y = today - dt.timedelta(days=1)
    w = {"l7": (y - dt.timedelta(days=6), y), "p7": (y - dt.timedelta(days=13), y - dt.timedelta(days=7)),
         "mtd": (min(bm.MONTH_START, y), y)}
    return y, w


def _in(d: str, w: tuple[dt.date, dt.date]) -> bool:
    return w[0].isoformat() <= d <= w[1].isoformat()


def _blank() -> dict:
    return {"spend": 0.0, "impr": 0.0, "purch": 0.0, "pval": 0.0,
            "n_new": 0.0, "n_ret": 0.0, "rev_new": 0.0, "rev_ret": 0.0}


def build(ctx: dict) -> dict:
    today = ctx["today"]
    y, W = _windows(today)
    lo = min(W["p7"][0], W["mtd"][0])
    notes, errors = [], []
    token = (os.environ.get("META_ADS_ACCESS_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("META_ADS_ACCESS_TOKEN is not set")

    # ── Meta: daily spend/purchases per ad set × segment ────────────────────
    daily = _insights(token, level="adset", breakdowns="user_segment_key",
                      fields="campaign_id,campaign_name,adset_id,adset_name,spend,impressions,actions,action_values",
                      since=lo, until=y, daily=True)
    adsets: dict[str, dict] = {}
    camp_by_id, camp_by_norm = {}, {}
    mrows = []                                           # (date, adset_id, seg, metrics)
    for r in daily:
        seg = SEG_OF.get(r.get("user_segment_key") or "unknown", "new")
        aid = r["adset_id"]
        if aid not in adsets:
            mkt = market_of(r.get("adset_name"), r.get("campaign_name"))
            adsets[aid] = {"id": aid, "name": r.get("adset_name") or "", "campaign": r.get("campaign_name") or "",
                           "campaign_id": r.get("campaign_id"), "market": _mk(mkt if mkt != "all" else None),
                           **classify(r.get("campaign_name"), r.get("adset_name"))}
        camp_by_id[str(r.get("campaign_id"))] = r.get("campaign_name") or ""
        camp_by_norm[_norm(r.get("campaign_name"))] = r.get("campaign_name") or ""
        mrows.append((r["date_start"], aid, seg, float(r.get("spend") or 0), float(r.get("impressions") or 0),
                      _act(r.get("actions"), "purchase"), _act(r.get("action_values"), "purchase")))

    # ── Meta: true frequency per window (reach does not add up across days) ──
    freq = {}                                            # (window, key...) → frequency
    for wk, (a, b) in W.items():
        for r in _insights(token, level="adset", breakdowns="user_segment_key",
                           fields="adset_id,impressions,reach", since=a, until=b):
            seg = SEG_OF.get(r.get("user_segment_key") or "unknown", "new")
            k = (wk, "adset", r["adset_id"], seg)
            imp, rch = freq.get(k, (0.0, 0.0))
            freq[k] = (imp + float(r.get("impressions") or 0), rch + float(r.get("reach") or 0))
        for r in _insights(token, level="account", breakdowns="country,user_segment_key",
                           fields="impressions,reach", since=a, until=b):
            seg = SEG_OF.get(r.get("user_segment_key") or "unknown", "new")
            k = (wk, "mseg", _mk(r.get("country")), seg)
            imp, rch = freq.get(k, (0.0, 0.0))
            freq[k] = (imp + float(r.get("impressions") or 0), rch + float(r.get("reach") or 0))
        for r in _insights(token, level="account", breakdowns="country",
                           fields="impressions,reach", since=a, until=b):
            k = (wk, "mkt", _mk(r.get("country")))
            imp, rch = freq.get(k, (0.0, 0.0))
            freq[k] = (imp + float(r.get("impressions") or 0), rch + float(r.get("reach") or 0))
        for r in _insights(token, level="account", breakdowns="user_segment_key",
                           fields="impressions,reach", since=a, until=b):
            seg = SEG_OF.get(r.get("user_segment_key") or "unknown", "new")
            k = (wk, "mseg", "all", seg)
            imp, rch = freq.get(k, (0.0, 0.0))
            freq[k] = (imp + float(r.get("impressions") or 0), rch + float(r.get("reach") or 0))
        for r in _insights(token, level="account", breakdowns="", fields="impressions,reach", since=a, until=b):
            freq[(wk, "mkt", "all")] = (float(r.get("impressions") or 0), float(r.get("reach") or 0))

    def fq(*k):
        imp, rch = freq.get(k, (0.0, 0.0))
        return round(imp / rch, 2) if rch else None

    # ── Norce: Meta-click orders, grossed up for orders not yet matched ─────
    nmeta, nsite = _norce_meta(lo, y)
    site = defaultdict(lambda: [0.0, 0.0])               # (d, mkt) → [all, matched]
    for r in nsite:
        s = site[(r["d"], _mk(r["market"]))]
        s[0] += r["all_orders"] or 0; s[1] += r["matched"] or 0
    unmapped = defaultdict(float)
    norce = []                                           # (d, mkt, campaign name, is_new, orders, rev)
    for r in nmeta:
        mkt = _mk(r["market"])
        a, m = site[(r["d"], mkt)]
        gross_up = (a / m) if m else 1.0
        raw = r["camp"] or ""
        camp = camp_by_id.get(raw) or camp_by_norm.get(_norm(raw))
        if not camp:                                    # renamed campaigns: match on the name's tail
            nr = _norm(raw)
            camp = next((v for k, v in camp_by_norm.items() if nr and (nr in k or k in nr)), None)
        if not camp:
            unmapped[raw or "(none)"] += r["orders"] or 0
            camp = raw or "(unknown campaign)"
        norce.append((r["d"], mkt, camp, bool(r["nw"]), float(r["orders"] or 0) * gross_up,
                      float(r["rev"] or 0) * gross_up))

    # ── Aggregate per window into rows keyed by level ───────────────────────
    agg = defaultdict(_blank)                            # (window, level-key) → metrics

    def keys(mkt, seg, a):
        l3 = (mkt, seg, a["fmt"], a["offer"], a["line"])
        return [("m", mkt), ("m", "all"), ("t", mkt, seg), ("t", "all", seg), ("x",) + l3]

    l3_purch = defaultdict(float)                        # (window, mkt, campaign, seg, l3key) → Meta purchases
    l3_spend = defaultdict(float)
    for d, aid, seg, sp, imp, pu, pv in mrows:
        a = adsets[aid]
        for wk, w in W.items():
            if not _in(d, w):
                continue
            for k in keys(a["market"], seg, a):
                g = agg[(wk,) + k]
                g["spend"] += sp; g["impr"] += imp; g["purch"] += pu; g["pval"] += pv
            l3k = (a["market"], seg, a["fmt"], a["offer"], a["line"])
            l3_purch[(wk, a["market"], a["campaign"], seg, l3k)] += pu
            l3_spend[(wk, a["market"], a["campaign"], seg, l3k)] += sp

    for d, mkt, camp, nw, n, rev in norce:
        seg = "new" if nw else "existing"
        f_n, f_r = ("n_new", "rev_new") if nw else ("n_ret", "rev_ret")
        for wk, w in W.items():
            if not _in(d, w):
                continue
            for k in (("m", mkt), ("m", "all"), ("t", mkt, seg), ("t", "all", seg)):
                agg[(wk,) + k][f_n] += n; agg[(wk,) + k][f_r] += rev
            # level 3: allocate the campaign's orders over its ad-set groups in
            # that market by Meta purchases (spend when Meta reports none)
            cands = {k[4]: v for k, v in l3_purch.items() if k[:4] == (wk, mkt, camp, seg)}
            if not sum(cands.values()):
                cands = {k[4]: v for k, v in l3_spend.items() if k[:4] == (wk, mkt, camp, seg)}
            tot = sum(cands.values())
            if not tot:
                c = classify(camp, "")
                cands, tot = {(mkt, seg, c["fmt"], c["offer"], c["line"]): 1.0}, 1.0
            for l3k, v in cands.items():
                g = agg[(wk, "x") + l3k]
                g[f_n] += n * v / tot; g[f_r] += rev * v / tot

    # demand index per market: site orders L7 ÷ P7
    def site_orders(mkt, w):
        return sum(v[0] for (d, m), v in site.items() if _in(d, w) and (mkt == "all" or m == mkt))
    demand = {m: (site_orders(m, W["l7"]) / site_orders(m, W["p7"])) if site_orders(m, W["p7"]) else 1.0
              for m in MKTS + ["all"]}

    # ── Economics inputs from pacing + repeat value ─────────────────────────
    pac = _pacing(ctx)
    bp = ((pac.get("brands") or {}).get("babyshop") or {})
    margins = bp.get("margins") or pac.get("margins") or {}
    headroom = bp.get("headroom") or pac.get("headroom") or {}
    try:
        rep = _repeat_value()
    except Exception as e:                               # noqa: BLE001
        rep = {}
        errors.append(f"Repeat value not loaded: {e}")

    def gm2(m):
        v = (margins.get(m) or {}).get("gm2") or (margins.get("all") or {}).get("gm2")
        return float(v) if v else 0.29

    def repeat_gp2(m):
        r = rep.get(m) or rep.get("all") or {}
        return (r.get("repeat_rev") or 0) * gm2(m)

    def value(g, m, segs, src):
        """Incremental GP2 (incl. 12-month repeat for new customers) for a metrics
        bucket. src 'norce' uses Norce orders; 'meta' uses Meta's purchases,
        attributing them to the bucket's segment(s)."""
        r = gm2(m)
        if src == "norce":
            v = 0.0
            if "new" in segs:
                v += (g["rev_new"] * r + g["n_new"] * repeat_gp2(m)) * INC["new"]
            if "existing" in segs:
                v += g["rev_ret"] * r * INC["existing"]
            return v
        return None

    def meta_value(gs, m):
        """gs: {seg: metrics} — Meta's purchases are split by segment natively."""
        v = 0.0
        for seg, g in gs.items():
            if seg == "new":
                v += (g["pval"] * gm2(m) + g["purch"] * repeat_gp2(m)) * INC["new"]
            else:
                v += g["pval"] * gm2(m) * INC["existing"]
        return v

    def band(x):
        if x is None:
            return None
        return "scale" if x >= T["scale"] else "hold" if x >= T["cut"] else "cut"

    def step_down(v):
        return {"scale": "hold", "hold": "cut", "cut": "cut"}.get(v, v)

    rows = []

    def make_row(level, mkt, seg, l3, label, gsrc):
        """gsrc(wk) → {seg: metrics} for the row's scope."""
        segs = list(gsrc("l7").keys()) or ([seg] if seg else ["new", "existing"])
        win = {}
        for wk in W:
            gs = gsrc(wk)
            tot = _blank()
            for g in gs.values():
                for f in tot:
                    tot[f] += g[f]
            low = value(tot, mkt if mkt != "all" else "all", segs, "norce")
            high = meta_value(gs, mkt if mkt != "all" else "all")
            sp = tot["spend"]
            win[wk] = {
                "spend": round(sp), "impr": round(tot["impr"]),
                "spend_new": round(sum(g["spend"] for s, g in gs.items() if s == "new")),
                "spend_existing": round(sum(g["spend"] for s, g in gs.items() if s == "existing")),
                "meta_purch": round(tot["purch"], 1), "meta_value": round(tot["pval"]),
                "meta_purch_new": round(sum(g["purch"] for s, g in gs.items() if s == "new"), 1),
                "n_new": round(tot["n_new"], 1), "n_ret": round(tot["n_ret"], 1),
                "rev_new": round(tot["rev_new"]), "rev_ret": round(tot["rev_ret"]),
                "cpm": round(sp / tot["impr"] * 1000, 1) if tot["impr"] else None,
                "cpa_meta": round(sp / tot["purch"], 1) if tot["purch"] else None,
                "value_low": round(low), "value_high": round(high),
                "ret_low": round(low / sp, 2) if sp else None, "ret_high": round(high / sp, 2) if sp else None,
            }
        l7, p7, mtd = win["l7"], win["p7"], win["mtd"]
        dm = demand.get(mkt, 1.0)

        def n_rel(w):                                   # the orders that count for this row
            return (w["n_new"] if seg == "new" else w["n_ret"] if seg == "existing"
                    else w["n_new"] + w["n_ret"])

        def enough(w):
            return w["spend"] >= T["min_spend"] and n_rel(w) >= T["min_orders"]

        # Basis: last 7 days; when they are too thin, the month to date (low confidence).
        basis = "l7" if enough(l7) else "mtd" if enough(mtd) else None
        b = win[basis] if basis else l7
        # A slope needs volume in both weeks, a real spend move, and a market or tactic
        # row (ad-set groups are too small: a handful of orders swings it to 0 or 10).
        moved = (basis == "l7" and level != "detail" and enough(p7)
                 and abs(l7["spend"] - p7["spend"]) >= T["spend_move"] * p7["spend"])
        marg = {}
        for end in ("low", "high"):
            avg = b[f"ret_{end}"]
            if avg is None:
                marg[end] = None
            elif moved:
                m_ = (l7[f"value_{end}"] - p7[f"value_{end}"] * dm) / (l7["spend"] - p7["spend"])
                marg[end] = round(max(0.0, min(avg, m_)), 2)
            else:
                marg[end] = round(avg * MARGINAL_HAIRCUT, 2)
        if moved:
            method = (f"slope: spend {'+' if l7['spend'] > p7['spend'] else '−'}"
                      f"{abs(l7['spend'] - p7['spend']) / p7['spend']:.0%} vs the 7 days before, "
                      f"their value scaled ×{dm:.2f} for the market's demand")
        else:
            method = f"{'month-to-date' if basis == 'mtd' else '7-day'} average × {MARGINAL_HAIRCUT}"

        # frequency
        if level == "market":
            f7 = {"all": fq("l7", "mkt", mkt), "new": fq("l7", "mseg", mkt, "new"),
                  "existing": fq("l7", "mseg", mkt, "existing")}
            fp = {"all": fq("p7", "mkt", mkt)}
            fm = {"all": fq("mtd", "mkt", mkt)}
        elif level == "tactic":
            f7 = {seg: fq("l7", "mseg", mkt, seg)}
            fp = {seg: fq("p7", "mseg", mkt, seg)}
            fm = {seg: fq("mtd", "mseg", mkt, seg)}
        else:
            ids = [a["id"] for a in adsets.values()
                   if (a["market"], a["fmt"], a["offer"], a["line"]) == (mkt, l3[0], l3[1], l3[2])]

            def wf(wk):                                 # impression-weighted across the group's ad sets
                num = den = 0.0
                for i in ids:
                    imp, rch = freq.get((wk, "adset", i, seg), (0.0, 0.0))
                    if rch:
                        num += imp; den += rch
                return round(num / den, 2) if den else None
            f7, fp, fm = {seg: wf("l7")}, {seg: wf("p7")}, {seg: wf("mtd")}

        # break-even cost per customer (Norce basis)
        m2 = mkt if mkt != "all" else "all"
        aov_new = (b["rev_new"] / b["n_new"]) if b["n_new"] else None
        aov_ret = (b["rev_ret"] / b["n_ret"]) if b["n_ret"] else None
        be = {
            "new": round((aov_new * gm2(m2) + repeat_gp2(m2)) * INC["new"]) if aov_new else None,
            "existing": round(aov_ret * gm2(m2) * INC["existing"]) if aov_ret else None,
            "cost_new": round(b["spend_new"] / b["n_new"]) if b["n_new"] and b["spend_new"] else None,
            "cost_existing": round(b["spend_existing"] / b["n_ret"]) if b["n_ret"] and b["spend_existing"] else None,
        }

        # ── verdict ─────────────────────────────────────────────────────────
        reasons = []
        hr = (headroom.get(mkt) or {}).get("headroom_per_day")
        fn, fe = f7.get("new"), f7.get("existing")
        shift = False
        gated = False                                    # a measured gate (frequency, costs, headroom) set it
        if basis is None:
            verdict, conf = "data", None
            reasons.append(f"Too little data: {_n(l7['spend'])} SEK and {n_rel(l7):.0f} Norce orders in 7 days, "
                           f"{n_rel(mtd):.0f} this month (needs {_n(T['min_spend'])} SEK and {T['min_orders']} orders)")
        else:
            lo_, hi_ = marg["low"], marg["high"]
            vl, vh = band(lo_), band(hi_)
            if vl == vh and basis == "l7":
                verdict, conf = vl, "high"
            else:
                verdict, conf = band((lo_ + hi_) / 2), "low"
            reasons.append(f"Incremental GP3 per krona {lo_:.2f} on Norce clicks, {hi_:.2f} on Meta's count"
                           + (" (month to date: too few orders in 7 days)" if basis == "mtd" else ""))
            # frequency gates
            if fe is not None and fe > T["freq_existing"]:
                if level == "market":                   # a market is not cut for this: its existing slice is
                    shift = True
                    reasons.insert(0, f"Existing-customer frequency {fe:.1f} in 7 days (> {T['freq_existing']:.0f}): "
                                      "move money from existing to new customers")
                else:
                    verdict, conf, gated = "cut", "high", True
                    reasons.insert(0, f"Existing-customer frequency {fe:.1f} in 7 days (> {T['freq_existing']:.0f})")
            if level == "detail":                       # the segment as a whole can be saturated
                pf = fq("l7", "mseg", mkt, seg)
                lim = T["freq_new"] if seg == "new" else T["freq_existing"]
                if verdict == "scale" and pf is not None and pf > lim:
                    verdict, gated = "hold", True
                    reasons.insert(0, f"{SEG_LABEL[seg].split(' (')[0]} in {mkt} see Meta ads {pf:.1f} times in 7 days "
                                      f"(> {lim:.0f}) across all ad sets: test new ads here rather than more budget")
            if verdict == "scale" and fn is not None and fn > T["freq_new"]:
                verdict, gated = "hold", True
                reasons.insert(0, f"New-customer frequency {fn:.1f} in 7 days (> {T['freq_new']:.0f}): "
                                  "more money mostly reaches the same people")
            # cost-rise gate
            if (basis == "l7" and p7["cpm"] and p7["cpa_meta"] and l7["cpm"] and l7["cpa_meta"]
                    and l7["spend"] > p7["spend"]
                    and l7["cpm"] / p7["cpm"] - 1 > T["cost_rise"] and l7["cpa_meta"] / p7["cpa_meta"] - 1 > T["cost_rise"]):
                verdict, gated = step_down(verdict), True
                reasons.insert(0, f"Saturation: spend up, CPM +{l7['cpm'] / p7['cpm'] - 1:.0%} and "
                                  f"cost per Meta purchase +{l7['cpa_meta'] / p7['cpa_meta'] - 1:.0%}")
            # headroom gate
            if verdict == "scale" and hr is not None and hr < 0:
                verdict, gated = "hold", True
                reasons.insert(0, f"{mkt} is over its allowed spend ({_n(hr)} SEK/day headroom)")
        mid = ((marg["low"] or 0) + (marg["high"] or 0)) / 2 if marg["low"] is not None else None
        # Hold on the numbers alone, yet above break-even with market headroom to spend
        candidate = (verdict == "hold" and not gated and not shift and mid is not None
                     and mid > 1.0 and (hr or 0) > 0)

        if shift and verdict == "scale":
            action = (f"Raise up to +{T['step_up']:.0%} on new customers only; lower the existing-customer cap")
        elif shift and verdict == "hold":
            action = "Keep the total: lower the existing-customer cap and move that money to new customers"
        elif shift and verdict == "cut":
            action = f"Cut −{T['step_down']:.0%}, starting with existing customers (lower the existing-customer cap)"
        elif verdict == "scale":
            action = f"Raise budget up to +{T['step_up']:.0%}, then wait 3 days"
        elif verdict == "cut":
            action = (f"Lower the existing-customer budget cap first, then −{T['step_down']:.0%} budget"
                      if seg == "existing" else f"Cut budget −{T['step_down']:.0%}")
        elif verdict == "hold":
            action = "No budget change: work on the ads (Creatives section)"
        else:
            action = "Wait for more data"

        rows.append({
            "level": level, "market": mkt, "seg": seg, "fmt": l3[0] if l3 else None,
            "offer": l3[1] if l3 else None, "line": l3[2] if l3 else None, "label": label,
            "adsets": sorted({a["name"] for a in adsets.values()
                              if l3 and (a["market"], a["fmt"], a["offer"], a["line"]) == (mkt, l3[0], l3[1], l3[2])}),
            "campaigns": sorted({a["campaign"] for a in adsets.values()
                                 if l3 and (a["market"], a["fmt"], a["offer"], a["line"]) == (mkt, l3[0], l3[1], l3[2])}),
            "w": win, "marginal": marg, "method": method, "freq": {"l7": f7, "p7": fp, "mtd": fm},
            "be": be, "verdict": verdict, "confidence": conf, "reasons": reasons, "basis": basis, "shift": shift,
            "action": action, "candidate": candidate, "headroom_per_day": hr,
        })

    for mkt in MKTS + ["all"]:
        def g_m(wk, mkt=mkt):
            return {s: agg.get((wk, "t", mkt, s), _blank()) for s in ("new", "existing")}
        mt = agg.get(("mtd", "m", mkt))
        if not mt or not mt["spend"]:
            continue                                   # no Meta spend in this market this month
        make_row("market", mkt, None, None, mkt if mkt != "all" else "All markets", g_m)
        if mkt == "all":
            continue
        for seg in ("new", "existing"):
            make_row("tactic", mkt, seg, None, SEG_LABEL[seg],
                     lambda wk, mkt=mkt, seg=seg: {seg: agg.get((wk, "t", mkt, seg), _blank())})
            l3s = sorted({k[3:] for k in agg if k[1] == "x" and k[2] == mkt and k[3] == seg},
                         key=lambda k: -agg.get(("mtd", "x") + (mkt,) + k, _blank())["spend"])
            for k in l3s:
                _, fmt, off, line = k
                if agg.get(("l7", "x", mkt) + k, _blank())["spend"] < 1:
                    continue                                  # paused: no spend in the last 7 days
                make_row("detail", mkt, seg, (fmt, off, line),
                         f"{FMT_LABEL[fmt]} · {OFFER_LABEL[off]} · {LINE_LABEL[line]}",
                         lambda wk, mkt=mkt, seg=seg, k=k: {seg: agg.get((wk, "x", mkt) + k, _blank())})

    # ── Daily series (MTD) per market × segment ─────────────────────────────
    days = []
    d = W["mtd"][0]
    while d <= y:
        days.append(d.isoformat()); d += dt.timedelta(days=1)
    ix = {d: i for i, d in enumerate(days)}
    series = defaultdict(lambda: {"spend": [0.0] * len(days), "orders": [0.0] * len(days)})
    for d, aid, seg, sp, *_ in mrows:
        if d in ix:
            for m in (adsets[aid]["market"], "all"):
                series[f"{m}|{seg}"]["spend"][ix[d]] += sp
    for d, mkt, camp, nw, n, rev in norce:
        if d in ix:
            seg = "new" if nw else "existing"
            for m in (mkt, "all"):
                series[f"{m}|{seg}"]["orders"][ix[d]] += n
    series = {k: {f: [round(x, 1) for x in v] for f, v in s.items()} for k, s in series.items()}

    # ── Idle accounts (Lekmer / Kuling) ─────────────────────────────────────
    idle = {}
    for acct, name in IDLE_ACCOUNTS.items():
        try:
            idle[name] = round(_spend_only(token, acct, W["mtd"][0], y))
        except Exception as e:                           # noqa: BLE001
            errors.append(f"{name} Meta account: {e}")
    if unmapped:
        top = sorted(unmapped.items(), key=lambda kv: -kv[1])[:5]
        notes.append("Norce orders whose utm_campaign matches no live Meta campaign (kept at market and "
                     "tactic level, classified by name at detail level): "
                     + ", ".join(f"{k} ({v:.0f})" for k, v in top) + ".")

    notes += [
        "Tactic = Meta's own audience segment: 'existing customers' is the customer audience set in the "
        "Meta account's audience settings; everyone else is 'prospecting' (new). Every ad set targets "
        "broadly, so campaign names cannot separate retargeting from prospecting.",
        "Norce orders = orders whose GA4 session came from a paid Meta click (utm source facebook/instagram/"
        "fb/ig, medium cpc/paid), matched to Norce; new vs returning by the warehouse flag (no order in that "
        "market in the previous 365 days). Recent days are grossed up for orders not yet matched to GA4.",
        "Norce knows the campaign, not the ad set: at detail level a campaign's orders are shared over its "
        "ad-set groups by Meta's purchases in the same segment.",
        "Meta purchases are Meta's 7-day click / 1-day view count: it includes view-throughs and other "
        "devices, so it is the optimistic end of each range; Norce clicks are the conservative end.",
        f"Value: GP2 rate per market from the pacing section (Norce − BC returns − Norce COGS − shipping, "
        f"pick & pack); 12-month repeat GP2 per new customer from customers acquired Nov 2024–Oct 2025 "
        f"(repeat revenue capped at {REPEAT_CAP:,} SEK per customer); incrementality assumptions: new "
        f"{INC['new']}, existing {INC['existing']}.".replace(",", " "),
        "Frequency is Meta's own period frequency (impressions ÷ unique reach) for the 7-day window and the "
        "month, by market and segment; at detail level it is impression-weighted over the group's ad sets.",
    ]

    return {
        "asof": y.isoformat(), "today": today.isoformat(),
        "windows": {k: [a.isoformat(), b.isoformat()] for k, (a, b) in W.items()},
        "thresholds": T, "incrementality": INC, "marginal_haircut": MARGINAL_HAIRCUT,
        "repeat": rep, "gm2": {m: round(gm2(m), 4) for m in MKTS + ["all"]},
        "demand": {m: round(v, 3) for m, v in demand.items()},
        "rows": rows, "days": days, "series": series, "idle_accounts": idle,
        "notes": notes, "errors": errors,
    }
