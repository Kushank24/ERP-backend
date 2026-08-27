-- Migration: add offer-enquiry CRM tables
-- Run once against the Supabase database.

-- ── Companies (customer directory) ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS companies (
    id          SERIAL PRIMARY KEY,
    name        VARCHAR(200) NOT NULL,
    contact_person VARCHAR(100),
    email       VARCHAR(100),
    phone       VARCHAR(20),
    address     TEXT,
    gstin       VARCHAR(15),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Enquiries ───────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS enquiries (
    id               SERIAL PRIMARY KEY,
    company_id       INTEGER REFERENCES companies(id) ON DELETE SET NULL,
    enquiry_number   VARCHAR(50) UNIQUE NOT NULL,
    enquiry_date     DATE NOT NULL DEFAULT CURRENT_DATE,
    status           VARCHAR(20) NOT NULL DEFAULT 'pending',
    priority         VARCHAR(10) NOT NULL DEFAULT 'medium',
    notes            TEXT,
    reference_number VARCHAR(255),
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS enquiry_items (
    id           SERIAL PRIMARY KEY,
    enquiry_id   INTEGER NOT NULL REFERENCES enquiries(id) ON DELETE CASCADE,
    product_id   INTEGER REFERENCES products(id) ON DELETE SET NULL,
    product_name VARCHAR(200),
    quantity     INTEGER NOT NULL DEFAULT 1,
    specifications TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ── Offers / Quotations ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS offers (
    id                    SERIAL PRIMARY KEY,
    enquiry_id            INTEGER REFERENCES enquiries(id) ON DELETE SET NULL,
    company_id            INTEGER REFERENCES companies(id) ON DELETE SET NULL,
    offer_number          VARCHAR(50) UNIQUE NOT NULL,
    offer_date            DATE NOT NULL DEFAULT CURRENT_DATE,
    valid_until           DATE,
    status                VARCHAR(20) NOT NULL DEFAULT 'draft',
    currency              VARCHAR(5)  NOT NULL DEFAULT 'INR',
    packing_charges_pct   NUMERIC(5,2)  NOT NULL DEFAULT 0,
    freight_charges       NUMERIC(10,2) NOT NULL DEFAULT 0,
    gst_pct               NUMERIC(5,2)  NOT NULL DEFAULT 18,
    subtotal              NUMERIC(12,2) NOT NULL DEFAULT 0,
    total_amount          NUMERIC(12,2) NOT NULL DEFAULT 0,
    terms_conditions      TEXT,
    follow_up_completed   BOOLEAN NOT NULL DEFAULT FALSE,
    follow_up_comments    TEXT,
    notes                 TEXT,
    sales_order_id        INTEGER REFERENCES sales_orders(id) ON DELETE SET NULL,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS offer_items (
    id          SERIAL PRIMARY KEY,
    offer_id    INTEGER NOT NULL REFERENCES offers(id) ON DELETE CASCADE,
    product_id  INTEGER REFERENCES products(id) ON DELETE SET NULL,
    description TEXT NOT NULL,
    quantity    INTEGER       NOT NULL DEFAULT 1,
    unit_price  NUMERIC(10,2) NOT NULL DEFAULT 0,
    total_price NUMERIC(12,2) NOT NULL DEFAULT 0,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
