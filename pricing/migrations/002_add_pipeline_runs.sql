-- Pipeline run tracking table
-- One row per Lambda execution (~5 rows/day). Auto-prunable.

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id SERIAL PRIMARY KEY,
    stage VARCHAR(32) NOT NULL,        -- 'scraper', 'fx-updater', 'calculator', 'shopify', 'ebay'
    status VARCHAR(16) NOT NULL,       -- 'success', 'failure', 'partial'
    rows_affected INTEGER DEFAULT 0,
    detail TEXT,                        -- optional error message or summary
    completed_at TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_pipeline_runs_stage_time
    ON pipeline_runs (stage, completed_at DESC);
