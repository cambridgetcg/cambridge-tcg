# eBay API Opportunities — Cambridge TCG

> Research date: 2026-02-10
> Current state: Trading API only (ReviseInventoryStatus for price push, GetMyeBaySelling/ReviseFixedPriceItem for metadata sync)

---

## What We Use Today

| Operation | API Call | Location |
|-----------|----------|----------|
| Batch price updates (4/call) | `ReviseInventoryStatus` | `pricing/push/ebay/lambda_function.py` |
| Single-item price fallback | `ReviseFixedPriceItem` | same |
| Fetch active listings | `GetMyeBaySelling` | `stock/sync/ebay/client.py` |
| Get listing details | `GetItem` | same |
| Sync titles/descriptions/specifics | `ReviseFixedPriceItem` | same |
| Auth | OAuth 2.0 user token via Secrets Manager | `pricing/push/ebay/ebay_auth.py` |

**Scope**: `sell.inventory`. Site ID: 3 (UK). Compat level: 1349. Rate limit: 5000/15s (conservative, eBay allows 6000).

**Data sent**: item_id + selling_price (pricing), title + description + item_specifics (metadata sync).

**Data NOT sent**: stock quantities, condition descriptors, promoted listings.

---

## High-Value Opportunities

### 1. Taxonomy API — Fix Item Specifics Compliance

**Problem**: eBay now mandates **Condition Descriptors** for all trading card listings. Our metadata sync (`stock/sync/ebay/item_specifics.py`) sets Game, Set, Card Number, Language — but does NOT set the mandatory card condition descriptor.

**What's required for ungraded TCG cards** (category 183050):
- `conditionDescriptors[0].name = "40001"` (Card Condition - Ungraded)
- `conditionDescriptors[0].values = ["400010"]` (Near Mint or Better)

Without this, listings risk compliance violations and reduced search visibility.

**Action**: Call `getItemAspectsForCategory(183050)` to get the full required/recommended aspect list for eBay UK TCG cards. Update `item_specifics.py` to include condition descriptors. One-time API call (no auth needed, uses application token).

**Effort**: Small. One API call to understand requirements, then update the metadata sync.

**Impact**: Avoid future enforcement actions + improve search ranking.

---

### 2. Compliance API — Catch Violations Before Enforcement

**Problem**: No visibility into listing violations until eBay sends a warning or removes a listing.

**Solution**: Poll `getListingViolations` daily/weekly. Returns per-listing violations with:
- `PRODUCT_ADOPTION` — missing catalog matching
- `HTTPS` — HTTP links in descriptions (our HTML template should be checked)
- `OUTSIDE_EBAY_BUYING_AND_SELLING` — external links

**Implementation**: Lightweight Lambda on a daily schedule. Single API call per compliance type. Could feed into the health-check system (new check: "eBay compliance violations > 0").

**Effort**: Small. REST API, straightforward JSON response.

**Impact**: Prevents surprise listing removals.

---

### 3. Browse API — Competitor Price Monitoring

**Problem**: Our pricing formula is cost-based (`cost_yen × margin × VAT / (1 - fee)`). No market awareness — we might be pricing significantly above or below competitors.

**Solution**: Use Browse API `search` to find competitor listings for the same cards. Compare our prices against market. Could generate a daily report or feed into the pricing calculator as a price ceiling/floor.

**Example query**: Search category 183050 for "One Piece OP-01 062 Japanese" → get competitor prices.

**Limitations**:
- Cannot search **sold/completed** listings (only active)
- Application-level rate limits (request increase via Application Growth Check)
- No user auth needed (client_credentials token)

**Implementation**: New Lambda that queries Browse API for each SKU, stores competitor price data in RDS or S3. Run weekly or on-demand.

**Effort**: Medium. Need to design search queries that reliably find matching cards, handle result parsing.

