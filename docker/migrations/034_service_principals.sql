-- 034_service_principals.sql
-- Per-caller service identities for Bridge access (Stufe 2 of the Bridge-
-- robustness plan, Rafael GO 2026-07-05). Spec: werking-report workspace
-- bridge-service-principals-spec-20260705.md.
--
-- Why this exists
-- ---------------
-- Today ALL callers share ONE AI_BRIDGE_API_KEY (verify_api_key, src/auth.py).
-- A single leak (verified 2026-07: foreign IPs succeeding on /v1/chat/completions)
-- exposes every app, and X-User-ID is a self-declared header the Bridge never
-- verifies — so a leaked key can attribute spend to any user or to none.
--
-- A service principal is a real DB-backed caller identity with its OWN token,
-- an app allowlist (which X-App-ID it may claim), an optional path allowlist,
-- and a monthly EUR cap for calls it makes WITHOUT an end-user (tooling /
-- workflow / voice — what the transition today books as 'anonymous:<grund>').
-- When a leak happens you rotate ONE principal and the token_prefix in the
-- structured logs names exactly which channel leaked.
--
-- Rollout is staged behind BRIDGE_PRINCIPALS_ENABLED (default OFF), same toggle
-- discipline as BRIDGE_ATTRIBUTION_ENFORCE. While OFF the table is inert and
-- auth is byte-identical to today. While ON, an unknown token is 401 and the
-- legacy AI_BRIDGE_API_KEY resolves to a synthetic 'legacy' principal
-- (allowed_apps '{*}') so nothing breaks until every caller has its own token.
--
-- Forward-only, idempotent.

CREATE TABLE IF NOT EXISTS service_principals (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Stable human name: 'report-vercel', 'energy-railway', 'workflows-engine',
    -- 'cui', 'dev-tooling'. Used in logs and the admin UI.
    name            TEXT NOT NULL UNIQUE,
    -- sha256(token) hex. The cleartext token is shown ONCE at creation and never
    -- stored — same discipline as a password hash.
    token_hash      TEXT NOT NULL,
    -- First 8 chars of the cleartext token, for log lines / admin display /
    -- "which token leaked" without revealing the secret.
    token_prefix    TEXT NOT NULL,
    -- Which X-App-ID values this principal may claim. '{*}' = any (legacy /
    -- operator tooling). A call whose X-App-ID is not covered is 403.
    allowed_apps    TEXT[] NOT NULL DEFAULT '{}',
    -- Optional path allowlist ('{*}' or empty = all enforced paths). A call to a
    -- path not covered is 403. Kept coarse on purpose (prefix-free exact paths).
    allowed_paths   TEXT[] NOT NULL DEFAULT '{*}',
    -- Hard monthly EUR cap for calls this principal makes WITHOUT an end-user
    -- (booked to the principal itself). NULL = the principal never makes its own
    -- billable calls (a pure customer-proxy like report-vercel); any no-user
    -- call from such a principal is 403, not silently free.
    monthly_cap_eur NUMERIC,
    active          BOOLEAN NOT NULL DEFAULT TRUE,
    -- Set when the token was last rotated (audit only).
    rotated_at      TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Token lookup is the hot path (every request). Only active principals are
-- resolvable; a deactivated principal's token is dead immediately.
CREATE UNIQUE INDEX IF NOT EXISTS service_principals_token_hash_active_idx
    ON service_principals (token_hash) WHERE active;
