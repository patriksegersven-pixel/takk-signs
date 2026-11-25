"""bm_meta_geo.py — Meta market from the ad set's target country.

Campaign names are not a safe market key: one campaign can hold ad sets for
several countries (since 9 Nov 2026 'KIR | SE+NO+FI+DK+DE+UK-BS-offers' holds
SE, NO, FI, DK, UK, DE and EE ad sets), and the warehouse's name rule books
such a campaign to SE. The ad set's own geo targeting is the truth, so every
Black Month section maps Meta rows through here first and only falls back to
names when the ad set has no target country (not yet synced, or no country
targeting).

Source: claude-private-499703.babyshop_staging.stg_meta__ad_sets, which keeps
the first country of targeting.geo_locations.countries (multi-country ad sets
are rare: none in Nov 2026).

Meta markets are split, not bundled: SE/NO/FI/DK plus each country in
EXTRA_MARKETS (Meta campaigns launched for Black Friday 2026 on 23 Nov) get
their own market. Any other country still maps to ROW. Orders, margins and
Google keep a single ROW bucket, so lookups into those use base_market().
To split out another country, add its code to EXTRA_MARKETS (and to
EXTRA_MARKETS in bm-paid.js / bm-meta.js for the page order).
"""
from __future__ import annotations

import threading

import bm_common as bm

STG = "`claude-private-499703.babyshop_staging.{}`"

# Meta-only markets beyond the Nordics, in display order. Ad set targeting
# uses ISO codes (GB); the ad set names and the team say UK.
EXTRA_MARKETS = ["UK", "DE", "EE"]
COUNTRY_ALIAS = {"GB": "UK"}
META_MARKETS = bm.MARKETS + EXTRA_MARKETS + ["ROW"]
# Which order-side market (warehouse fct_order_ga4_match.market) each extra
# Meta market sells through: the UK has its own site (GB); Germany and Estonia
# both buy on the EU site, which carries no country, so EU orders are shared
# between them by Meta purchases.
ORDER_SITE = {"UK": "GB", "DE": "EU", "EE": "EU"}


def order_market(site_market: str | None) -> str:
    """Order-side market → Meta market: 'GB' → 'UK', 'EU' stays 'EU' (to be
    split by the caller), SE/NO/FI/DK as they are, everything else 'ROW'."""
    m = (site_market or "").upper()
    if m in bm.MARKETS:
        return m
    for meta_m, site in ORDER_SITE.items():
        if site == m and list(ORDER_SITE.values()).count(site) == 1:
            return meta_m
    return "EU" if m == "EU" else "ROW"

_lock = threading.Lock()
_cache: dict | None = None


def _load() -> dict:
    """{'adset': {adset_id: country}, 'ad': {ad_id: country}} — one warehouse
    query per process; a few thousand rows."""
    global _cache
    with _lock:
        if _cache is None:
            import refresh_meta as rm
            try:
                rows = rm.q(f"""
                    SELECT DISTINCT CAST(i.ad_id AS STRING) ad_id, s.adset_id, s.target_country c
                    FROM {STG.format('stg_meta__ad_sets')} s
                    LEFT JOIN (SELECT DISTINCT ad_id, adset_id FROM {STG.format('stg_meta__ads_insights')}) i
                      ON i.adset_id = s.adset_id
                    WHERE s.target_country IS NOT NULL AND s.target_country != ''""")
            except Exception as e:  # noqa: BLE001 — names are a usable fallback
                print(f"[bm_meta_geo] ad set targeting unavailable, falling back to names: {e}"[:300])
                rows = []
            adset, ad = {}, {}
            for r in rows:
                adset[str(r["adset_id"])] = r["c"]
                if r["ad_id"]:
                    ad[str(r["ad_id"])] = r["c"]
            _cache = {"adset": adset, "ad": ad}
    return _cache


def country_to_market(country: str | None) -> str | None:
    """'SE' → 'SE', 'GB' → 'UK', 'NL' → 'ROW'; None when there is no country."""
    if not country:
        return None
    c = COUNTRY_ALIAS.get(country.upper(), country.upper())
    return c if c in bm.MARKETS or c in EXTRA_MARKETS else "ROW"


def base_market(m: str) -> str:
    """The market as order, margin and Google data know it: UK/DE/EE → ROW."""
    return m if m in bm.MARKETS else "ROW"


def adset_country(adset_id) -> str | None:
    return _load()["adset"].get(str(adset_id)) if adset_id is not None else None


def ad_country(ad_id) -> str | None:
    return _load()["ad"].get(str(ad_id)) if ad_id is not None else None


def adset_market(adset_id) -> str | None:
    """Market from the ad set's targeting, or None (caller falls back to names)."""
    return country_to_market(adset_country(adset_id))


def ad_market(ad_id) -> str | None:
    return country_to_market(ad_country(ad_id))
