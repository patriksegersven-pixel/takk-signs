#!/usr/bin/env python3
"""
Executive P&L snapshot — Business Central (cross-project) → Firestore.

Writes the single document the "Executive P&L" tab reads. Same client/auth
wiring and the same {data, fetched_at, expires_at, ttl_seconds, workspace}
wrapper as refresh_meta.py / refresh_customer_insights.py.

  apps/babyshop-dashboard/refresh_exec_pl.py   BigQuery (BC raw) -> Firestore
  GET /api/exec-pl                             what the tab reads

CROSS-PROJECT READ
  The BC tables live in the NEW stack's warehouse, claude-private-499703,
  dataset babyshop_raw, as bc_*. The query JOB is billed to project-a7ade44e
  (where the runtime SA holds bigquery.jobUser), so the SA needs only READ on
  that one dataset. That grant already exists: it was made for the Meta
  creatives tab (see pipeline/setup-meta.sh), which put
  871631085269-compute@developer.gserviceaccount.com on babyshop_raw as READER.
  Nothing new was needed here.

THE LADDER
  Every definition below is reconciled to Finance's own July 2026 management
  report. Do NOT re-derive them; they were tested against every plausible
  alternative and this is the only set that reproduces Babyshop's figures.

    Gross Sales     invoice lines, lineType='Item', EXCLUDING the SHIPPING
                    pseudo-SKU, FX-normalised to SEK.
    Returns         credit-memo product lines + the COMPENSATION pseudo-SKU.
                    EXCLUDES shipping returns (they belong to Net Shipping) and
                    RETURNFEE (Finance books that in Transaction Fees).
    Net Sales       Gross - Returns. The denominator for every percentage.
    Total COGS      GL 4006 + 4014 + 4037 + 4055. NOT the item ledger, which
                    runs ~3.2% off on posting cut-off. The item ledger is used
                    ONLY for market-level COGS, because it is the only basis
                    that can carry a country (via its document number).
    Net Shipping    (shipping revenue - shipping returns) netted against
                    GL 4336 + 4337 + 4338 + 4450. Packaging (4450) belongs here.
    Fulfillment     GL 4480 + 4481 + 4482 + 4497 + 4498 + 4499.
    Transaction fees GL 4372-4375. Nets to a credit in some months because
                    Walley's revenue share exceeds the Adyen/Amex fees.
    Total Marketing GL 5911 + 5913 + 5914 + 5916 + 5917 + 5990, EXCLUDING
                    consultants (5982, which sits in overhead). GL is primary;
                    de-duplicated Funnel spend is reconciling detail only.

    GP1 = Net Sales - COGS
    GP2 = GP1 - Net Shipping - Fulfillment - Transaction fees
    GP3 = GP2 - Marketing

  SIGN NOTE on transaction fees. The brief for this tab wrote
  "GP2 = GP1 - Net Shipping - Fulfillment + Transaction Fees". Both readings
  describe the same arithmetic: the GL block is carried here as
  (debit - credit), i.e. positive = expense, and GP2 SUBTRACTS it, so a credit
  balance (negative) raises GP2 exactly as a "+ Transaction Fees" income line
  would. Implemented the way that reproduces the validated numbers.

FX NORMALISATION IS MANDATORY
  Invoices are booked in document currency across six currencies. SEK per unit
  is relationalExchangeRateAmount / exchangeRateAmount from
  bc_currency_exchange_rates, joined on posting date inside the rate's validity
  window. Two traps, both live:
    • The rate table has NO SEK row at all. An inner join silently drops about
      57% of invoices. The join here is a LEFT join defaulting to 1.0.
    • RETURNFEE exists only on credit memos and is stored negative.
  Skipping FX understates gross by about 28%.

BELOW GP3
  Total Overhead is ten GL groups matching Babyshop's own forecast lines.
  Nine are enumerated; "Personnel costs HQ" is the 7210-7699 block EXCEPT 7213
  (customer service, its own line) and 7640 (interim consultants, which sits in
  Consultants - other). That rule was recovered by subset-sum against the
  reconciled July figure and reproduces YTD 2026 to 2 SEK; the prose
  documentation only said "7210 and the related personnel accounts".
  "Other overhead costs" is deliberately a RESIDUAL over 5000-6999 rather than
  an enumeration, so an account Finance opens next month lands somewhere visible
  instead of silently vanishing from the ladder.

  EBITDA is not built up from its parts; it is swept from the ledger:
      EBITDA = -(sum of every P&L account 3000-7999
                 except 7810 D&A and the FX block 7960/7983/7984)
  and "Other operating items" is then the plug that makes the ladder meet it:
      Other operating items = EBITDA - GP3 + Total Overhead
  That is what the line genuinely is (return-fee income, popup and intercompany
  sales, one-off external income, the slow-moving write-down, the inbound
  freight flow, and the basis differences between document lines and the GL
  revenue accounts). Building it as a plug means EBITDA and EBT tie to the
  ledger by construction instead of drifting as accounts are added. The
  documented components are itemised alongside it for display, and the gap
  between the itemised sum and the plug is published as a check, never hidden.

  D&A is GL 7810. Financial items are interest (8390/8413/8415) plus the FX
  block (7960/7983/7984/8329/8429). No tax line posts in 2026, so Net Income
  equals EBT rather than being estimated.

KNOWN GAP, published as a check rather than papered over
  GL 8020 and 8490 carry real money that NO ladder line claims (1 669 618 SEK
  YTD 2026). The prototype's EBT_GL_TIE rule passes on July only because
  neither account posts in July. Across 2026 the ladder's EBT sits that much
  above the booked GL net result. `checks.unclaimed_pl_accounts` lists every
  such account every run, so this surfaces instead of being rediscovered.

MARKET BREAKDOWN STOPS AT CONTRIBUTION — an explicit user decision
  BC holds logistics only at carrier-invoice granularity with no geography
  (shipmentMethodId is 100% empty), and overhead posts at company level. So the
  per-market ladder runs Gross -> Returns -> Net -> COGS (item ledger) ->
  Marketing -> Contribution and stops. Logistics stays group-level.
  `logistics_allocation` carries a flat allocation by order count purely as an
  indication, every figure flagged allocated=true, and it is shaped so a real
  per-order shipping cost can be dropped into `per_order_actual` later without
  restructuring the payload or the tab.

  bc_return_receipts is deliberately NOT used. Return VALUE comes from credit
  memos, which carry `sellToCountry`; the receipts table spells the same idea
  `sellToCountryRegionCode` and joining the two on the obvious name silently
  produces nulls. It is listed in `sources` so the omission is visible.

Run locally:
  # query BC as the kuvio account, dump the payload, write nothing
  CLOUDSDK_CORE_ACCOUNT=patrik@kuvio.io \\
    EXEC_PL_BQ_BILLING_PROJECT=claude-private-499703 \\
    SKIP_FIRESTORE=1 EXEC_PL_OUT=/tmp/exec_pl.json python3 refresh_exec_pl.py

  # write a previously dumped payload to Firestore, no BigQuery at all
  EXEC_PL_IN=/tmp/exec_pl.json python3 refresh_exec_pl.py
"""
from __future__ import annotations
import datetime, json, os, subprocess, time
from google.cloud import bigquery

