-- Migration 016: Add cardrush_url_subgrade column to cardrush_link
-- Stores the CardRush product page URL for the subgrade (状態A-) condition.

ALTER TABLE cardrush_link
  ADD COLUMN IF NOT EXISTS cardrush_url_subgrade TEXT;
