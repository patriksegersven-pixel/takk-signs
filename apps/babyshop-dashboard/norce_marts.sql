-- ============================================================================
--  Customer Insights marts over the Norce landing tables.
--
--  Applied by norce_sync.py (`apply_marts()`), which substitutes ${DATASET}
--  with `project-a7ade44e-e7e3-4871-a83.norce` and runs each statement.
--  Statements are separated by a double-at sentinel comment, NOT by `;` — a
--  semicolon inside a string or comment must never split a statement in half.
--  (This paragraph deliberately spells the sentinel out rather than writing it
--  literally, so the splitter cannot cut its own documentation in two.)
--  Paste any single block straight into the BigQuery console when debugging;
--  just replace ${DATASET} first.
--
--  Everything here is a VIEW: ~416k orders / ~1.43M lines is small enough that
--  materialising buys nothing but a staleness bug.
--
--  RULES BAKED IN (do not "fix" these without re-reading the audit)
--   • Order value  = SUM(LineAmount) EXCLUDING the shipping line PartNo
--                    '1000014'. There is no order-total field in Norce.
--   • Amounts are EX-VAT. Real VAT sits per line; the header VatRate is 0.
--   • NO status filtering anywhere. User decision: returns and cancellations
--     are ignored, all values are gross.
--   • customer_hash = SHA256(LOWER(TRIM(email))) hex, computed at extract time
--     in norce_sync.py. It is the ONLY identity — Norce has no stable
--     customer id (BuyerCustomerId is null on 100% of orders).
--   • Identity is scoped PER SHOP (babyshop vs lekmer): the two are separate
--     businesses and the same address shopping in both is two customers.
--   • History starts 2025-06-11 (platform cutover). A customer whose first
--     Norce order falls in the first three months may well be an existing
--     customer from the old platform — customer_facts.migration_window_flag
--     marks them so "new customer" can be caveated in the UI.
-- ============================================================================

-- @@
-- customer_orders — one row per order, the grain everything else builds on.
--
-- market/shop come from app_key ("babyshop-se" -> shop babyshop, market SE);
-- the market codes line up with the Funnel export's market_level_1_kv so the
-- two sources can be joined at month x market.
CREATE OR REPLACE VIEW `${DATASET}.customer_orders` AS
SELECT
  o.Id                                              AS order_id,
  o.OrderNo                                         AS order_no,
  o.customer_hash,
  o.app_key,
  SPLIT(o.app_key, '-')[OFFSET(0)]                  AS shop,
  UPPER(SPLIT(o.app_key, '-')[OFFSET(1)])           AS market,
  DATE(o.OrderDate)                                 AS order_date,
  DATE_TRUNC(DATE(o.OrderDate), MONTH)              AS order_month,
  o.StatusId                                        AS status_id,
  o.CurrencyId                                      AS currency_id,
  -- Merchandise value only: the shipping line is a real order line in Norce.
  COALESCE(i.order_value, 0)                        AS order_value,
  COALESCE(i.items_count, 0)                        AS items_count,
  COALESCE(i.units, 0)                              AS units,
  COALESCE(i.shipping_value, 0)                     AS shipping_value
FROM `${DATASET}.orders` o
LEFT JOIN (
  SELECT
    OrderId,
    SUM(IF(PartNo != '1000014', LineAmount, 0))     AS order_value,
    COUNTIF(PartNo != '1000014')                    AS items_count,
    SUM(IF(PartNo != '1000014', QtyOrdered, 0))     AS units,
    SUM(IF(PartNo  = '1000014', LineAmount, 0))     AS shipping_value
  FROM `${DATASET}.order_items`
  GROUP BY OrderId
) i ON i.OrderId = o.Id
-- IsForgotten rows are DELETEd by norce_sync.purge_forgotten(); this is a
-- second belt in case a run is interrupted between the pull and the purge.
WHERE NOT o.IsForgotten
  AND o.customer_hash IS NOT NULL;

