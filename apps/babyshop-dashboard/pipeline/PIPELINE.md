# Babyshop — GP3 Optimization

A static dashboard that turns Google Ads **Target ROAS bid-simulator** data into a GP3
recommendation per portfolio bidding strategy: where to set each target, what it is worth,
and how to split a fixed budget across strategies.

```
GP2 = conversion value reported in Google Ads   (already gross profit — see below)
GP3 = GP2 − ad cost
```

### Why no gross margin is applied

The **primary conversion action** in these Google Ads accounts sends **cart-level gross
profit** as its conversion value. Every bid strategy therefore already bids on profit, and
`Conversions Value` in a bid simulation **is GP2**. The dashboard takes it at face value and
subtracts only ad cost. Multiplying it by a gross margin would deduct cost of goods a second
time, which is why the old "Revenue / Profit basis" toggle is gone.

Revenue does exist in the accounts, but only as a **separate secondary conversion action**.
Bid simulations report a single conversion-value figure — the one the strategy optimises —
so revenue is simply not present in this data and cannot be derived from it.

There is one escape hatch, normally unused: an optional per-account
**`valueToGp2Multiplier`** (default `1.0`) served in the payload `config`. Set it only for an
account whose conversion value is *revenue* rather than GP2 — for example one that has not
been migrated yet — using that account's gross margin. When any account carries a value
other than `1.0`, the dashboard shows a small `GP2 = value × 0.30` badge on the affected
rows; at `1.0` there is no UI for it at all.

The whole point is the **marginal** view. A strategy can keep buying gross profit long after
it has stopped adding *net* profit. GP3 peaks exactly where the next krona of spend returns
less than a krona of GP2 — where **marginal ROAS = ΔGP2 / ΔCost** crosses 1.0.

## Incrementality: why brand and private label are managed differently

Ad **cost is 100% real for every campaign**. The conversion value next to it is not equally
*caused* by the ad, and two campaign types are systematically overstated:

- **Brand** campaigns only match `babyshop` / `lekmer` brand queries. That shopper has
  already chosen us; most of that value arrives anyway through organic, direct or a later
  session. The spend is largely **defensive** — it stops competitors buying our brand terms.
- **Private label (`pb`)** campaigns advertise our own-label products, **sold nowhere else**.
  A shopper who wants one has exactly one place to buy it, so a large share of the value
  converts without the ad.

Their raw curves therefore promise GP3 that does not exist, while every krona of cost does.
Left unadjusted they win budget in the allocator against generic prospecting that is actually
creating demand. So the dashboard classifies every campaign and applies an **incrementality
factor** to GP2 before any recommendation is computed:

```
iGP2 = incrementality factor × GP2
iGP3 = iGP2 − ad cost          ← every recommendation on the page is read off this
```

| Class | Matched by (case-insensitive, in this order) | Default factor |
|---|---|---|
| `brand` | name contains `brand` | **0.20** |
| `generic` | name contains `pb-generic` | **1.00** |
| `private-label` | name contains `-pb-`, or ends with `-pb` | **0.50** |
| `generic` | everything else | **1.00** |

**Precedence is explicit, and incrementality follows the query, not the product.**
`pb-generic` campaigns sell private-label products but match on *generic* search terms —
that searcher was not coming to us anyway, so they capture open-market demand and take no
incrementality discount (`p-shopping-se-pb-generic` → **generic**). `pb-product` campaigns
match on the private-label products themselves, which are sold nowhere else, so much of
that value converts organically (→ **private label**, 0.50). `p-shopping-se-brand`
classifies as brand despite being a shopping campaign.

What changes in the UI:

- The **summary table is segmented** into Generic → Private label → Brand, each with its own
  subtotals. Generic comes first because that is where moving targets on the curve honestly
  earns money.
- Recommended target, interpolated breakeven, status pills, the KPI row (potential /
  incremental / optimization score) and the portfolio allocator all run on **iGP3**.
- Nothing is hidden: **observed GP3 is shown next to incremental GP3** in every table, and
  the curve chart draws both the GP3 and iGP3 lines whenever the factor is not 1.0.
- Every brand and private-label campaign gets a **sensitivity line** in its drill-down —
  the iGP3-max target on its own curve at ×1.00, at the class default, and at half the class
  default (e.g. `210% @×1.00 → 430% @×0.50 → 520% @×0.25`, and `net-negative` when no
  simulated target pays at that factor). If a recommendation swings hundreds of percent on an
  unmeasured number, it should say so on its face.
