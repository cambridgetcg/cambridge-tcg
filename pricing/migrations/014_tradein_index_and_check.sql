-- Migration 014: Add tradein_items.sku index + tradein_submissions status CHECK constraint
-- Rationale: index speeds up SKU lookups in status checks; CHECK prevents invalid status values

CREATE INDEX IF NOT EXISTS idx_tradein_items_sku ON tradein_items (sku);

-- Add CHECK constraint only if it doesn't already exist
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_tradein_status'
    ) THEN
        ALTER TABLE tradein_submissions
            ADD CONSTRAINT chk_tradein_status
            CHECK (status IN ('submitted', 'received', 'paid', 'cancelled'));
    END IF;
END $$;