-- @@
-- customer_facts — one row per customer_hash x shop.
--
-- revenue_365d_from_first is the 1-year CLV numerator and stays NULL until the
-- customer's own 365 days have actually elapsed; a partially-observed cohort
-- averaged in with mature ones is the classic way to under-report CLV.
-- No 2- or 3-year CLV anywhere: user decision.
CREATE OR REPLACE VIEW `${DATASET}.customer_facts` AS
WITH base AS (
  SELECT
    customer_hash, shop,
    MIN(order_date)                                             AS first_order_date,
    MAX(order_date)                                             AS last_order_date,
    COUNT(*)                                                    AS orders_cnt,
    SUM(order_value)                                            AS revenue_total,
    ARRAY_AGG(app_key ORDER BY order_date, order_id LIMIT 1)[OFFSET(0)] AS first_app_key,
    ARRAY_AGG(market  ORDER BY order_date, order_id LIMIT 1)[OFFSET(0)] AS first_market
  FROM `${DATASET}.customer_orders`
  GROUP BY customer_hash, shop
),
window_365 AS (
  SELECT b.customer_hash, b.shop, SUM(o.order_value) AS revenue_365d
  FROM base b
  JOIN `${DATASET}.customer_orders` o
    ON o.customer_hash = b.customer_hash AND o.shop = b.shop
   AND o.order_date < DATE_ADD(b.first_order_date, INTERVAL 365 DAY)
  GROUP BY 1, 2
)
SELECT
  b.customer_hash, b.shop,
  b.first_order_date, b.first_app_key, b.first_market,
  DATE_TRUNC(b.first_order_date, MONTH)                          AS cohort_month,
  b.last_order_date, b.orders_cnt, b.revenue_total,
  SAFE_DIVIDE(b.revenue_total, b.orders_cnt)                     AS aov,
  -- Only counts once the customer has had a full year to spend it.
  IF(DATE_ADD(b.first_order_date, INTERVAL 365 DAY) <= CURRENT_DATE(),
     w.revenue_365d, NULL)                                       AS revenue_365d_from_first,
  b.orders_cnt > 1                                               AS is_repeat,
  DATE_DIFF(CURRENT_DATE(), b.last_order_date, DAY)              AS days_since_last_order,
  -- First three months of Norce history: these "new" customers may be
  -- pre-cutover regulars, so acquisition counts here are not trustworthy.
  b.first_order_date < DATE '2025-09-11'                         AS migration_window_flag
FROM base b
LEFT JOIN window_365 w
  ON w.customer_hash = b.customer_hash AND w.shop = b.shop;

-- @@
-- monthly_customer_metrics — month x market x shop.
--
-- new_customers counts FIRST-EVER orders landing that month (from
-- customer_facts, so a customer is new exactly once), returning_customers
-- counts distinct customers with an order that month who were not new.
-- Market is the market of the order, so a customer who buys SE then NO is
-- new in SE and returning in NO — that is the intended reading for a
-- market-level acquisition view.
CREATE OR REPLACE VIEW `${DATASET}.monthly_customer_metrics` AS
WITH activity AS (
  SELECT
    o.order_month AS month, o.market, o.shop,
    o.customer_hash,
    COUNT(*)          AS orders,
    SUM(o.order_value) AS revenue,
    MIN(f.first_order_date) AS first_order_date
  FROM `${DATASET}.customer_orders` o
  JOIN `${DATASET}.customer_facts` f
    ON f.customer_hash = o.customer_hash AND f.shop = o.shop
  GROUP BY 1, 2, 3, 4
)
SELECT
  month, market, shop,
  COUNTIF(DATE_TRUNC(first_order_date, MONTH) = month)      AS new_customers,
  COUNTIF(DATE_TRUNC(first_order_date, MONTH) < month)      AS returning_customers,
  COUNT(*)                                                  AS active_customers,
  SUM(orders)                                               AS orders,
  SUM(revenue)                                              AS revenue,
  SAFE_DIVIDE(SUM(revenue), SUM(orders))                    AS aov,
  -- Share of the month's active customers that had bought before.
  SAFE_DIVIDE(COUNTIF(DATE_TRUNC(first_order_date, MONTH) < month), COUNT(*)) AS repeat_rate
FROM activity
GROUP BY 1, 2, 3;

-- @@
-- cohort_retention — cohort_month x market x shop x months_since_first.
--
-- This is the CLV maturity triangle: 1-year CLV is
-- cumulative_revenue_per_customer at months_since_first = 12.
-- Offsets are generated up to the elapsed age of each cohort so the triangle
-- is dense (a month with no returning customers must show 0, not vanish) and
-- never runs past the present.
CREATE OR REPLACE VIEW `${DATASET}.cohort_retention` AS
WITH cohorts AS (
  SELECT cohort_month, first_market AS market, shop,
         COUNT(*) AS cohort_size,
         -- Cohorts younger than this many months are still filling up.
         DATE_DIFF(DATE_TRUNC(CURRENT_DATE(), MONTH), cohort_month, MONTH) AS max_offset
  FROM `${DATASET}.customer_facts`
  GROUP BY 1, 2, 3
),
grid AS (
  SELECT c.cohort_month, c.market, c.shop, c.cohort_size, off AS months_since_first
  FROM cohorts c, UNNEST(GENERATE_ARRAY(0, c.max_offset)) AS off
),
activity AS (
  SELECT
    f.cohort_month, f.first_market AS market, f.shop,
    DATE_DIFF(o.order_month, f.cohort_month, MONTH) AS months_since_first,
    COUNT(DISTINCT o.customer_hash) AS active_customers,
    SUM(o.order_value)              AS revenue
  FROM `${DATASET}.customer_facts` f
  JOIN `${DATASET}.customer_orders` o
    ON o.customer_hash = f.customer_hash AND o.shop = f.shop
  GROUP BY 1, 2, 3, 4
)
SELECT
  g.cohort_month, g.market, g.shop, g.months_since_first, g.cohort_size,
  COALESCE(a.active_customers, 0)                                   AS active_customers,
  SAFE_DIVIDE(COALESCE(a.active_customers, 0), g.cohort_size)       AS retention_rate,
  COALESCE(a.revenue, 0)                                            AS revenue,
  SAFE_DIVIDE(
    SUM(COALESCE(a.revenue, 0)) OVER (
      PARTITION BY g.cohort_month, g.market, g.shop
      ORDER BY g.months_since_first
      ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW),
    g.cohort_size)                                                  AS cumulative_revenue_per_customer
