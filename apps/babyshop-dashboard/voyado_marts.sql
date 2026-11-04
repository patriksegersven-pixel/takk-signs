-- ============================================================================
--  Email / CRM marts over the Voyado Engage landing tables.
--
--  Applied by voyado_sync.py (`apply_marts()`), which substitutes ${DATASET}
--  with `project-a7ade44e-e7e3-4871-a83.voyado` and runs each statement.
--  Statements are separated by a double-at sentinel comment, NOT by `;` — a
--  semicolon inside a string or comment must never split a statement in half.
--  (This paragraph deliberately spells the sentinel out rather than writing it
--  literally, so the splitter cannot cut its own documentation in two.)
--  Paste any single block straight into the BigQuery console when debugging;
--  just replace ${DATASET} first.
--
--  Everything here is a VIEW. The largest input is ~40M delivery rows, which
--  BigQuery scans happily; materialising would buy a staleness bug instead.
--
--  RULES BAKED IN (do not "fix" these without re-reading the audit in
--  voyado_sync.py's module docstring)
--
--   • MARKETING ONLY. messages is one row per SENDOUT, and 76% of them are
--     transactional (order confirmations) while 99.7% are automation. Every
--     view here filters `isTransactional = false` and `channel = 'email'`.
--     Drop that filter and order confirmations swamp every campaign.
--
--   • REVENUE IS localPrice × A DERIVED RATE, NEVER exchangeRate. 13,325 of
--     151,378 non-SEK receipts (all between 2025-01 and 2025-05) carry
--     exchangeRate = 1.0 with the amount left unconverted, so `totalGrossPrice`
--     is USD-as-SEK on those rows. `totalLocalPrice`/`localPrice` are always
--     the true local amount, and currency_rates below recovers the real rate
--     from the good rows. Rates are FIXED per currency in Voyado (SEK 1.00,
--     NOK 0.95, DKK 1.45, EUR 10.90, USD 9.60, GBP 12.90), so this is
--     constant-currency for all history — the same convention norce_marts.sql
--     uses, and what makes markets comparable.
--
--   • RETURNS ARE ALREADY NET. receipt_items.type = 'Return' rows carry
--     negative quantity and negative price, so a plain SUM is net of returns.
--     Do not subtract totalReturned on top — that double-counts.
--
--   • "SENT" IS NOT "DELIVERED". The share has no bounce or delivery-status
--     data at all, so `sent` counts delivery ROWS and nothing distinguishes a
--     hard bounce from an inbox placement. Any "delivery rate" built on this
--     would be fiction.
--
--   • OPENS ARE INFLATED. Apple Mail Privacy Protection pre-fetches images, so
--     open counts include machine opens. They are exposed because the trend is
--     still readable, but click_rate is the metric to lead with, and open_rate
--     must be labelled as directional in the UI.
--
--   • MARKET COMES FROM THE CONTACT, NOT THE MESSAGE. The share is a single
--     tenant with no market dimension on messages. Events and receipts carry
--     a `market` column that voyado_sync.stamp_markets() copies from the
--     contact while it still exists; read it FIRST and fall back to the live
--     contact join. Erasures run at scale (135,214 in the four weeks to
--     2026-11-04) and remove the contact, so without the stamp 46-64% of each
--     past month's sends had no market. Rows erased before the stamp existed
--     (2026-11-04, minus the 7 days time travel recovered) stay 'Unknown', as
--     do contacts with no countryCode (~16%). 'Unknown' must be SHOWN, never
--     hidden: message-level counts are complete; market-split counts are not.
--
--   • ATTRIBUTION IS LAST CLICK, 7-DAY WINDOW, on Voyado's own contactId. It
--     credits a receipt to the most recent non-transactional email click by the
--     same contact in the 7 days before the purchase. It is NOT incrementality:
--     only 25 of 737,751 sendouts use a control group, so there is no holdout
--     to measure against. Label it "email-influenced revenue", not "revenue
--     email generated".
-- ============================================================================

-- @@
-- currency_rates — every currency's value in SEK, recovered from the receipts.
--
-- Derived rather than hard-coded so a Voyado rate change flows through on its
-- own. The filter is the whole point: rows with exchangeRate = 1.0 on a non-SEK
-- currency are the broken ones, and including them would drag the median toward
-- 1.0 and silently under-convert every early market.
CREATE OR REPLACE VIEW `${DATASET}.currency_rates` AS
SELECT
  localCurrency,
  APPROX_QUANTILES(exchangeRate, 2)[OFFSET(1)] AS sek_rate,
  COUNT(*)                                     AS receipts_with_good_rate
FROM `${DATASET}.receipts`
WHERE localCurrency IS NOT NULL
  AND (localCurrency = 'SEK' OR exchangeRate != 1.0)
GROUP BY localCurrency

-- @@
-- receipts_sek — receipts with a trustworthy SEK amount.
--
-- revenue_sek is rebuilt from totalLocalPrice; totalGrossPrice is deliberately
-- NOT exposed, because on 13k early rows it is a local amount wearing a SEK
-- label and there is no way to tell from the row itself.
CREATE OR REPLACE VIEW `${DATASET}.receipts_sek` AS
SELECT
  r.receiptId,
  r.contactId,
  r.storeId,
  r.receiptNumber,
  r.createdOnDateTime,
  r.createdOnDate,
  r.localCurrency,
  r.totalLocalPrice,
  r.totalQuantity,
  r.numberOfItems,
  COALESCE(cr.sek_rate, 1.0)                    AS sek_rate,
  r.totalLocalPrice * COALESCE(cr.sek_rate, 1.0) AS revenue_sek,
  r.market                                       AS stamped_market,
  -- The window in which Voyado's own exchangeRate cannot be trusted. Exposed so
  -- a chart can grey it out rather than quietly showing a wrong early baseline.
  r.createdOnDate < DATE '2025-06-01'            AS in_broken_fx_window
FROM `${DATASET}.receipts` r
LEFT JOIN `${DATASET}.currency_rates` cr USING (localCurrency)

-- @@
-- receipt_items_sek — line items with a trustworthy SEK amount.
--
-- Returns arrive as their own rows with negative quantity and price, so
-- SUM(revenue_sek) is already net. is_return exists to split gross vs returns
-- when that is wanted, not to filter them out of the total.
CREATE OR REPLACE VIEW `${DATASET}.receipt_items_sek` AS
SELECT
  i.receiptItemId,
  i.receiptId,
  i.contactId,
  i.transactionDateTime,
  i.transactionDate,
  i.sku,
  i.articleNumber,
  i.articleName,
  i.quantity,
  i.localCurrency,
  i.localPrice,
  i.discounts,
  i.discountPercent,
  i.type,
  i.type = 'Return'                            AS is_return,
  i.localPrice * COALESCE(cr.sek_rate, 1.0)    AS revenue_sek
FROM `${DATASET}.receipt_items` i
LEFT JOIN `${DATASET}.currency_rates` cr USING (localCurrency)

-- @@
-- contact_market — the market dimension, plus the real permission signal.
--
-- `canReceiveEmail` looks like the field to use and is 100% NULL on this
-- tenant; acceptsEmail + statusEmail are the pair that actually carries state.
-- Deleted contacts are kept (they still have message history) but flagged.
CREATE OR REPLACE VIEW `${DATASET}.contact_market` AS
SELECT
  contactId,
  customer_hash,
  COALESCE(NULLIF(TRIM(countryCode), ''), 'Unknown') AS market,
  memberNumber,
  registrationDateTime,
  latestReceiptDateTime,
  purchaseAmountTotal,
  purchaseAmount12months,
  numberOfArticlesTotal,
  averageReceiptTotal,
  acceptsEmail,
  statusEmail,
  isDeleted,
  COALESCE(acceptsEmail, FALSE) AND statusEmail = 'Active' AS is_subscribed
FROM `${DATASET}.contacts`

-- @@
-- marketing_messages — the sendout spine, transactional traffic removed.
--
-- messageSource is 'manual' (2,214 rows — real campaigns) or 'automation'
-- (735,537 rows — one sendout per trigger, e.g. every welcome flow email).
-- Both are marketing; they just need to be counted separately, because an
-- automation "campaign" is a flow, not a send.
CREATE OR REPLACE VIEW `${DATASET}.marketing_messages` AS
SELECT
  messageId,
  messageName,
  subject,
  status,
  scheduledDateTime,
  DATE(scheduledDateTime)                      AS scheduledDate,
  recipientCount,
  messageSource,
  workflowId,
  controlGroupPercent,
  IF(messageSource = 'manual', 'Manual campaign', 'Automation') AS message_type
FROM `${DATASET}.messages`
WHERE channel = 'email'
  AND NOT COALESCE(isTransactional, FALSE)

-- @@
-- message_engagement — one row per marketing sendout, whole-life funnel.
--
-- Engagement is attributed to the MESSAGE regardless of when it happened, which
-- is the right grain for a campaign table (opens trail sends by days). Use
-- email_daily for "what happened on day X" instead.
--
-- Rates are computed against `sent`, and `sent` is delivery rows — see the
-- header: there is no bounce data, so this is not a deliverable-adjusted rate.
CREATE OR REPLACE VIEW `${DATASET}.message_engagement` AS
WITH d AS (
  SELECT messageId, COUNT(*) AS sent, COUNT(DISTINCT contactId) AS recipients,
         MIN(deliveryDateTime) AS first_delivery, MAX(deliveryDateTime) AS last_delivery
  FROM `${DATASET}.deliveries` GROUP BY messageId
), o AS (
  SELECT messageId, COUNT(*) AS opens, COUNT(DISTINCT contactId) AS unique_openers
  FROM `${DATASET}.opens` GROUP BY messageId
), c AS (
  SELECT messageId, COUNT(*) AS clicks, COUNT(DISTINCT contactId) AS unique_clickers
  FROM `${DATASET}.clicks` GROUP BY messageId
), u AS (
  SELECT messageId, COUNT(*) AS unsubscribes
  FROM `${DATASET}.unsubscribes` GROUP BY messageId
), g AS (
  SELECT messageId, COUNT(DISTINCT contactId) AS control_group_size
  FROM `${DATASET}.control_group` GROUP BY messageId
)
SELECT
  m.messageId, m.messageName, m.subject, m.message_type, m.messageSource,
  m.workflowId, m.status, m.scheduledDateTime, m.scheduledDate, m.controlGroupPercent,
  COALESCE(d.sent, 0)               AS sent,
  COALESCE(d.recipients, 0)         AS recipients,
  d.first_delivery, d.last_delivery,
  COALESCE(o.opens, 0)              AS opens,
  COALESCE(o.unique_openers, 0)     AS unique_openers,
  COALESCE(c.clicks, 0)             AS clicks,
  COALESCE(c.unique_clickers, 0)    AS unique_clickers,
  COALESCE(u.unsubscribes, 0)       AS unsubscribes,
  COALESCE(g.control_group_size, 0) AS control_group_size,
  SAFE_DIVIDE(o.unique_openers,  d.sent) AS open_rate,
  SAFE_DIVIDE(c.unique_clickers, d.sent) AS click_rate,
  SAFE_DIVIDE(c.unique_clickers, o.unique_openers) AS click_to_open_rate,
  SAFE_DIVIDE(u.unsubscribes,    d.sent) AS unsubscribe_rate
FROM `${DATASET}.marketing_messages` m
LEFT JOIN d USING (messageId)
LEFT JOIN o USING (messageId)
LEFT JOIN c USING (messageId)
LEFT JOIN u USING (messageId)
LEFT JOIN g USING (messageId)

-- @@
-- attributed_receipts — last non-transactional email click within 7 days.
--
-- One row per receipt, whether or not a click preceded it, so this view is also
-- the denominator for "share of revenue that email touched". A receipt with no
-- qualifying click keeps NULL attribution rather than dropping out.
--
-- The 7-day window and last-click rule are the two knobs here; both are stated
-- in the header. QUALIFY picks the most recent qualifying click per receipt.
CREATE OR REPLACE VIEW `${DATASET}.attributed_receipts` AS
SELECT
  r.receiptId,
  r.contactId,
  r.createdOnDateTime,
  r.createdOnDate,
  r.revenue_sek,
  r.in_broken_fx_window,
  COALESCE(r.stamped_market, cm.market, 'Unknown') AS market,
  -- Erased contact, never stamped: the market existed but cannot be read back.
  -- email_daily allocates these by message mix; nothing else does.
  r.stamped_market IS NULL AND cm.contactId IS NULL AS market_lost,
  k.messageId       AS attributed_messageId,
  k.clickDateTime   AS attributed_clickDateTime,
  k.messageId IS NOT NULL AS is_email_attributed
FROM `${DATASET}.receipts_sek` r
LEFT JOIN `${DATASET}.contact_market` cm USING (contactId)
LEFT JOIN (
  SELECT c.contactId, c.messageId, c.clickDateTime
  FROM `${DATASET}.clicks` c
  JOIN `${DATASET}.marketing_messages` m USING (messageId)
  WHERE c.contactId IS NOT NULL
) k
  ON  k.contactId     =  r.contactId
  AND k.clickDateTime <= r.createdOnDateTime
  AND k.clickDateTime >  TIMESTAMP_SUB(r.createdOnDateTime, INTERVAL 7 DAY)
WHERE r.contactId IS NOT NULL
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY r.receiptId ORDER BY k.clickDateTime DESC) = 1

