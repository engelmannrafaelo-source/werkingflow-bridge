-- Sprint Phase-1 — invoices.
-- Bridge becomes Mollie-Owner for invoicing too. Each invoice ties to a user
-- and optionally a subscription / pending payment / credit purchase.

CREATE TYPE invoice_status AS ENUM (
    'draft',       -- generated but not yet sent
    'issued',      -- sent to customer, awaiting payment
    'paid',        -- mollie webhook confirmed payment
    'cancelled',   -- voided before payment
    'refunded'     -- refunded after payment
);

CREATE TABLE IF NOT EXISTS invoices (
    id                  UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    invoice_number      VARCHAR(64)     UNIQUE NOT NULL,
    user_id             UUID            NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
    tenant_id           VARCHAR(128)    REFERENCES tenants(id) ON DELETE SET NULL,
    subscription_id     UUID            REFERENCES subscriptions(id) ON DELETE SET NULL,
    credit_purchase_id  UUID            REFERENCES credit_purchases(id) ON DELETE SET NULL,
    mollie_payment_id   VARCHAR(128)    UNIQUE,           -- idempotency / mollie correlation
    status              invoice_status  NOT NULL DEFAULT 'draft',

    -- Amounts (NUMERIC for precision — never float for money)
    subtotal_eur        NUMERIC(10,2)   NOT NULL,
    tax_rate            NUMERIC(5,2)    NOT NULL DEFAULT 20.00,   -- e.g. 20.00 = 20% USt
    tax_eur             NUMERIC(10,2)   NOT NULL,
    total_eur           NUMERIC(10,2)   NOT NULL,
    currency            VARCHAR(3)      NOT NULL DEFAULT 'EUR',

    -- Structured details
    line_items          JSONB           NOT NULL DEFAULT '[]',
    -- [{ description, quantity, unit_price_eur, total_eur, metadata? }]

    billing_address     JSONB,
    -- { name, street, city, postcode, country, vat_id? }

    -- Timestamps
    issued_at           TIMESTAMPTZ,
    paid_at             TIMESTAMPTZ,
    due_at              TIMESTAMPTZ,
    cancelled_at        TIMESTAMPTZ,
    refunded_at         TIMESTAMPTZ,
    sent_at             TIMESTAMPTZ,

    notes               TEXT,
    metadata            JSONB           NOT NULL DEFAULT '{}',

    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    -- Defence: total must equal subtotal + tax (rounded to 2 decimals).
    -- Soft check — allow 0.01 tolerance for cent-rounding artifacts.
    CONSTRAINT invoices_total_matches CHECK (
        ABS(total_eur - (subtotal_eur + tax_eur)) < 0.02
    )
);

CREATE INDEX IF NOT EXISTS idx_invoices_user_id       ON invoices(user_id);
CREATE INDEX IF NOT EXISTS idx_invoices_tenant_id     ON invoices(tenant_id);
CREATE INDEX IF NOT EXISTS idx_invoices_status        ON invoices(status);
CREATE INDEX IF NOT EXISTS idx_invoices_issued_at     ON invoices(issued_at DESC);
CREATE INDEX IF NOT EXISTS idx_invoices_mollie_pid    ON invoices(mollie_payment_id) WHERE mollie_payment_id IS NOT NULL;

-- Helper: sequence for human-readable invoice numbers (e.g. INV-2026-00001).
-- Bridge generates these server-side via app logic, not via SERIAL — we want
-- invoice numbers to be deterministic per year + immune to gaps from
-- rolled-back transactions.
CREATE SEQUENCE IF NOT EXISTS invoice_seq_2026 START 1 INCREMENT 1;
