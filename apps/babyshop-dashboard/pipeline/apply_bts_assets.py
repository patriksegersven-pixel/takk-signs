#!/usr/bin/env python3
"""
Back to School & Preschool 2026 — one-shot Google Ads asset setup.

Creates the seasonal asset package documented in BTS-2026-ASSETS.md:

  - 6 sitelink assets   -> linked at ACCOUNT level and on every enabled Search
                           campaign whose name contains "brand" (sitelinks do
                           not merge across levels; campaign-level wins, so the
                           brand campaigns need their own copies)
  - 4 callout assets    -> linked at ACCOUNT level
  - 1 structured snippet-> linked at ACCOUNT level (evergreen; the API has no
                           scheduling on snippets, values are non-seasonal)
  - 1 extra RSA per brand Search ad group, tagged ad.name = "bts-2026"
                           (never edits existing RSAs — that resets their
                           performance history)
  - optionally, a promotion asset (occasion BACK_TO_SCHOOL) — only when
    PROMOTION["percent_off"] is set, because promotion assets REQUIRE a
    real discount. Left off by default.

Sitelinks and callouts carry end_date 2026-09-15 and stop serving by
themselves. The RSAs do not expire — run --cleanup after the end date.

Modes
  (default)   dry run: reads the account, prints every mutation it WOULD make
  --apply     performs the mutations (refuses while URLS_VERIFIED is False)
  --cleanup   pauses every enabled ad named "bts-2026" in the account

Credentials: the same five env vars as refresh_roas_sims.py
(GOOGLE_ADS_DEVELOPER_TOKEN, _CLIENT_ID, _CLIENT_SECRET, _REFRESH_TOKEN,
_LOGIN_CUSTOMER_ID). Target account defaults to Babyshop SE; override with
GOOGLE_ADS_TARGET_CID.

Re-running --apply is safe: assets are reused when an identical one already
exists (matched on content), existing account/campaign links are skipped, and
ad groups that already contain a bts-2026 ad are skipped.
"""

from __future__ import annotations

import os
import re
import sys

API_VERSION = "v25"  # keep in lockstep with refresh_roas_sims.py / requirements.txt

# ════════════════════════════════════════════════════════════════════════════
#  Campaign config — edit copy/URLs here. All sv-SE, Babyshop SE.
# ════════════════════════════════════════════════════════════════════════════

TARGET_CID = re.sub(r"\D", "", os.environ.get("GOOGLE_ADS_TARGET_CID", "4851485396"))

END_DATE = "2026-09-15"          # last serving day for sitelinks/callouts
AD_NAME = "bts-2026"             # tag on the seasonal RSAs; --cleanup pauses by it
BRAND_NAME_MATCH = "brand"       # case-insensitive substring selecting brand campaigns
LANDING = "https://www.babyshop.com/sv-se/shop-by/back-to-school"

# The sandbox this was authored in cannot reach babyshop.com, so every URL
# marked VERIFY below is an educated guess. Check each one in a browser (each
# sitelink must have a DISTINCT, working landing page or Google disapproves
# it), fix them, then flip this to True. --apply refuses to run until then.
URLS_VERIFIED = False

SITELINKS = [
    {"text": "Back to School",      "d1": "Allt för skola & förskolestart",
     "d2": "Ryggsäckar, kläder, skor & mer",  "url": LANDING},
    {"text": "Ryggsäckar & väskor", "d1": "Skolryggsäckar för alla åldrar",
     "d2": "Första skolväskan – handla nu",   "url": "https://www.babyshop.com/sv-se/vaskor"},          # VERIFY
    {"text": "Vardagskläder",       "d1": "Basplagg för skola & förskola",
     "d2": "Leggings, tröjor & multipack",    "url": "https://www.babyshop.com/sv-se/klader"},          # VERIFY
    {"text": "Ytterkläder",         "d1": "Regnkläder, fleece & skaljackor",
     "d2": "Redo för höst och regn",          "url": "https://www.babyshop.com/sv-se/ytterklader"},     # VERIFY
    {"text": "Skor",                "d1": "Sneakers, stövlar & innerskor",
     "d2": "Kavat, Viking, Reima & fler",     "url": "https://www.babyshop.com/sv-se/skor"},            # VERIFY
    {"text": "Matlådor & flaskor",  "d1": "Matlådor & vattenflaskor",
     "d2": "Redo för lunch och mellis",       "url": "https://www.babyshop.com/sv-se/FILL-ME-IN"},      # VERIFY
]

CALLOUTS = [
    "Allt för skolstarten",
    "För skola & förskola",
    "Ryggsäckar & matlådor",
    "Regnkläder inför hösten",
]