# The warehouse holding the BC data (the NEW stack's project) …
DATA_PROJECT = os.environ.get("EXEC_PL_DATA_PROJECT", "claude-private-499703")
BC_DATASET   = os.environ.get("EXEC_PL_BC_DATASET", "babyshop_raw")
# … and the project the query JOB is billed to. Defaults to this dashboard's own
# project: the runtime SA has jobUser there and needs only dataViewer on the
# dataset above. Override to the data project when running locally as a kuvio
# user who has no rights in project-a7ade44e.
BQ_BILLING_PROJECT = os.environ.get("EXEC_PL_BQ_BILLING_PROJECT",
                                    "project-a7ade44e-e7e3-4871-a83")
BQ_LOCATION = os.environ.get("EXEC_PL_BQ_LOCATION", "EU")

# Funnel export, for the de-duplicated ad-spend reconciling detail only.
FUNNEL_TABLE = os.environ.get(
    "BQ_TABLE", "babyshop-funnel-data.bs_funnel_export.funnel_data")

WORKSPACE  = os.environ.get("FUNNEL_WORKSPACE", "-Ln87GcdqU9CMJV6zMBY")
COLLECTION = "funnel_cache"
DOC_KEY    = "exec-pl"
TTL        = 30 * 24 * 3600
FIRESTORE_PROJECT = os.environ.get("FIRESTORE_PROJECT", "project-a7ade44e-e7e3-4871-a83")

# The series starts here. Earlier BC data exists but predates the current
# posting conventions, so the tab would show a discontinuity rather than history.
HISTORY_START = os.environ.get("EXEC_PL_HISTORY_START", "2025-06-01")

# Babyshop-supplied inputs, read from disk next to this module (see _load_json).
FORECAST_FILE  = os.environ.get("EXEC_PL_FORECAST_FILE", "forecast_2026_rolling.json")
ESTIMATOR_FILE = os.environ.get("EXEC_PL_ESTIMATOR_FILE", "cost-estimator.json")

# Firestore's hard document limit is 1,048,576 bytes; the check measures compact
# JSON, which over-states the real cost (see refresh_customer_insights for the
# measured comparison). When this trips, window `markets` — it is the only
# section that grows with both months and countries — rather than raising it.
DOC_BUDGET_BYTES = 900_000

# Countries kept individually in the market breakdown. The rest roll into
# "Other", which keeps the document bounded as BC picks up long-tail markets.
MARKET_TOP_N = int(os.environ.get("EXEC_PL_MARKET_TOP_N", "12"))

# ── GL account groups ────────────────────────────────────────────────────────
# Every list here is reconciled; see the module docstring before changing one.
GL_COGS        = ["4006", "4014", "4037", "4055"]
GL_COGS_MAIN   = ["4006"]                       # shown split out on the rung
GL_SHIP_COST   = ["4336", "4337", "4338", "4450"]
GL_FULFILLMENT = ["4480", "4481", "4482", "4497", "4498", "4499"]
GL_TXN_FEES    = ["4372", "4373", "4374", "4375"]
GL_MARKETING   = ["5911", "5913", "5914", "5916", "5917", "5990"]
GL_MKTG_AD     = ["5911"]                       # "Marketing ad spend"
GL_INTEREST    = ["8390", "8413", "8415"]
GL_FX          = ["7960", "7983", "7984", "8329", "8429"]

# D&A is the WHOLE 7800-7899 block, not just 7810.
# The prototype mapped D&A to 7810 alone. That is right for 2026, where nothing
# else in the block posts, but GL 7801 carries 19 474 721 SEK across 2025 and a
# 7810-only rule leaves it in neither D&A nor EBITDA — it silently vanished from
# the prototype's 2025 figures. Sweeping the block puts it back in D&A and makes
# 2025 EBITDA agree with 2026's basis. Verified: switching to the range moves
# every 2026 month by 0 and brings 2025 into line.
def _is_da(a: str) -> bool:
    return "7800" <= a <= "7899"

