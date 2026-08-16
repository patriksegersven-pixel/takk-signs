# Customer Insights pipeline

Everything behind the Customer Insights tab: what runs, what has to be created
before the Norce half works, and how to backfill it.

**Status (2026-08-16):** the Funnel half is LIVE — a real snapshot is in
Firestore and the tab renders from it. The Norce half is BUILT BUT NOT RUN:
`NORCE_CLIENT_ID` / `NORCE_CLIENT_SECRET` do not exist yet, so
`data.norce` is `null` and the tab shows its Norce sections as awaiting data.
Nothing fails; nothing has been created in BigQuery.

## Pieces

| File | Role |
|---|---|
| `norce_sync.py` | Norce OData v4 Query API → BigQuery `norce` dataset. Backfill + nightly incremental. Hashes the email at extract; honours `IsForgotten`. |
| `norce_marts.sql` | The five mart views `norce_sync.py` applies. **App root, not `pipeline/`** — `.dockerignore` excludes `pipeline/`, so anything in there is missing from the image at runtime. |
| `refresh_customer_insights.py` | BigQuery → the Firestore snapshot the tab reads. Funnel side always; Norce side only if the marts exist and have rows. |
| `pipeline/setup-customer-insights.sh` | One-time secrets / IAM / jobs / scheduler wiring. **Not yet run.** |
| `/api/customer-insights` (in `app.py`) | Serves the snapshot to the page. Owned by the dashboard, not by this pipeline. |

## Data sources

**Funnel export** — `babyshop-funnel-data.bs_funnel_export.funnel_data`, EU,
month-partitioned on `Date`, live since 2025-01. Read with the same ADC /
gcloud-fallback client as `bq_source.py`.

**Norce Commerce** — prod OData v4 Query API at
`https://babyshop.api-se.norce.tech`, landed into
`project-a7ade44e-e7e3-4871-a83.norce` (EU, so it joins the Funnel export
without a cross-region copy).

### Rules that are load-bearing (from the data audit — do not "simplify" these)

- **The Funnel customer columns are BANNED.** `newCustomers__File_Import`,
  `oldCustomers__File_Import` and `Orders_count__File_Import` are a
  pre-aggregated file import and they are wrong: measured against Norce
  first-orders they undercount **DK by ~80×** and **SE by ~9×**, which produced
  a DK CAC of ~17,000 kr where the truth is ~185 kr. Norce is the customer
  source of record. `refresh_customer_insights.py` reads those columns nowhere.
- **Funnel contributes cost only** (plus sessions).
- Market is **`market_level_1_kv`**. Never `market_new` — NULL on 98.8% of cost rows.
- The Funnel export is a **union of disjoint feeds**. Cost is aggregated in its
  own query at its own grain and joined to Norce on month × market — never at
  row level.
- **Cost is market-grain, not shop-grain.** Cost rows do carry `shop_new`, but
  not reliably: FI × Lekmer reports 77 kr over 90 days against 205 Norce
  first-orders — a CAC of 0.38 kr. All CAC is therefore market-level, and
  market × shop rows carry it on the `babyshop` row only (that shop is ~93% of
  spend); `lekmer` rows carry `null`.
- **Channel-level CAC is impossible today.** Norce has no channel/UTM data
  (`Source='WEB'` always) and the Funnel export has no order identity, so
  nothing links an acquisition to the channel that paid for it. `channel_spend`
  reports spend only. Order-level linkage is expected via a **Kuvio API** — the
  seam is `channel_spend()` in `refresh_customer_insights.py`; add the join and
  fill its `cac` field then.
- Norce order value = `SUM(Items.LineAmount)` **excluding the shipping line
  `PartNo='1000014'`**. There is no order-total field. Amounts are **ex-VAT**
  (the header `VatRate` is 0; real VAT is per line).
- **No status filtering anywhere.** User decision: returns and cancellations are
  ignored, all values are gross.
- **No 2- or 3-year CLV.** 1-year only, plus the cohort maturity curves.

## Identity and PII

Norce has no stable customer id — `BuyerCustomerId` is null on 100% of orders.
The identity is `Buyer.EmailAddress` (100% populated), reduced at extract time to

```
customer_hash = SHA256(LOWER(TRIM(email)))     hex
```