SNIPPET = {
    # Header must be one of Google's localized snippet headers; "Typer" is the
    # sv-SE form of "Types".
    "header": "Typer",
    "values": ["Ryggsäckar", "Regnkläder", "Sneakers", "Matlådor", "Basplagg", "Skaljackor"],
}

RSA = {
    "final_url": LANDING,
    "path1": "skolstart",
    "headlines": [
        "Back to School hos Babyshop",
        "Allt inför skolstarten",
        "Skolstart & förskolestart",
        "Ryggsäckar till skolstarten",
        "Regnkläder & ytterkläder",
        "Skor för skola & fritid",
        "Basplagg & multipack",
        "Kläder för förskolestarten",
        "Skolstartsshop t.o.m. 15/9",
        # Countdown option — urgency copy without a discount reads oddly, and a
        # malformed customizer fails the whole create call, so off by default:
        # 'Slutar om {=COUNTDOWN("2026-09-15 23:59:59","sv")}',
    ],
    "descriptions": [
        "Allt för skola, förskola och nya rutiner – ryggsäckar, kläder, skor och matlådor.",
        "Hitta ryggsäckar, regnkläder och basplagg för skolstarten. Handla tryggt hos Babyshop.",
        "Back to School-utbudet finns t.o.m. 15 september. För barn i alla åldrar.",
        "Mjuka basplagg, regnkläder och skor för skola, förskola och fritid – samlat på ett ställe",
    ],
}

# Promotion assets REQUIRE a discount. Fill in percent_off (whole percent,
# e.g. 30 for "upp till 30 %") to enable; None skips the promotion entirely.
PROMOTION = {
    "percent_off": None,             # e.g. 30
    "target": "skolstartssortimentet",
    "language": "sv",
    "url": LANDING,
}

# ════════════════════════════════════════════════════════════════════════════
#  Copy-length validation — Google Ads hard limits, checked before any API call
# ════════════════════════════════════════════════════════════════════════════

def validate_copy() -> list[str]:
    errs = []

    def check(kind, text, limit):
        if len(text) > limit:
            errs.append(f"{kind} over {limit} chars ({len(text)}): {text!r}")

    for s in SITELINKS:
        check("sitelink link text", s["text"], 25)
        check("sitelink description", s["d1"], 35)
        check("sitelink description", s["d2"], 35)
    urls = [s["url"] for s in SITELINKS]
    if len(set(urls)) != len(urls):
        errs.append("sitelink final URLs are not all distinct")
    for c in CALLOUTS:
        check("callout", c, 25)
    for v in SNIPPET["values"]:
        check("snippet value", v, 25)
    for h in RSA["headlines"]:
        if "{=" not in h:                      # customizers render shorter than raw text
            check("RSA headline", h, 30)
    for d in RSA["descriptions"]:
        check("RSA description", d, 90)
    check("RSA path1", RSA["path1"], 15)
    if not (3 <= len(RSA["headlines"]) <= 15):
        errs.append("RSA needs 3-15 headlines")
    if not (2 <= len(RSA["descriptions"]) <= 4):
        errs.append("RSA needs 2-4 descriptions")
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", END_DATE):
        errs.append(f"END_DATE not YYYY-MM-DD: {END_DATE!r}")
    return errs


# ════════════════════════════════════════════════════════════════════════════
#  Client plumbing — mirrors refresh_roas_sims.py
# ════════════════════════════════════════════════════════════════════════════

ADS_ENV_KEYS = [
    "GOOGLE_ADS_DEVELOPER_TOKEN", "GOOGLE_ADS_CLIENT_ID", "GOOGLE_ADS_CLIENT_SECRET",
    "GOOGLE_ADS_REFRESH_TOKEN", "GOOGLE_ADS_LOGIN_CUSTOMER_ID",
]


def ads_client():
    missing = [k for k in ADS_ENV_KEYS if not (os.environ.get(k) or "").strip()]
    if missing:
        sys.exit("Google Ads credentials not configured — missing " + ", ".join(missing))
    from google.ads.googleads.client import GoogleAdsClient

    return GoogleAdsClient.load_from_dict({
        "developer_token": os.environ["GOOGLE_ADS_DEVELOPER_TOKEN"].strip(),
        "client_id": os.environ["GOOGLE_ADS_CLIENT_ID"].strip(),
        "client_secret": os.environ["GOOGLE_ADS_CLIENT_SECRET"].strip(),
        "refresh_token": os.environ["GOOGLE_ADS_REFRESH_TOKEN"].strip(),
        "login_customer_id": re.sub(r"\D", "", os.environ["GOOGLE_ADS_LOGIN_CUSTOMER_ID"]),
        "use_proto_plus": True,
    }, version=API_VERSION)


