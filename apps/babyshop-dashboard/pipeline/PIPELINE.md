# Babyshop — GP3 Optimization

A dashboard that turns Google Ads **Target ROAS bid-simulator** data into a GP3
recommendation per portfolio bidding strategy: where to set each target, what it is worth,
and how to split a fixed budget across strategies.

```
GP2 = conversion value reported in Google Ads   (already gross profit — see below)
GP3 = GP2 − ad cost
```

> ## Two pipelines, one payload
>
> The data reaching `babyshop-roas-simulations.html` can come from either of two
> collectors. **They produce the identical JSON payload**, so everything in this document
> about *what the numbers mean* — attribution schemes, incrementality, GP2 multipliers,
> blank-vs-0, join keys — is true of both. Only the plumbing differs.
>
> | | **Google Ads API** (current) | **Google Sheets** (legacy / fallback) |
> |---|---|---|
> | Collector | `refresh_roas_sims.py` (Cloud Run) | `pipeline/gp3-simulations.js` (Ads MCC script) |
> | Store | Firestore `roas_sim_snapshots` | Google Sheet, `Raw` / `Shares` / `Actuals` tabs |
> | Config | Firestore `roas_sim_config/config` | the sheet's `Config` tab |
> | Serve | `GET /api/roas-sims` (same origin) | `pipeline/webapp.gs` `doGet` (Apps Script `/exec`) |
> | Trigger | Cloud Scheduler → `POST /internal/refresh-roas-sims` | the Ads script's own schedule |
> | Retention | 90 days, pruned per run | 90 days, `LOOKBACK_PRUNE_DAYS` |
>
> **The legacy path is still deployed and still works.** Nothing about it was removed —
> both scripts remain in `pipeline/`, the spreadsheet keeps filling, and switching the
> dashboard back is a one-line edit (see *Dashboard wiring*). It is the fallback while the
> API credentials are being provisioned, and the escape hatch if the API path breaks.
>
> No historical backfill is done: the API pipeline's history accrues fresh from its first
> run. The two histories are independent; the sheet's remains readable through the
> Apps Script endpoint.

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

> **On the API pipeline these live in Firestore `roas_sim_config/config`**, not a
> spreadsheet — same keys, same meanings, same forgiving number parsing, still editable by
> a human with no deploy. See *Editing the config* under Setup. The rest of this section
> describes the sheet's `Config` tab, which the legacy path still reads.

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

## Architecture — the API pipeline (current)

```
  Cloud Scheduler          Cloud Run (babyshop-dashboard)            Firestore
 ┌──────────────┐        ┌────────────────────────────────┐    ┌─────────────────────┐
 │ roas-sims-   │  POST  │ /internal/refresh-roas-sims    │    │ roas_sim_snapshots/ │
 │ daily 05:30  │───────▶│   refresh_roas_sims.refresh()  │───▶│   2026-08-07        │
 │ X-Internal-  │        │   google-ads GAQL × 8 accounts │ 1× │   2026-08-06 …      │
 │ Token        │        └────────────────────────────────┘/day│   (90-day history)  │
 └──────────────┘                                              │ roas_sim_config/    │
                         ┌────────────────────────────────┐    │   config  ← EDITABLE│
   babyshop-roas-        │ /api/roas-sims?runs=30&token=  │◀───│                     │
   simulations.html ────▶│   build_payload()              │    └─────────────────────┘
   (same origin)  fetch  │   → the webapp.gs payload shape│
        │                └────────────────────────────────┘
        │ localStorage                    ▲
        ▼                                 │ GAQL, one query set per child account
   last good snapshot        campaign_simulation        TARGET_ROAS point lists
                             bidding_strategy_simulation (portfolio, off by default)
                             campaign + metrics.*_impression_share   last 7 days
                             campaign/bidding_strategy + metrics      actuals over the
                                                                     simulation's own
                                                                     window, click time
                                                                     AND conversion time
```

Each stage is replaceable and none of them holds state the next one needs:

| Stage | File | Responsibility |
|---|---|---|
| Collect | `refresh_roas_sims.py` | One `GoogleAdsClient` (credentials from five env vars), then per child account: a customer probe, a bidding-strategy map, a campaign map, the simulation query, the impression-share query, and one actuals query per distinct simulation window per level. Per-account failures are captured as `status: "error"` in the snapshot, so one dead account never loses the other seven. Shares and actuals each sit behind their own guard — losing them costs a factor refinement or two display columns, never the run. |
| Store | Firestore `roas_sim_snapshots/<YYYY-MM-DD>` | One document per run date, so a re-run **overwrites itself** (idempotent per day) and pruning is a document delete. Written with `set()` and no `merge`, so a shrinking dataset cannot leave stale keys. Pruned to 90 days on every run. Row grids ride as JSON strings (`rows_json` / `shares_json` / `actuals_json`) because **Firestore cannot store an array inside an array**; a day is ~120 KB, well under the 1 MiB document limit. |
| Config | Firestore `roas_sim_config/config` | What the sheet's `Config` tab held: account → `valueToGp2Multiplier`, class/pattern → incrementality factor, and the four reserved keys → share-derived floors and caps. **User-editable with no deploy** — see *Editing the config*. Seeded with the sheet defaults on the first run if absent. |
| Serve | `app.py` → `refresh_roas_sims.build_payload()` | `GET /api/roas-sims` re-assembles the snapshots into **exactly** the JSON `doGet` in `webapp.gs` returned, applying the same `toMetricOrZero` / `toMetric` / `toShare` normalisation per column. Same `runs` / `account` / `token` query params, same asymmetry (shares default to the latest run only, actuals carry every run). |
| Present | `babyshop-roas-simulations.html` | Unchanged. Fetches the JSON, does **all** economics in the browser, caches the last good payload. |

### Architecture — the sheet pipeline (legacy / fallback)

```
  Google Ads MCC                Google Sheet                Apps Script            dashboard
 ┌────────────────┐          ┌───────────────────┐       ┌──────────────┐       ┌──────────────┐
 │ gp3-simulations│  append  │ Raw   (snapshots) │  read │ webapp.gs    │ fetch │ .html        │
 │ .js            │─────────▶│ Shares(impr.share)│──────▶│ doGet + token│──────▶│ all math     │
 │ scheduled      │  1×/run  │ Actuals(measured) │       │ → JSON       │ CORS  │ client-side  │
 └────────────────┘          │ Config(optional)  │       └──────────────┘       └──────────────┘
        │                    │ 90-day history    │
        │                    └───────────────────┘
        │ AdsApp.search(campaign_simulation)   TARGET_ROAS point lists
        │ AdsApp.search(campaign)              last-7-days impression share
        │ AdsApp.search(campaign)              actuals over the simulation window,
        ▼                                      click time AND conversion time
   one row per simulated target ROAS, one per campaign's share, one per campaign's
   measured performance — all tagged with the same Run Date
```

| Stage | File | Responsibility |
|---|---|---|
| Collect | `pipeline/gp3-simulations.js` | Query simulations, last-7-days impression share **and** measured performance over each simulation window in every account, **append** a dated snapshot to three tabs. Never clears, never reads back. All three datasets ride back from each child account in the one string `executeInParallel` allows, split by group separators; if the ~100 KB cap bites they are trimmed in value order — actuals first (display only), then shares (they change which factor a recommendation used), then the simulations, which *are* the run. |
| Store | Google Sheet, `Raw` + `Shares` + `Actuals` + `Config` tabs | Append-only history on all three data tabs, pruned at 90 days. `Config` maps account → `valueToGp2Multiplier` (normally `1.0`), class/pattern → incrementality factor, and the four reserved keys → share-derived floors and caps. |
| Serve | `pipeline/webapp.gs` | `doGet` checks a token, normalises dates/numbers/shares/metrics, returns JSON. A missing `Shares` or `Actuals` tab serves `null` for that block rather than failing. |
| Present | `babyshop-roas-simulations.html` | Same file, pointed at the `/exec` URL instead. |

The API collector deliberately mirrors the Ads script decision for decision: the same
column grids, the same "blank is never 0 on `Shares`, a real 0 is a measurement on
`Actuals`" rule, the same per-window actuals with the same click-time-only retry, the same
`safeName()` flattening so a campaign name is spelled identically on both paths, and the
same trim order if a payload ever overflows its transport.

### GAQL the API collector issues

Per child account, five query shapes. Field names are verified against the installed
`google-ads` package (there is a test for it) — never written from memory.

```sql
-- account name + currency (the "Customer Name" join key)
SELECT customer.id, customer.descriptive_name, customer.currency_code FROM customer LIMIT 1

-- portfolio strategies: serves both the portfolio-level rows and the campaign-level
-- "current target lives on the strategy" fallback
SELECT bidding_strategy.id, bidding_strategy.name, bidding_strategy.type,
       bidding_strategy.target_roas.target_roas,
       bidding_strategy.maximize_conversion_value.target_roas
FROM bidding_strategy

-- campaigns: name, bidding type, and the current Target ROAS
SELECT campaign.id, campaign.name, campaign.bidding_strategy,
       campaign.bidding_strategy_type, campaign.target_roas.target_roas,
       campaign.maximize_conversion_value.target_roas
FROM campaign

-- the simulations themselves
SELECT campaign_simulation.campaign_id, campaign_simulation.start_date,
       campaign_simulation.end_date, campaign_simulation.type,
       campaign_simulation.target_roas_point_list.points
FROM campaign_simulation WHERE campaign_simulation.type = 'TARGET_ROAS'
-- and, when INCLUDE_PORTFOLIO is on, the same shape FROM bidding_strategy_simulation

-- impression share
SELECT campaign.id, campaign.name, metrics.search_impression_share,
       metrics.search_top_impression_share, metrics.search_absolute_top_impression_share
FROM campaign WHERE segments.date DURING LAST_7_DAYS AND campaign.status = 'ENABLED'

-- actuals, one query per (level, window) — see "The window is the simulation's own window"
SELECT <level>.id, <level>.name, metrics.cost_micros, metrics.conversions,
       metrics.conversions_value,
       metrics.conversions_by_conversion_date, metrics.conversions_value_by_conversion_date
FROM <level> WHERE segments.date BETWEEN '<start>' AND '<end>'
```