- Brand additionally carries a **defensive-role caption**: the counterfactual behind a 0.20
  factor assumes nobody else shows up in the auction, which is exactly what brand bidding
  prevents. Low incrementality ≠ minimise spend; those recommendations are **directional only**.
- The portfolio note quantifies the shift in one line: how much of the same budget the
  allocator moves out of brand + private label and into generic versus the unadjusted split.

**These factors are assumptions, not measurements.** 0.20 and 0.50 are plausible priors, not
findings. The only things that replace them are a **geo holdout** (hold the campaign type out
in matched regions and compare total sales) or a **conversion-lift test**. Until then the page
labels them "assumed" everywhere they touch a number.

### Impression share makes the factor campaign-specific

A flat 0.20 says exactly the same thing about a brand campaign that already sits in the
absolute top slot for 99% of its brand searches as about one that is being outbid half the
time. Those are opposite situations:

- The first **owns the auction**. The shopper sees us first whatever we bid next, so marginal
  spend is close to pure insurance and buys almost nothing incremental.
- The second has **real defensive headroom**. The impressions it is losing are going to a
  competitor bidding on our name, and buying them back genuinely changes the outcome.

So wherever the Ads script has collected impression share (the `Shares` tab), the factor is
**derived from the headroom left in the auction** instead of assumed:

```
headroom  h = 1 − <share metric>              clamped to [0, 1]
factor      = floor + (cap − floor) × h       clamped to [floor, cap]
```

| Class | Metric driving `h` | Why that metric | Floor | Cap |
|---|---|---|---|---|
| `brand` | `search_absolute_top_impression_share` | On our own brand terms the question is not whether we appear but whether we appear **first** — a competitor above us is what the spend defends against. | **0.10** | **0.60** |
| `private-label` | `search_impression_share` | These products are sold nowhere else, so what matters is being **present** in the auction at all, not the exact slot. | **0.40** | **1.00** |
| `generic` | — | Never share-adjusted. Its factor is 1.00 by definition: the click *is* the demand. | — | — |

Worked examples from the demo data (illustrative share values, real simulation curves):

| Campaign | Metric | Share | `h` | Factor | vs. flat |
|---|---|---|---|---|---|
| `p-shopping-se-brand` | abs top | 0.93 | 0.07 | `0.10 + 0.50 × 0.07` = **0.135** | was 0.20 → iGP3-max target moves 860% → **1200%** |
| `p-shopping-se-pb-product` | search IS | 0.71 | 0.29 | `0.40 + 0.60 × 0.29` = **0.574** | was 0.50 → iGP3-max target moves 450% → **370%** |

A campaign already owning its auction lands on the **floor**; one absent from it lands on the
**cap**. Note the direction: a *lower* factor pushes the recommended target ROAS **up** (spend
down), a *higher* factor pushes it **down** (spend up).

**Fallback ladder**, strongest first — each rung applies only when the one above it is absent:

1. an explicit **per-campaign pattern override** in the `Config` tab (a human pinned this
   number, and no derivation may quietly overrule it);
2. the **impression-share-derived factor**, for brand / private-label campaigns that have
   share data;
3. the **flat class factor** from the `Config` tab;
4. the **built-in default** (0.20 / 0.50 / 1.00), so a payload with no `incrementality` block
   at all still works.

Missing share data is a normal state, not an error: `null`, a blank cell **and a literal 0**
all mean *no data* and fall back to rung 3. Performance Max, Display and video campaigns
report no impression share at all. Deriving `h = 1 − 0 = 1` from an absent metric would claim
maximum headroom for a campaign we simply cannot see, which is why 0 is never trusted.

**The `<10%` / `>90%` caveat.** Google does not report these metrics continuously at the
extremes: anything below 10% arrives as `0.0999`, and everything above 90% collapses into a
`>0.9` bucket. Values are taken at face value regardless — the dashboard says so in the
drill-down — but it means a headroom near 0 or near 1 is coarser than the two decimals
suggest. In practice this hurts least where it matters most: a brand campaign anywhere in the
`>90%` bucket sits within a few hundredths of the floor either way.

**What impression share does and does not buy you.** It does not measure incrementality. It
decides *where between the floor and the cap* a campaign sits; the floor and the cap
themselves are still assumptions, and only a geo holdout or a conversion-lift test replaces
those. What it does buy is that two campaigns in genuinely different competitive positions
stop being handed the same number.