The raw email never reaches BigQuery, a log line or a local file. The request
asks for the minimum — `$expand=Buyer($select=EmailAddress,CountryId,ZipCode)` —
so no name, phone or street address is even transferred.

`IsForgotten` is Norce's GDPR erasure flag. **Every** run re-reads the forgotten
orders across all applications and DELETEs their header and line rows: an order
can be flagged long after its last update, so scanning only the delta would miss
it. The whole order goes, not just the hash — a header with the hash blanked
would still leave the basket linkable.

After the first backfill, verify nothing leaked:

```bash
bq show --schema --format=prettyjson project-a7ade44e-e7e3-4871-a83:norce.orders
```

## Secrets to create

| Secret | Where it comes from |
|---|---|
| `NORCE_CLIENT_ID` | Norce Admin → Settings → Users → OAUTH |
| `NORCE_CLIENT_SECRET` | same integration user |

The token endpoint is `POST {base}/identity/1.0/connect/token` with
`grant_type=client_credentials` and `scope=prod` (`scope` is the **environment**,
not a permission — API access comes from the resources enabled on the
integration user). Tokens live 3600 s; the token endpoint allows 3 requests per
minute per IP, so `norce_sync.py` caches one token per run.

Until both secrets are set, `python3 norce_sync.py` prints

```
⚠️  Norce sync skipped — missing env var(s): NORCE_CLIENT_ID, NORCE_CLIENT_SECRET
```

and exits **0 without creating the dataset**. A scheduled run must not page
anyone over a credential that was never issued.

## IAM

The jobs run as the dashboard's runtime service account (currently the default
compute SA, `871631085269-compute@developer.gserviceaccount.com`).

| Role | On | Why |
|---|---|---|
| `roles/secretmanager.secretAccessor` | each `NORCE_*` secret | read the credentials |
| `roles/bigquery.jobUser` | `project-a7ade44e-e7e3-4871-a83` | run load / MERGE / view DDL |
| `roles/bigquery.dataEditor` | `project-a7ade44e-e7e3-4871-a83` | **project level, not dataset level** — `norce_sync` creates the dataset itself on first run, so there is nothing to grant on yet |
| `roles/datastore.user` | project | write the Firestore snapshot (already held) |
| BigQuery read | `babyshop-funnel-data` | separate project; already granted, since `bq_source.py` reads it daily |

