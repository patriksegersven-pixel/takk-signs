# Back to School & Preschool 2026 — Google Ads asset setup

Seasonal asset package for the Back to School / Preschool campaign
(`https://www.babyshop.com/sv-se/shop-by/back-to-school`), running until
**2026-09-15**. Applied by `apply_bts_assets.py` (same credential pattern as
`refresh_roas_sims.py` — the five `GOOGLE_ADS_*` env vars).

Angle (per campaign brief): **"Allt för skola, förskola och nya rutiner"** —
school AND preschool/daycare start, since the audience spans babies to ~10 years.
Hero categories: backpacks · everyday clothing · outerwear · shoes.
Supporting: lunch & hydration, rain gear, preschool start, basics/multipacks.

## What gets created (Babyshop SE, `485-148-5396`)

| Unit | Level | Auto-expires? |
|---|---|---|
| 6 sitelink assets | Account **+ each enabled Search brand campaign** | ✅ asset `end_date` 2026-09-15 |
| 4 callout assets | Account | ✅ asset `end_date` 2026-09-15 |
| 1 structured snippet ("Typer") | Account | ❌ evergreen by design — values are non-seasonal product types; the API has no scheduling on snippets |
| 1 BTS RSA per brand ad group | Brand Search ad groups | ❌ pause after 15.9 via `--cleanup` |
| Promotion asset | — | **Not created** — requires a real discount (% / amount / code). Hook exists in the script (`PROMOTION` config) if one materializes; occasion `BACK_TO_SCHOOL` fits the ≤15.9 window. Add a matching Merchant Center promotion too if products carry a discount — Search assets never touch Shopping (`p-shopping-se-brand`). |

**Why sitelinks go on the brand campaigns too:** sitelinks do not merge across
levels — the most specific level wins. Brand campaigns carry their own
campaign-level sitelinks, so account-level BTS sitelinks would never serve
there. The script attaches them at campaign level on every enabled Search
campaign whose name contains `brand`, where they rotate alongside the existing set.

**Why a new RSA instead of editing brand RSAs:** editing an RSA resets its
performance history and re-enters learning. The script adds one extra RSA per
brand ad group (skipping ad groups already at the 3-RSA limit), tagged
`ad.name = "bts-2026"` so `--cleanup` can find and pause exactly these ads.

## Copy — sitelinks (link text ≤25, descriptions ≤35)

| Link text | Description 1 | Description 2 | Final URL (on `www.babyshop.com`) |
|---|---|---|---|
| Back to School | Allt för skola & förskolestart | Ryggsäckar, kläder, skor & mer | `/sv-se/shop-by/back-to-school` |
| Ryggsäckar & väskor | Skolryggsäckar för alla åldrar | Första skolväskan – handla nu | `/sv-se/accessoarer/vaskor-och-ryggsackar` |
| Vardagskläder | Basplagg för skola & förskola | Leggings, tröjor & multipack | `/sv-se/barnklader` |
| Ytterkläder | Regnkläder, fleece & skaljackor | Redo för höst och regn | `/sv-se/ytterklader` |
| Skor | Sneakers, stövlar & innerskor | Kavat, Viking, Reima & fler | `/sv-se/barnskor` |
| Matlådor & flaskor | Matlådor & vattenflaskor | Redo för lunch och mellis | `/sv-se/inredning/ata-och-dricka/lunchlador-och-lunchboxar` |

Every sitelink needs a **distinct, working final URL** — Google disapproves
sitelinks sharing landing pages. These URLs were verified against Google's
index of babyshop.com (2026-08-12); the authoring sandbox could not load the
pages directly. Two URL schemes are live on the site (`/sv-se/…` and
`/se/sv/c/…`) — these use `/sv-se/`, same as the campaign landing page. Give
each a one-click check in a browser, then flip `URLS_VERIFIED = True` in the
script. `--apply` refuses to run until you do.

