# Lens: Demand & assortment

Question: what do people want that Babyshop does not serve well — brands or
products it lacks, demand that lands on thin or mis-named categories, and
assortment that exists but is not found?

## Sources

`babyshop_marts`: `agg_daily_on_site_search` (date × market × search_term),
`agg_product_velocity` (per-SKU sales, GA4 views, stock, ad spend, 28/90d),
`dim_product` (brand, product_type, category_root per market),
`agg_daily_kuvio_products` (P&L by brand × category),
`agg_daily_kpis_by_brand`, `agg_daily_kpis_by_gads_product`,
`fct_product_sales_daily`, `agg_daily_landing_page_sessions`.
`babyshop_raw.gads_<market>_<cid>_keyword_view_native` and
`…_shopping_product_performance_native` for paid-search demand.

Search Console marts are empty for Babyshop — do not use them.

## Lines of enquiry

1. **Brand gaps.** On-site search terms that are brand names not in
   `dim_product` (or in it with almost no sellable SKUs). Rank by searching
   users; check the pattern across markets and across months.
2. **Unmet product demand.** High-volume search terms whose matching
   assortment is thin, out of stock, or converts far below the site average.
3. **Category language.** Terms people search for that do not match the
   category / product-type names used on the site in that market (especially
   FI, NO, DK where names are translated from Swedish).
4. **Under-exposed winners.** SKUs/brands with high conversion and margin but
   low views or ad spend; and the reverse — heavy traffic or spend on
   products that do not convert.
5. **Market assortment gaps.** Brands or categories that sell strongly in one
   Nordic market and are missing, unlisted or unsold in another.
6. **Seasonality timing.** Categories where demand (search, views) rises
   before the assortment or ad spend is in place.
