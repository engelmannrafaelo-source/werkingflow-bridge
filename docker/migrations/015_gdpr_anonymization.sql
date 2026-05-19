-- GDPR / DSGVO anonymization marker.
--
-- anonymized_at: set when a user exercises the right to erasure (Art. 17).
-- Anonymized users have email/name/password_hash cleared but the row is
-- retained to satisfy FK constraints from invoices + subscriptions
-- (HGB/AO ~10-year tax/accounting retention obligation). The anonymized_at
-- column is the single source of truth for "is this account closed".
--
-- The row is NEVER deleted. A placeholder email
-- ("deleted+{uuid}@werkingflow.invalid") satisfies the UNIQUE NOT NULL
-- constraint on users.email while clearly marking the row as anonymized.

ALTER TABLE users ADD COLUMN IF NOT EXISTS anonymized_at TIMESTAMPTZ;