FROM grid g
LEFT JOIN activity a
  ON  a.cohort_month = g.cohort_month AND a.market = g.market AND a.shop = g.shop
  AND a.months_since_first = g.months_since_first;

-- @@
-- first_purchase_products — what NEW customers buy on their very first order.
--
-- One view, three grains, distinguished by dim_type ('brand' | 'category_l1' |
-- 'product'), because the dashboard renders them as three ranked lists off the
-- same query. Counts are FIRST ORDERS CONTAINING the thing (not units), so a
-- basket with three Molo items counts Molo once.
--
-- Joins, per the audit: OrderItem.PartNo -> product_skus.PartNo ->
-- products.ManufacturerId -> dim_manufacturers.Name for brand; the primary
-- ProductCategory -> dim_categories.DefaultFullName for category. The category
-- path is "Ecom - <L1> - <L2> - ...", so L1 is the segment AFTER the root.
-- Product names come from products.DefaultName — OrderItem.ProductName is the
-- variant label ("Foggy White Blueberry-50 cm") and is useless on its own.
CREATE OR REPLACE VIEW `${DATASET}.first_purchase_products` AS
WITH first_orders AS (
  SELECT o.order_id, o.order_month AS month, o.market, o.shop, o.order_value
  FROM `${DATASET}.customer_orders` o
  JOIN `${DATASET}.customer_facts` f
    ON f.customer_hash = o.customer_hash AND f.shop = o.shop
   AND f.first_order_date = o.order_date
),
lines AS (
  SELECT
    fo.month, fo.market, fo.shop, fo.order_id,
    li.LineAmount,
    COALESCE(m.Name, '(unknown brand)')                                  AS brand,
    COALESCE(SPLIT(c.DefaultFullName, ' - ')[SAFE_OFFSET(1)],
             c.DefaultName, '(uncategorised)')                           AS category_l1,
    COALESCE(p.DefaultName, li.ProductName, li.PartNo)                   AS product
  FROM first_orders fo
  JOIN `${DATASET}.order_items` li ON li.OrderId = fo.order_id
  LEFT JOIN `${DATASET}.product_skus`  s ON s.PartNo = li.PartNo
  LEFT JOIN `${DATASET}.products`      p ON p.Id = s.ProductId
  LEFT JOIN `${DATASET}.dim_manufacturers` m ON m.ManufacturerId = p.ManufacturerId
  LEFT JOIN `${DATASET}.product_categories` pc
         ON pc.ProductId = p.Id AND pc.IsPrimary
  LEFT JOIN `${DATASET}.dim_categories` c ON c.Id = pc.CategoryId
  WHERE li.PartNo != '1000014'
),
totals AS (
  SELECT month, market, shop,
         COUNT(DISTINCT order_id) AS first_orders,
         SUM(LineAmount)          AS first_order_revenue
  FROM lines GROUP BY 1, 2, 3
),
stacked AS (
  SELECT month, market, shop, 'brand'       AS dim_type, brand       AS dim_value, order_id, LineAmount FROM lines
  UNION ALL
  SELECT month, market, shop, 'category_l1' AS dim_type, category_l1 AS dim_value, order_id, LineAmount FROM lines
  UNION ALL
  SELECT month, market, shop, 'product'     AS dim_type, product     AS dim_value, order_id, LineAmount FROM lines
)
SELECT
  s.month, s.market, s.shop, s.dim_type, s.dim_value,
  COUNT(DISTINCT s.order_id)                                  AS new_customer_orders,
  SUM(s.LineAmount)                                           AS revenue,
  SAFE_DIVIDE(COUNT(DISTINCT s.order_id), t.first_orders)     AS order_share,
  SAFE_DIVIDE(SUM(s.LineAmount), t.first_order_revenue)       AS revenue_share
FROM stacked s
JOIN totals t ON t.month = s.month AND t.market = s.market AND t.shop = s.shop
GROUP BY s.month, s.market, s.shop, s.dim_type, s.dim_value,
         t.first_orders, t.first_order_revenue;
