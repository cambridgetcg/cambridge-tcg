# Trade-In Flow Design — Cambridge TCG

> Comprehensive design for the end-to-end trade-in experience at `tradein.cambridgetcg.com`.
> Covers both mail-in and in-store channels, edge cases, and implementation plan.

---

## 1. Current State

- **Buylist page** live at `tradein.cambridgetcg.com` — browse-only, no submission
- **486 cards** listed (OP only), dual pricing: cash (55%) / credit (77% of landed cost)
- **Want tiers**: stock=0 → want 4, stock 1-2 → want 2, stock 3+ → want 0
- Credit want: unlimited (always buying for credit regardless of stock)
- **No email infrastructure** — only SNS for internal pipeline alerts
- **No customer accounts** — Shopify handles the storefront, no auth on tradein subdomain
- **Contact page** on Shopify is broken (404)

---

## 2. Design Principles

1. **Trust through transparency** — Show prices, conditions, and policies upfront
2. **Low friction for customers** — No account required, email-based communication
3. **Low overhead for store** — Manual processing is fine; automate notifications only
4. **Dual channel** — In-store (primary, competitive advantage) + mail-in (extends reach)
5. **Fair to both sides** — Clear rules on price locks, grading, and disputes

---

## 3. Customer Journey — Mail-In

### Step 1: Browse Buylist
- Customer visits `tradein.cambridgetcg.com`
- Sees all cards with cash/credit prices and want quantities
- Can search, filter by set, sort by price/want
- **Insight**: Cash want shows stock-based demand; credit want is always unlimited

### Step 2: Build Sell Cart
- Customer clicks "Add" on cards they want to sell
- Each card row gets a quantity selector (1-10, capped at cash_want for cash, uncapped for credit)
- Running total displayed in a sticky footer bar: "X cards — Cash: £Y / Credit: £Z"
- Cart persisted in localStorage (survives page refresh)
- **Edge case**: Card removed from buylist between browsing and submission → validate on submit

### Step 3: Review Quote
- Customer clicks "Review Trade-In" → slides to a review panel/page
- Shows itemized list: Card | Qty | Cash Price | Credit Price
- Shows totals for both payment methods
- Minimum trade-in threshold: **£5 credit value** (below this, processing cost exceeds value)
- Clear notice: "Prices valid for 7 days from submission. Card condition must be Near Mint."

### Step 4: Submit Trade-In Request
- **Form fields:**
  - Full name (required)
  - Email (required) — all communication goes here
  - Phone (optional) — for urgent issues only
  - Payment preference: Cash (bank transfer) / Store credit (radio)
  - If cash: sort code + account number (or "will provide later")
  - Delivery method: Mail-in / In-store drop-off (radio)
  - Condition declaration: "I confirm all cards are Near Mint" (checkbox, required)
  - Notes (optional textarea) — e.g. "Some cards are LP, please quote accordingly"
  - Under-18 declaration: "I am 18 or over, or a parent/guardian is submitting on my behalf" (checkbox, required)

