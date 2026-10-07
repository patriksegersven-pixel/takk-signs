# Lens: Operations

Question: where is GP2 leaking through stock, returns, shipping and
fulfilment — money already earned and then lost?

## Sources

`babyshop_marts`: `agg_product_velocity` (per SKU × market: stock state,
`days_out_of_stock_28d`, `lost_units_est`, `lost_revenue_est`,
`ga4_views_while_oos`, `ad_spend_28d`, `is_out_of_stock_with_demand`),
`agg_stockout_daily`, `agg_norce_critical_stock` (incl. `incoming_units`,
`next_delivery_date`), `agg_norce_inventory_daily`,
`agg_daily_profit_ladder` (`cost_returns`, `cost_shipping`, `cost_payment`,
`cost_fulfilment` by market × channel), `agg_daily_kuvio_products`
(shipping/extra cost by brand × category).
`babyshop_staging`: `stg_norce__orders` (`freight_cost`, delivery mode),
`stg_norce__order_items`, `stg_norce__order_status`.
`babyshop_raw`: `bc_return_receipts`, `bc_sales_credit_memo_lines`,
`bc_sales_invoice_lines` (Business Central returns and credits by item).

## Lines of enquiry

1. **Stock-outs with demand.** GP2 lost on SKUs that are out of stock while
   still getting views, searches or ad clicks. Concentration by brand,
   category and size; how much is recurring (same items, repeatedly) and
   therefore a buying/replenishment problem rather than a one-off.
2. **Ad spend on unavailable products.** Spend on SKUs that are out of stock
   or have a broken size run.
3. **Returns.** Return cost as a share of revenue by market, brand, category
   and size. Outliers that point to a fix: sizing information, product
   content, a specific brand's fit, serial returners.
4. **Shipping economics.** Orders where shipping cost eats the GP2: small
   baskets, heavy/bulky categories, markets with high cost per order. What
   the free-shipping threshold does to basket size in each market.
5. **Over-stock.** Capital tied in slow stock with high days of cover that
   could be cleared at a planned margin rather than a later fire-sale.
6. **Market cost gaps.** Any cost line where one market is far out of line
   with the others without a structural reason.
