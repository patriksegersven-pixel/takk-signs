# Lens: Customer & CRM

Question: where can Babyshop earn more GP2 from customers it already has or
is already acquiring — repeat purchase, lifetime value, email, upsell,
cross-sell and bundles?

## Sources

New warehouse (`babyshop_marts`): `fct_customer_orders` (one row per order
with `customer_key`, `order_seq_observed`, `days_since_prev_order`,
`is_new_365`), `agg_customer_cohorts_monthly`, `agg_customer_kpis_monthly`,
`agg_customer_retention_monthly`, `agg_customer_scope_summary`,
`agg_customer_first_purchase_drivers`, `fct_email_campaign_daily`.
Order lines: `babyshop_staging.stg_norce__order_items` joined to
`stg_norce__orders`; product attributes in `babyshop_marts.dim_product`.

Old project: `voyado.*` (contacts, messages, deliveries, opens, clicks,
`attributed_receipts`, `control_group`, `subscriber_base`, `unsubscribes`)
and `norce.*` (`customer_facts`, `cohort_retention`,
`first_purchase_products`, `order_items`).

## Lines of enquiry

1. **Second order.** What share of new customers buy again within 90/180/365
   days, per market and per first-purchase category/brand? Where is the gap
   between the best and worst segments, and when does the second order
   typically come (the window a flow should hit)?
2. **First-purchase product → LTV.** Which entry products or brands produce
   customers with clearly higher 12-month value? Are those products the ones
   acquisition is pushed towards?
3. **Life-stage progression.** Children outgrow sizes on a predictable clock.
   Do customers who bought size X come back for the next size / next season?
   How many do not, and when should a reminder fire?
4. **Replenishment.** Consumables and fast-outgrown staples: observed
   re-order interval vs. customers who never re-order.
5. **Cross-sell and bundles.** Product pairs frequently bought together or in
   sequence (within 30 days) but seldom in the same basket; categories with
   single-item baskets that have a natural companion.
6. **Lapsed and at-risk.** Size and past value of customers 12–24 months
   since last order; win-back potential.
7. **Email programme.** Coverage (share of buyers who are reachable
   subscribers per market), automation vs. campaign revenue share, flows that
   exist vs. obvious ones that do not (welcome, post-purchase, abandoned
   cart, browse abandonment, win-back, size-up, birthday). Use the Voyado
   control group where it gives a measured lift.
8. **Market gaps.** Any of the above where one Nordic market is far behind
   the best one without an obvious structural reason.