# Overhead, in Babyshop's own display order. `None` marks the two rule-based
# groups, resolved in _overhead_groups().
OVERHEAD_GROUPS: list[tuple[str, list[str] | None]] = [
    ("Personnel costs HQ",           None),          # 7210-7699 except 7213, 7640
    ("Customer service costs",       ["7213"]),
    ("Consultants - marketing",      ["5982"]),
    ("Consultants - audit & lawyers", ["6420", "6530", "6580"]),
    ("Consultants - other",          ["6550", "7640"]),
    ("Consultants - IT",             ["6540"]),
    ("IT costs",                     ["4571", "4572", "4573", "4574", "4575", "4576"]),
    ("Occupancy",                    ["5011", "5090"]),
    ("Other overhead costs",         None),          # residual over 5000-6999
    ("Quality & CSR",                ["6994", "6996", "6997"]),
]

# Accounts that belong to a rung ABOVE overhead and must never be swept into the
# "Other overhead costs" residual.
_NON_OVERHEAD_5000_6999 = set(GL_MARKETING) | set(GL_FX) | {"5982", "5011", "5090",
                                                            "6420", "6530", "6580",
                                                            "6550", "6540",
                                                            "6994", "6996", "6997"}


def I(v) -> int:
    return int(round(float(v or 0)))


def F(v, nd=1) -> float:
    return round(float(v or 0), nd)


def _credentials():
    """ADC in production (Cloud Run SA); gcloud user token as a local fallback.

    The fallback refreshes by shelling back out to gcloud — a bare token lasts
    about an hour and carries no refresh material, so a long local run would die
    with a RefreshError partway through. Same shape as every other module here."""
    try:
        import google.auth
        creds, _ = google.auth.default()
        return creds
    except Exception:
        import google.oauth2.credentials

        class _GcloudToken(google.oauth2.credentials.Credentials):
            def refresh(self, request):  # noqa: ARG002 - signature fixed by google-auth
                self.token = subprocess.check_output(
                    ["gcloud", "auth", "print-access-token"]).decode().strip()
                # google-auth compares expiry against a NAIVE utcnow().
                self.expiry = datetime.datetime.utcnow() + datetime.timedelta(minutes=45)

        tok = subprocess.check_output(["gcloud", "auth", "print-access-token"]).decode().strip()
        c = _GcloudToken(tok)
        c.expiry = datetime.datetime.utcnow() + datetime.timedelta(minutes=45)
        return c


_client = None
def bq() -> bigquery.Client:
    global _client
    if _client is None:
        _client = bigquery.Client(project=BQ_BILLING_PROJECT, location=BQ_LOCATION,
                                  credentials=_credentials())
    return _client


_scanned = [0]
def _rows(sql: str, params=None) -> list:
    job = bq().query(sql, job_config=bigquery.QueryJobConfig(
        query_parameters=params or []))
    out = list(job.result())
    _scanned[0] += job.total_bytes_processed or 0
    return out


def T(name: str) -> str:
    return f"`{DATA_PROJECT}.{BC_DATASET}.{name}`"


# ── FX ───────────────────────────────────────────────────────────────────────
# One CTE, reused by the invoice and credit-memo queries. `exchangeRateAmount`
# is 1 for every row today, but the division is kept because BC allows the pair
# to express e.g. "per 100 JPY" and a future rate row could.
FX_CTE = f"""
fx AS (
  SELECT currencyCode, startingDate,
         SAFE_DIVIDE(relationalExchangeRateAmount, exchangeRateAmount) AS sek,
         LEAD(startingDate) OVER (PARTITION BY currencyCode
                                  ORDER BY startingDate) AS next_date
  FROM (
    SELECT currencyCode, startingDate, exchangeRateAmount, relationalExchangeRateAmount
    FROM {T('bc_currency_exchange_rates')}
    -- BC can restate a rate for a date it has already published; keep the last
    -- word on each (currency, date) so the join cannot multiply rows.
    QUALIFY ROW_NUMBER() OVER (PARTITION BY currencyCode, startingDate
                               ORDER BY lastModifiedDateTime DESC) = 1
  )
)"""


def docs_by_month_market() -> list[dict]:
    """Invoice and credit-memo item lines, FX-normalised, by month x country.

    The LEFT JOIN to `fx` defaulting to 1.0 is the whole point: SEK has no row
    in the rate table, so an inner join would drop every domestic invoice."""
    sql = f"""
    WITH {FX_CTE},
    inv AS (
      SELECT FORMAT_DATE('%Y-%m', i.postingDate) AS month,
             IFNULL(NULLIF(i.sellToCountry, ''), 'XX') AS country,
             i.id AS doc,
             l.lineObjectNumber AS sku,
             l.netAmount * COALESCE(f.sek, 1.0) AS sek
      FROM {T('bc_sales_invoice_lines')} l
      JOIN {T('bc_sales_invoices')} i ON l.documentId = i.id
      LEFT JOIN fx f ON f.currencyCode = i.currencyCode
                    AND i.postingDate >= f.startingDate
                    AND (f.next_date IS NULL OR i.postingDate < f.next_date)
      WHERE l.lineType = 'Item' AND i.postingDate >= @start
    ),
    cm AS (
      SELECT FORMAT_DATE('%Y-%m', c.postingDate) AS month,
             IFNULL(NULLIF(c.sellToCountry, ''), 'XX') AS country,
             c.id AS doc,
             l.lineObjectNumber AS sku,
             l.netAmount * COALESCE(f.sek, 1.0) AS sek
      FROM {T('bc_sales_credit_memo_lines')} l
      JOIN {T('bc_sales_credit_memos')} c ON l.documentId = c.id
      LEFT JOIN fx f ON f.currencyCode = c.currencyCode
                    AND c.postingDate >= f.startingDate
                    AND (f.next_date IS NULL OR c.postingDate < f.next_date)
      WHERE l.lineType = 'Item' AND c.postingDate >= @start
    ),
    u AS (
      SELECT 'I' AS src, * FROM inv
      UNION ALL
      SELECT 'C' AS src, * FROM cm
    )
    SELECT month, country,
           COUNT(DISTINCT IF(src = 'I', doc, NULL)) AS orders,
           COUNT(DISTINCT IF(src = 'C', doc, NULL)) AS credit_notes,
           -- Gross Sales: product lines only. SHIPPING has its own rung and
           -- COMPENSATION is a goodwill credit that belongs in Returns.
           SUM(IF(src = 'I' AND sku NOT IN ('SHIPPING', 'COMPENSATION', 'RETURNFEE'),
                  sek, 0)) AS gross,
           SUM(IF(src = 'I' AND sku = 'SHIPPING', sek, 0)) AS ship_rev,
           SUM(IF(src = 'C' AND sku NOT IN ('SHIPPING', 'COMPENSATION', 'RETURNFEE'),
                  sek, 0)) AS ret_product,
           SUM(IF(src = 'C' AND sku = 'SHIPPING', sek, 0)) AS ret_shipping,
           -- RETURNFEE is stored NEGATIVE and exists only on credit memos.
           SUM(IF(src = 'C' AND sku = 'RETURNFEE', sek, 0)) AS return_fee,
           SUM(IF(sku = 'COMPENSATION', sek, 0)) AS compensation
    FROM u
    GROUP BY 1, 2
    """
    p = [bigquery.ScalarQueryParameter("start", "DATE", HISTORY_START)]
    return [dict(r) for r in _rows(sql, p)]