**Floors and caps are Config-editable**, under four reserved keys — see below.

### Editing them without touching code

Everything above lives in the `Config` tab, columns **D/E/F**, next to the existing
account/multiplier pair in A/B/C:

| Incrementality Class or Name Pattern | Incrementality Factor | Notes |
|---|---|---|
| `brand` | `0.20` | class default — the **fallback** used when a brand campaign has no share data |
| `private-label` | `0.50` | class default — same, for private label |
| `generic` | `1.00` | leave at 1.00; never share-adjusted |
| `brand-floor` | `0.10` | **reserved key** — factor for a brand campaign at ~100% absolute-top share |
| `brand-cap` | `0.60` | **reserved key** — factor for a brand campaign with no absolute-top share |
| `private-label-floor` | `0.40` | **reserved key** — at ~100% search impression share |
| `private-label-cap` | `1.00` | **reserved key** — at ~0% search impression share |
| `p-shopping-se-pb-product` | `0.65` | per-campaign override — beats both the derived and the class factor |

`0.2`, `0,2`, `20` and `20%` all parse to 0.20. The three class names and the four reserved
share keys are matched with punctuation and case stripped, so `Brand Cap` and `brand-cap` are
the same row. A row whose key is **neither** a class name **nor** a reserved key is a
**campaign-name pattern override**: matched case-insensitively as a substring of the campaign
name, and when several patterns match, the **longest one wins**. Rows with a blank key or an
unreadable factor are ignored, so free-text comment rows in the tab are harmless.

`setupConfigTab()` seeds all seven keyed rows and is **idempotent per key**: re-run it after
upgrading `webapp.gs` and an existing tab gains only the four bound rows it was missing, with
every factor you have edited left untouched.

The endpoint serves this as:

```js
config.incrementality = {
  classes:      { brand: 0.2, 'private-label': 0.5, generic: 1.0 },
  overrides:    [ { pattern: 'p-shopping-se-pb-product', factor: 0.65 } ],
  shareWeights: { brand:           { floor: 0.10, cap: 0.60 },
                  'private-label': { floor: 0.40, cap: 1.00 } }
}
```

and the impression-share snapshot alongside it, or `null` when the tab does not exist yet:

```js
payload.shares = {
  columns:  ['Customer Name','Campaign Id','Campaign Name','Search IS','Top IS','Abs Top IS','Currency','Run Date'],
  rows:     [ ['Babyshop SE','21700388337','p-shopping-se-brand',0.97,0.96,0.93,'SEK','2026-08-03'], ... ],
  runDates: ['2026-08-03']
}
```

Rows are joined to campaigns by `(account, campaign id)`, falling back to `(account, name)`.
Only the **latest run date** is served (and used), because a factor derived from three-week-old
impression share would be worse than the flat prior it replaces; `runs=N` widens that when a
client wants share data aligned with older snapshots. The dashboard flags it in the trust
panel when the share run date differs from the simulation run date, and when a brand or
private-label campaign has no share row at all.

If `config.incrementality` is missing entirely — an older deployment, or a `Config` tab that
was never seeded — the dashboard falls back to the same built-in defaults, so nothing breaks.
The same is true of `payload.shares`: absent means every campaign keeps its flat class factor,
which is exactly the behaviour that predates this feature.

This is **orthogonal to `valueToGp2Multiplier`** above and applies strictly after it: the
multiplier answers *"is this number GP2?"*, incrementality answers *"how much of this GP2 did
the ad cause?"*. Cost is never scaled by either.

## Architecture

```
  Google Ads MCC                Google Sheet                Apps Script              GitHub Pages
 ┌────────────────┐          ┌──────────────────┐        ┌──────────────┐          ┌──────────────┐
 │ gp3-simulations│  append  │ Raw   (snapshots)│  read  │ webapp.gs    │  fetch   │ index.html   │
 │ .js            │─────────▶│ Shares(impr.share)│──────▶│ doGet + token│─────────▶│ dashboard    │
 │ scheduled      │  1×/run  │ Config(optional) │        │ → JSON       │   CORS   │ all math     │
 └────────────────┘          │ 90-day history   │        └──────────────┘          │ client-side  │
        │                    └──────────────────┘                                  └──────────────┘
        │ AdsApp.search(campaign_simulation)   TARGET_ROAS point lists                     │
        │ AdsApp.search(campaign)              last-7-days impression share                │ localStorage
        ▼                                                                                  ▼
   one row per simulated target ROAS + one row per campaign's share,            last good snapshot
   both tagged with the same Run Date
```

