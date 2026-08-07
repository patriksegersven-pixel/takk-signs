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

## Actual performance: real ROAS (= POAS), two attribution schemes

Everything above is a **projection**. Google's bid simulator answers a counterfactual — *what
would the last 7 days have looked like at a different target* — and it does not reconcile with
reported ROAS. So the collector also writes what each simulated entity **actually did over the
same window** into an `Actuals` tab, and the dashboard shows it next to the curve.

Because conversion value in these accounts is gross profit, **actual ROAS is actual POAS**:

```
ROAS (conv. time)  = Conv Time Conversions Value / Cost
ROAS (click time)  = Conversions Value          / Cost
```

Cost is identical under both schemes, which is why the tab carries it once and both figures
divide by the same denominator.

| Scheme | Google Ads metric | A conversion is counted on… | Why you'd read it |
|---|---|---|---|
| **conversion time** (primary) | `metrics.conversions_value_by_conversion_date`, `metrics.conversions_by_conversion_date` | the date the **conversion happened** | Google Ads' "by conv. time" columns — **the default view in this account**, and how the week's banked profit is actually reported |
| **click time** | `metrics.conversions_value`, `metrics.conversions` | the date of the **click** that led to it | the scheme the bid simulator and Target ROAS bidding themselves run on, so it is the one directly comparable with the simulated points |

Neither is more correct. Click time answers *"what did this spend eventually return"*;
conversion time answers *"what was banked in this window"*. **Both lag** — conversions keep
arriving for days after the window closes and are back-dated under either scheme, so a fresh
snapshot's actuals are still filling in. The dashboard flags any campaign where the two differ
by more than 15%, which is the usual signature of a long conversion lag or an unsettled window.

### The window is the simulation's own window

An Actuals row is measured over **the same `Start Date` / `End Date` as the simulation it joins
to**, not over a fixed last-7-days range, so actual and simulated ROAS describe the same week.
Google refreshes simulation windows per entity, so the collector issues **one GAQL query per
distinct window per account** (normally exactly one). An entity whose simulation carried no
window falls back to `LAST_7_DAYS`, and the dashboard flags the mismatch.

### GAQL

One query per (level, window). `<level>` is `campaign` for campaign-level rows and
`bidding_strategy` for portfolio-level rows (only collected when `INCLUDE_PORTFOLIO` is on):

```sql
SELECT
  <level>.id,
  <level>.name,
  metrics.cost_micros,
  metrics.conversions,
  metrics.conversions_value,
  metrics.conversions_by_conversion_date,
  metrics.conversions_value_by_conversion_date
FROM <level>
WHERE segments.date BETWEEN '<start>' AND '<end>'
```

`segments.date` is **filtered but not selected**, so the API aggregates the whole window into
one row per entity. Every entity in the account comes back; rows whose id was not simulated are
dropped client-side (a GAQL id list long enough for a large account is worse than a filter), as
are rows with **no spend in the window** — ROAS is undefined there and a blank row would only
add noise to the join. `Start Date` / `End Date` are validated against `^\d{4}-\d{2}-\d{2}$`
before being interpolated into the query string.

Failure handling is **per window**, not per level or per account, so a window that fails can
never discard the windows that already succeeded — and the whole Actuals collection sits inside
one more try/catch, so none of it can cost the simulations or the impression shares.

A failed window is triaged on the error text:

- the resource **will not serve** the conversion-time metrics (`UNRECOGNIZED_FIELD`,
  `PROHIBITED_FIELD_COMBINATION…`, anything naming `conversion_date`) → **retry that window
  without them**, so click-time actuals still arrive. The conversion-time cells stay blank, the
  dashboard shows a dash in the primary column and the real figure in the secondary one, and the
  trust panel says why. Degrade, never vanish.
- **anything else** (timeout, quota, internal error) → skip **that window only** and log it.
  Retrying without the conversion-time metrics here would silently blank a column that works
  perfectly well, trading a visible gap for a wrong number. An unrecognised error is treated as
  transient, which is the safe side of that call.

### `Actuals` tab schema

| Column | Notes |
|---|---|
| `Customer Name` | join key |
| `Bidding Strategy Id` | join key — campaign id at campaign level, bidding strategy id at portfolio level; the **same id the `Raw` tab carries** |
| `Bidding Strategy Name` | for the name-based join fallback |
| `Level` | `campaign` or `portfolio` |
| `Start Date`, `End Date` | the simulation window this was measured over |
| `Cost Micros` | account currency, micros — same unit as the `Raw` tab |
| `Conversions`, `Conversions Value` | **click time** |
| `Conv Time Conversions`, `Conv Time Conversions Value` | **conversion time** |
| `Currency` | account currency |
| `Run Date` | join key |