## Copy — callouts (≤25)

`Allt för skolstarten` · `För skola & förskola` · `Ryggsäckar & matlådor` · `Regnkläder inför hösten`

## Copy — structured snippet

Header `Typer` (sv): `Ryggsäckar` · `Regnkläder` · `Sneakers` · `Matlådor` · `Basplagg` · `Skaljackor`

## Copy — brand RSA (headlines ≤30, descriptions ≤90)

Final URL: the BTS landing page. Display path: `/skolstart`.

Headlines:
1. Back to School hos Babyshop
2. Allt inför skolstarten
3. Skolstart & förskolestart
4. Ryggsäckar till skolstarten
5. Regnkläder & ytterkläder
6. Skor för skola & fritid
7. Basplagg & multipack
8. Kläder för förskolestarten
9. Skolstartsshop t.o.m. 15/9

Descriptions:
1. Allt för skola, förskola och nya rutiner – ryggsäckar, kläder, skor och matlådor.
2. Hitta ryggsäckar, regnkläder och basplagg för skolstarten. Handla tryggt hos Babyshop.
3. Back to School-utbudet finns t.o.m. 15 september. För barn i alla åldrar.
4. Mjuka basplagg, regnkläder och skor för skola, förskola och fritid – samlat på ett ställe

Headline 9 is static on purpose. A countdown customizer
(`Slutar om {=COUNTDOWN("2026-09-15 23:59:59","sv")}`) is available as a
commented-out option in the script — countdown urgency reads oddly when there
is no discount, and a malformed customizer fails the whole ad-create call.

All copy lengths are validated by the script at startup against the API limits.

## Running it

```bash
export GOOGLE_ADS_DEVELOPER_TOKEN=... GOOGLE_ADS_CLIENT_ID=... \
       GOOGLE_ADS_CLIENT_SECRET=... GOOGLE_ADS_REFRESH_TOKEN=... \
       GOOGLE_ADS_LOGIN_CUSTOMER_ID=6899790415
cd apps/babyshop-dashboard

python3 pipeline/apply_bts_assets.py                    # dry run: reads the account, prints the plan
python3 pipeline/apply_bts_assets.py --apply            # creates + links everything
python3 pipeline/apply_bts_assets.py --cleanup          # after 15.9: dry run of the pause
python3 pipeline/apply_bts_assets.py --cleanup --apply  # after 15.9: pauses the bts-2026 RSAs
```

Secrets are already in Secret Manager — from Cloud Shell:
`for N in GOOGLE_ADS_{DEVELOPER_TOKEN,CLIENT_ID,CLIENT_SECRET,REFRESH_TOKEN,LOGIN_CUSTOMER_ID}; do export $N=$(gcloud secrets versions access latest --secret=$N); done`

Re-running `--apply` is safe: existing assets are reused by content match,
existing account/campaign links are skipped, and ad groups that already have a
`bts-2026` ad are skipped.

The mutate calls were written against `google-ads` 31.x (API v25) but have no
live precedent in this repo (the pipeline is read-only) — hence dry-run first,
and the first `--apply` should be eyeballed in the UI afterwards
(Assets → Associations, and the new ads' policy status).

## Timeline

| Date | What |
|---|---|
| now | verify sitelink URLs → `--apply` → check policy approval in UI |
| 2026-09-15 | sitelinks + callouts stop serving on their own (`end_date`) |
| 2026-09-16 | run `--cleanup --apply` (or set a Google Ads automated rule pausing ads named/labelled bts-2026) |

## Other markets

Everything here is sv-SE / Babyshop SE. NO / DK / FI need their own localized
copy, local-language URLs (`/nb-no/…`, `/da-dk/…`, `/fi-fi/…`) and a run with
`GOOGLE_ADS_TARGET_CID=<that account>` — assets are per-account, nothing is
shareable across accounts. Translate the config block, don't reuse Swedish.
