-- Migration 012: Add USD price columns for English card pricing (OPTCG API)
-- price_usd: TCGPlayer market price in USD (EN cards only; JP cards use price_yen)
-- usd_to_gbp: FX rate — GBP per 1 USD (e.g. 0.79)
-- Language is encoded in SKU suffix (-EN vs -JP), no dedicated column needed.

ALTER TABLE cardrush_link ADD COLUMN IF NOT EXISTS price_usd NUMERIC(10, 2);
ALTER TABLE cardrush_link ADD COLUMN IF NOT EXISTS usd_to_gbp NUMERIC(10, 6);

ALTER TABLE price_history ADD COLUMN IF NOT EXISTS price_usd NUMERIC(10, 2);
