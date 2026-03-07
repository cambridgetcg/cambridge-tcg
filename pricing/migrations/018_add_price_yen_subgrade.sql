-- 018: Add A- condition price column to cardrush_link
-- Populated by JP scraper Lambda during each pipeline run (A- pass)

ALTER TABLE cardrush_link ADD COLUMN IF NOT EXISTS price_yen_subgrade INTEGER;