Each stage is replaceable and none of them holds state the next one needs:

| Stage | File | Responsibility |
|---|---|---|
| Collect | `ads-script/gp3-simulations.js` | Query simulations **and** last-7-days impression share in every account, **append** a dated snapshot to two tabs. Never clears, never reads back. Both datasets ride back from each child account in the one string `executeInParallel` allows, split by a group separator; if the ~100 KB cap bites, share rows are dropped first because the simulations are the primary payload. |
| Store | Google Sheet, `Raw` + `Shares` + `Config` tabs | Append-only history on both data tabs, pruned at 90 days. `Config` maps account → `valueToGp2Multiplier` (normally `1.0`), class/pattern → incrementality factor, and the four reserved keys → share-derived floors and caps. |
| Serve | `apps-script/webapp.gs` | `doGet` checks a token, normalises dates/numbers/shares, returns JSON. A missing `Shares` tab serves `shares: null` rather than failing. |
| Present | `index.html` | Single file. Fetches the JSON, does **all** economics in the browser, caches the last good payload. |

## What the dashboard shows

- **Overview** — incremental GP3 today, iGP3 at the optimum, the gap, and an optimization
  score, per currency. Then one row per strategy — **grouped by incrementality class, with
  per-class subtotals** — showing current target, recommended target, interpolated breakeven,
  cost change, observed GP3 next to incremental GP3, iGP3 uplift, and a status pill.
- **Strategy curves** — GP2, GP3 and (where the factor bites) iGP3 against cost with the
  current and recommended points marked, plus marginal incremental ROAS against target with
  the 1.0 breakeven line and the linearly-interpolated crossing. Brand and private-label
  campaigns also show the incrementality factor and its sensitivity line, plus — where share
  data exists — all three impression-share metrics, which one drives the factor, the headroom
  it leaves, the arithmetic, and the floor and cap in play. The summary pill says which it is:
  `×0.13 (IS-derived)` against `×0.20 (assumed)`.
- **Portfolio budget** — enter a total budget and get the equal-marginal-return split:
  spend is allocated greedily to the highest **incremental** marginal ROAS available anywhere
  until the budget runs out, so every strategy ends on the same marginal return. A note sizes
  how much budget that moves out of brand + private label versus the unadjusted allocation.
  Includes a sweep of portfolio iGP3 against budget, whose peak is the unconstrained optimum.
- **History** — one row per snapshot; from the second run onward, a trend of the
  recommended target and current GP3 for the selected strategy.

Two numbers deliberately differ and both are shown:

- **Recommended target** — the *simulated point* with the highest iGP3. Always achievable,
  always inside the data. (For a generic campaign, factor 1.0, this is the plain GP3 maximum.)
- **Breakeven target** — where marginal ROAS crosses 1.0, interpolated between the two
  bracketing segments. The smooth-curve estimate, typically a few points above the
  recommendation because the simulated grid is coarse.

## Setup

### 1. Google Ads script

1. MCC → **Tools & Settings → Bulk actions → Scripts → +**.
2. Paste `ads-script/gp3-simulations.js`.
3. Set the `CONFIG` block:
   - `SPREADSHEET_URL` — the sheet that will hold the data.
   - `ACCOUNT_IDS` — child account CIDs (Babyshop SE/NO/ROW/DK/FI, Lekmer SE/NO).
   - `LOOKBACK_PRUNE_DAYS` — history retention, default 90.
   - `COLLECT_SHARES` — leave `true` to also collect last-7-days impression share into the
     `Shares` tab, which is what makes brand and private-label incrementality factors
     campaign-specific. Set `false` and everything falls back to flat class factors.
4. **Authorise → Preview → Run.** It creates the `Raw` and `Shares` tabs and their header rows.
5. Schedule it **Weekly** (matching the 7-day simulation window) or Daily.

Re-running on the same day replaces that day's rows instead of duplicating them, so a
manual run between scheduled ones is safe.

### 2. Apps Script web app

1. In the spreadsheet: **Extensions → Apps Script**, paste `apps-script/webapp.gs`.
2. Set `SCRIPT_TOKEN` to a long random string:
   ```
   node -e "console.log(require('crypto').randomBytes(24).toString('hex'))"
   ```