-- @@
-- campaign_performance — the campaign table: funnel + email-influenced revenue.
--
-- Revenue is joined on the ATTRIBUTED message, so a receipt counts once, to the
-- last campaign clicked. AOV is per attributed order, not per recipient.
CREATE OR REPLACE VIEW `${DATASET}.campaign_performance` AS
WITH rev AS (
  SELECT attributed_messageId AS messageId,
         COUNT(*)                       AS attributed_orders,
         COUNT(DISTINCT contactId)      AS attributed_buyers,
         SUM(revenue_sek)               AS attributed_revenue_sek
  FROM `${DATASET}.attributed_receipts`
  WHERE attributed_messageId IS NOT NULL
  GROUP BY messageId
)
SELECT
  e.*,
  COALESCE(rev.attributed_orders, 0)      AS attributed_orders,
  COALESCE(rev.attributed_buyers, 0)      AS attributed_buyers,
  COALESCE(rev.attributed_revenue_sek, 0) AS attributed_revenue_sek,
  SAFE_DIVIDE(rev.attributed_revenue_sek, rev.attributed_orders) AS attributed_aov_sek,
  SAFE_DIVIDE(rev.attributed_revenue_sek, e.sent)                AS revenue_per_send_sek,
  SAFE_DIVIDE(rev.attributed_orders, e.unique_clickers)          AS click_to_order_rate