Audit what the SA really holds — never trust the output of
`add-iam-policy-binding` (CLAUDE.md, "Auditing what roles a service account
actually has"):

```bash
gcloud projects get-iam-policy project-a7ade44e-e7e3-4871-a83 \
  --flatten="bindings[].members" \
  --filter="bindings.members:serviceAccount:871631085269-compute@developer.gserviceaccount.com" \
  --format="value(bindings.role)" | sort -u
```

## Scheduling

Two **Cloud Run jobs**, not `/internal/*` endpoints. The ROAS collector is an
endpoint because one collection is ~30 s; a Norce backfill is ~416k orders
across 13 applications, far past Cloud Run's request ceiling — and Scheduler's
retry-on-timeout, which is harmless for a 30 s job, would start a second
hour-long backfill on top of the first. Both jobs reuse the dashboard's image,
so they ship on the same Cloud Build.

| Job | Command | Schedule | Timeout |
|---|---|---|---|
| `norce-sync` | `python3 norce_sync.py` | 03:00 Europe/Stockholm | 6 h |
| `customer-insights-refresh` | `python3 refresh_customer_insights.py` | 03:30 Europe/Stockholm | 30 min |

Cloud Scheduler is **not offered in europe-north1** — these jobs live in
`europe-west1`, same as the existing `roas-sims-daily`
(`pipeline/SETUP-STATUS.md`). Cloud Run *jobs* are triggered through the Admin
API `:run` endpoint with an **OAuth** token, not the OIDC token an HTTPS service
takes.

The snapshot's `ttl_seconds` is 30 days — the same value `bq_source.py` and
`refresh_roas_impact.py` use. Far above the daily cadence, so `expires_at` can
never lapse between runs and blank the tab.

## Runbook: first backfill

Run once, after `./pipeline/setup-customer-insights.sh` has created the secrets
and jobs.

```bash
PROJECT=project-a7ade44e-e7e3-4871-a83
REGION=europe-north1

# 1. One market first — cheap, and it proves the auth, the schema and the marts.
gcloud run jobs execute norce-sync --project=$PROJECT --region=$REGION \
  --args=norce_sync.py,--backfill,--apps,babyshop-se --wait

# 2. Check what landed.
bq query --project_id=$PROJECT --location=EU --nouse_legacy_sql \
  'SELECT market, COUNT(*) orders, COUNT(DISTINCT customer_hash) customers,
          MIN(order_date) first_order, MAX(order_date) last_order,
          ROUND(SUM(order_value)) revenue
   FROM `project-a7ade44e-e7e3-4871-a83.norce.customer_orders`
   GROUP BY market ORDER BY orders DESC'

# 3. All 13 applications. ~1-2 h.
gcloud run jobs execute norce-sync --project=$PROJECT --region=$REGION \
  --args=norce_sync.py,--backfill --wait

# 4. Rebuild the snapshot so the tab picks up the Norce sections.
gcloud run jobs execute customer-insights-refresh --project=$PROJECT --region=$REGION --wait
```

From then on the nightly job runs incrementally off the watermark in
`norce.sync_state` — filtered on `Updated` (falling back to `Created`, which
matters because `Updated` is nullable). The watermark is the time the run
*started*, never `max(Updated)`: an order updated mid-run would otherwise be
skipped forever. Each run re-reads a 6 h overlap (`NORCE_WATERMARK_LAP_H`); the
MERGE makes that idempotent.

### Local runs

```bash
cd apps/babyshop-dashboard

# Funnel-only snapshot, no Firestore write — inspect the JSON first.
SKIP_FIRESTORE=1 CUSTOMER_INSIGHTS_OUT=/tmp/ci.json python3 refresh_customer_insights.py

# Write it for real (gcloud fallback auth).
python3 refresh_customer_insights.py

# Re-apply only the mart views after editing norce_marts.sql.
python3 norce_sync.py --marts-only
```

## The marts

All five are **views** — ~416k orders is small enough that materialising buys
nothing but a staleness bug. `${DATASET}` is substituted by `norce_sync.py`.

| View | Grain | Notes |
|---|---|---|
| `customer_orders` | one row per order | `order_value` = line sum excluding `PartNo='1000014'`. `market`/`shop` derived from `app_key` (`babyshop-se` → `babyshop` / `SE`), so the codes line up with `market_level_1_kv`. |
| `customer_facts` | customer_hash × shop | `revenue_365d_from_first` stays NULL until the customer's own 365 days have elapsed. `migration_window_flag` marks first orders before 2025-09-11. |
| `monthly_customer_metrics` | month × market × shop | `new_customers` = first-ever order that month, so a customer is new exactly once. |
| `cohort_retention` | cohort_month × market × shop × months_since_first | The CLV maturity triangle. 1-year CLV = `cumulative_revenue_per_customer` at offset 12. Offsets are generated densely up to each cohort's elapsed age. |
| `first_purchase_products` | month × market × shop × dim_type × dim_value | `dim_type` is `brand` / `category_l1` / `product`. Counts first orders *containing* the thing, not units. |

### Product names — resolution order

Norce's `Product.DefaultName` is **variant-grain**, so on its own the product
dimension reads `2-4 Y` / `One Size` / `86/92 cm` instead of a product. The mart
resolves a name best-source-first:

```sql
COALESCE(sku_titles.title,        -- 1. Channable feed
         variants.DefaultName,    -- 2. the real PARENT product  <- workhorse
         products.DefaultName,    -- 3. variant label
         order_items.ProductName, -- 4. variant label
         order_items.PartNo)      -- 5. never NULL
```

**`norce.variants`** (`Id`, `DefaultName`) is the primary source.
`Products/Product.VariantId → Variants.Id`, and `Variants.DefaultName` is the
real product — e.g. products 501 (`90 cm`) and 502 (`120 cm`) both carry
`VariantId 77` = **"Pointelle Dress Dusty Violet"**, so every size collapses under
one title. Measured on the live tenant: 133,970 products, only **14** with a NULL
`VariantId`, 26,661 variants. Note `Variants.DefaultTitle` is null in practice —
use `DefaultName`.

**`norce.sku_titles`** (`PartNo`, `title`, `brand`) comes from the Channable
Google Shopping feed, whose `g:id` **is** `OrderItem.PartNo`. It is preferred
when present because it is the merchandiser-facing title, but it is the
**current catalogue only** and skews Babyshop SE.

Measured coverage of first-order lines (664,228 lines, shipping excluded):

| Source | Coverage |
|---|---|
| Channable `sku_titles` | **34.4%** |
| Norce product row → variant parent | **95.7%** |
| Neither (falls back to variant label / PartNo) | ~4.3% |

Both are joined on the raw order line where possible, so a SKU missing from one
source still resolves through the other. Empty tables degrade to the old
behaviour rather than blanking the dimension.

`sync_variants()` and `sync_sku_titles()` run in the product pass.
`sync_sku_titles` reuses `inventory_client`'s config and streaming iterparse
(~30 MB peak on a 96 MB document) and needs **`CHANNABLE_FEED_URL`** — mounted on
the dashboard *service* but **not** on the `norce-sync` job. Without it the step
logs and returns 0.

```bash
# products + variants + dimensions + titles + marts, skipping the ~30 min orders phase
python3 norce_sync.py --products-only

# reload titles only, or re-apply marts only (neither needs Norce credentials)
python3 norce_sync.py --titles-only
python3 norce_sync.py --marts-only
```

`--products-only` forces a **full** product re-pull (never incremental): the
extract shape changed when `VariantId` was added, so a delta pass would leave
every untouched product with a NULL `VariantId` and no parent name. It leaves
the orders watermark alone, so the next normal run still picks up the orders it
skipped.

Identity is scoped **per shop** — Babyshop and Lekmer are separate businesses,
and the same address shopping in both is two customers.

### Norce join notes

- `OrderItem.PartNo` → `product_skus.PartNo` → `products.ManufacturerId` →
  `dim_manufacturers.Name` for brand.
- Primary `product_categories` row → `dim_categories.DefaultFullName`, which is
  `"Ecom - <L1> - <L2> - …"`, so **L1 is the segment after the root**.
- `OrderItem.ProductName` is the variant label ("Foggy White Blueberry-50 cm") —
  join to `products.DefaultName` for a real product name.
- `OrderItems` and `ProductCategories` have **no standalone entity set** (they
  404); the only way to get them is `$expand` from their parent.
- `Application/*` and `Core/*` lookups have no `ApplicationId` property — they
  are tenant-wide and 400 if you filter them per application.
- `Reporting/OrderSummaryByMonth` and `Core/KpiOrders` both return HTTP 500.
  Everything is built from raw `Orders`.

## Applications (market × shop)

| Key | ApplicationId | | Key | ApplicationId |
|---|---|---|---|---|
| babyshop-se | 1244 | | babyshop-uk | 1269 |
| babyshop-no | 1264 | | babyshop-row | 1270 |
| babyshop-fi | 1265 | | babyshop-asia | 1271 |
| babyshop-dk | 1266 | | lekmer-se | 1272 |
| babyshop-eu | 1267 | | lekmer-no | 1273 |
| babyshop-na | 1268 | | lekmer-fi | 1274 |
| | | | lekmer-dk | 1275 |

Norce history starts **2025-06-11** (platform cutover) — nothing earlier exists.

## Firestore snapshot

`funnel_cache/-Ln87GcdqU9CMJV6zMBY__customer-insights`, wrapped as
`{data, fetched_at, expires_at, ttl_seconds, workspace}` like every other
snapshot. Read it ad hoc with the REST API (CLAUDE.md, "Operational" — `gcloud`
has no `firestore documents` subcommand):

```bash
TOKEN=$(gcloud auth print-access-token)
PROJ=project-a7ade44e-e7e3-4871-a83
curl -sH "Authorization: Bearer $TOKEN" \
  "https://firestore.googleapis.com/v1/projects/$PROJ/databases/(default)/documents/funnel_cache/-Ln87GcdqU9CMJV6zMBY__customer-insights"
```

`data.norce` is `null` until the sync has produced rows — and "the dataset
exists" is deliberately not enough, since `norce_sync` creates its tables before
it loads anything and a half-finished first run would otherwise serve an
all-zero tab.

### Snapshot shape

Top level: `generated_at`, `sources`, `meta`, `kpis`, `trend`, `cac_matrix`,
`channel_spend`, `norce`, `caveats`.

| Section | Grain | Contents |
|---|---|---|
| `trend` | month × market × shop | Norce customers/orders/revenue **+ that month's market-level funnel `cost`, `sessions`, `cac`** |
| `cac_matrix` | market | `cost_90d`, `new/returning/total_customers_90d`, `cac` — the authoritative CAC surface |
| `channel_spend` | market × channel_l1 | `cost_90d`, `share_of_market_spend`, `cac: null` + `note` |
| `norce` | — | `cohorts`, `clv_1y_by_market`, `churn`, `first_purchase_products`, `last_sync` |

**`funnel.monthly` and `norce.monthly_customer_metrics` are gone** — both are
superseded by top-level `trend`, which is the same rows plus cost and CAC.

⚠️ `trend[].cost`, `trend[].sessions` and `trend[].cac` are **market-level** and
repeat across the shop rows of a market. **Do not sum them across shops.** The
customer columns are pure Norce and do sum correctly. `meta.trend_cost_is_market_level`
flags this in the payload.

A market with no recorded spend (EU, ASIA, NA) has `cac: null`, never `0.0` —
zero would claim acquisition there is free.

### Purchases-per-customer — three different denominators

| Field | Where | Definition |
|---|---|---|
| `orders_per_customer_12m` | `kpis`, `clv_1y_by_market[]`, `churn[]` | orders in the trailing 365 d ÷ distinct customers with ≥ 1 order in that window (**active base**) |
| `orders_per_customer` | `clv_1y_by_market[]`, `churn[]` | **lifetime** orders per customer over that row's cohort base (matured cohorts only) |
| `orders_per_customer_month` | `trend[]` | that month's orders ÷ that month's active customers |

The KPI is all shops. The per-row variants are keyed on `first_market`, so they
describe the same population as the row they sit on.

### Customer-count fields

`returning_customers` is `COUNTIF(is_repeat)` — customers with ≥ 2 orders —
computed over **that row's own `customers` base**, so it is always a subset of
the total beside it. The two Norce bases differ on purpose: `clv_1y_by_market`
counts customers whose own 365 days have elapsed, `churn` counts customers whose
first order is at least 365 days old. On `trend` and `cac_matrix`,
new + returning = total exactly.

### Document size

The snapshot is one Firestore document, hard-capped at 1,048,576 bytes.
`refresh_customer_insights.py` refuses to write above `DOC_BUDGET_BYTES`
(900,000 B of compact JSON) and names the biggest sections when it trips.

Measured 2026-08-16 after the Funnel demotion: compact JSON **444 KB** (was 690 KB
— dropping the `funnel.monthly` customer series more than paid for the Norce
trend). The check over-states Firestore's real accounting, so it fires before
Firestore would reject the write.

`norce.cohorts` is bounded to keep it that way: `months_since_first <= 12` (the
1-year CLV is the value at offset 12, and 2-/3-year CLV is ruled out) and
`cohort_size >= 50` (a retention rate off six customers is noise). Both are
tunable via `CUSTOMER_INSIGHTS_COHORT_MAX_OFFSET` / `_MIN_SIZE`.

**Known growth risk:** `funnel.monthly` (+~90 rows/month) and `norce.cohorts`
(+~170 rows/month even bounded) both grow indefinitely. At the current rate the
budget is reached in roughly a year. The fix then is a rolling window on both —
raising `DOC_BUDGET_BYTES` only moves the failure to the Firestore API.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `Norce sync skipped — missing env var(s)` | The secrets are not wired onto the job. Expected until setup runs. |
| Token exchange 400 | Wrong `scope`. Prod is `prod`; the host carries no environment segment. |
| HTTP 429 | Token endpoint is 3/min per IP. The job caches one token per run — suspect a retry loop. |
| Query API 400 "Could not find a property named 'ApplicationId'" | A tenant-wide lookup (`Application/*`, `Core/*`) was filtered per application. |
| Query API 404 on `OrderItems` / `ProductCategories` | Those sets do not exist standalone. `$expand` from the parent. |
| Marts missing after a deploy | `norce_marts.sql` moved back under `pipeline/`, which `.dockerignore` excludes. Keep it at the app root. |
| Tab shows no data between runs | `expires_at` lapsed — check `ttl_seconds` is still 30 days, not shrunk to the refresh interval. |