Two deliberate differences from the Ads script, neither of which changes an output value:

- **The simulation queries select no attributed resource.** The Ads script selected
  `campaign.*` from `campaign_simulation`; here the campaign and strategy maps are fetched
  once per account and joined in Python on `campaign_simulation.campaign_id` /
  `bidding_strategy_simulation.bidding_strategy_id`. One query then serves both the
  portfolio simulation rows and the campaign-level current-target fallback, and no query's
  validity depends on a cross-resource select.
- **Presence, not truthiness.** Simulation points and metrics are protobuf messages whose
  optional scalars read back as `0` / `0.0` when never set. Every "is this reported?"
  decision uses `HasField`, so an absent `search_impression_share` becomes `None` (→ blank)
  while an explicitly-reported `0.0` conversion count stays `0`. Getting this backwards is
  precisely the blank-vs-0 bug the two tabs are designed to avoid.

### Firestore document schema

`roas_sim_snapshots/2026-08-07`:

| Field | Type | Notes |
|---|---|---|
| `run_date` | string | `YYYY-MM-DD`, identical to the document id |
| `generated_at` | timestamp | `SERVER_TIMESTAMP` |
| `generated_at_iso` | string | the same instant, as a UTC ISO string |
| `source` | string | `google-ads-api` |
| `api_version` | string | the Google Ads API version the client used, e.g. `v25` |
| `ok` | bool | at least one account returned |
| `include_campaigns`, `include_portfolio` | bool | which levels this run collected |
| `columns` | map | `{raw: [...], shares: [...], actuals: [...]}` — the three header grids |
| `counts` | map | `{raw, shares, actuals}` row counts |
| `rows_json`, `shares_json`, `actuals_json` | string | JSON arrays of arrays; see below |
| `accounts` | array of maps | `{cid, name, currency, status, error, sim_rows, share_rows, actual_rows, warnings[]}` |
| `dropped_datasets` | array of strings | non-empty only if a run overflowed the byte budget |

**Why the grids are JSON strings.** Firestore forbids an array as a direct element of
another array, so `rows: [[...], [...]]` is not storable. The alternative — one map per
row, keyed by column name — would pay the header names 600 times over. JSON keeps a day at
~120 KB against the 1 MiB document limit; `DATASET_BUDGET_BYTES` (800 KB) trims in the Ads
script's value order (actuals → shares → simulations) if a run ever approaches it, and says
so in `dropped_datasets`.

`roas_sim_locks/refresh` — the run lock. Google Ads is metered and Cloud Scheduler retries on timeout, so two overlapping collections would double-spend quota across eight accounts. `create()` makes the common case atomic rather than a read-then-write race, and the lock is **self-expiring** (`expires_at`, 30 min by default): a container killed mid-run leaves the document behind, and the next run reclaims it instead of blocking forever. A held lock answers `409 already-running`, which is a normal Scheduler-retry outcome and not an error.

`roas_sim_config/config` — nested maps, never dotted keys:

```json
{
  "valueToGp2Multipliers": { "Babyshop ROW": 0.30 },
  "defaultValueToGp2Multiplier": 1.0,
  "incrementality": {
    "classes":      { "brand": 0.20, "private-label": 0.50, "generic": 1.00 },
    "overrides":    [ { "pattern": "p-shopping-se-pb-product", "factor": 0.65 } ],
    "shareWeights": { "brand":         { "floor": 0.10, "cap": 0.60 },
                      "private-label": { "floor": 0.40, "cap": 1.00 } }
  }
}
```

Both collections are **dedicated to this feature** and shared with nothing else, per the
repo's collection-isolation rule. Neither read path uses `where` at all — document ids are
ISO dates, so `list_documents()` plus a Python-side sort gives chronological order with no
composite index to build.

### Endpoint contracts