def search(client, query: str):
    svc = client.get_service("GoogleAdsService")
    for batch in svc.search_stream(customer_id=TARGET_CID, query=query):
        yield from batch.results


# ════════════════════════════════════════════════════════════════════════════
#  Reads — current account state, for idempotency and targeting
# ════════════════════════════════════════════════════════════════════════════

def existing_assets(client) -> dict:
    """Content-keyed maps of assets already in the account, so reruns reuse them."""
    out = {"sitelink": {}, "callout": {}, "snippet": {}}
    for row in search(client,
                      "SELECT asset.resource_name, asset.type, asset.final_urls, "
                      "asset.sitelink_asset.link_text, asset.callout_asset.callout_text, "
                      "asset.structured_snippet_asset.header, "
                      "asset.structured_snippet_asset.values "
                      "FROM asset WHERE asset.type IN ('SITELINK','CALLOUT','STRUCTURED_SNIPPET')"):
        a = row.asset
        t = a.type_.name
        if t == "SITELINK":
            key = (a.sitelink_asset.link_text, tuple(a.final_urls))
            out["sitelink"][key] = a.resource_name
        elif t == "CALLOUT":
            out["callout"][a.callout_asset.callout_text] = a.resource_name
        else:
            key = (a.structured_snippet_asset.header,
                   tuple(a.structured_snippet_asset.values))
            out["snippet"][key] = a.resource_name
    return out


def existing_links(client) -> tuple[set, set]:
    """(customer-level, campaign-level) links that already exist."""
    cust = set()
    for row in search(client,
                      "SELECT customer_asset.asset, customer_asset.field_type FROM customer_asset"):
        cust.add((row.customer_asset.asset, row.customer_asset.field_type.name))
    camp = set()
    for row in search(client,
                      "SELECT campaign_asset.campaign, campaign_asset.asset, "
                      "campaign_asset.field_type FROM campaign_asset"):
        ca = row.campaign_asset
        camp.add((ca.campaign, ca.asset, ca.field_type.name))
    return cust, camp


def brand_search_campaigns(client) -> list[dict]:
    out = []
    for row in search(client,
                      "SELECT campaign.id, campaign.name, campaign.resource_name, "
                      "campaign.advertising_channel_type "
                      "FROM campaign WHERE campaign.status = 'ENABLED'"):
        c = row.campaign
        if c.advertising_channel_type.name != "SEARCH":
            continue
        if BRAND_NAME_MATCH not in c.name.lower():
            continue
        out.append({"id": str(c.id), "name": c.name, "rn": c.resource_name})
    return out


def brand_ad_groups(client, campaign_ids: list[str]) -> list[dict]:
    if not campaign_ids:
        return []
    ids = ", ".join(campaign_ids)
    groups = {}
    for row in search(client,
                      "SELECT ad_group.id, ad_group.name, ad_group.resource_name, "
                      "campaign.id, campaign.name "
                      f"FROM ad_group WHERE campaign.id IN ({ids}) "
                      "AND ad_group.status = 'ENABLED' AND ad_group.type = 'SEARCH_STANDARD'"):
        groups[str(row.ad_group.id)] = {
            "id": str(row.ad_group.id), "name": row.ad_group.name,
            "rn": row.ad_group.resource_name, "campaign": row.campaign.name,
            "rsa_count": 0, "has_bts": False,
        }
    if not groups:
        return []
    for row in search(client,
                      "SELECT ad_group.id, ad_group_ad.ad.name, ad_group_ad.ad.type, "
                      "ad_group_ad.status "
                      f"FROM ad_group_ad WHERE campaign.id IN ({ids}) "
                      "AND ad_group_ad.status != 'REMOVED'"):
        g = groups.get(str(row.ad_group.id))
        if g is None:
            continue
        if row.ad_group_ad.ad.type_.name == "RESPONSIVE_SEARCH_AD":
            g["rsa_count"] += 1
        if row.ad_group_ad.ad.name == AD_NAME:
            g["has_bts"] = True
    return list(groups.values())


# ════════════════════════════════════════════════════════════════════════════
#  Mutations
# ════════════════════════════════════════════════════════════════════════════

