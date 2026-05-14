-- Phase 5 — billing_events: every Mollie / billing-state mutation logged.
--
-- Distinct from /v1/activity (workflow / AI calls) and audit_log
-- (admin clicks). billing_events captures the money-flow trail:
-- payment.created, subscription.activated, customer.created, refunded, etc.
--
-- Append-only. Webhook handler and admin actions append; nothing updates.

CREATE TABLE IF NOT EXISTS billing_events (
    id                  UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp           TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    event_type          VARCHAR(128)    NOT NULL,           -- 'payment.paid', 'subscription.activated', 'invoice.issued', …
    user_id             UUID            REFERENCES users(id) ON DELETE SET NULL,
    tenant_id           VARCHAR(128)    REFERENCES tenants(id) ON DELETE SET NULL,
    subscription_id     UUID            REFERENCES subscriptions(id) ON DELETE SET NULL,
    invoice_id          UUID            REFERENCES invoices(id) ON DELETE SET NULL,
    mollie_payment_id   VARCHAR(128),
    amount_eur          NUMERIC(10,2),
    source              VARCHAR(64)     NOT NULL DEFAULT 'system',  -- 'mollie-webhook', 'admin', 'migration', 'system'
    payload             JSONB           NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_billing_events_ts        ON billing_events(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_billing_events_user      ON billing_events(user_id);
CREATE INDEX IF NOT EXISTS idx_billing_events_tenant    ON billing_events(tenant_id);
CREATE INDEX IF NOT EXISTS idx_billing_events_type      ON billing_events(event_type);
CREATE INDEX IF NOT EXISTS idx_billing_events_mollie    ON billing_events(mollie_payment_id) WHERE mollie_payment_id IS NOT NULL;
