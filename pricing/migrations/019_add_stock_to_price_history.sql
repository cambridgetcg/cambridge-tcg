-- 019: Add CardRush stock and A- price columns to price_history
-- Snapshotted by calculator (Step 4) alongside existing price fields

ALTER TABLE price_history ADD COLUMN IF NOT EXISTS cardrush_stock INTEGER;
ALTER TABLE price_history ADD COLUMN IF NOT EXISTS cardrush_stock_subgrade INTEGER;
ALTER TABLE price_history ADD COLUMN IF NOT EXISTS price_yen_subgrade INTEGER;