FROM `${DATASET}.message_engagement` e
LEFT JOIN rev USING (messageId)

-- @@
-- message_market_mix — each sendout's market split among recipients we can see.
--
-- The basis for ESTIMATING the market of mail to erased contacts. Every row
-- that is not itself lost counts, INCLUDING live contacts with no country as
-- 'Unknown': leaving them out would assume every erased contact had a country
-- and push their mail into real markets. Mix is
-- far from pure — a manual campaign averages 79% of its known recipients in
-- one market — which is why email_daily SPLITS a lost row across the mix
-- rather than handing it to the top market.
CREATE OR REPLACE VIEW `${DATASET}.message_market_mix` AS
WITH k AS (
  SELECT x.messageId,
         COALESCE(x.market, cm.market) AS market,
         COUNT(*) AS known_recipients
  FROM `${DATASET}.deliveries` x
  JOIN `${DATASET}.marketing_messages` USING (messageId)
  LEFT JOIN `${DATASET}.contact_market` cm ON cm.contactId = x.contactId
  WHERE x.market IS NOT NULL OR cm.contactId IS NOT NULL
  GROUP BY 1, 2
)
SELECT messageId, market, known_recipients,
       known_recipients / SUM(known_recipients) OVER (PARTITION BY messageId) AS share
FROM k