**Impact**: Data-driven pricing instead of pure cost-plus. Identify overpriced (won't sell) and underpriced (leaving money on table) cards.

---

### 4. Marketing API — Auto-Promote Stale Inventory

**Problem**: Some cards sit unsold for weeks/months. No systematic promotion strategy.

**Solution**: Promoted Listings Standard (CPS) — pay a % of sale price only when the item sells. Safe for low-margin items.

**Strategy**:
1. Identify stale listings (listed > 30 days, no sale) via Analytics API or local tracking
2. Create a Standard campaign via Marketing API
3. Add stale listings with 2-3% ad rate
4. Remove listings once they sell (automatic) or after a review period

**Economics**: Card at GBP 5.80 with 2% ad rate = GBP 0.12 per sale. Acceptable if it unsticks inventory. Do NOT promote cards under GBP 3 (margin too thin) or fast sellers (they'd sell anyway).

**Implementation**: New Lambda that:
1. Queries Analytics API for low-impression listings
2. Creates/manages a Standard campaign via Marketing API
3. Adds qualifying listings with `bulkCreateAdsByListingId`

**Effort**: Medium. Campaign lifecycle management adds complexity.

**Impact**: Convert stale inventory to sales. ROI is measurable since CPS = known cost.

---

### 5. Analytics API — Traffic Intelligence

**Problem**: No visibility into which listings get impressions, views, or what our conversion rate is.

**Solution**: `getTrafficReport` returns per-listing metrics:
- `LISTING_IMPRESSION_TOTAL` — search result impressions
- `LISTING_VIEWS_TOTAL` — page views
- `CLICK_THROUGH_RATE` — impressions-to-views
- `SALES_COUNT_TOTAL` — units sold

**Use cases**:
- **High impressions, low views**: Title or main image isn't compelling
- **High views, low sales**: Price too high or listing quality issue
- **Zero impressions**: Item specifics/category wrong, or title missing key search terms
- **Seasonal trends**: Track demand patterns for TCG sets over time

**Implementation**: Weekly Lambda that pulls traffic data, stores in RDS, generates a summary report.

**Effort**: Small-Medium. Single API call, needs a storage schema for historical data.

**Impact**: Actionable intelligence for listing optimization. Identifies which cards need attention.

---

### 6. Notification API — Real-Time Stock Decrements

**Problem**: Stock pipeline has no stock reduction mechanism. Sales aren't tracked — only additions via parser imports.

**Solution**: Subscribe to `FixedPriceTransaction` (item sold) events. When a card sells on eBay, a webhook fires to our endpoint, which decrements the stock count.

**Two approaches**:
1. **Webhook (real-time)**: Requires an HTTPS endpoint (API Gateway + Lambda). eBay POSTs sale events with `X-EBAY-SIGNATURE` for verification.
2. **Polling (simpler)**: Use Fulfillment API `getOrders` every 15-30 min to fetch new orders, then decrement stock. No webhook infrastructure needed.

**Polling is better for our scale**: 200-400 SKUs, likely <20 sales/day. A scheduled Lambda calling `getOrders` every 30 min is simpler than maintaining webhook infrastructure and handles the same use case with negligible delay.

**Effort**: Medium. Need to build the stock decrement logic + order tracking (avoid double-counting).

**Impact**: Solves the #3 stock pipeline issue ("No stock reduction"). Enables accurate multi-channel inventory.

---

### 7. Feed API — eBay-vs-RDS Reconciliation

**Problem**: No way to verify that our RDS data (prices, item IDs) matches what's actually live on eBay. A failed price push or stale item ID would go unnoticed.

**Solution**: `createInventoryTask` with `LMS_ACTIVE_INVENTORY_REPORT` downloads a full dump of all active listings with current prices and quantities. Compare against RDS.

**Implementation**: Weekly Lambda that:
1. Creates an inventory task (async)
2. Polls until complete
3. Downloads the TSV report
4. Compares each listing's price against `ebay_business_selling_price` in RDS
5. Flags mismatches in the health-check system

**Effort**: Medium. Async workflow (create task → poll → download → parse).

**Impact**: Catches price drift, dead item IDs, and sync failures. Confidence that what's in RDS is what's on eBay.

---

## Lower Priority (But Worth Knowing)

### Inventory API — Migration Target (Not Urgent)

The modern REST replacement for Trading API selling calls. Key differences:
- SKU-based (not ItemID-based)
- `bulkUpdatePriceQuantity` handles 25 items/call (vs 4 for `ReviseInventoryStatus`)
- Requires inventory locations (warehouse setup)

**Critical**: Migration is **one-way per listing**. Once a listing is migrated via `bulkMigrateListing`, it can only be managed via Inventory API. Trading API calls will fail on migrated listings.

At 400 SKUs, the batch size improvement (25 vs 4) saves ~84 API calls — negligible. Stay on Trading API until there's a compelling feature reason to migrate, or until eBay announces deprecation of the core selling calls.

### Fulfillment API — Order Automation

Manage orders: `getOrders`, `createShippingFulfillment` (attach tracking), `issueRefund`. Useful if order volume justifies automation. At current scale, Seller Hub is probably sufficient.

### Account API — Multi-Marketplace Expansion

Programmatic management of shipping/return/payment policies. Only valuable if expanding to eBay DE, eBay US, etc.

### Recommendation API — Limited Value

Modern version only returns Promoted Listings ad rate suggestions. Legacy version has broader recommendations but is XML-based. Not worth building against.

---

## eBay Managed Payments (UK Context)

Mandatory for all UK sellers. Key fee structure:
- Final value fee: 10-13% + GBP 0.30/order for collectibles/TCG
- No separate payment processing fee
- Payouts: 1-3 business days to bank account
- Refunds: via Fulfillment API `issueRefund`

Our pricing formula already accounts for eBay fees via the `F` parameter (default 12%). Verify this matches actual fee schedule periodically.

---

## Implementation Roadmap

### Phase 1 — Compliance & Visibility (effort: small)
1. **Taxonomy API**: Query required aspects for category 183050, update condition descriptors in metadata sync
2. **Compliance API**: Daily check for listing violations, integrate with health-check Lambda

### Phase 2 — Market Intelligence (effort: medium)
3. **Analytics API**: Weekly traffic report, store historical data
4. **Browse API**: Competitor price monitoring for top-selling SKUs

### Phase 3 — Revenue Optimization (effort: medium)
5. **Marketing API**: Auto-promote stale inventory via Standard CPS campaigns
6. **Feed API**: Weekly reconciliation report (eBay live vs RDS)

### Phase 4 — Stock Integration (effort: medium-large)
7. **Fulfillment API polling**: Order tracking + stock decrements (solves stock reduction gap)

### Phase 5 — Future-Proofing (effort: large, not urgent)
8. **Inventory API migration**: When/if eBay announces Trading API deprecation

---

## Rate Limits (Not a Concern at Our Scale)

| API | Limit | Our usage |
|-----|-------|-----------|
| Trading API general | 5,000/day | ~200 calls/run |
| `ReviseInventoryStatus` | 6,000/15s | ~100 calls/run |
| Browse API | Application-level | Would need increase for daily monitoring |
| Analytics, Compliance, Feed | Standard REST | Single calls |

At 200-400 SKUs, we are nowhere near any rate limit. Request Application Growth Check only if implementing Browse API at scale.

---

## Key Decision: Stay on Trading API

**Do not migrate to Inventory API yet.** Reasons:
1. Core Trading API selling calls have no announced deprecation date
2. Migration is one-way — no reverting
3. Batch size improvement (25 vs 4) is negligible at our scale
4. All new eBay REST APIs (Browse, Analytics, Marketing, etc.) work alongside Trading API listings
5. Only migrate when eBay forces it or when a feature is Inventory API-exclusive

All opportunities in this document (compliance, analytics, marketing, notifications, feed) work with our current Trading API setup. No migration required.