**Join key: `(Customer Name, Bidding Strategy Id, Run Date)`**, falling back to
`(Customer Name, Bidding Strategy Name, Run Date)`. The run date is part of the key on purpose:
the `Raw` tab holds up to 90 days of snapshots and the history view rebuilds each one, so an
actuals row is only ever attached to the run it was measured for. (This is the opposite of
`Shares`, where only the latest run is served and applied — auction position is a statement
about *now*, while a measurement belongs to *its own* week.)

Same four guarantees as the other tabs: **append only, write only, idempotent per day,
self-pruning** at `LOOKBACK_PRUNE_DAYS`.

**Blank is never 0 here either, but for the opposite reason.** On `Shares`, 0 and blank both
mean "no data". On `Actuals` a **real 0 is a measurement** — spend that returned nothing — and
must stay distinguishable from "the API did not report this". Blank renders as a dash, 0
renders as `0.00×`.

### What the dashboard does with it

The endpoint serves it as `payload.actuals`, in the same compact shape as `Raw` and `Shares`:

```js
payload.actuals = {
  columns:  ['Customer Name','Bidding Strategy Id','Bidding Strategy Name','Level','Start Date',
             'End Date','Cost Micros','Conversions','Conversions Value','Conv Time Conversions',
             'Conv Time Conversions Value','Currency','Run Date'],
  rows:     [ ['Babyshop SE','21700388337','p-shopping-se-brand','campaign','2026-07-27',
               '2026-08-02',412000000,96.4,1284000,99.1,1297000,'SEK','2026-08-05'], ... ],
  runDates: ['2026-07-22','2026-07-29','2026-08-05']
}
```

Unlike `shares`, **every requested run date is served** (`runs=N` narrows it exactly the way it
narrows `Raw`), because each row is joined to the snapshot of its own run.

Where the two numbers appear:

- **Campaign summary** — two columns, `Actual ROAS (conv. time)` and `Actual ROAS (click time)`,
  sitting with the other ROAS-scale figures (current target, recommendation, breakeven) so the
  target you set and the ROAS you got can be read across in one line. Conversion time leads and
  is unmuted; click time is the secondary read. Class subtotals and the grand total carry both,
  **cost-weighted** (total value ÷ total cost, never a mean of ratios).
- **Campaign drill-down** — both in the key-value strip, and **two measured rows at the foot of
  the simulated-point table**, one per scheme, carrying measured cost / GP2 / GP3 / iGP3 in the
  same units as the simulated rows above them.
- **Snapshot history** — both per run, aggregated across the run's campaigns, so you can see
  actual POAS move as targets change.
- **Trust panel** — flags campaigns with no measured row, windows that fell back to
  `LAST_7_DAYS`, campaigns reporting click time but not conversion time (the retry path above),
  a >15% gap between the two schemes, and the total absence of the tab.

### Locale note on parsing

Google reports conversions and conversion value with **three decimals**, and these sheets are
sv-SE / nb-NO / da-DK, where the decimal mark is a comma. So `4523.456` *displays* as
`"4 523,456"` and `45.125` as `"45,125"`. Any "a comma followed by three digits is a thousands
separator" heuristic reads both a thousand times too large. `webapp.gs` therefore takes every
numeric column on `Raw` and `Actuals` from **`getValues()`, not `getDisplayValues()`** — real
numbers, no formatting in the way — while text and dates keep their display values, because a
date read with `getValues()` comes back as an instant at midnight in the *spreadsheet's*
timezone and formats to the previous day in UTC. The string parser (`toMetric()`) is a fallback
for hand-typed cells only, and in it **a lone comma is always a decimal mark**.

The per-point column previously labelled `Actual ROAS` in the drill-down table was **never a
measurement** — it is the value Google projects at that simulated target over the cost it
projects with it. It is now labelled **`Sim. ROAS`** so the three numbers cannot be confused.

**Missing actuals is a normal state, not an error.** A payload with no `actuals` block (an
endpoint that predates the tab, `COLLECT_ACTUALS: false`, or a cached snapshot from before this
feature) renders **exactly** the page that predates the feature: the columns are not emitted at
all, no dashes and no empty cells. A campaign with no row inside a payload that has others shows
a dash. Neither state changes a single recommendation — actuals are reported alongside the
economics and never fed into them.

## Architecture

