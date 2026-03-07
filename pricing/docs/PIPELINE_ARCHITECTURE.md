# Japanese TCG Pricing Pipeline — Architecture

> Source of truth: **CardRush** (cardrush.jp) scraped prices
> NOT the CardForum pipeline (which uses CardMarket/TCGPlayer)

## Pipeline Overview

```
                    JAPANESE TCG PRICING PIPELINE
                    =============================

  ┌─────────────────────────────────────────────────────────┐
  │                   DATA SOURCES                          │
  │                                                         │
  │  CardRush (cardrush.jp)     Amdoren FX API              │
  │  - Japanese card prices     - GBP/JPY exchange rate      │
  │  - Scraped via HTTP/BS4     - REST API                   │
  └──────────┬──────────────────────────┬───────────────────┘
             │                          │
             ▼                          ▼
  ┌─────────────────────┐   ┌──────────────────────┐
  │  scraper-cardrush   │   │  cardrush-fx-updater │
  │  (Py 3.12, arm64)   │   │  (Py 3.12, arm64)    │
  │  VPC-connected       │   │  VPC-connected        │
  │                     │   │                      │
  │  Scrapes JPY prices │   │  Fetches live rate   │
  │  Dual-write:        │   │  from Amdoren API →  │
  │  → S3 xlsx (archive)│   │  writes gbp_to_jpy   │
  │  → RDS cost_yen     │   │  to RDS              │
  └──────────┬──────────┘   └──────────┬───────────┘
             │                          │
             ▼                          ▼
  ┌─────────────────────────────────────────────────────────┐
  │              RDS PostgreSQL (cardrush_link)              │
  │         cost_yen ◄────┘          gbp_to_jpy ◄────┘      │
  └──────────────────────────┬──────────────────────────────┘
                             │
                             ▼
                  ┌──────────────────────┐
                  │  price-calculator    │
                  │  (Py 3.12, arm64)    │
                  │  VPC-connected        │
                  │                      │
                  │  Step 1: Derive      │
                  │    cost_gbp =        │
                  │    cost_yen /        │
                  │    gbp_to_jpy        │
                  │                      │
                  │  Step 2: Calculate   │
                  │    3 channel selling │
                  │    prices            │
                  │    P = C(1+M)/(1-F)  │
                  │    round up + £0.80  │
                  └──────────┬───────────┘
                             │
                  ┌──────────┴───────────┐
                  ▼                      ▼
       ┌─────────────────┐   ┌──────────────────┐
       │  api-shopify    │   │  api-ebay        │
       │  (Py 3.12, VPC) │   │  (Py 3.12, VPC)  │
       │                 │   │                   │
       │  Reads RDS      │   │  Reads RDS        │
       │  GraphQL lookup │   │  ReviseInventory  │
       │  REST update    │   │  Status (batch 4) │
       │  Rate: 2/sec   │   │  Rate: 5000/15sec │
       └────────┬────────┘   └─────────┬────────┘
                │                      │
                ▼                      ▼
  ┌─────────────────────────────────────────────────────────┐
  │                   SALES PLATFORMS                       │
  │                                                         │
  │  Shopify (via api-shopify)                              │
  │  eBay Business (via api-ebay)                           │
  │  Cardmarket (TBD — prices calculated in RDS)            │
  └─────────────────────────────────────────────────────────┘
```

## Pipeline Steps

| Step | Lambda | Runtime | What it does |
|------|--------|---------|--------------|
| 1 | `scraper-cardrush` | Py 3.12, arm64, VPC | Scrapes JPY prices from CardRush → S3 xlsx (archive) + RDS `cost_yen` |
| 2 | `cardrush-fx-updater` | Py 3.12, arm64, VPC | Fetches live GBP/JPY rate from Amdoren API → updates `gbp_to_jpy` in RDS |
| 3 | `price-calculator` | Py 3.12, arm64, VPC | Derives `cost_gbp` from cost_yen/gbp_to_jpy, then calculates 3 channel selling prices |
| 4 | `api-shopify` | Py 3.12, arm64, VPC | Reads RDS → pushes Shopify prices (GraphQL + REST) |
| 5 | `api-ebay` | Py 3.12, arm64, VPC | Reads RDS → pushes eBay prices (Trading API batch) |

Steps 1 and 2 can run in parallel. Steps 4 and 5 run in parallel after step 3 completes.

## RDS Column Lineage (`cardrush_link` table)

Every column has a known writer. No unknown data flows.

