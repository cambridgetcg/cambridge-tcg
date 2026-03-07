-- Competitor price monitoring table
-- ~2,000 rows/day (400 SKUs × 5 results). Prune after 90 days.
-- price_ratio = competitor_price / cost_gbp (our Japanese acquisition cost)

CREATE TABLE IF NOT EXISTS browse_price_monitor (
    id SERIAL PRIMARY KEY,
    sku VARCHAR(32) NOT NULL,
    cost_gbp NUMERIC(10,2) NOT NULL,
    selling_price NUMERIC(10,2) NOT NULL,
    competitor_price NUMERIC(10,2) NOT NULL,
    competitor_seller VARCHAR(64),
    competitor_item_id VARCHAR(64),
    competitor_url VARCHAR(512),
    competitor_title VARCHAR(256),
    price_ratio NUMERIC(6,4),
    classification VARCHAR(16) NOT NULL,
    rank SMALLINT NOT NULL,
    scanned_at TIMESTAMP NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_browse_monitor_classification
    ON browse_price_monitor (classification, scanned_at DESC);

CREATE INDEX IF NOT EXISTS idx_browse_monitor_sku_time
    ON browse_price_monitor (sku, scanned_at DESC);

CREATE INDEX IF NOT EXISTS idx_browse_monitor_scanned_at
    ON browse_price_monitor (scanned_at);