3. Run `setupConfigTab()` once — creates `Config`, pre-fills the accounts found in `Raw`
   with a `Value to GP2 Multiplier` of `1`, and seeds the incrementality rows in columns
   D/E/F. Leave the multipliers at `1`: conversion value is already GP2. Change a row only
   for an account that reports revenue instead, entering its gross margin (`30%`, `30` and
   `0.3` all work); the `Default` row covers anything missing. The incrementality factors
   (`brand` 0.20, `private-label` 0.50, `generic` 1.00) and the four impression-share bounds
   (`brand-floor` 0.10, `brand-cap` 0.60, `private-label-floor` 0.40, `private-label-cap`
   1.00) *are* meant to be edited — see
   [Incrementality](#incrementality-why-brand-and-private-label-are-managed-differently).
   Re-running `setupConfigTab()` never overwrites values you have already entered and adds
   only the keyed rows that are missing, so run it again after upgrading this file. The
   endpoint serves these as `config.valueToGp2Multipliers` /
   `config.defaultValueToGp2Multiplier` / `config.incrementality` (including
   `config.incrementality.shareWeights`), plus `payload.shares` from the `Shares` tab.
4. **Deploy → New deployment → Web app**, *Execute as* **Me**, *Who has access*
   **Anyone with the link**. Copy the `/exec` URL.
5. Check it: `<exec-url>?token=<token>&runs=1`.

Editing the script later requires **Manage deployments → Edit → New version**, otherwise
the live URL keeps serving the old code.

### 3. Dashboard

At the top of the `<script>` block in `index.html`:

```js
const DATA_ENDPOINT = 'https://script.google.com/macros/s/.../exec';
const DATA_TOKEN    = 'the-same-token';
```

Leave them empty and the page runs on `DEMO_DATA` — one real Babyshop SE snapshot, 92
simulation points across 9 strategies — and labels itself **Demo data** throughout. That block
carries two **illustrative** impression-share rows (the SE brand campaign at 0.93 absolute-top,
`pb-product` at 0.71 search IS) so the dynamic path is exercised in demo mode; unlike the
simulation rows, those two are made up.

### 4. GitHub Pages

**Settings → Pages → Source: Deploy from a branch**, branch `main`, folder `/ (root)`.
The page is one self-contained file; Chart.js and the fonts come from CDNs.

Login: user `babyshop`, password `gp3`. To change it, regenerate the hash and replace
`GATE_HASH`:

```
echo -n "user:password" | shasum -a 256
```

## Security

**The login gate and the token are deterrents, not authentication.**

- `GATE_HASH` sits in the page source. Anyone who views source can extract it and attack
  it offline; the password is not secret.
- `DATA_TOKEN` is likewise published in the HTML. It stops crawlers and accidental hits,
  not a determined reader. Anyone with the endpoint URL can read the simulation data.
- The endpoint is read-only and exposes aggregate simulation figures only — no customer
  data, no credentials, no write path back into Google Ads.

What this buys is that **the data is never committed to this repository.** The repo holds
code and one demo snapshot; everything live is fetched at runtime and cached only in the
viewer's own browser. If the underlying figures ever become genuinely sensitive, move the
page behind real authentication (an identity-aware proxy, Cloudflare Access, or a hosted
app with server-side sessions) rather than hardening the gate.

Do not commit real exports, `.csv`/`.xlsx` snapshots, or a populated `DEMO_DATA`.

## Reading the numbers honestly

Google's simulator answers one narrow question: *what would the last 7 days have looked
like at a different target, holding everything else constant?* It assumes the same budgets,
creatives, competitors, seasonality and inventory, and it does not reconcile exactly with
reported ROAS — the same discrepancy you see in the Google Ads UI.

So treat every recommendation as a **directional step move**. Change one target at a time,
give the strategy a week or two to re-learn, then re-check against a fresh snapshot. The
dashboard flags stale snapshots (>7 days), short simulation grids, targets sitting outside
the simulated range, and strategies whose optimum lies beyond the simulated window. It also
flags anything that weakens the incrementality factors: no `Shares` tab at all, brand or
private-label campaigns with no share row, and share data collected on a different run date
than the simulations being shown.

## Repository layout

```
index.html                     the dashboard (single file, no build step)
ads-script/gp3-simulations.js  Google Ads MCC collector
apps-script/webapp.gs          Apps Script JSON endpoint
.gitignore                     keeps data exports out of the repo
README.md                      this file
```