-- @@
-- email_daily — activity by EVENT date and market, for the trend charts.
--
-- Every metric sits on the date it happened (a click on the 3rd counts on the
-- 3rd even if the send was the 1st), which is what makes a daily series add up
-- to what the mailbox actually did that day. Market is the stamped one, else
-- the live contact's (see the header).
--
-- ROWS WHOSE MARKET WAS LOST TO AN ERASURE ARE ESTIMATED. A row is "lost" when
-- it was never stamped and its contact is gone; it is split across its
-- message's message_market_mix, so counts are FRACTIONAL and every est_*
-- column says how much of its metric is such an estimate. Weights sum to 1 per
-- row, so totals over markets equal the exact counts. A lost row whose message
-- has no known recipient stays 'Unknown', as does a live contact with no
-- country — that is missing data, not lost data, and is never estimated.
--
-- Unique counts (recipients, openers, clickers) on lost rows give each contact
-- a weight of 1 per (day, type), spread as the average mix of the rows they
-- had that day, so a contact never counts more than once in the total.
CREATE OR REPLACE VIEW `${DATASET}.email_daily` AS
WITH msg AS (SELECT messageId, message_type FROM `${DATASET}.marketing_messages`),
ev AS (
  SELECT 'sent' AS kind, x.deliveryDate AS event_date, x.messageId, x.contactId,
         COALESCE(x.market, cm.market) AS mk, x.market IS NULL AND cm.contactId IS NULL AS lost
  FROM `${DATASET}.deliveries` x
  LEFT JOIN `${DATASET}.contact_market` cm ON cm.contactId = x.contactId
  UNION ALL
  SELECT 'open', x.openDate, x.messageId, x.contactId,
         COALESCE(x.market, cm.market), x.market IS NULL AND cm.contactId IS NULL
  FROM `${DATASET}.opens` x
  LEFT JOIN `${DATASET}.contact_market` cm ON cm.contactId = x.contactId
  UNION ALL
  SELECT 'click', x.clickDate, x.messageId, x.contactId,
         COALESCE(x.market, cm.market), x.market IS NULL AND cm.contactId IS NULL
  FROM `${DATASET}.clicks` x
  LEFT JOIN `${DATASET}.contact_market` cm ON cm.contactId = x.contactId
  UNION ALL
  SELECT 'unsub', x.eventDate, x.messageId, x.contactId,
         COALESCE(x.market, cm.market), x.market IS NULL AND cm.contactId IS NULL
  FROM `${DATASET}.unsubscribes` x
  LEFT JOIN `${DATASET}.contact_market` cm ON cm.contactId = x.contactId
), alloc AS (
  -- Market is COALESCEd to 'Unknown' BEFORE the pivot: a NULL market would
  -- split one day's sends and clicks into rows that never group together.
  SELECT e.kind, e.event_date, msg.message_type, e.contactId, e.lost,
         IF(e.lost, COALESCE(mx.market, 'Unknown'), COALESCE(e.mk, 'Unknown')) AS market,
         IF(e.lost, COALESCE(mx.share, 1.0), 1.0) AS w
  FROM ev e
  JOIN msg USING (messageId)
  LEFT JOIN `${DATASET}.message_market_mix` mx ON e.lost AND mx.messageId = e.messageId
), rows_per_contact AS (
  -- How many lost ROWS each contact has per (kind, day, type) — a row, not a
  -- message, because opens repeat. Dividing by it makes a contact's unique
  -- weight sum to exactly 1 across markets.
  SELECT kind, event_date, message_type, contactId, COUNT(*) AS n
  FROM ev JOIN msg USING (messageId)
  WHERE lost
  GROUP BY 1, 2, 3, 4
), ev_agg AS (
  SELECT a.event_date, a.market, a.message_type, a.kind,
         SUM(a.w)                                             AS n_events,
         COUNT(DISTINCT IF(a.lost, NULL, a.contactId))
           + COALESCE(SUM(IF(a.lost, a.w / p.n, 0)), 0)       AS n_unique,
         SUM(IF(a.lost, a.w, 0))                              AS est_events,
         COALESCE(SUM(IF(a.lost, a.w / p.n, 0)), 0)           AS est_unique
  FROM alloc a
  LEFT JOIN rows_per_contact p
    ON a.lost AND p.kind = a.kind AND p.event_date = a.event_date
   AND p.message_type = a.message_type AND p.contactId = a.contactId
  GROUP BY 1, 2, 3, 4
), e AS (
  SELECT event_date, market, message_type,
         SUM(IF(kind = 'sent',  n_events,   0)) AS sent,
         SUM(IF(kind = 'sent',  n_unique,   0)) AS recipients,
         SUM(IF(kind = 'open',  n_events,   0)) AS opens,
         SUM(IF(kind = 'open',  n_unique,   0)) AS unique_openers,
         SUM(IF(kind = 'click', n_events,   0)) AS clicks,
         SUM(IF(kind = 'click', n_unique,   0)) AS unique_clickers,
         SUM(IF(kind = 'unsub', n_events,   0)) AS unsubscribes,
         SUM(IF(kind = 'sent',  est_events, 0)) AS est_sent,
         SUM(IF(kind = 'open',  est_unique, 0)) AS est_unique_openers,
         SUM(IF(kind = 'click', est_unique, 0)) AS est_unique_clickers,
         SUM(IF(kind = 'unsub', est_events, 0)) AS est_unsubscribes
  FROM ev_agg
  GROUP BY 1, 2, 3
), r AS (
  -- Attributed receipts follow the same rule, split by the ATTRIBUTED
  -- message's mix when the buyer's market was lost.
  SELECT a.createdOnDate AS event_date,
         IF(a.market_lost, COALESCE(mx.market, 'Unknown'), a.market) AS market,
         m.message_type,
         SUM(IF(a.market_lost, COALESCE(mx.share, 1.0), 1.0))                 AS attributed_orders,
         SUM(IF(a.market_lost, COALESCE(mx.share, 1.0), 1.0) * a.revenue_sek) AS attributed_revenue_sek,
         SUM(IF(a.market_lost, COALESCE(mx.share, 1.0), 0))                   AS est_attributed_orders,
         SUM(IF(a.market_lost, COALESCE(mx.share, 1.0) * a.revenue_sek, 0))  AS est_attributed_revenue_sek
  FROM `${DATASET}.attributed_receipts` a
  JOIN `${DATASET}.marketing_messages` m ON m.messageId = a.attributed_messageId
  LEFT JOIN `${DATASET}.message_market_mix` mx
    ON a.market_lost AND mx.messageId = a.attributed_messageId
  GROUP BY 1, 2, 3
)
SELECT
  COALESCE(e.event_date, r.event_date)     AS event_date,
  COALESCE(e.market, r.market, 'Unknown')  AS market,
  COALESCE(e.message_type, r.message_type) AS message_type,
  COALESCE(e.sent, 0)                      AS sent,
  COALESCE(e.recipients, 0)                AS recipients,
  COALESCE(e.opens, 0)                     AS opens,
  COALESCE(e.unique_openers, 0)            AS unique_openers,
  COALESCE(e.clicks, 0)                    AS clicks,
  COALESCE(e.unique_clickers, 0)           AS unique_clickers,
  COALESCE(e.unsubscribes, 0)              AS unsubscribes,
  COALESCE(r.attributed_orders, 0)         AS attributed_orders,
  COALESCE(r.attributed_revenue_sek, 0)    AS attributed_revenue_sek,
  COALESCE(e.est_sent, 0)                  AS est_sent,
  COALESCE(e.est_unique_openers, 0)        AS est_unique_openers,
  COALESCE(e.est_unique_clickers, 0)       AS est_unique_clickers,
  COALESCE(e.est_unsubscribes, 0)          AS est_unsubscribes,
  COALESCE(r.est_attributed_orders, 0)     AS est_attributed_orders,
  COALESCE(r.est_attributed_revenue_sek, 0) AS est_attributed_revenue_sek,
  SAFE_DIVIDE(e.unique_openers,  e.sent)   AS open_rate,
  SAFE_DIVIDE(e.unique_clickers, e.sent)   AS click_rate,
  SAFE_DIVIDE(e.unsubscribes,    e.sent)   AS unsubscribe_rate,
  SAFE_DIVIDE(r.attributed_revenue_sek, e.sent) AS revenue_per_send_sek
