-- Migration 021 — auth_token_webhook_deliveries: per-token webhook fan-out audit.
--
-- ADR cross-app/0002: Bridge issues cleartext password-reset / email-verification
-- tokens but is intentionally NOT mail-stack-aware. Apps need to receive these
-- tokens to send the user-facing mail. Phase M1 (this migration + Bridge worker)
-- replaces the stdout-only token-log with a HMAC-signed webhook POST to the
-- app's configured receiver URL, with retry + audit-trail.
--
-- One row per (token, app) attempt-chain. The row tracks the full delivery
-- lifecycle: created `pending` when the token is issued, transitions to
-- `delivered` on first 2xx, `failed` on a 4xx (Bridge does NOT retry 4xx —
-- a 400 from the App means the App rejected our payload, which is an App bug,
-- not transient), or stays `pending` with `attempts++` and a future
-- `next_retry_at` on 5xx / network errors. After max attempts a row settles
-- as `abandoned` and an operator alert fires.
--
-- token_id FK with ON DELETE CASCADE because the delivery row is meaningless
-- once the token is gone (auth_tokens themselves are never deleted today,
-- so this is a forward-compat guarantee, not the hot path).
--
-- `(status, next_retry_at)` index — hot path of the dispatcher loop:
--   SELECT ... WHERE status='pending' AND (next_retry_at IS NULL
--                                          OR next_retry_at <= NOW())
--                   ORDER BY created_at LIMIT 50
-- Postgres can use the index to skip every delivered/failed/abandoned row.
--
-- `(token_id)` index — operators investigating "did this token get delivered?"
-- look it up by token_id; without the index this would be a seq scan once
-- the table grows.
--
-- `token_cleartext` — cleartext token, present ONLY for status='pending' rows.
-- The dispatcher loop needs the cleartext to POST in the webhook payload
-- (ADR cross-app/0002: `{token, kind, email, expiresAt, userId}`), but the
-- auth_tokens table itself stores sha256(token) only (see migration 018).
-- Storing cleartext in the deliveries queue is a deliberate, bounded
-- exposure: it is NULLed on any terminal state transition (delivered /
-- failed / abandoned), so a database dump can leak at most the still-
-- pending tokens (minutes-of-window on a healthy worker). The auth_tokens
-- invariant — "a DB dump leaks NOTHING usable" — is therefore weakened for
-- the deliveries table but preserved everywhere else. Operators concerned
-- about this can shorten retry windows or run dispatcher more aggressively;
-- the data is short-lived by construction.
--
-- A future Phase (M1.5 / M2) may eliminate this column by sending only a
-- token-id and having the app call back to Bridge through a service-token
-- endpoint to redeem cleartext. That trade-off (extra RTT + new endpoint
-- vs the column) is deferred — Phase M1 ships with the column to match
-- the ADR payload contract verbatim.

CREATE TABLE IF NOT EXISTS auth_token_webhook_deliveries (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    token_id        UUID         NOT NULL REFERENCES auth_tokens(id) ON DELETE CASCADE,
    app_id          app_id       NOT NULL,
    -- 'verify' | 'reset' | 'resend' — matches ADR vocabulary. Stored as text
    -- (not an enum) because the value is operator-facing and adding new kinds
    -- (e.g. 'magic-link') shouldn't need an enum migration.
    kind            TEXT         NOT NULL CHECK (kind IN ('verify', 'reset', 'resend')),
    -- 'pending' on insert; the worker transitions it to 'delivered' / 'failed' / 'abandoned'.
    status          TEXT         NOT NULL DEFAULT 'pending'
                                 CHECK (status IN ('pending', 'delivered', 'failed', 'abandoned')),
    attempts        INTEGER      NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    last_attempt_at TIMESTAMPTZ,
    next_retry_at   TIMESTAMPTZ,
    response_status INTEGER,
    response_body   TEXT,
    -- Cleartext token; see header. Required while pending, NULLed on terminal state.
    token_cleartext TEXT,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    -- Cleartext invariant: present while pending, absent once settled.
    CONSTRAINT chk_cleartext_lifecycle
        CHECK (
            (status = 'pending'  AND token_cleartext IS NOT NULL)
         OR (status <> 'pending' AND token_cleartext IS NULL)
        )
);

CREATE INDEX IF NOT EXISTS idx_auth_token_webhook_deliveries_pending
    ON auth_token_webhook_deliveries (status, next_retry_at)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS idx_auth_token_webhook_deliveries_token_id
    ON auth_token_webhook_deliveries (token_id);
