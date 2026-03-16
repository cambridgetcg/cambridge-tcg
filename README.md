# Cambridge TCG — Pricing & Stock Management Pipeline

In-house stock management replacing Zoho, integrating with existing AWS pricing pipeline.

> **This is the Japanese TCG pipeline (CardRush source of truth).**
> NOT the CardForum pipeline (which uses CardMarket/TCGPlayer).

## Structure

```
cambridge-tcg/
├── pricing/                    # Japanese TCG pricing pipeline
│   ├── scrapers/
│   │   └── cardrush/           # CardRush price scraper (S3 archive + RDS cost_yen)
│   ├── fx/                     # FX rate updater (GBP/JPY → RDS)
│   ├── calculator/             # Derives cost_gbp, calculates 4 channel selling prices
│   ├── push/
│   │   ├── shopify/            # Shopify price push (RDS → GraphQL + REST)
│   │   ├── ebay/               # eBay price push (RDS → Trading API batch)
│   │   └── cardmarket/         # Cardmarket price push (planned)
│   ├── fetcher/                # CDK-managed Node.js price fetcher (separate pipeline)
│   ├── migrations/             # DB schema migrations & one-time data migrations
│   └── cache/                  # Local price cache
│
├── stock/                      # Stock count & sales channel sync
│   ├── count/                  # Stock levels, SKUs, quantities, conditions
│   ├── sync/
│   │   ├── shopify/            # Stock count → Shopify inventory sync
│   │   ├── ebay/               # Stock count → eBay inventory sync
│   │   └── cardmarket/         # Stock count → Cardmarket inventory sync
│   └── imports/                # Zoho migration data
│
├── analytics/                  # Stock valuation, margin reports, trends
├── scripts/                    # Automation & CLI tools
├── config/                     # Environment config, API keys, DB settings
└── logs/                       # Runtime logs
```

## Pipeline Flow

```
CardRush (cardrush.jp)          Amdoren FX API
        │                              │
        ▼                              ▼
  scraper-cardrush              cardrush-fx-updater
  (scrape JPY prices)           (live GBP/JPY rate → RDS)
  ├── S3 xlsx (archive)
  └── RDS cost_yen
                │                      │
                └──────────┬───────────┘
                           ▼
                   price-calculator
                   (1. cost_gbp = cost_yen / gbp_to_jpy)
                   (2. calc 3 channel selling prices)
                           │
                   ┌───────┴───────┐
                   ▼               ▼
              api-shopify       api-ebay
              (RDS → Shopify)   (RDS → eBay)
                   │               │
                   ▼               ▼
  ┌─────────────────────────────────────────┐
  │     Shopify / eBay / Cardmarket         │
  └─────────────────────────────────────────┘
```

Every RDS column is written by a known Lambda — see [Pipeline Architecture](pricing/docs/PIPELINE_ARCHITECTURE.md) for full column lineage.

## Current State

- **Pricing pipeline**: 6 Lambdas, single RDS-backed track
- **API pushes**: Shopify (live), eBay (built, needs deployment)
- **Stock management**: Zoho (migrating to this in-house solution)

## Key Docs

- [Pipeline Architecture](pricing/docs/PIPELINE_ARCHITECTURE.md) — Full technical docs with formulas, fees, column lineage, and AWS resource map

## Next Steps

### Pricing
- [x] Build eBay API push Lambda
- [x] Consolidate pipeline to single RDS-backed track
- [x] Migrate api-shopify from S3 source to RDS source
- [x] Add cost_yen RDS write to scraper-cardrush (dual-write)
- [x] Add cost_gbp derivation to price-calculator
- [x] Remove legacy S3-only Lambdas (get-gbp-jpy, daily-to-pricefeed, calculator-pricefeed)
- [ ] Deploy scraper-cardrush to AWS (Py 3.12, arm64, VPC-connected)
- [ ] Deploy api-ebay Lambda to AWS (VPC, arm64, Py 3.12)
- [ ] Deploy updated api-shopify to AWS (add VPC + RDS env vars)
- [ ] Run migration: `001_add_platform_identifiers.sql` then `migrate_platform_ids.py`
- [ ] Set up Secrets Manager `ebay-trading-api-credentials` with eBay OAuth tokens
- [ ] Configure EventBridge schedule for pipeline (steps 1-5)
- [ ] Build Cardmarket API push Lambda
- [ ] Set up monitoring for eBay OAuth refresh token expiry (~1.9yr)

### Stock
- [ ] Define stock data model (SKUs, quantities, conditions, locations)
- [ ] Zoho data export & migration plan
- [ ] Choose storage layer (SQLite for local dev, Postgres for prod)
- [ ] Build core inventory CRUD
