-- Migration 015: Dynamic pricing support
-- Partial index for fast velocity lookups + audit column for applied margin

-- Partial index: only 'sale' rows in recent window matter for velocity
CREATE INDEX IF NOT EXISTS idx_sales_events_velocity
    ON sales_events (sku, created_at DESC)
    WHERE event_type = 'sale';

-- Record which margin was applied to each price snapshot
ALTER TABLE price_history ADD COLUMN IF NOT EXISTS applied_margin NUMERIC(5,4);