| | |
|---|---|
| **`POST /internal/refresh-roas-sims`** | Runs the collector and writes one snapshot. Header `X-Internal-Token: $INTERNAL_TOKEN`, the same gate `/internal/refresh` uses — and **`DEV_MODE` does not bypass it**: the service is `--allow-unauthenticated`, and this endpoint spends metered Google Ads quota across eight accounts. **POST only**, for the same reason: a GET route is reachable by any crawler or link prefetcher that learns the path. Optional `?run_date=YYYY-MM-DD` overrides the document id (replay / backfill); it is validated at the boundary, and the write is idempotent per day either way. |
| ↳ `200` | `{status:"ok", run_date, counts:{raw,shares,actuals}, accounts:[…], written, pruned:[…], seconds}` — at least one account returned. Per-account errors and warnings ride inside `accounts`. |
| ↳ `502` | `{status:"error", …}` — every account failed. `accounts[].error` says why for each. |
| ↳ `503` | `{status:"not-configured", error:"… missing GOOGLE_ADS_REFRESH_TOKEN, …"}` — names the exact env vars still unset. |
| ↳ `409` | `{status:"already-running", …}` — another collection holds the run lock. Not an error: overlapping Scheduler retries are expected and skipping the second is the point. |
| ↳ `400` | `{status:"bad-request", …}` — `run_date` was not a `YYYY-MM-DD`. |
| ↳ `500` | `{status:"error", error:"<Type>: <message>"}` — anything else. |
| **`GET /api/roas-sims`** | The payload the dashboard reads. Behind the dashboard's own HTTP Basic auth like every other `/api/*` route. **Always `200`** — the page distinguishes states by `error` / `status`, and a non-2xx would read to it as a transient network blip rather than a rejected key. |
| ↳ params | `runs=N` keep the N most recent run dates (0 = all); `account=` exact `Customer Name` filter. The access key goes in the **`X-Roas-Sims-Key` header** (Cloud Run logs full request URLs, so a key in the query string lands in Cloud Logging on every page load); `token=` is still accepted for the legacy rollback path, which cannot send headers. |
| ↳ `status:"ok"` | the full payload — see *Payload shape* below. |
| ↳ `status:"no-snapshots"` | plus an `error` string. The collection is empty; the page keeps its cached/demo snapshot. Deliberately does **not** contain the word "unauthorized", which is what stops the page from discarding a perfectly good key. |
| ↳ `status:"unauthorized"` | `{"error":"Unauthorized"}` — only when `ROAS_SIMS_TOKEN` is set on the service and the key does not match. The page drops the stored key and re-prompts. |
| ↳ `status:"read-error"` | Firestore was unreachable. Same shape, empty grids, defaults for `config`. The reason is logged server-side and deliberately **not** echoed into the browser-visible string. |

### Payload shape

Byte-for-byte the shape `doGet` in `webapp.gs` emitted, so the dashboard's
`normalizePayload` / `normalizeShares` / `normalizeActuals` run unchanged:

```js
{
  generatedAt, source, spreadsheet,
  config:  { valueToGp2Multipliers, defaultValueToGp2Multiplier,
             incrementality: { classes, overrides, shareWeights } },
  columns, rows, rowCount, runDates, truncated,
  shares:  { columns, rows, runDates } | null,
  actuals: { columns, rows, runDates } | null,
  status,                                  // additive
  droppedDatasets                          // additive: [{runDate, datasets:[…]}]
}
```

The two additive keys are new and ignored by anything that predates them. `status` is the
machine-readable state (see the contract table). `droppedDatasets` names any dataset or
account a collection had to drop to fit the storage budget — almost always `[]`, and the
trust panel surfaces it when it is not, so a missing account can never look like an
account that simply had no data.

The three column grids are identical to `HEADERS` / `SHARE_HEADERS` / `ACTUAL_HEADERS` in
the Ads script, and the per-column normalisation is the same: `toMetricOrZero` on `Raw`
(a blank `Current Target Roas` reaches the page as `0`, which `parseRoas()` depends on),
`toMetric` on `Actuals` (a blank stays blank; a measured `0` stays `0`), `toShare` on
`Shares` (blank **and** a literal `0` both become blank).

### The access-key gate on a same-origin endpoint

The page keeps an access key in `localStorage` and sends it as `?token=`. That gate was
built against a **public** Apps Script URL, where it was the only lock. The same-origin
route is already behind the dashboard's HTTP Basic auth, so the key is now a second,
optional deterrent:

- `ROAS_SIMS_TOKEN` **set** on the service → the key is enforced, and a mismatch returns
  the same `{"error": "Unauthorized"}` body the Apps Script returned, which is exactly what
  makes the page drop the key and re-prompt.
- `ROAS_SIMS_TOKEN` **unset** → any key is accepted. The page still asks once and remembers
  it; nothing about the UX changes.

Presence of the env var is the **only** switch — `DEV_MODE` deliberately does not bypass
it. The live service runs with `DEV_MODE=true`, so a `DEV_MODE` short-circuit would mean
setting `ROAS_SIMS_TOKEN` silently did nothing, which is the opposite of what setting it
asks for. There is a regression test for exactly that.