def build_asset_ops(client, existing: dict, log: list[str]):
    """Return (operations, planned) where planned maps a plan-key -> asset
    resource name for assets that already exist, or the op index for new ones."""
    ops, planned = [], {}

    def op_for(build):
        op = client.get_type("AssetOperation")
        build(op.create)
        ops.append(op)
        return len(ops) - 1

    for s in SITELINKS:
        key = ("sitelink", s["text"])
        found = existing["sitelink"].get((s["text"], (s["url"],)))
        if found:
            planned[key] = found
            log.append(f"reuse sitelink asset {s['text']!r} ({found})")
            continue

        def build(a, s=s):
            a.final_urls.append(s["url"])
            a.sitelink_asset.link_text = s["text"]
            a.sitelink_asset.description1 = s["d1"]
            a.sitelink_asset.description2 = s["d2"]
            a.sitelink_asset.end_date = END_DATE
        planned[key] = op_for(build)
        log.append(f"create sitelink asset {s['text']!r} -> {s['url']} (ends {END_DATE})")

    for c in CALLOUTS:
        key = ("callout", c)
        found = existing["callout"].get(c)
        if found:
            planned[key] = found
            log.append(f"reuse callout asset {c!r} ({found})")
            continue

        def build(a, c=c):
            a.callout_asset.callout_text = c
            a.callout_asset.end_date = END_DATE
        planned[key] = op_for(build)
        log.append(f"create callout asset {c!r} (ends {END_DATE})")

    key = ("snippet", SNIPPET["header"])
    found = existing["snippet"].get((SNIPPET["header"], tuple(SNIPPET["values"])))
    if found:
        planned[key] = found
        log.append(f"reuse structured snippet ({found})")
    else:
        def build(a):
            a.structured_snippet_asset.header = SNIPPET["header"]
            a.structured_snippet_asset.values.extend(SNIPPET["values"])
        planned[key] = op_for(build)
        log.append(f"create structured snippet {SNIPPET['header']}: {', '.join(SNIPPET['values'])}")

    if PROMOTION["percent_off"]:
        def build(a):
            a.final_urls.append(PROMOTION["url"])
            p = a.promotion_asset
            p.promotion_target = PROMOTION["target"]
            # percent_off is in micro-percent: 1_000_000 == 1%.
            p.percent_off = int(PROMOTION["percent_off"]) * 1_000_000
            p.occasion = client.enums.PromotionExtensionOccasionEnum.BACK_TO_SCHOOL
            p.language_code = PROMOTION["language"]
            p.redemption_end_date = END_DATE
            p.end_date = END_DATE
        planned[("promotion", "bts")] = op_for(build)
        log.append(f"create promotion asset: {PROMOTION['percent_off']}% off "
                   f"{PROMOTION['target']!r} (occasion BACK_TO_SCHOOL, ends {END_DATE})")

    return ops, planned


def resolve(planned: dict, created: list[str]) -> dict:
    """Map plan-keys to final asset resource names after the mutate ran."""
    return {k: (v if isinstance(v, str) else created[v]) for k, v in planned.items()}


FIELD_TYPE_BY_KIND = {"sitelink": "SITELINK", "callout": "CALLOUT",
                      "snippet": "STRUCTURED_SNIPPET", "promotion": "PROMOTION"}