FROM e
FULL JOIN r USING (event_date, market, message_type)

-- @@
-- email_revenue_share — how much of ALL trade email touched, by month/market.
--
-- The honest framing of the revenue question: total receipts revenue as the
-- denominator, email-attributed as the numerator. Because attribution is last
-- click on a 7-day window with no holdout, this is an upper bound on email's
-- contribution, not a measurement of it.
--
-- NOT market-estimated, unlike email_daily: a receipt with no attributed
-- message has no mix to split by, so only the numerator could move and the
-- share would stop meaning anything. Lost-market receipts stay 'Unknown' here.
CREATE OR REPLACE VIEW `${DATASET}.email_revenue_share` AS
SELECT
  DATE_TRUNC(createdOnDate, MONTH)      AS month,
  COALESCE(market, 'Unknown')           AS market,
  COUNT(*)                              AS orders,
  SUM(revenue_sek)                      AS revenue_sek,
  COUNTIF(is_email_attributed)          AS email_attributed_orders,
  SUM(IF(is_email_attributed, revenue_sek, 0)) AS email_attributed_revenue_sek,
  SAFE_DIVIDE(SUM(IF(is_email_attributed, revenue_sek, 0)), SUM(revenue_sek)) AS email_revenue_share,
  LOGICAL_OR(in_broken_fx_window)       AS contains_broken_fx_rows
