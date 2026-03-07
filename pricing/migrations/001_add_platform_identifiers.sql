-- Migration 001: Add platform identifier columns to cardrush_link
-- These columns store the listing IDs for each sales platform,
-- enabling automated price pushes via API.
--
-- Run against: op_cardrush_link database
-- Table: cardrush_link

ALTER TABLE cardrush_link
  ADD COLUMN IF NOT EXISTS ebay_item_number_business VARCHAR(64),
  ADD COLUMN IF NOT EXISTS cardmarket_id VARCHAR(64);

-- Optional: Add indexes for lookup performance during API push
CREATE INDEX IF NOT EXISTS idx_cardrush_link_ebay_business
  ON cardrush_link (ebay_item_number_business)
  WHERE ebay_item_number_business IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_cardrush_link_cardmarket
  ON cardrush_link (cardmarket_id)
  WHERE cardmarket_id IS NOT NULL;
