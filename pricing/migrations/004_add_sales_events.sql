-- Sales event tracking for cross-platform stock sync.
-- Records every sale/cancellation from Shopify and eBay, tracks cross-sync
-- and local-sync status independently.

CREATE TABLE IF NOT EXISTS sales_events (
    id SERIAL PRIMARY KEY,
    platform VARCHAR(16) NOT NULL,            -- 'shopify' or 'ebay'
    order_id VARCHAR(64) NOT NULL,            -- platform order ID
    sku VARCHAR(64) NOT NULL,
    quantity INTEGER NOT NULL,                -- positive=sale, negative=cancellation
    event_type VARCHAR(16) NOT NULL DEFAULT 'sale',  -- 'sale' or 'cancellation'
    unit_price_gbp NUMERIC(10,2),
    cross_synced BOOLEAN NOT NULL DEFAULT FALSE,
    cross_synced_at TIMESTAMP,
    cross_sync_error TEXT,
    local_synced BOOLEAN NOT NULL DEFAULT FALSE,
    local_synced_at TIMESTAMP,
    raw_payload JSONB,
    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_sales_event UNIQUE (platform, order_id, sku, event_type)
);

CREATE INDEX IF NOT EXISTS idx_sales_events_cross_sync
    ON sales_events (cross_synced, created_at DESC)
    WHERE cross_synced = FALSE;

CREATE INDEX IF NOT EXISTS idx_sales_events_local_sync
    ON sales_events (local_synced, created_at DESC)
    WHERE local_synced = FALSE;

-- Platform listing cache: maps SKU → platform-specific IDs.
-- Refreshed by push_ebay_stock.py and push_shopify_stock.py after each run.

CREATE TABLE IF NOT EXISTS platform_listings (
    sku VARCHAR(64) NOT NULL,
    platform VARCHAR(16) NOT NULL,            -- 'ebay' or 'shopify'
    platform_id VARCHAR(128) NOT NULL,        -- eBay ItemID or Shopify inventory_item_id
    secondary_id VARCHAR(128),                -- Shopify location_id (eBay: NULL)
    current_available INTEGER DEFAULT 0,
    last_refreshed TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (sku, platform)
);