- **On submit:**
  1. Validate cart against current buylist (re-fetch /buylist, check prices haven't changed >10%)
  2. If prices changed significantly → show warning, ask to re-confirm
  3. Generate reference number: `TI-YYYYMMDD-XXXX` (e.g. TI-20260213-0001)
  4. Store submission in RDS `tradein_submissions` table
  5. Send confirmation email to customer (via SES)
  6. Send notification to store (via SNS to contact@cambridgetcg.com)

### Step 5: Confirmation Email (to customer)
```
Subject: Trade-In Request TI-20260213-0001 — Cambridge TCG

Hi [Name],

Thanks for your trade-in request! Here's your quote summary:

Reference: TI-20260213-0001
Payment: Store Credit
Items: 12 cards
Quote Total: £47.60 (credit)

── Card List ──
OP01-001  x2  £6.80 each  = £13.60
OP03-121  x1  £22.00 each = £22.00
...

── What happens next ──

[If mail-in:]
1. Pack your cards carefully (sleeved, in toploaders, rigid mailer)
2. Ship to: Cambridge TCG, [address], Cambridge, CB1 XXX
3. Use tracked delivery (Royal Mail Tracked 48 recommended)
4. Reply to this email with your tracking number
5. Prices are locked for 7 days from today ([expiry date])

[If in-store drop-off:]
1. Bring your cards to our shop at [address]
2. Reference your order number: TI-20260213-0001
3. We'll verify and process on the spot

── Important ──
- All cards must be Near Mint condition
- Cards below NM may be accepted at a reduced price or returned
- If we find discrepancies, we'll email you before processing
- You can cancel this request any time before we process it

Cambridge TCG — Japanese TCG specialists, Cambridge UK
```

### Step 6: Ship Cards
- Customer ships at own expense
- **Recommended**: Royal Mail Tracked 48 (UK), tracked service (international)
- Customer replies to confirmation email with tracking number
- Store logs tracking number against submission

### Step 7: Receipt & Grading
- Store receives package, marks submission as "Received" in system
- Auto-email to customer: "We've received your cards for TI-XXXXX. Processing takes 1-3 business days."
- Staff grades each card:
  - **NM**: Accept at quoted price
  - **LP (Lightly Played)**: Accept at 75% of quoted price
  - **MP or worse**: Reject (offer to return)
  - **Wrong card / not on buylist**: Set aside for return
  - **Suspected counterfeit**: Reject, notify customer

### Step 8: Resolution
- **All cards NM, matches quote** → proceed directly to payment
- **Minor discrepancies** (LP cards, small qty mismatch):
  - Calculate adjusted total
  - Email customer: "Your adjusted quote is £X (was £Y). [Accept] [Return cards]"
  - Customer has **7 days** to respond
  - No response after 7 days → process at adjusted price (stated in T&Cs)
- **Major discrepancies** (wrong cards, many condition issues):
  - Email customer with detailed breakdown
  - Offer: accept adjusted amount, or return all cards (return shipping: £2.50)
  - Customer has **7 days** to respond
  - No response → return cards

### Step 9: Payment
- **Store credit**: Applied as Shopify discount code (single-use, amount = total)
  - Email: "Your store credit code is CTCG-XXXXX (£47.60). Use at cambridgetcg.com"
  - No expiry on credit
- **Cash (bank transfer)**: Paid within 2 business days of acceptance
  - Email: "£26.18 transferred to your account ending XXXX. Reference: TI-XXXXX"
- **Receipt**: Final email with itemized breakdown of what was accepted/rejected/paid

### Step 10: Completion
- Submission marked "Completed" in system
- Stock inventory updated (quantities added to stock_inventory)
- Trade-in register entry created (legal compliance)

---

## 4. Customer Journey — In-Store

### Step 1: Arrive
- Customer brings cards to shop (walk-in, no appointment needed)
- Optional: customer pre-built a cart online and has reference number

### Step 2: Evaluate
- Staff checks each card against buylist prices (on tradein.cambridgetcg.com or POS)
- Condition graded face-to-face with customer present
- Customer sees the grading happen — builds trust

### Step 3: Offer
- Staff presents total: "12 cards — £26.18 cash or £47.60 store credit"
- Customer chooses payment method

### Step 4: Accept / Decline
- Customer accepts → immediate payment
- Customer declines → takes cards back, no obligation

### Step 5: Payment
- **Cash**: From till, immediately
- **Store credit**: Shopify discount code generated and emailed/printed

### Step 6: Record
- Log in trade-in register: date, seller name, items, condition, price paid
- Update stock_inventory

---

## 5. Edge Cases & Handling

### Pricing & Quotes

| Edge Case | Handling |
|-----------|----------|
| **Price drops >10% between submission and receipt** | Honor the locked price (7-day lock is a commitment) |
| **Price rises between submission and receipt** | Pay the higher price (goodwill, builds loyalty) |
| **Quote expires (>7 days, not received)** | Email customer: "Your quote has expired. Would you like a new quote?" |
| **Card removed from buylist after submission** | Accept at last quoted price (was in scope when submitted) |
| **Buylist temporarily paused** (e.g. CardRush maintenance) | Show banner on site, reject new submissions, honor existing ones |

### Condition & Grading

| Edge Case | Handling |
|-----------|----------|
| **All cards NM** | Process at full quoted price |
| **Some cards LP** | Accept at 75% of quoted price, email adjusted total |
| **Cards MP or worse** | Reject those cards, offer return or discard |
| **Counterfeit suspected** | Reject, notify customer, do not return (inform they can dispute) |
| **Cards damaged in transit** | Not store's responsibility (customer bears shipping risk), but offer goodwill if minor |
| **Wrong edition / variant** | Reject, treat as "wrong card" |
| **Foreign language (not Japanese)** | Reject (buylist is JP only) |

### Logistics

| Edge Case | Handling |
|-----------|----------|
| **Package lost in transit** | Customer's responsibility (they chose shipping method). Advise tracked + insured. |
| **Package arrives with no reference number** | Try to match by name/email. If impossible, email sender (return address). |
| **More cards sent than declared** | Grade and quote extras, email updated offer |
| **Fewer cards sent than declared** | Process what's received, note shortage in email |
| **Cards not sorted per confirmation** | Process anyway (small shop, not worth penalizing) |
| **International customer** | Accept, but customer pays all shipping + any customs. Cash via PayPal/Wise only. |

### Customer Behavior

| Edge Case | Handling |
|-----------|----------|
| **Cancel before shipping** | Allowed, mark as "Cancelled", no penalty |
| **Cancel after shipping, before receipt** | Ask to not send / arrange return if already in transit. No penalty. |
| **Cancel after receipt, before grading** | Return at customer's expense (£2.50 return postage) |
| **Customer wants to change cash↔credit** | Allowed any time before payment is issued |
| **Customer disputes grading** | Offer to return cards (at customer's expense). Grading is final. |
| **Customer is under 18** | Require parent/guardian as contracting party (in submission form) |
| **Customer unresponsive to discrepancy email** | 7 days → auto-process at adjusted price (per T&Cs) |
| **Duplicate submission** | Detect by email + similar items within 24h, flag for manual review |
| **Spam/bot submissions** | Honeypot field + rate limit (max 3 submissions per email per day) |

### High Value

| Edge Case | Handling |
|-----------|----------|
| **Single card worth >£100** | Flag for priority processing, consider requiring signature delivery |
| **Total submission >£500** | Require ID verification (photo of ID + selfie, per UK second-hand dealer regs) |
| **Total submission >£1000** | Manual approval before confirming quote, direct email from store owner |

---

## 6. Policies & Terms

### Price Lock
- Prices locked for **7 calendar days** from submission date
- Cards must be **received** (not just posted) within 7 days
- After expiry: customer offered a new quote at current prices

### Condition Requirements
- **Near Mint (NM)**: Listed price. No visible wear, clean edges, no bends.
- **Lightly Played (LP)**: 75% of listed price. Minor edge wear, light scratches.
- **Below LP**: Not accepted for mail-in. In-store: case-by-case at staff discretion.
- Visual condition guide to be published (with photo examples for NM, LP, MP)

### Minimum Values
- **Mail-in**: Minimum £5 total credit value (or £3 cash value)
- **In-store**: No minimum

### Payment Timeline
- **Store credit**: Issued within 1 business day of acceptance
- **Cash (bank transfer)**: Paid within 2 business days of acceptance
- **In-store cash**: Immediate

### Returns
- Rejected cards returned via Royal Mail 2nd Class (store pays)
- Customer can request return of all cards if they decline the adjusted offer
- Return postage charged: £2.50 (deducted from payment or invoiced)

### Age Policy
- Must be 18+ to submit a trade-in
- Under-18s: parent/guardian must be the contracting party
- Parent/guardian details required on submission form

### Record Keeping (Legal)
- All trade-ins recorded in register: date, seller name, items, condition, price paid
- Records kept for minimum 2 years (UK second-hand dealer obligation)
- Submissions >£500: seller ID recorded (name, address, ID reference)

### Cancellation
- Customer may cancel at any time before payment is issued
- After payment: no cancellation (transaction complete)

---

## 7. Communication Templates

### Email 1: Submission Confirmation
- **Trigger**: Form submitted
- **From**: tradein@cambridgetcg.com (or noreply@)
- **Subject**: "Trade-In Request [REF] — Cambridge TCG"
- **Body**: Quote summary, card list, shipping instructions, reference number, expiry date

### Email 2: Cards Received
- **Trigger**: Store marks submission as "Received"
- **Subject**: "Cards Received — [REF]"
- **Body**: "We've received your cards. Processing takes 1-3 business days."

### Email 3a: Accepted (no issues)
- **Trigger**: Grading complete, no discrepancies
- **Subject**: "Trade-In Complete — [REF]"
- **Body**: Itemized acceptance, payment confirmation (credit code or transfer reference)

### Email 3b: Discrepancy Found
- **Trigger**: Grading reveals condition/quantity issues
- **Subject**: "Trade-In Update — [REF] — Action Required"
- **Body**: Original quote vs adjusted quote, itemized changes, options (accept/return), 7-day deadline

### Email 4: Payment Issued
- **Trigger**: Payment processed
- **Subject**: "Payment Sent — [REF]"
- **Body**: Amount, method (credit code or bank transfer ref), thank you

### Email 5: Quote Expired
- **Trigger**: 7 days elapsed, cards not received
- **Subject**: "Quote Expired — [REF]"
- **Body**: "Your quote has expired. Would you like a new quote at current prices? Reply to this email."

### Internal Notification (to store)
- **Trigger**: Every new submission
- **Via**: SNS → contact@cambridgetcg.com
- **Subject**: "New Trade-In: [REF] — [X] cards — £[Y] [cash/credit]"
- **Body**: Customer name, email, item count, total, payment preference, delivery method

---

## 8. Submission States

```
SUBMITTED  →  SHIPPED  →  RECEIVED  →  GRADING  →  ACCEPTED  →  PAID  →  COMPLETED
                                           ↓
                                      DISCREPANCY  →  WAITING_CUSTOMER  →  ACCEPTED / RETURNED
    ↓
 CANCELLED
    ↓
 EXPIRED (7 days, not received)
```

| Status | Description | Trigger |
|--------|-------------|---------|
| `submitted` | Quote locked, awaiting shipment | Form submission |
| `shipped` | Customer provided tracking number | Customer reply/update |
| `received` | Cards arrived at store | Store marks received |
| `grading` | Staff inspecting cards | Store begins processing |
| `accepted` | All items accepted, awaiting payment | Grading complete (no issues) |
| `discrepancy` | Issues found, customer notified | Grading found condition/qty issues |
| `waiting_customer` | Awaiting customer response to discrepancy | Discrepancy email sent |
| `paid` | Payment issued | Credit code sent or bank transfer made |
| `completed` | Trade-in fully resolved | Payment confirmed |
| `cancelled` | Customer cancelled | Customer request |
| `expired` | Quote expired (7 days, not received) | Automatic |
| `returned` | Cards shipped back to customer | Customer declined adjusted offer |

---

## 9. Data Model

### `tradein_submissions` (new table)

| Column | Type | Notes |
|--------|------|-------|
| `id` | SERIAL PK | Auto-increment |
| `reference` | VARCHAR(20) UNIQUE | TI-YYYYMMDD-XXXX |
| `status` | VARCHAR(30) | See states above |
| `customer_name` | VARCHAR(200) | Required |
| `customer_email` | VARCHAR(200) | Required |
| `customer_phone` | VARCHAR(30) | Optional |
| `payment_method` | VARCHAR(10) | 'cash' or 'credit' |
| `bank_details` | TEXT | Encrypted, for cash payments |
| `delivery_method` | VARCHAR(10) | 'mail' or 'instore' |
| `is_over_18` | BOOLEAN | Age declaration |
| `notes` | TEXT | Customer notes |
| `quoted_cash_total` | NUMERIC(10,2) | Total at time of submission |
| `quoted_credit_total` | NUMERIC(10,2) | Total at time of submission |
| `final_total` | NUMERIC(10,2) | After grading adjustments |
| `tracking_number` | VARCHAR(100) | Royal Mail / courier tracking |
| `payment_reference` | VARCHAR(100) | Discount code or bank transfer ref |
| `quote_expires_at` | TIMESTAMPTZ | submission + 7 days |
| `created_at` | TIMESTAMPTZ | DEFAULT NOW() |
| `updated_at` | TIMESTAMPTZ | DEFAULT NOW() |

### `tradein_items` (new table)

| Column | Type | Notes |
|--------|------|-------|
| `id` | SERIAL PK | Auto-increment |
| `submission_id` | INTEGER FK | → tradein_submissions.id |
| `sku` | VARCHAR(30) | Card SKU |
| `quantity` | INTEGER | Declared quantity |
| `quoted_cash_price` | NUMERIC(10,2) | Price at submission time |
| `quoted_credit_price` | NUMERIC(10,2) | Price at submission time |
| `accepted_qty` | INTEGER | After grading (NULL = pending) |
| `condition_grade` | VARCHAR(5) | NM, LP, MP, etc. (NULL = pending) |
| `final_unit_price` | NUMERIC(10,2) | After condition adjustment |

### `tradein_register` (legal compliance)

| Column | Type | Notes |
|--------|------|-------|
| `id` | SERIAL PK | Auto-increment |
| `submission_id` | INTEGER FK | → tradein_submissions.id (NULL for in-store) |
| `seller_name` | VARCHAR(200) | |
| `seller_email` | VARCHAR(200) | |
| `seller_id_reference` | VARCHAR(100) | For >£500 transactions |
| `items_description` | TEXT | Summary of cards traded |
| `total_paid` | NUMERIC(10,2) | |
| `payment_method` | VARCHAR(10) | |
| `date` | DATE | |
| `created_at` | TIMESTAMPTZ | DEFAULT NOW() |

---

## 10. Technical Implementation

### Backend (API Lambda)

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `GET /buylist` | GET | Existing — browse buylist |
| `POST /tradein` | POST | New — submit trade-in request |
| `GET /tradein/{ref}` | GET | New — check submission status (by ref + email) |
| `POST /tradein/{ref}/tracking` | POST | New — add tracking number |
| `POST /tradein/{ref}/cancel` | POST | New — cancel submission |

### Email (AWS SES)
- **Sender**: `tradein@cambridgetcg.com` (or `noreply@cambridgetcg.com`)
- **Setup**: Verify domain in SES, request production access
- **Templates**: Store as Python string templates in Lambda code (simple, no template engine needed)
- **Fallback**: If SES not yet set up, use SNS to contact@cambridgetcg.com for all notifications

### Frontend (tradein.cambridgetcg.com)
- Add "sell cart" functionality to existing buylist page
- New hash routes: `#/cart`, `#/submit`, `#/status`
- Cart stored in localStorage
- Form validation client-side + server-side
- Honeypot field for bot prevention

### Admin (for store staff)
- **Phase 1**: Email-based workflow (staff processes via email replies + RDS direct)
- **Phase 2**: Simple admin page at `tradein.cambridgetcg.com/#/admin` (protected by a shared secret in URL or basic auth)
- Admin actions: mark received, update status, record grading, issue payment

---

## 11. Key Insights from Industry Research

### What the best stores do
1. **Card Kingdom** gives a 30% store credit bonus over cash — Cambridge TCG's 40% bonus (77/55) is even more generous
2. **Star City Games** uses a pre-approved deduction threshold — smart for managing disputes without per-item negotiation
3. **All major stores** require tracked shipping — customer bears shipping risk
4. **TCGPlayer** tracks a Quality Ratio per seller — not needed at Cambridge TCG's scale but good concept for repeat customers
5. **UK stores** (Big Orbit, Troll Trader) charge £2-13 for return shipping — £2.50 is fair and competitive

### What small stores should avoid
1. **Don't over-automate** — manual email processing is fine for <50 trade-ins/month
2. **Don't build customer accounts** — email + reference number is sufficient
3. **Don't offer price adjustments for condition online** — too complex, too many disputes. Accept NM only at listed price, grade in person.
4. **Don't accept non-Japanese cards** — scope creep. OP-JP only for now.
5. **Don't promise same-day processing** — 1-3 business days sets realistic expectations

### UK-specific requirements
1. **Under-18s**: Parent/guardian must be the contracting party (minor can void the contract)
2. **Record keeping**: Trade-in register kept 2+ years, shown to police/trading standards on request
3. **>£500 transactions**: Record seller ID
4. **Stolen goods due diligence**: Check valuable singles against theft reports

---

## 12. Implementation Priority

### Phase 1: MVP (minimum viable trade-in)
- [ ] Sell cart UI (add/remove cards, quantity, running total)
- [ ] Submit form (name, email, payment preference, delivery method)
- [ ] `POST /tradein` endpoint (validate, store in RDS, send SNS notification to store)
- [ ] Confirmation page with reference number and instructions
- [ ] `tradein_submissions` + `tradein_items` tables (migration)
- [ ] Trade-in terms page (link in footer)

### Phase 2: Communication
- [ ] AWS SES setup (verify domain, production access)
- [ ] Confirmation email to customer
- [ ] "Cards received" email
- [ ] "Payment issued" email
- [ ] Status check page (`#/status` — enter ref + email to see status)

### Phase 3: Admin & Grading
- [ ] Admin panel for store staff (view submissions, update status)
- [ ] Grading workflow (accept/adjust/reject per card)
- [ ] Discrepancy email with accept/return options
- [ ] Automatic quote expiry (7-day cron)
- [ ] `tradein_register` table for legal compliance

### Phase 4: Polish
- [ ] Visual condition guide (NM/LP/MP photo examples)
- [ ] Email tracking number update flow
- [ ] Repeat customer recognition (by email)
- [ ] Export trade-in register (CSV for auditing)
- [ ] In-store kiosk mode (staff-facing quick entry)

---

## 13. Open Questions

1. **Store address** — needed for shipping instructions in confirmation email
2. **SES or alternative** — is AWS SES acceptable, or prefer a third-party (SendGrid, etc.)?
3. **Bank details handling** — encrypt in RDS or collect separately via secure link?
4. **Shopify discount code generation** — API integration needed for credit payments
5. **In-store POS integration** — does the current POS support discount code entry?
6. **Volume expectation** — how many trade-ins per week expected? (affects automation priority)
7. **LP pricing** — 75% of NM price, or case-by-case?
8. **Return address label** — include a pre-paid return label, or charge £2.50?