def gl_by_month_account() -> list[dict]:
    """Every P&L account, by month. Mapped to rungs in Python.

    Pulled wholesale rather than filtered to the mapped accounts so that an
    account nobody has mapped yet shows up in `checks.unclaimed_pl_accounts`
    instead of quietly falling out of the ladder."""
    sql = f"""
    SELECT FORMAT_DATE('%Y-%m', postingDate) AS month,
           accountNumber,
           -- positive = expense
           SUM(debitAmount - creditAmount) AS amt,
           COUNT(*) AS n
    FROM {T('bc_general_ledger_entries')}
    WHERE postingDate >= @start
      AND accountNumber BETWEEN '3000' AND '8999'
    GROUP BY 1, 2
    """
    p = [bigquery.ScalarQueryParameter("start", "DATE", HISTORY_START)]
    return [dict(r) for r in _rows(sql, p)]


def item_ledger_by_month_market() -> list[dict]:
    """Item-ledger COGS by month x country — the ONLY COGS basis with a country.

    Geography comes from the CUSTOMER on the entry, not from its documentNumber.

    NEVER join this table to the invoice tables on documentNumber. The item
    ledger books a sale under its posted SHIPMENT number, which lives in a
    different BC number series from the invoice number, and the two series
    OVERLAP NUMERICALLY about four months apart. July 2026 shipments run
    470343-498682; July invoices run 569961-598700; the invoices that actually
    sit in the shipment range were posted in March and April. So the join
    matches ~100% of rows, silently, to unrelated older invoices belonging to
    different customers in different countries.

    That is not a hypothetical. It shipped once: the market table put Sweden at
    a NEGATIVE 9.7% GP1 and Korea at 80.5%, because the country column had
    become a reshuffle of the March/April customer mix. Zero July shipment
    numbers matched a July invoice.

    `sourceNumber` is the customer number on every sale row (`sourceType` =
    'Customer'), and `bc_customers.number` is unique across 316k rows, so this
    join is 1:1 and cannot fan out. Two independent checks on July 2026:
    customer country agrees with the invoice's own sellToCountry on 26,344 of
    26,390 invoices (99.83%), and with bc_return_receipts' own country column
    on 7,674 of 7,676 return rows.

    `entryType = 'Sale'` is the cost of goods sold. It spans Sales Shipment,
    Sales Invoice, Sales Credit Memo and Sales Return Receipt, so returns net
    off the market that generated them. The old query dropped return receipts
    into 'XX' entirely and never netted them anywhere. Purchase, Positive and
    Negative Adjmt. and Transfer are inventory movements, not COGS, and are
    excluded. Filter on entryType, never documentType: August also carries a
    'Sales Invoice' documentType under the same entryType.

    This basis remains INDEPENDENT of the group ladder, which takes COGS from
    the general ledger. The two disagree month to month on the posting cut-off
    (Jun -9.0%, Jul +3.4%, Aug +2.0% against GL 4006 and friends), which is
    exactly why no single market-month should be read on its own. The gap is
    published per month in checks.item_ledger_vs_gl_cogs rather than being
    scaled away, because scaling market rows onto the GL total would turn a
    measurement into an allocation."""
    sql = f"""
    SELECT FORMAT_DATE('%Y-%m', e.postingDate) AS month,
           IFNULL(NULLIF(c.country, ''), 'XX') AS country,
           -- costAmountActual is signed by BC (negative on sales) and already SEK.
           SUM(-e.costAmountActual) AS cogs
    FROM {T('bc_item_ledger_entries')} e
    LEFT JOIN {T('bc_customers')} c ON c.number = e.sourceNumber
    WHERE e.postingDate >= @start
      AND e.entryType = 'Sale'
    GROUP BY 1, 2
    """
    p = [bigquery.ScalarQueryParameter("start", "DATE", HISTORY_START)]
    return [dict(r) for r in _rows(sql, p)]


