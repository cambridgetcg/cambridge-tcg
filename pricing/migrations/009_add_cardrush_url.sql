-- Migration 009: Add cardrush_url column to cardrush_link
-- Stores the source CarRush product page URL for each SKU.
-- Previously only stored transiently in the S3 xlsx pricefeed.

ALTER TABLE cardrush_link
  ADD COLUMN IF NOT EXISTS cardrush_url TEXT;
