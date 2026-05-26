-- Migration 018 — auth_tokens: single-use tokens for password reset + email verification.
--
-- Two token types live in one table because they share identical lifecycle:
-- random token issued, sha256 stored, single-use (used_at), TTL via expires_at.
-- Separating into two tables would duplicate schema/indexes without adding
-- safety; the token_type column + ENUM is the discriminator.
--
-- Security choices:
--   - We store sha256(token), never cleartext. A DB dump leaks nothing usable.
--   - token_hash is UNIQUE so an attacker cannot bind a chosen token to a user.
--     The hash space (2^256) makes accidental collision unreachable.
--   - (user_id, token_type) UNIQUE WHERE used_at IS NULL: a user can hold
--     exactly one unused token per type at a time. Issuing a new one MUST
--     either mark the prior unused token as used or fail-loud — the partial
--     unique index turns silent re-issuance bugs into hard errors at insert.
--   - used_at + expires_at are both checked at consume-time. Tokens are NEVER
--     deleted after use; the row remains as an audit trail.
--
-- email_verified on users: present here because resend-verification +
-- verify-email both gate on it. Default FALSE preserves a fail-closed posture
-- for new users; existing users are unaffected (column is added with DEFAULT
-- so the rewrite is a metadata-only operation, not a row update).

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'auth_token_type') THEN
        CREATE TYPE auth_token_type AS ENUM ('password_reset', 'email_verification');
    END IF;
END$$;

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS auth_tokens (
    id          UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID            NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    token_hash  CHAR(64)        NOT NULL UNIQUE,
    token_type  auth_token_type NOT NULL,
    expires_at  TIMESTAMPTZ     NOT NULL,
    used_at     TIMESTAMPTZ,
    created_at  TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

-- Hash lookup at consume-time is the hot path (verify-email, reset-password).
-- UNIQUE on token_hash already creates an index, but we name it explicitly so
-- the lookup query plan is obvious to operators.
CREATE INDEX IF NOT EXISTS idx_auth_tokens_token_hash ON auth_tokens(token_hash);

-- One active token per (user, type). Partial index so re-issuance after
-- consumption is allowed without violation.
CREATE UNIQUE INDEX IF NOT EXISTS uq_auth_tokens_active_per_user_type
    ON auth_tokens(user_id, token_type)
    WHERE used_at IS NULL;

-- Rate-limit + audit queries scan by (user_id, token_type, created_at).
CREATE INDEX IF NOT EXISTS idx_auth_tokens_user_type_created
    ON auth_tokens(user_id, token_type, created_at DESC);
