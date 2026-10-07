# Lens: Conversion & content

Question: where do visitors who want to buy fail to, because information or
infrastructure is missing?

## Sources

`babyshop_marts`: `agg_funnel_snapshots` (user-based closed funnel by market
and scope), `agg_daily_landing_page_sessions`,
`agg_daily_landing_page_events`, `agg_daily_events_by_session_channel`,
`fct_product_sales_daily` (views → add-to-cart → purchase per SKU),
`dim_product` (feed attributes: title, product_type, gtin, labels).
`babyshop_staging.stg_catalog__product_feed_daily` for feed completeness.

Reviews, size-guide coverage and on-page content are NOT in the warehouse —
put those under `needs_data` unless the feed proves the gap.

## Lines of enquiry

1. Funnel step drop-off per market vs. the best market, by device and
   channel where available.
2. Products and categories with many views and a low add-to-cart rate.
3. Feed/content completeness (missing attributes, thin titles) against
   conversion.
4. Landing pages with high paid or organic entry volume and poor engagement.