```
  Google Ads MCC                Google Sheet                Apps Script              GitHub Pages
 ┌────────────────┐          ┌───────────────────┐       ┌──────────────┐          ┌──────────────┐
 │ gp3-simulations│  append  │ Raw   (snapshots) │  read │ webapp.gs    │  fetch   │ index.html   │
 │ .js            │─────────▶│ Shares(impr.share)│──────▶│ doGet + token│─────────▶│ dashboard    │
 │ scheduled      │  1×/run  │ Actuals(measured) │       │ → JSON       │   CORS   │ all math     │
 └────────────────┘          │ Config(optional)  │       └──────────────┘          │ client-side  │
        │                    │ 90-day history    │                                 └──────────────┘
        │                    └───────────────────┘                                         │
        │ AdsApp.search(campaign_simulation)   TARGET_ROAS point lists                      │
        │ AdsApp.search(campaign)              last-7-days impression share                 │ localStorage
        │ AdsApp.search(campaign)              actuals over the simulation window,          ▼
        ▼                                      click time AND conversion time      last good snapshot
   one row per simulated target ROAS, one per campaign's share, one per campaign's
   measured performance — all tagged with the same Run Date
```

Each stage is replaceable and none of them holds state the next one needs:

| Stage | File | Responsibility |
|---|---|---|
| Collect | `ads-script/gp3-simulations.js` | Query simulations, last-7-days impression share **and** measured performance over each simulation window in every account, **append** a dated snapshot to three tabs. Never clears, never reads back. All three datasets ride back from each child account in the one string `executeInParallel` allows, split by group separators; if the ~100 KB cap bites they are trimmed in value order — actuals first (display only), then shares (they change which factor a recommendation used), then the simulations, which *are* the run. |
| Store | Google Sheet, `Raw` + `Shares` + `Actuals` + `Config` tabs | Append-only history on all three data tabs, pruned at 90 days. `Config` maps account → `valueToGp2Multiplier` (normally `1.0`), class/pattern → incrementality factor, and the four reserved keys → share-derived floors and caps. |
| Serve | `apps-script/webapp.gs` | `doGet` checks a token, normalises dates/numbers/shares/metrics, returns JSON. A missing `Shares` or `Actuals` tab serves `null` for that block rather than failing. |
| Present | `index.html` | Single file. Fetches the JSON, does **all** economics in the browser, caches the last good payload. |

## What the dashboard shows

- **Overview** — incremental GP3 today, iGP3 at the optimum, the gap, and an optimization
  score, per currency. Then one row per strategy — **grouped by incrementality class, with
  per-class subtotals** — showing current target, recommended target, interpolated breakeven,
  **actual ROAS by conversion time and by click time**, cost change, observed GP3 next to
  incremental GP3, iGP3 uplift, and a status pill.
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
- **History** — one row per snapshot, including **actual ROAS both ways** aggregated across the
  run's campaigns; from the second run onward, a trend of the recommended target and current
  GP3 for the selected strategy.

Three ROAS figures live on the drill-down and they are deliberately distinct:

- **`Sim. ROAS`** — per simulated point: the value Google *projects* at that target over the cost
  it projects with it. A projection, not a measurement.
- **`Actual ROAS (conv. time)`** — measured, conversions counted on the date they happened.
- **`Actual ROAS (click time)`** — measured, conversions counted on the date of the click.

Two targets deliberately differ and both are shown:

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
   - `COLLECT_ACTUALS` — leave `true` to also collect measured performance over each
     simulation window into the `Actuals` tab, which is what puts real ROAS/POAS (by
     conversion time and by click time) next to the simulated curves. Set `false` and the
     dashboard hides those columns entirely.
4. **Authorise → Preview → Run.** It creates the `Raw`, `Shares` and `Actuals` tabs and their
   header rows.
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
   `config.incrementality.shareWeights`), plus `payload.shares` from the `Shares` tab and
   `payload.actuals` from the `Actuals` tab.
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
simulation rows, those two are made up. It also carries an **invented `actuals` block**, one row
per campaign per run, deliberately bent off the simulated curve (cost ×0.97, click-time value
×0.94, conversion-time value a further ×1.01) so the actual-vs-simulated gap is visible rather
than suspiciously exact. `demo-search-pb-bundle` has **no** actuals row on purpose, so the
missing-measurement path renders too.

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

The `Actuals` tab is the honest check on all of it — but read it knowing that **actual and
simulated ROAS are not the same measurement.** The simulator projects a counterfactual week at
one target; the actuals are the one week that really happened, at whatever targets were live
(and possibly changing) inside it. A gap between them is expected, not a bug. The dashboard
flags campaigns with no measured row, actuals that fell back to `LAST_7_DAYS` instead of the
simulation's own window, and a >15% gap between the two attribution schemes — which usually
means conversion lag, i.e. the window is still filling in.

## Repository layout

```
index.html                     the dashboard (single file, no build step)
ads-script/gp3-simulations.js  Google Ads MCC collector (Raw + Shares + Actuals)
apps-script/webapp.gs          Apps Script JSON endpoint
.gitignore                     keeps data exports out of the repo
README.md                      this file
```
