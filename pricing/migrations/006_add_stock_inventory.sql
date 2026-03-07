-- 006: Add stock_inventory and stock_config tables
-- Migrates stock tracking from local JSON (stock_data.json) to RDS as source of truth.
--
-- stock_inventory: per-SKU stock quantities and cost basis
-- stock_config: key-value store for listing tier configuration
--
-- Run: psql -h $PROXY_ENDPOINT -U $DB_USER -d op_cardrush_link -f 006_add_stock_inventory.sql

CREATE TABLE IF NOT EXISTS stock_inventory (
    sku             VARCHAR(64) PRIMARY KEY,
    quantity        INTEGER NOT NULL DEFAULT 0,
    total_cost_yen  INTEGER NOT NULL DEFAULT 0,
    purchased_qty   INTEGER NOT NULL DEFAULT 0,
    last_updated    TIMESTAMP NOT NULL DEFAULT NOW(),
    created_at      TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_stock_inventory_has_stock
    ON stock_inventory (sku) WHERE quantity > 0;

CREATE TABLE IF NOT EXISTS stock_config (
    config_key    VARCHAR(64) PRIMARY KEY,
    config_value  JSONB NOT NULL,
    updated_at    TIMESTAMP NOT NULL DEFAULT NOW()
);
