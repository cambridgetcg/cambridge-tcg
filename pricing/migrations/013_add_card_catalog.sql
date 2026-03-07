-- 013_add_card_catalog.sql
-- Card catalog: stores all OPTCG card metadata (base + parallel variants).
-- card_image_id FK on cardrush_link links pricing rows to catalog metadata.

CREATE TABLE IF NOT EXISTS card_catalog (
    card_image_id    VARCHAR(50)  PRIMARY KEY,  -- e.g. "OP01-001", "OP01-001_p1"
    card_set_id      VARCHAR(20)  NOT NULL,     -- e.g. "OP01-001" (same for base + parallels)
    set_id           VARCHAR(20),               -- e.g. "OP-01" (OPTCG set identifier)
    card_name        VARCHAR(200),
    rarity           VARCHAR(20),               -- C, UC, R, SR, SEC, L, SP
    card_color       VARCHAR(50),               -- Red, Blue, Green, Purple, Black, Yellow
    card_type        VARCHAR(50),               -- Leader, Character, Event, Stage, DON!!
    card_text        TEXT,                       -- card ability text
    card_cost        VARCHAR(100),              -- play cost (some cards have text values)
    card_power       VARCHAR(100),              -- sometimes contains text (API quirk)
    card_counter     VARCHAR(100),
    card_life        VARCHAR(100),              -- leaders only
    card_attribute   VARCHAR(200),              -- Slash, Strike, Ranged, Wisdom, Special
    card_trigger     TEXT,                       -- trigger effect text
    market_price     NUMERIC(10, 2),            -- TCGPlayer market price USD
    inventory_price  NUMERIC(10, 2),            -- TCGPlayer inventory price USD
    date_scraped     DATE,
    updated_at       TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_card_catalog_set_id ON card_catalog(card_set_id);
CREATE INDEX IF NOT EXISTS idx_card_catalog_set ON card_catalog(set_id);

-- FK column on cardrush_link to JOIN into card_catalog
ALTER TABLE cardrush_link ADD COLUMN IF NOT EXISTS card_image_id VARCHAR(50);
CREATE INDEX IF NOT EXISTS idx_cardrush_card_image ON cardrush_link(card_image_id);

-- Unique index on sku (enables ON CONFLICT (sku) for OPTCG scraper UPSERT)
CREATE UNIQUE INDEX IF NOT EXISTS idx_cardrush_link_sku ON cardrush_link(sku);
