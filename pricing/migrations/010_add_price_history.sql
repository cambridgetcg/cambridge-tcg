-- Migration 010: Add price_history table for daily price snapshots
-- One row per SKU per pipeline run (~486 rows/day, ~177K rows/year)
-- No FK to cardrush_link — history survives if SKU is removed

CREATE TABLE IF NOT EXISTS price_history (
    id SERIAL PRIMARY KEY,
    sku VARCHAR(64) NOT NULL,
    price_yen NUMERIC(10,2),
    cost_gbp NUMERIC(10,4),
    landed_cost_gbp NUMERIC(10,4),
    gbp_to_jpy NUMERIC(10,4),
    shopify_selling_price NUMERIC(10,2),
    ebay_selling_price NUMERIC(10,2),
    cardmarket_selling_price NUMERIC(10,2),
    recorded_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_price_history_sku_date
    ON price_history (sku, recorded_at DESC);

CREATE INDEX IF NOT EXISTS idx_price_history_date
    ON price_history (recorded_at DESC);
