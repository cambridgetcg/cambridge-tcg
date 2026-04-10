-- Migration 020: Fix trade-in table schema for storefront
-- The storefront route.ts inserts columns that don't exist in RDS yet.
-- Without these, every POST /api/tradein/submit fails with 500.

-- tradein_items: add card_number, name, set_code
ALTER TABLE tradein_items ADD COLUMN IF NOT EXISTS card_number VARCHAR(30);
ALTER TABLE tradein_items ADD COLUMN IF NOT EXISTS name VARCHAR(500);
ALTER TABLE tradein_items ADD COLUMN IF NOT EXISTS set_code VARCHAR(20);

-- tradein_submissions: add bank details for cash payments
ALTER TABLE tradein_submissions ADD COLUMN IF NOT EXISTS bank_sort_code VARCHAR(10);
ALTER TABLE tradein_submissions ADD COLUMN IF NOT EXISTS bank_account_number VARCHAR(30);

-- Update status CHECK constraint to include all storefront states
ALTER TABLE tradein_submissions DROP CONSTRAINT IF EXISTS chk_tradein_status;
ALTER TABLE tradein_submissions ADD CONSTRAINT chk_tradein_status
  CHECK (status IN (
    'submitted', 'shipped', 'received', 'grading', 'accepted',
    'discrepancy', 'waiting_customer', 'paid', 'completed',
    'cancelled', 'expired', 'returned'
  ));