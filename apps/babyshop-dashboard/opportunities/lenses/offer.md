# Lens: Offer

Question: is Babyshop's offer — price, shipping, delivery promise, payment —
competitive in each market, and where does a change pay for itself?

## Sources

`babyshop_marts.pricewatch_positions / _offers / _changes` (competitor price
positions; SE only, stale since 2026-09-02 — treat as indicative),
`babyshop_staging.stg_norce__sku_prices` (price lists per market),
`stg_norce__orders` (`freight_cost`, delivery mode, payment method),
`agg_daily_profit_ladder` (`cost_shipping`, `cost_payment`).

Competitor shipping and payment terms are NOT available — put those ideas
under `needs_data`.

## Lines of enquiry

1. Cross-market price consistency: the same SKU priced out of line between
   markets after currency and VAT (e.g. FI prices set as SEK ÷ 10).
2. Price position vs. competitors where we have it: products priced above
   the cheapest buyable offer with margin to spare, and products priced below
   it where we are leaving margin.
3. Basket-size distribution around each market's free-shipping threshold.
4. Delivery-mode and payment-method mix per market vs. cost per order.
