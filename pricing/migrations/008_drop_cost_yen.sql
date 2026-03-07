-- Migration 008: Drop cost_yen column from cardrush_link
--
-- cost_yen was the tax-inclusive scraped price (~11.56% above shelf price).
-- The pipeline now uses price_yen (tax-excluded shelf price) directly,
-- eliminating double-counting of Japanese consumption tax in the landed cost model.
--
-- The scraper now writes to price_yen instead of cost_yen.
-- Calculator derives: cost_gbp = price_yen / gbp_to_jpy

ALTER TABLE cardrush_link DROP COLUMN IF EXISTS cost_yen;
