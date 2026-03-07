-- 017: Add CardRush stock columns to cardrush_link
-- Populated by JP scraper Lambda during each pipeline run

ALTER TABLE cardrush_link ADD COLUMN IF NOT EXISTS cardrush_stock INTEGER;
ALTER TABLE cardrush_link ADD COLUMN IF NOT EXISTS cardrush_stock_subgrade INTEGER;
