-- Invoice approval gate — no invoice email leaves the system without an
-- explicit operator release in Platform Admin.
--
-- Rationale: automated invoicing (pending-order "Rechnungs-Lane" +
-- POST /v1/invoices/{id}/send) could put a customer email on the wire with no
-- human in the loop. For the initial billing period every outbound invoice
-- must be approved by an operator first, so nothing goes out by mistake.
--
-- The gate itself is toggled by the INVOICE_REQUIRE_APPROVAL env var on the
-- bridge (default ON). These columns record WHO approved and WHEN — they exist
-- regardless of the toggle so the audit trail is complete if the gate is ever
-- switched off and back on.
--
-- Forward-only: adding nullable columns is safe on a live table.

ALTER TABLE invoices ADD COLUMN IF NOT EXISTS approved_at  TIMESTAMPTZ;
ALTER TABLE invoices ADD COLUMN IF NOT EXISTS approved_by  VARCHAR(255);

-- Operator dashboard queries "everything awaiting my release" — index the
-- not-yet-approved rows for a fast partial scan.
CREATE INDEX IF NOT EXISTS idx_invoices_awaiting_approval
    ON invoices(created_at DESC)
    WHERE approved_at IS NULL;
