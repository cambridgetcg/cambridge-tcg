-- Migration 005: Add itemized cost columns to cardrush_link
-- Purpose: Store landed cost breakdown for self-contained pricing (no xlsx dependency)
--
-- landed_cost_gbp = cost_gbp + shipping_fee_gbp + (cost_gbp * import_duty_rate) + handling_fee_gbp
--
-- Usage: psql -h $PROXY_ENDPOINT -U $DB_USER -d op_cardrush_link -f 005_add_itemized_costs.sql

ALTER TABLE cardrush_link
  ADD COLUMN IF NOT EXISTS shipping_fee_gbp NUMERIC(10,4),
  ADD COLUMN IF NOT EXISTS import_duty_rate NUMERIC(6,4),
  ADD COLUMN IF NOT EXISTS handling_fee_gbp NUMERIC(10,4),
  ADD COLUMN IF NOT EXISTS landed_cost_gbp  NUMERIC(10,4);