**Where the key travels.** On the same-origin route the page sends it as the
`X-Roas-Sims-Key` request header, not `?token=`: Cloud Run logs the full request URL, so a
key in the query string would be written into Cloud Logging on every page load. The server
reads the header first and falls back to `token=`, because Apps Script `doGet` only ever
sees `e.parameter` and never headers — dropping the param would break the rollback.

**When the gate is shown.** With `ROAS_SIMS_TOKEN` unset (the documented default) the page
no longer demands a key before its first fetch on the same-origin route; it fetches with an
empty key and only shows the gate if the server actually answers `Unauthorized`. Making a
first-time viewer invent a string the server accepts regardless was pure friction. The
legacy endpoint always requires a token, so it still prompts up front.

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

## Setup — the API pipeline (current)

### Credential prerequisites

Five values, all of them one-time. Nothing here is per-account; one MCC-level OAuth
identity reads every child account.

**1. Developer token** — MCC → **Tools & Settings → Setup → API Center**. Apply for
**Basic access** (Test access only sees test accounts, which have no simulations). Basic
allows 15 000 operations/day, orders of magnitude more than this collector's ~40 queries
a day. Approval is typically same-day to a few days; the token is visible on that page
once granted. The token belongs to the **manager** account, not a child.

**2 & 3. OAuth client id + secret** — Google Cloud console → **APIs & Services →
Credentials → Create credentials → OAuth client ID → Desktop app**. Use the project that
also has the **Google Ads API** enabled (`gcloud services enable googleads.googleapis.com`).
Desktop-app is the right type: this is a one-off local flow to mint a refresh token, not a
hosted web login, so no redirect URI has to be registered anywhere.

**4. Refresh token** — mint it **once, locally**, signed in as a Google account with access
to the MCC. The `google-ads` library ships the flow:

```bash
python3 -m venv /tmp/ads && /tmp/ads/bin/pip install google-ads==31.2.0
curl -sO https://raw.githubusercontent.com/googleads/google-ads-python/main/examples/authentication/generate_user_credentials.py
/tmp/ads/bin/python generate_user_credentials.py \
  --client_id=<CLIENT_ID> --client_secret=<CLIENT_SECRET>
```

It prints a URL, you consent in the browser, paste the code back, and it prints the refresh
token. Refresh tokens for a **published** OAuth app do not expire; one still in "Testing"
expires after 7 days, so publish the consent screen (Internal is fine) before minting the
one you deploy. Scope is `https://www.googleapis.com/auth/adwords`.

**5. Login customer id** — the **MCC's** customer id, digits only (the collector strips
dashes anyway). It is what tells the API "read these children through this manager".

Verify all five before deploying them — a bad credential is far cheaper to find here:

```bash
GOOGLE_ADS_DEVELOPER_TOKEN=... GOOGLE_ADS_CLIENT_ID=... GOOGLE_ADS_CLIENT_SECRET=... \
GOOGLE_ADS_REFRESH_TOKEN=... GOOGLE_ADS_LOGIN_CUSTOMER_ID=... \
SKIP_FIRESTORE=1 python3 refresh_roas_sims.py
```

That runs the whole collection and prints a per-account line without touching Firestore.

### One-time setup commands

`pipeline/setup-roas-sims.sh` does all of it and is idempotent — re-running only fills
gaps. It resolves the service's runtime service account itself rather than assuming one.

```bash
export GOOGLE_ADS_DEVELOPER_TOKEN=...
export GOOGLE_ADS_CLIENT_ID=...
export GOOGLE_ADS_CLIENT_SECRET=...
export GOOGLE_ADS_REFRESH_TOKEN=...
export GOOGLE_ADS_LOGIN_CUSTOMER_ID=1234567890      # the MCC id, digits only
export INTERNAL_TOKEN=...                           # the value already on the service
cd apps/babyshop-dashboard && ./pipeline/setup-roas-sims.sh
```

The commands it runs, if you would rather do them by hand:

```bash
PROJECT=project-a7ade44e-e7e3-4871-a83
REGION=europe-north1
SERVICE=babyshop-dashboard

# which identity does the service actually run as?
RUNTIME_SA=$(gcloud run services describe $SERVICE --project=$PROJECT --region=$REGION \
  --format='value(spec.template.spec.serviceAccountName)')
# empty means the default compute SA: <project-number>-compute@developer.gserviceaccount.com

# 1. five secrets
for N in GOOGLE_ADS_DEVELOPER_TOKEN GOOGLE_ADS_CLIENT_ID GOOGLE_ADS_CLIENT_SECRET \
         GOOGLE_ADS_REFRESH_TOKEN GOOGLE_ADS_LOGIN_CUSTOMER_ID; do
  gcloud secrets create $N --project=$PROJECT --replication-policy=automatic
  printf '%s' "${!N}" | gcloud secrets versions add $N --project=$PROJECT --data-file=-
  gcloud secrets add-iam-policy-binding $N --project=$PROJECT \
    --member="serviceAccount:${RUNTIME_SA}" --role=roles/secretmanager.secretAccessor
done

# 2. verify the bindings actually landed — never trust add-iam-policy-binding's own output
for N in GOOGLE_ADS_DEVELOPER_TOKEN GOOGLE_ADS_CLIENT_ID GOOGLE_ADS_CLIENT_SECRET \
         GOOGLE_ADS_REFRESH_TOKEN GOOGLE_ADS_LOGIN_CUSTOMER_ID; do
  gcloud secrets get-iam-policy $N --project=$PROJECT \
    --flatten="bindings[].members" \
    --filter="bindings.members:serviceAccount:${RUNTIME_SA}" \
    --format="value(bindings.role)" | sort -u
done

# 3. mount them (--update-secrets, never --set-secrets: that would drop existing ones).
#    --timeout=900 because a full collection is ~40 GAQL round trips.
gcloud run services update $SERVICE --project=$PROJECT --region=$REGION --timeout=900 \
  --update-secrets=\
GOOGLE_ADS_DEVELOPER_TOKEN=GOOGLE_ADS_DEVELOPER_TOKEN:latest,\
GOOGLE_ADS_CLIENT_ID=GOOGLE_ADS_CLIENT_ID:latest,\
GOOGLE_ADS_CLIENT_SECRET=GOOGLE_ADS_CLIENT_SECRET:latest,\
GOOGLE_ADS_REFRESH_TOKEN=GOOGLE_ADS_REFRESH_TOKEN:latest,\
GOOGLE_ADS_LOGIN_CUSTOMER_ID=GOOGLE_ADS_LOGIN_CUSTOMER_ID:latest

# 4. confirm the new revision is the one serving traffic
gcloud run services describe $SERVICE --project=$PROJECT --region=$REGION \
  --format='value(status.latestReadyRevisionName,status.traffic[0].revisionName)'

# 5. daily refresh
URL=$(gcloud run services describe $SERVICE --project=$PROJECT --region=$REGION \
      --format='value(status.url)')
gcloud scheduler jobs create http roas-sims-daily \
  --project=$PROJECT --location=$REGION \
  --schedule="30 5 * * *" --time-zone="Europe/Stockholm" \
  --uri="$URL/internal/refresh-roas-sims" --http-method=POST \
  --headers="X-Internal-Token=${INTERNAL_TOKEN}" \
  --attempt-deadline=900s
```

> **Why the secrets are not in `cloudbuild.yaml`.** `gcloud run deploy
> --update-secrets=K=s:latest` **fails outright** when the secret does not exist yet, and a
> failed deploy silently leaves the previous revision serving traffic. Referencing the
> `GOOGLE_ADS_*` secrets from the build would therefore break every deploy of this app
> between merging the collector and creating them — exactly the window this change is
> designed to survive. It is also what the service already does: `DASH_PASS`, `FUNNEL_*`
> and `INTERNAL_TOKEN` are wired the same way, out of band, which is why the deploy step
> uses `--update-env-vars` rather than `--set-env-vars`. `cloudbuild.yaml` carries a
> comment saying so.

### Verify on the first live run

Two things have no live precedent and are worth eyeballing once, the first time real
credentials are in place:

- **The campaign-name join.** The simulation queries select no attributed resource and join
  `campaign_simulation.campaign_id` to a separately-fetched campaign map in Python. If that
  map ever misses an id the row still renders, but under the display name `campaign <id>`
  rather than a real campaign name. Check row 0 of the payload:

  ```bash
  curl -s -H "X-Roas-Sims-Key: $KEY" "$URL/api/roas-sims?runs=1" \
    | python3 -c 'import json,sys; p=json.load(sys.stdin); print(p["columns"]); print(p["rows"][0])'
  ```

  Column 1 (`Bidding Strategy Name`) must be a real campaign name. A `campaign 217003…`
  there means the map missed and the name-based join fallback is degraded — the id join
  still works, so nothing is wrong, but it is worth knowing. The prefix is deliberate: it
  can never be mistaken for a real campaign name by the dashboard's `(account, name)`
  fallback join.
- **Row volume vs. the storage budget.** `counts.raw` on the refresh response is the day's
  simulation-point count. Anything under ~5 200 fits comfortably; if it is higher, check
  `droppedDatasets` in the payload — see `DATASET_BUDGET_BYTES`.

### Smoke test