def apply_all(client, dry: bool):
    log: list[str] = []
    existing = existing_assets(client)
    cust_links, camp_links = existing_links(client)
    campaigns = brand_search_campaigns(client)
    groups = brand_ad_groups(client, [c["id"] for c in campaigns])

    print(f"Account {TARGET_CID}: {len(campaigns)} enabled Search brand campaign(s): "
          + (", ".join(c["name"] for c in campaigns) or "NONE"))
    if not campaigns:
        print("WARNING: no campaign name contains "
              f"{BRAND_NAME_MATCH!r} — campaign-level sitelinks and RSAs will be skipped.")

    # 1. assets
    asset_ops, planned = build_asset_ops(client, existing, log)
    created: list[str] = []
    if asset_ops and not dry:
        res = client.get_service("AssetService").mutate_assets(
            customer_id=TARGET_CID, operations=asset_ops)
        created = [r.resource_name for r in res.results]
    elif asset_ops:
        created = [f"<new asset #{i}>" for i in range(len(asset_ops))]
    assets = resolve(planned, created)

    # 2. account-level links (everything)
    cust_ops = []
    for (kind, _), rn in assets.items():
        ft = FIELD_TYPE_BY_KIND[kind]
        if (rn, ft) in cust_links:
            log.append(f"skip customer link (exists): {ft} {rn}")
            continue
        op = client.get_type("CustomerAssetOperation")
        op.create.asset = rn
        op.create.field_type = getattr(client.enums.AssetFieldTypeEnum, ft)
        cust_ops.append(op)
        log.append(f"link at ACCOUNT level: {ft} {rn}")
    if cust_ops and not dry:
        client.get_service("CustomerAssetService").mutate_customer_assets(
            customer_id=TARGET_CID, operations=cust_ops)

    # 3. campaign-level sitelinks on brand campaigns (campaign-level wins over
    #    account-level, and brand campaigns have their own sitelink sets)
    camp_ops = []
    for c in campaigns:
        for (kind, name), rn in assets.items():
            if kind != "sitelink":
                continue
            if (c["rn"], rn, "SITELINK") in camp_links:
                log.append(f"skip campaign link (exists): {c['name']} <- {name!r}")
                continue
            op = client.get_type("CampaignAssetOperation")
            op.create.campaign = c["rn"]
            op.create.asset = rn
            op.create.field_type = client.enums.AssetFieldTypeEnum.SITELINK
            camp_ops.append(op)
            log.append(f"link sitelink {name!r} on campaign {c['name']!r}")
    if camp_ops and not dry:
        client.get_service("CampaignAssetService").mutate_campaign_assets(
            customer_id=TARGET_CID, operations=camp_ops)

    # 4. one BTS RSA per brand ad group
    ad_ops = []
    for g in groups:
        if g["has_bts"]:
            log.append(f"skip ad group {g['name']!r} ({g['campaign']}): {AD_NAME} ad already there")
            continue
        if g["rsa_count"] >= 3:
            log.append(f"skip ad group {g['name']!r} ({g['campaign']}): already at 3-RSA limit")
            continue
        op = client.get_type("AdGroupAdOperation")
        aga = op.create
        aga.ad_group = g["rn"]
        aga.status = client.enums.AdGroupAdStatusEnum.ENABLED
        aga.ad.name = AD_NAME
        aga.ad.final_urls.append(RSA["final_url"])
        rsa = aga.ad.responsive_search_ad
        rsa.path1 = RSA["path1"]
        for h in RSA["headlines"]:
            t = client.get_type("AdTextAsset")
            t.text = h
            rsa.headlines.append(t)
        for d in RSA["descriptions"]:
            t = client.get_type("AdTextAsset")
            t.text = d
            rsa.descriptions.append(t)
        ad_ops.append(op)
        log.append(f"create {AD_NAME} RSA in ad group {g['name']!r} ({g['campaign']})")
    if ad_ops and not dry:
        client.get_service("AdGroupAdService").mutate_ad_group_ads(
            customer_id=TARGET_CID, operations=ad_ops)

    print()
    print("\n".join(log) or "nothing to do")
    print()
    if dry:
        print("DRY RUN — nothing was written. Re-run with --apply to execute.")
    else:
        print("Applied. Check Assets -> Associations and the new ads' policy "
              "status in the Google Ads UI; new ads enter policy review.")


def cleanup(client, dry: bool):
    ops, names = [], []
    for row in search(client,
                      "SELECT ad_group_ad.resource_name, ad_group_ad.ad.name, "
                      "ad_group_ad.status, ad_group.name, campaign.name "
                      "FROM ad_group_ad WHERE ad_group_ad.status = 'ENABLED'"):
        if row.ad_group_ad.ad.name != AD_NAME:
            continue
        op = client.get_type("AdGroupAdOperation")
        op.update.resource_name = row.ad_group_ad.resource_name
        op.update.status = client.enums.AdGroupAdStatusEnum.PAUSED
        op.update_mask.paths.append("status")
        ops.append(op)
        names.append(f"{row.campaign.name} / {row.ad_group.name}")
    if not ops:
        print(f"No enabled {AD_NAME!r} ads found — nothing to pause.")
        return
    print(f"Pausing {len(ops)} {AD_NAME!r} ad(s):\n  " + "\n  ".join(names))
    if dry:
        print("DRY RUN — re-run with --cleanup --apply to execute.")
    else:
        client.get_service("AdGroupAdService").mutate_ad_group_ads(
            customer_id=TARGET_CID, operations=ops)
        print("Paused. (Sitelinks/callouts expired on their own via end_date.)")


def main(argv: list[str]):
    dry = "--apply" not in argv
    errs = validate_copy()
    if errs:
        sys.exit("Copy validation failed:\n  " + "\n  ".join(errs))
    if not dry and "--cleanup" not in argv and not URLS_VERIFIED:
        sys.exit("URLS_VERIFIED is False — verify every sitelink URL marked "
                 "VERIFY in this file (the sandbox that authored it could not "
                 "reach babyshop.com), then flip the flag. See BTS-2026-ASSETS.md.")
    client = ads_client()
    if "--cleanup" in argv:
        cleanup(client, dry)
    else:
        apply_all(client, dry)


if __name__ == "__main__":
    main(sys.argv[1:])