def funnel_spend_by_month_market() -> list[dict]:
    """De-duplicated Funnel ad spend, month x market — RECONCILING DETAIL ONLY.

    Total Marketing on the ladder is the general ledger. This exists so the tab
    can show the gap, and so an open month has a marketing driver at all.

    Uses the repo's single definition of the de-duplication rule. A raw
    SUM(Cost) double-counts Google Shopping and reads ~21% high; never use one.
    Market resolution follows the ROW-bucket rule: `market_level_1_kv` is the KV
    market, but non-core markets bucket their COST under the literal 'ROW', and
    the real country is recoverable from `market_level_1` AFTER the grain."""
    from refresh_customer_insights import COST_GRAIN, COST_BLANK, COST_SPLIT, COST_FIXED
    sql = f"""
    WITH cost_grain AS (
      SELECT {COST_GRAIN},
             ANY_VALUE(market_level_1) AS ml1,
             {COST_BLANK} AS blank, {COST_SPLIT} AS split
      FROM `{FUNNEL_TABLE}`
      WHERE Date >= @start AND Cost IS NOT NULL
      GROUP BY Date, Campaign, Channel_Type_Level_1, Channel_Type_Level_2,
               market_level_1_kv, shop_new
    ),
    cost_rows AS (
      SELECT Date, market_level_1_kv, ml1, {COST_FIXED} AS cost FROM cost_grain
    )
    SELECT FORMAT_DATE('%Y-%m', Date) AS month,
           CASE
             WHEN market_level_1_kv IN ('ROW', 'ROW USD')
               THEN IFNULL(NULLIF(ml1, ''), 'ROW_UNATTRIBUTED')
             ELSE IFNULL(NULLIF(market_level_1_kv, ''), 'ROW_UNATTRIBUTED')
           END AS market,
           SUM(cost) AS cost
    FROM cost_rows
    GROUP BY 1, 2
    """
    p = [bigquery.ScalarQueryParameter("start", "DATE", HISTORY_START)]
    return [dict(r) for r in _rows(sql, p)]


def bc_freshness() -> dict:
    sql = f"""
    SELECT MAX(postingDate) AS max_posting,
           MAX(lastModifiedDateTime) AS last_modified
    FROM {T('bc_sales_invoices')}
    """
    r = _rows(sql)[0]
    return {"max_posting_date": str(r["max_posting"]) if r["max_posting"] else None,
            "last_modified": str(r["last_modified"]) if r["last_modified"] else None}


# ── Assembly ─────────────────────────────────────────────────────────────────
def _overhead_groups(acc: dict[str, float]) -> tuple[list[int], list[str]]:
    """Ten overhead group totals for one month, plus the names, in Babyshop's order.

    `acc` is {accountNumber: expense} for that month.

    Two groups are rules, not lists:
      • Personnel costs HQ — the 7210-7699 block except 7213 (customer service,
        its own line) and 7640 (interim consultants, in Consultants - other).
      • Other overhead costs — the residual of 5000-6999 after every other rung
        has taken its accounts, so a newly opened account lands here visibly
        rather than dropping out of the ladder.
    """
    claimed_by_named = {a for _, accs in OVERHEAD_GROUPS if accs for a in accs}
    out: list[int] = []
    for name, accs in OVERHEAD_GROUPS:
        if accs is not None:
            out.append(I(sum(acc.get(a, 0) for a in accs)))
        elif name == "Personnel costs HQ":
            out.append(I(sum(v for a, v in acc.items()
                             if "7210" <= a <= "7699" and a not in ("7213", "7640"))))
        else:  # Other overhead costs — residual over 5000-6999
            out.append(I(sum(v for a, v in acc.items()
                             if "5000" <= a <= "6999"
                             and a not in claimed_by_named
                             and a not in _NON_OVERHEAD_5000_6999)))
    return out, [n for n, _ in OVERHEAD_GROUPS]


def _month_ladder(acc: dict[str, float], doc: dict) -> dict:
    """One month of the ladder. `acc` is {account: expense}, `doc` the document
    lines aggregated for that month."""
    g = lambda accs: I(sum(acc.get(a, 0) for a in accs))  # noqa: E731

    gross = I(doc["gross"])
    ret   = I(doc["ret_product"] + doc["compensation"])
    net   = gross - ret
    cogs  = g(GL_COGS)
    gp1   = net - cogs

    ship_rev_net = I(doc["ship_rev"] - doc["ret_shipping"])
    nship = g(GL_SHIP_COST) - ship_rev_net
    ful   = g(GL_FULFILLMENT)
    tf    = g(GL_TXN_FEES)
    gp2   = gp1 - nship - ful - tf

    mktg  = g(GL_MARKETING)
    gp3   = gp2 - mktg

    oh, _ = _overhead_groups(acc)
    toh = sum(oh)

    # EBITDA is swept from the ledger, not built up, so it ties by construction.
    ebitda = -I(sum(v for a, v in acc.items()
                    if "3000" <= a <= "7999" and not _is_da(a) and a not in GL_FX))
    oo  = ebitda - gp3 + toh          # the plug; see the module docstring
    da  = I(sum(v for a, v in acc.items() if _is_da(a)))
    ebit = ebitda - da
    fin = g(GL_INTEREST) + g(GL_FX)
    ebt = ebit - fin

    return {
        "gross": gross, "ret": ret, "net": net, "cogs": cogs, "gp1": gp1,
        "nship": nship, "ful": ful, "tf": tf, "gp2": gp2,
        "mktg": mktg, "gp3": gp3,
        "oo": oo, "toh": toh, "ebitda": ebitda,
        "da": da, "ebit": ebit, "fin": fin, "ebt": ebt,
        # No tax line posts in 2026, so Net Income IS EBT. Carried explicitly so
        # the tab never has to encode that assumption itself.
        "ni": ebt,
        "margins": {
            "gp1": F(100 * gp1 / net) if net else None,
            "gp2": F(100 * gp2 / net) if net else None,
            "gp3": F(100 * gp3 / net) if net else None,
            "ebitda": F(100 * ebitda / net) if net else None,
        },
    }