```bash
URL=$(gcloud run services describe babyshop-dashboard --region=europe-north1 \
      --format='value(status.url)')
# POST only — there is no GET route on a paid-quota endpoint.
curl -sX POST -H "X-Internal-Token: $INTERNAL_TOKEN" "$URL/internal/refresh-roas-sims"
# The access key goes in a header, not the query string (Cloud Run logs full URLs).
curl -s -H "X-Roas-Sims-Key: $KEY" "$URL/api/roas-sims?runs=1"
```

Before the credentials exist, the first returns `503 {"status":"not-configured"}` naming
the missing variables and the second returns `{"status":"no-snapshots"}` — the dashboard
keeps its cached or demo payload and neither is mistaken for an auth failure.

### Editing the config

`roas_sim_config/config` replaces the sheet's `Config` tab and is edited the same way it
always was — **by a human, with no deploy**. `seed_config()` creates it at the end of the
first successful refresh (the write path, so serving a payload never needs write
permission) with the defaults this document describes (brand `0.20`, private-label `0.50`, generic `1.00`, the four share
bounds, and multiplier `1.0`). Edit it in the Firestore console, or over REST:

```bash
TOKEN=$(gcloud auth print-access-token)
PROJ=project-a7ade44e-e7e3-4871-a83
BASE="https://firestore.googleapis.com/v1/projects/$PROJ/databases/(default)/documents"

# read it
curl -sH "Authorization: Bearer $TOKEN" "$BASE/roas_sim_config/config"

# change one factor: brand class default 0.20 -> 0.15
curl -sX PATCH -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  "$BASE/roas_sim_config/config?updateMask.fieldPaths=incrementality.classes.brand" \
  -d '{"fields":{"incrementality":{"mapValue":{"fields":{"classes":{"mapValue":{"fields":
      {"brand":{"doubleValue":0.15}}}}}}}}}'
```

Values are parsed as forgivingly as the spreadsheet cells were: `0.2`, `"0,2"`, `20` and
`"20%"` all mean 0.20. An unreadable value falls back to the default instead of poisoning
the payload, exactly like a free-text comment row in the tab. Unknown class names and
unknown `shareWeights` keys are ignored; per-campaign overrides are
`{"pattern": "...", "factor": ...}` entries in `incrementality.overrides`.

### Dashboard wiring

At the top of the `<script>` block in `babyshop-roas-simulations.html`:

```js
const LEGACY_SHEET_ENDPOINT = 'https://script.google.com/macros/s/.../exec';
const DATA_ENDPOINT = '/api/roas-sims';
```

**To fall back to the sheet, change the second line to `= LEGACY_SHEET_ENDPOINT;`.** That
is the whole edit — the payload shape is identical, so nothing downstream changes. Leave
`DATA_ENDPOINT` empty and the page runs on `DEMO_DATA`, as it always has.

### Operational notes

- The collector needs `roles/datastore.user` on the runtime SA (Firestore) — the service
  already has it — plus `secretAccessor` on the five new secrets.
- Rotating a credential needs a **new revision**: Cloud Run caches secrets at revision
  start, so `gcloud secrets versions add` alone changes nothing on the running service.
  `gcloud run services update babyshop-dashboard --region=europe-north1
  --update-labels="secret-rotation=$(date +%s)"` forces one.
- `ROAS_SIMS_INCLUDE_PORTFOLIO=true` turns on portfolio-level simulations (off by default,
  mirroring the Ads script: those campaigns already appear in `campaign_simulation`, so
  collecting both double-counts the same auction traffic in a cross-strategy total).
  `ROAS_SIMS_INCLUDE_CAMPAIGNS`, `ROAS_SIMS_COLLECT_SHARES`, `ROAS_SIMS_COLLECT_ACTUALS`
  and `ROAS_SIMS_RETENTION_DAYS` are the other knobs, all with the Ads script's defaults.

## Setup — the sheet pipeline (legacy / fallback)

> Still deployed, still working, still the fallback. Nothing below was removed; keep the
> Ads script scheduled while the API path proves itself, and the two histories accrue
> independently.

### 1. Google Ads script

1. MCC → **Tools & Settings → Bulk actions → Scripts → +**.
2. Paste `pipeline/gp3-simulations.js`.
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

1. In the spreadsheet: **Extensions → Apps Script**, paste `pipeline/webapp.gs`.
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

To point the page back at the sheet, set `DATA_ENDPOINT` to `LEGACY_SHEET_ENDPOINT` (see
*Dashboard wiring* above) and enter the Apps Script `SCRIPT_TOKEN` in the page's key gate.

Leave `DATA_ENDPOINT` empty and the page runs on `DEMO_DATA` — one real Babyshop SE snapshot, 92
simulation points across 9 strategies — and labels itself **Demo data** throughout. That block
carries two **illustrative** impression-share rows (the SE brand campaign at 0.93 absolute-top,
`pb-product` at 0.71 search IS) so the dynamic path is exercised in demo mode; unlike the
simulation rows, those two are made up. It also carries an **invented `actuals` block**, one row
per campaign per run, deliberately bent off the simulated curve (cost ×0.97, click-time value
×0.94, conversion-time value a further ×1.01) so the actual-vs-simulated gap is visible rather
than suspiciously exact. `demo-search-pb-bundle` has **no** actuals row on purpose, so the
missing-measurement path renders too.

