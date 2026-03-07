-- 007: Add stock_manual table for inventory admin app
-- Isolated from stock_inventory (which has scraper-updated cost data).
-- This table is managed exclusively via the Streamlit admin UI.
--
-- Run: psql -h $PROXY_ENDPOINT -U $DB_USER -d op_cardrush_link -f 007_add_stock_manual.sql

CREATE TABLE IF NOT EXISTS stock_manual (
    sku             VARCHAR(64) PRIMARY KEY,
    quantity        INTEGER NOT NULL DEFAULT 0,
    total_cost_yen  INTEGER NOT NULL DEFAULT 0,
    purchased_qty   INTEGER NOT NULL DEFAULT 0,
    last_updated    TIMESTAMP NOT NULL DEFAULT NOW(),
    created_at      TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_stock_manual_has_stock
    ON stock_manual (sku) WHERE quantity > 0;

-- Seed from current stock_inventory data
INSERT INTO stock_manual (sku, quantity, total_cost_yen, purchased_qty, last_updated, created_at)
SELECT sku, quantity, total_cost_yen, purchased_qty, last_updated, created_at
FROM stock_inventory
ON CONFLICT (sku) DO NOTHING;