def _load_json(filename: str) -> dict | None:
    """Babyshop inputs live next to this module in the image; fall back to the
    repo-relative path so a local run from anywhere still finds them."""
    for base in (os.path.dirname(os.path.abspath(__file__)), os.getcwd()):
        p = os.path.join(base, filename)
        if os.path.exists(p):
            with open(p, encoding="utf-8") as fh:
                return json.load(fh)
    print(f"   ! {filename} not found — that section will be null", flush=True)
    return None


def build_payload() -> dict:
    t0 = time.time()
    print("→ BC documents (FX-normalised) …", flush=True)
    docs = docs_by_month_market()
    print(f"   {len(docs)} month x country rows", flush=True)

    print("→ general ledger …", flush=True)
    gl = gl_by_month_account()
    print(f"   {len(gl)} month x account rows", flush=True)

    print("→ item ledger (market COGS basis) …", flush=True)
    ile = item_ledger_by_month_market()

    print("→ Funnel de-duplicated spend (reconciling only) …", flush=True)
    try:
        funnel = funnel_spend_by_month_market()
    except Exception as e:
        # The ladder does not depend on this. A Funnel outage must not take the
        # whole P&L tab down with it.
        print(f"   ! Funnel spend unavailable: {type(e).__name__}: {e}", flush=True)
        funnel = []

    fresh = bc_freshness()

    # month -> {account: expense}
    acc_by_month: dict[str, dict[str, float]] = {}
    for r in gl:
        acc_by_month.setdefault(r["month"], {})[r["accountNumber"]] = float(r["amt"] or 0)

    # month -> aggregated document lines (all countries)
    FIELDS = ("orders", "credit_notes", "gross", "ship_rev", "ret_product",
              "ret_shipping", "return_fee", "compensation")
    doc_by_month: dict[str, dict] = {}
    for r in docs:
        d = doc_by_month.setdefault(r["month"], {k: 0.0 for k in FIELDS})
        for k in FIELDS:
            d[k] += float(r[k] or 0)

    # The open month is the one BC has posted into most recently. Anything BEYOND
    # it is a forward-dated accrual or prepayment (BC happily posts into next
    # year), and showing those as months would put half-empty future columns on
    # the tab. They are cut from the series and reported in `checks` instead.
    last_closed = open_month = None
    max_post = fresh.get("max_posting_date")
    if max_post:
        y, mm = int(max_post[:4]), int(max_post[5:7])
        open_month = f"{y:04d}-{mm:02d}"
        last_closed = f"{y:04d}-{mm-1:02d}" if mm > 1 else f"{y-1:04d}-12"

    all_months = sorted(m for m in set(acc_by_month) | set(doc_by_month)
                        if m >= HISTORY_START[:7])
    months = [m for m in all_months if not open_month or m <= open_month]
    forward = {m: -I(sum(acc_by_month.get(m, {}).values()))
               for m in all_months if open_month and m > open_month}

    ladder = {m: _month_ladder(acc_by_month.get(m, {}),
                               doc_by_month.get(m, {k: 0.0 for k in FIELDS}))
              for m in months}

    # ── components: the detail behind each rung ──────────────────────────────
    _, oh_names = _overhead_groups({})
    components = {}
    for m in months:
        acc = acc_by_month.get(m, {})
        d = doc_by_month.get(m, {k: 0.0 for k in FIELDS})
        oh, _ = _overhead_groups(acc)
        gsum = lambda accs: I(sum(acc.get(a, 0) for a in accs))  # noqa: E731
        components[m] = {
            "ship_revenue": I(d["ship_rev"]),
            "ship_returns": I(d["ret_shipping"]),
            "return_fee": I(d["return_fee"]),
            "compensation": I(d["compensation"]),
            "returns_product": I(d["ret_product"]),
            "orders": I(d["orders"]),
            "credit_notes": I(d["credit_notes"]),
            "cogs_4006": gsum(GL_COGS_MAIN),
            "cogs_adjustments": gsum(GL_COGS) - gsum(GL_COGS_MAIN),
            "mktg_ad_5911": gsum(GL_MKTG_AD),
            "mktg_other": gsum(GL_MARKETING) - gsum(GL_MKTG_AD),
            "mktg_consultants_5982": gsum(["5982"]),
            "ship_cost": {a: gsum([a]) for a in GL_SHIP_COST},
            "fulfillment": {a: gsum([a]) for a in GL_FULFILLMENT},
            "txn_fees": {a: gsum([a]) for a in GL_TXN_FEES},
            "overhead": oh,
            "interest": gsum(GL_INTEREST),
            "fx": gsum(GL_FX),
        }

    # ── market breakdown, stopping at contribution ───────────────────────────
    # Rank countries on total net sales across the whole window so the set is
    # stable month to month; everything else becomes "Other".
    # 'XX' means the document carried no country. It must NEVER fold into
    # "Other": the item-ledger query parks every entry whose document does not
    # resolve to a sale there (adjustments, transfers, revaluations), and mixing
    # that into a bucket of real small countries gave "Other" a NEGATIVE COGS of
    # 11.0 MSEK and a 12.1 MSEK contribution on the first production run — a
    # market apparently trading at a 1088% margin. It gets its own visible
    # bucket, so the market table still reconciles to the item-ledger total
    # without inventing a market.
    UNATTRIBUTED = "UNATTRIBUTED"
    net_by_country: dict[str, float] = {}
    for r in docs:
        if r["country"] == "XX":
            continue
        v = float(r["gross"] or 0) - float(r["ret_product"] or 0) - float(r["compensation"] or 0)
        net_by_country[r["country"]] = net_by_country.get(r["country"], 0) + v
    top = {c for c, _ in sorted(net_by_country.items(), key=lambda kv: -kv[1])[:MARKET_TOP_N]}

    def bucket(c: str) -> str:
        if c == "XX":
            return UNATTRIBUTED
        return c if c in top else "Other"

    ile_by = {}
    for r in ile:
        key = (r["month"], bucket(r["country"]))
        ile_by[key] = ile_by.get(key, 0) + float(r["cogs"] or 0)

    fun_by = {}
    for r in funnel:
        key = (r["month"], r["market"])
        fun_by[key] = fun_by.get(key, 0) + float(r["cost"] or 0)

    markets: dict[str, dict] = {}
    for r in docs:
        m, c = r["month"], bucket(r["country"])
        row = markets.setdefault(m, {}).setdefault(c, {
            "orders": 0, "gross": 0.0, "returns": 0.0})
        row["orders"] += int(r["orders"] or 0)
        row["gross"] += float(r["gross"] or 0)
        row["returns"] += float(r["ret_product"] or 0) + float(r["compensation"] or 0)

    for m, byc in markets.items():
        for c, row in byc.items():
            gross = I(row["gross"]); rets = I(row["returns"])
            net = gross - rets
            cogs = I(ile_by.get((m, c), 0))
            mk = I(fun_by.get((m, c), 0))
            row.update({
                "gross": gross, "returns": rets, "net": net,
                "cogs_item_ledger": cogs,
                "gp1": net - cogs,
                "marketing_funnel": mk,
                # The ladder STOPS here for a market. Everything below has no
                # country dimension in BC.
                "contribution": net - cogs - mk,
                "basis": {
                    "cogs": "item_ledger",
                    "marketing": "funnel_dedup",
                    "note": "group ladder uses GL for both; market uses the only "
                            "bases that carry a country",
                },
            })

    # ── logistics allocation (indicative, explicitly not a fact) ─────────────
    logistics = {
        "method": "flat_by_order_count",
        "allocated": True,
        "is_fact": False,
        "note": ("BC posts logistics at carrier-invoice granularity with no "
                 "geography and shipmentMethodId is 100% empty, so per-market "
                 "shipping and fulfilment cost cannot be measured. These are "
                 "group totals spread on order count, for indication only."),
        "replaces_with": "per_order_actual",
        "per_order_actual": None,
        "group_totals": {m: {"nship": ladder[m]["nship"], "ful": ladder[m]["ful"]}
                         for m in months},
        "per_market": {},
    }
    for m in months:
        byc = markets.get(m, {})
        tot_orders = sum(r["orders"] for r in byc.values()) or 0
        if not tot_orders:
            continue
        logistics["per_market"][m] = {
            c: {"share": F(r["orders"] / tot_orders, 4),
                "nship_allocated": I(ladder[m]["nship"] * r["orders"] / tot_orders),
                "ful_allocated": I(ladder[m]["ful"] * r["orders"] / tot_orders),
                "allocated": True}
            for c, r in byc.items()
        }

    # ── checks ───────────────────────────────────────────────────────────────
    claimed = (set(GL_COGS) | set(GL_SHIP_COST) | set(GL_FULFILLMENT)
               | set(GL_TXN_FEES) | set(GL_MARKETING)
               | set(GL_INTEREST) | set(GL_FX)
               | {a for _, accs in OVERHEAD_GROUPS if accs for a in accs})

    def _claimed(a: str) -> bool:
        if a in claimed:
            return True
        if _is_da(a):                             # D&A block
            return True
        if "7210" <= a <= "7699":                 # personnel rule
            return True
        if "3000" <= a <= "4999":                 # swept into EBITDA / other operating
            return True
        if "5000" <= a <= "6999":                 # swept into the overhead residual
            return True
        return False

    unclaimed: dict[str, float] = {}
    for r in gl:
        a = r["accountNumber"]
        if not _claimed(a):
            unclaimed[a] = unclaimed.get(a, 0) + float(r["amt"] or 0)
    unclaimed = {a: I(v) for a, v in sorted(unclaimed.items()) if I(v)}

    gl_net_result = {m: -I(sum(acc_by_month.get(m, {}).values())) for m in months}
    ebt_gap = {m: ladder[m]["ebt"] - gl_net_result[m] for m in months}

    checks = {
        "unclaimed_pl_accounts": unclaimed,
        "unclaimed_total": sum(unclaimed.values()),
        "unclaimed_note": ("P&L accounts with activity that no ladder line claims. "
                           "GL 8020 and 8490 are the known pair: they post nothing "
                           "in July, which is the only reason a July-only EBT tie "
                           "test passes. Any month where they post, ladder EBT sits "
                           "above the booked GL net result by that amount."),
        "gl_net_result": gl_net_result,
        "ebt_vs_gl_net_result": ebt_gap,
        "forward_posted_months": forward,
        "forward_posted_note": ("GL entries dated beyond the open month (accruals "
                                "and prepayments). Cut from the series so the tab "
                                "never shows a half-empty future column."),
        "fx_note": ("SEK has no row in bc_currency_exchange_rates; the join "
                    "defaults to 1.0. An inner join would drop ~57% of invoices."),
        # The market table's COGS is the item ledger; the group ladder's is the
        # general ledger. They are two honest measures of the same thing on
        # different posting cut-offs, and they disagree month to month. Publish
        # the gap so the tab can state it instead of quietly scaling one onto
        # the other, which would turn a measurement into an allocation.
        "item_ledger_vs_gl_cogs": {
            m: {"item_ledger": I(sum(r["cogs_item_ledger"]
                                     for r in markets.get(m, {}).values())),
                "gl": ladder[m]["cogs"],
                "pct": F(100 * (sum(r["cogs_item_ledger"]
                                    for r in markets.get(m, {}).values())
                                - ladder[m]["cogs"]) / ladder[m]["cogs"], 2)
                       if ladder[m]["cogs"] else None}
            for m in months},
        "item_ledger_note": ("Market COGS is the item ledger, the only basis "
                             "carrying a country. The group ladder is the "
                             "general ledger. Market rows therefore do not foot "
                             "to the ladder, and the gap is posting cut-off, "
                             "not error."),
    }

    forecast = _load_json(FORECAST_FILE)
    estimator = _load_json(ESTIMATOR_FILE)

    payload = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc)
                                 .isoformat(timespec="seconds"),
        "sources": {
            "bc": {"project": DATA_PROJECT, "dataset": BC_DATASET,
                   "tables": "bc_*", **fresh,
                   "not_used": {"bc_return_receipts":
                                "return value comes from credit memos, which carry "
                                "sellToCountry; this table spells it "
                                "sellToCountryRegionCode"}},
            "funnel": {"table": FUNNEL_TABLE,
                       "role": "reconciling detail only, never the ladder",
                       "rows": len(funnel)},
            "forecast": (forecast or {}).get("_meta"),
            "estimator": {"generated_at": (estimator or {}).get("generated_at")},
            "billed_to": BQ_BILLING_PROJECT,
        },
        "months": months,
        "last_closed": last_closed,
        "open_month": open_month,
        "overhead_groups": oh_names,
        "ladder": ladder,
        "components": components,
        "markets": markets,
        # UNATTRIBUTED is listed last and named, not hidden: it is reconciliation
        # residue (item-ledger entries with no sales document), not a market.
        "market_countries": sorted(top) + ["Other", UNATTRIBUTED],
        "logistics_allocation": logistics,
        "forecast": forecast,
        "estimator": estimator,
        "checks": checks,
        "caveats": [
            "Gross Sales is FX-normalised at BC's own rate for the posting date; "
            "skipping FX understates it by about 28%.",
            "Total COGS is the general ledger (4006/4014/4037/4055), not the item "
            "ledger, which runs about 3.2% off on posting cut-off.",
            "Market rows stop at contribution. Logistics, transaction fees and "
            "overhead have no country dimension in BC.",
            "Per-market logistics in logistics_allocation is a flat allocation by "
            "order count, not a measurement.",
            "The open month is incomplete below GP1: logistics, packaging and most "
            "marketing post in batches after month end.",
            "EBITDA is swept from the ledger, so it ties to the booked operating "
            "result every month. For 2025 it therefore differs from the prototype, "
            "which built EBITDA up from an itemised Other-operating-items list that "
            "was only ever reconciled against July 2026. Every 2026 month agrees to "
            "single SEK; 2025 months differ by up to 4.5 M, mostly in the "
            "purchase-to-inventory flow (GL 4041/4091/4092/4333/4335).",
            "GL 8020, 8070, 8490, 8893 and 8940 carry real money that no ladder line "
            "claims, so EBT is not the booked GL net result. See "
            "checks.unclaimed_pl_accounts, which quantifies the gap every run.",
        ],
    }
    print(f"   built in {time.time()-t0:.1f}s · {_scanned[0]:,} B scanned", flush=True)
    return payload


