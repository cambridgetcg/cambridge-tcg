-- Migration 011: Trade-in submissions and items tables
-- Stores customer trade-in requests submitted via tradein.cambridgetcg.com

CREATE TABLE IF NOT EXISTS tradein_submissions (
    id SERIAL PRIMARY KEY,
    reference VARCHAR(20) UNIQUE NOT NULL,
    status VARCHAR(30) NOT NULL DEFAULT 'submitted',
    customer_name VARCHAR(200) NOT NULL,
    customer_email VARCHAR(200) NOT NULL,
    customer_phone VARCHAR(30),
    payment_method VARCHAR(10) NOT NULL CHECK (payment_method IN ('cash', 'credit')),
    delivery_method VARCHAR(10) NOT NULL DEFAULT 'mail' CHECK (delivery_method IN ('mail', 'instore')),
    is_over_18 BOOLEAN NOT NULL DEFAULT TRUE,
    notes TEXT,
    quoted_cash_total NUMERIC(10,2),
    quoted_credit_total NUMERIC(10,2),
    final_total NUMERIC(10,2),
    tracking_number VARCHAR(100),
    payment_reference VARCHAR(100),
    quote_expires_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS tradein_items (
    id SERIAL PRIMARY KEY,
    submission_id INTEGER NOT NULL REFERENCES tradein_submissions(id) ON DELETE CASCADE,
    sku VARCHAR(30) NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    quoted_cash_price NUMERIC(10,2) NOT NULL,
    quoted_credit_price NUMERIC(10,2) NOT NULL,
    accepted_qty INTEGER,
    condition_grade VARCHAR(5),
    final_unit_price NUMERIC(10,2)
);

CREATE INDEX IF NOT EXISTS idx_tradein_submissions_ref ON tradein_submissions(reference);
CREATE INDEX IF NOT EXISTS idx_tradein_submissions_email ON tradein_submissions(customer_email);
CREATE INDEX IF NOT EXISTS idx_tradein_submissions_status ON tradein_submissions(status);
CREATE INDEX IF NOT EXISTS idx_tradein_items_submission ON tradein_items(submission_id);