### 4. Hosting (historical)

The page originally shipped on GitHub Pages from a standalone repo. It now lives in
`apps/babyshop-dashboard/` and deploys with the rest of the service: push to `main`, the
`babyshop-dashboard-main` Cloud Build trigger builds and deploys, and a final `promote`
step routes traffic. The page itself is still one self-contained file; Chart.js and the
fonts come from CDNs.

## Security

**On the API pipeline the page-level gate is no longer the only lock.** `/api/roas-sims`
is same-origin and sits behind the dashboard's own HTTP Basic auth (`DASH_USER` /
`DASH_PASS`) like every other `/api/*` route, and `/internal/refresh-roas-sims` is behind
`INTERNAL_TOKEN`. The access key is now an optional second deterrent, enforced only when
`ROAS_SIMS_TOKEN` is set. Note the service currently runs with `DEV_MODE` bypassing Basic
auth — setting `DEV_MODE=false` with a strong `DASH_PASS` is what makes that lock real.

What genuinely is new and worth guarding: **the five Google Ads credentials**. They live
only in Secret Manager, are read only by the runtime service account, and grant read
access to the whole MCC. Never put them in an env var literal, `cloudbuild.yaml`, or this
repository. Rotating one requires a new revision (Cloud Run caches secrets at revision
start) — see *Operational notes*.

On the legacy path, **the login gate and the token are deterrents, not authentication:**

- `GATE_HASH` sits in the page source. Anyone who views source can extract it and attack
  it offline; the password is not secret.
- The Apps Script `/exec` URL is public. Its token stops crawlers and accidental hits, not
  a determined reader. Anyone with the URL can read the simulation data.
- That endpoint is read-only and exposes aggregate simulation figures only — no customer
  data, no credentials, no write path back into Google Ads.

Either way, **the data is never committed to this repository.** The repo holds code and
one demo snapshot; everything live is fetched at runtime and cached only in the viewer's
own browser or in Firestore.

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
apps/babyshop-dashboard/
  babyshop-roas-simulations.html   the dashboard (single file, no build step)
  refresh_roas_sims.py             API collector + snapshot reader + payload builder
  app.py                           /api/roas-sims, /internal/refresh-roas-sims
  requirements.txt                 google-ads pinned here
  cloudbuild.yaml                  build/deploy (deliberately no --update-secrets)
  pipeline/                        NOT deployed — dockerignored
    PIPELINE.md                    this file
    setup-roas-sims.sh             the one-time setup, idempotent
    gp3-simulations.js             LEGACY Google Ads MCC collector (Raw+Shares+Actuals)
    webapp.gs                      LEGACY Apps Script JSON endpoint
```

`pipeline/` is in `.dockerignore`, so nothing in it ships in the image — it is
documentation and operator tooling. `refresh_roas_sims.py` sits at the app root precisely
because it *does* need to be in the image: `/internal/refresh-roas-sims` imports it.

## Testing

The payload builder is pure and takes an injectable `db`, so the whole read path runs
against a mock Firestore with no GCP access. What is covered:

- **payload-shape equivalence** — the served keys, sub-shapes and column grids checked
  against the `webapp.gs` contract, plus a replay of the dashboard's own `COLUMN_MAP` to
  prove every column still binds to a field it consumes;
- **blank-vs-0** — `Shares` blank *and* literal `0` → blank; `Actuals` measured `0` stays
  `0` while unreported stays blank; `Raw` unset current target → `0`; and the sv-SE
  decimal-comma cases (`"45,125"`, `"4 523,456"`, `"1.234,5"`);
- **idempotency + pruning** — three writes of one run date produce one document, a
  shrinking payload leaves no stale keys, the 90-day boundary day is kept, non-date
  document ids are never touched, pruning is idempotent;
- **missing credentials** — the error names each unset variable, whitespace-only counts as
  unset, and the empty-collection payload is distinguishable from an auth failure;
- **`?runs` / `?account`** — including the shares-default-to-latest asymmetry, whole-run
  `MAX_ROWS` truncation, and that `get_all()`'s arbitrary order is never trusted;
- **config** — seeding, the forgiving number parsing, and that garbage falls back cleanly;
- **protobuf conversion** — against real `google-ads` messages, including `HasField`
  presence vs. a reported `0.0`;
- **GAQL field names** — every `campaign_simulation.*` / `metrics.*` / `customer.*`
  reference in the module is checked to exist on the installed `google-ads` package.
