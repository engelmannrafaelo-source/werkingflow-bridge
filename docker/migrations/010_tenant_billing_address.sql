-- Phase 10 — tenant billing address.
--
-- Why: auto_create_invoice (billing/billing_service.py) must populate the
-- invoices.billing_address field for legally valid invoices (§11 UStG
-- requires the recipient's address on the face of the invoice). The address
-- belongs to the issuing entity — the tenant (company) — not the individual
-- user, because B2B invoices are addressed to the subscribing organisation.
--
-- Fields mirror the BillingAddress Pydantic model in invoices/routes.py:
--   name, street, city, postcode, country (ISO-3166-1 alpha-2), vat_id.
-- Stored as flat columns (not JSONB) for explicit type constraints and
-- efficient filtering.
--
-- billing_country uses CHAR(2) to enforce ISO-3166-1 alpha-2 at the
-- storage layer. Application layer normalises to uppercase before writing.
--
-- All columns are nullable: existing tenants have no address data. The
-- auto_create_invoice function marks invoices with a missing billing address
-- as incomplete via metadata flag {"incomplete": true, "missingBillingAddress": true}
-- so that operators can identify and remediate them.
--
-- Forward-only / idempotent: ADD COLUMN IF NOT EXISTS is a no-op when applied
-- a second time. No BEGIN/COMMIT — the migration runner wraps each file in a
-- single transaction.

ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS billing_name     VARCHAR(255),
    ADD COLUMN IF NOT EXISTS billing_street   VARCHAR(255),
    ADD COLUMN IF NOT EXISTS billing_city     VARCHAR(255),
    ADD COLUMN IF NOT EXISTS billing_postcode VARCHAR(64),
    ADD COLUMN IF NOT EXISTS billing_country  CHAR(2),
    ADD COLUMN IF NOT EXISTS billing_vat_id   VARCHAR(64);