FROM `${DATASET}.attributed_receipts`
GROUP BY 1, 2

-- @@
-- subscriber_base — the list itself, by market. The denominator nothing else has.
--
-- Deleted contacts are excluded: they have no email and cannot be mailed, so
-- counting them would inflate every list-health percentage. (In practice they
-- never land — Voyado sends erasures as changeType='DELETE' — so this filter is
-- belt and braces.)
--
-- TWO REVENUE COLUMNS, DELIBERATELY. `revenue_12m_computed` is summed from
-- receipts_sek over the trailing 365 days; `revenue_12m_voyado` is Voyado's own
-- contact-level purchaseAmount12months. They disagree by a consistent ~15%
-- (SE 0.846, NO 0.845, KR 0.863, DK 0.905, FI 0.875 — but Unknown 1.012), and
-- the cause is not established: the leading suspect is that Voyado's aggregate
-- is recomputed on its own schedule over its own window rather than a true
-- trailing 365 days. Until someone confirms it with Voyado, showing one number
-- and hiding the other would be picking a winner without evidence. Lead with
-- the computed column — it is reproducible from rows in this warehouse.
-- The two aggregates are computed in SEPARATE CTEs and joined by a final SELECT
-- that aggregates nothing. The obvious shape — aggregating contact_market while
-- carrying ANY_VALUE(rev.revenue_12m_computed) from a joined CTE — creates the
-- view happily and then fails at QUERY time with "Aggregations of aggregations
-- are not allowed", because BigQuery inlines the view into the caller and the
-- ANY_VALUE lands on top of the CTE's SUM. Validating the CREATE is not enough;
-- a view like this must be SELECTed from before it is believed.
CREATE OR REPLACE VIEW `${DATASET}.subscriber_base` AS
WITH base AS (
  SELECT
    market,
    COUNT(*)                                        AS contacts,
    COUNTIF(is_subscribed)                          AS subscribed,
    COUNTIF(statusEmail = 'Unsubscribed')           AS unsubscribed,
    COUNTIF(statusEmail IN ('HardBounced', 'BouncedTooManyTimes', 'Dropped')) AS undeliverable,
    COUNTIF(statusEmail = 'Inactive')               AS inactive,
    SAFE_DIVIDE(COUNTIF(is_subscribed), COUNT(*))   AS subscribed_rate,
    COUNTIF(latestReceiptDateTime IS NOT NULL)      AS with_purchase,
    SUM(purchaseAmountTotal)                        AS lifetime_revenue_voyado,
    SUM(purchaseAmount12months)                     AS revenue_12m_voyado
  FROM `${DATASET}.contact_market`
  WHERE NOT COALESCE(isDeleted, FALSE)
  GROUP BY market
), rev AS (
  SELECT cm.market, SUM(r.revenue_sek) AS revenue_12m_computed
  FROM `${DATASET}.receipts_sek` r
  JOIN `${DATASET}.contact_market` cm USING (contactId)
  WHERE r.createdOnDate >= DATE_SUB(CURRENT_DATE(), INTERVAL 365 DAY)
  GROUP BY cm.market
)
SELECT base.*, rev.revenue_12m_computed
FROM base
LEFT JOIN rev USING (market)