| Column | Type | Written by | Read by |
|--------|------|-----------|---------|
| `sku` (PK) | varchar | pre-existing | all Lambdas |
| `cost_yen` | numeric | **scraper-cardrush** | price-calculator |
| `gbp_to_jpy` | numeric | cardrush-fx-updater (Amdoren API) | price-calculator |
| `cost_gbp` | numeric | **price-calculator** (derived: cost_yen / gbp_to_jpy) | price-calculator |
| `ebay_business_selling_price` | numeric | price-calculator | api-ebay |
| `cardmarket_selling_price` | numeric | price-calculator | (future: api-cardmarket) |
| `shopify_selling_price` | numeric | price-calculator | api-shopify |
| `ebay_item_number_business` | varchar(64) | migrate_platform_ids.py (one-time) | api-ebay |
| `cardmarket_id` | varchar(64) | (future) | (future: api-cardmarket) |

## Pricing Formula

```python
# Step 1 (price-calculator): Derive cost_gbp
cost_gbp = cost_yen / gbp_to_jpy

# Step 2 (price-calculator): Calculate VAT-inclusive selling prices per channel
# VAT is on the total (fee and VAT interact), so denominator is (1 - F(1+V))
P = C × (1 + M) × (1 + V) / (1 - F × (1 + V))    # C=cost_gbp, M=margin, V=VAT, F=platform_fee
selling_price = ceil(P) + 0.80
```

Default VAT: **20%**

Platform fees (env vars on price-calculator):
| Channel | Fee |
|---------|-----|
| eBay Business | 12% |
| Cardmarket | 8% |
| Shopify | 5% |

Default target margin: **22%**

## CDK-managed Track (separate pipeline)

| Lambda | Runtime | Trigger | What it does |
|--------|---------|---------|--------------|
| `trading-card-price-fetcher` | Node 20, x86, 300s | cron(0 8,20 * * ? *) | CDK stack `TradingCardPricerStack`, uses SNS + Secrets Manager, tagged `tcg-tradein`. Writes to `card_prices_v2` table (not `cardrush_link`). |

## Scheduled Triggers (EventBridge)

| Rule | Schedule | Target |
|------|----------|--------|
| `trading-card-price-fetcher-schedule` | `cron(0 8,20 * * ? *)` | trading-card-price-fetcher |

> **TODO**: Add EventBridge rules for the main pipeline (steps 1-5 above).

## AWS Resources

| Resource | Purpose |
|----------|---------|
| S3 Bucket | Excel file storage (scraper price archive) |
| RDS PostgreSQL | `op_cardrush_link` database, `cardrush_link` table |
| RDS Proxy | Connection pooling for VPC Lambdas |
| VPC | `vpc-073cdce8e84cbccdc` (for RDS-connected Lambdas) |
| Secrets Manager | `ebay-trading-api-credentials` (eBay OAuth), other API keys |
| Amdoren API | GBP/JPY exchange rate |
| Shopify API | Price updates (GraphQL lookup + REST update) |
| eBay Trading API | Price updates (ReviseInventoryStatus batch) |
| SNS | Price update notifications (trading-card-price-fetcher) |

## Lambda Layers / Dependencies

| Lambda | Dependencies |
|--------|-------------|
| scraper-cardrush | psycopg2-binary, requests, openpyxl, beautifulsoup4 (bundled or layer) |
| cardrush-fx-updater | requests, psycopg2 (layer: `price-scraper-py312`) |
| price-calculator | psycopg2 (layer: `price-scraper-py312`) |
| api-shopify | requests, psycopg2-binary (bundled via requirements.txt) |
| api-ebay | requests, psycopg2-binary (bundled via requirements.txt) |

## Migrations

| File | Purpose |
|------|---------|
| `pricing/migrations/001_add_platform_identifiers.sql` | Add ebay_item_number_business, cardmarket_id columns |
| `pricing/migrations/migrate_platform_ids.py` | One-time data migration: extract item numbers from xlsx → RDS |

## Key Observations

1. **Naming confusion**: `cardrush_scraper` Lambda in AWS is NOT a scraper — it's the FX rate updater (repo name: `cardrush-fx-updater`)
2. **S3 xlsx preserved as archive**: scraper-cardrush still writes date columns to xlsx for historical record, but RDS is the operational data path
3. **eBay API push**: Implemented via `api-ebay` using ReviseInventoryStatus (batch of 4)
4. **Shopify API push**: GraphQL lookup + REST update, reads from RDS
5. **Cardmarket push**: Prices calculated in RDS but no API push Lambda yet
6. **eBay OAuth**: Refresh token ~1.9 year validity, stored in Secrets Manager. Monitor expiry.