def _check_size(payload: dict) -> int:
    n = len(json.dumps(payload, ensure_ascii=False))
    if n > DOC_BUDGET_BYTES:
        big = sorted(((len(json.dumps(v, ensure_ascii=False)), k)
                      for k, v in payload.items()), reverse=True)[:3]
        raise RuntimeError(
            f"Executive P&L payload is {n:,} B, over the {DOC_BUDGET_BYTES:,} B "
            f"budget (Firestore's hard limit is 1,048,576). Biggest sections: "
            + ", ".join(f"{k} {s:,} B" for s, k in big)
            + ". Lower EXEC_PL_MARKET_TOP_N or window `markets` by month — do not "
              "just raise the budget.")
    return n


def write_firestore(payload: dict) -> str:
    from google.cloud import firestore
    _check_size(payload)
    db = firestore.Client(project=FIRESTORE_PROJECT, credentials=_credentials())
    doc_id = f"{WORKSPACE}__{DOC_KEY}"
    db.collection(COLLECTION).document(doc_id).set({
        "data": payload, "fetched_at": firestore.SERVER_TIMESTAMP,
        "expires_at": time.time() + TTL, "ttl_seconds": TTL, "workspace": WORKSPACE})
    return f"{COLLECTION}/{doc_id}"


def main():
    t0 = time.time()
    src = os.environ.get("EXEC_PL_IN")
    if src:
        with open(src, encoding="utf-8") as fh:
            p = json.load(fh)
        print(f"→ loaded {src} ({os.path.getsize(src):,} bytes), skipping BigQuery")
    else:
        p = build_payload()

    out = os.environ.get("EXEC_PL_OUT")
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(p, fh, ensure_ascii=False)
        print(f"   wrote {out} ({os.path.getsize(out):,} bytes)")

    where = "(skipped)" if os.environ.get("SKIP_FIRESTORE") else write_firestore(p)

    # YTD = January of the open month's year through the open month itself.
    year = (p["open_month"] or p["months"][-1])[:4]
    ytd = [m for m in p["months"] if m.startswith(year)]
    agg = {k: sum(p["ladder"][m][k] for m in ytd)
           for k in ("net", "gp1", "gp2", "gp3", "ebitda")}
    print(f"✓ Executive P&L refresh · {where} · "
          f"{p['months'][0]}→{p['months'][-1]} ({len(p['months'])} months) · "
          f"last closed {p['last_closed']} · "
          f"YTD net {agg['net']:,} · GP1 {agg['gp1']:,} · GP2 {agg['gp2']:,} · "
          f"GP3 {agg['gp3']:,} · EBITDA {agg['ebitda']:,} · "
          f"{len(p['market_countries'])} markets · "
          f"{len(p['checks']['unclaimed_pl_accounts'])} unclaimed accounts · "
          f"{len(json.dumps(p, ensure_ascii=False)):,} B · {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
