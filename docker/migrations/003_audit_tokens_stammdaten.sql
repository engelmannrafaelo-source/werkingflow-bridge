-- Sprint Phase-1.B — three platform tables.
-- Migration 003: audit_log + developer_tokens + stammdaten.

-- ---------------------------------------------------------------------------
-- audit_log — admin action history.
-- Separate from activities (which is workflow / per-call AI tracking).
-- audit_log captures: who clicked which admin button on what.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_log (
    id            UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    actor_user_id UUID         REFERENCES users(id) ON DELETE SET NULL,
    actor_label   VARCHAR(255),                       -- "service:cui", "Admin Rafael", …
    action        VARCHAR(128) NOT NULL,              -- "user.approved", "tenant.deleted", "subscription.cancelled"
    target_kind   VARCHAR(64),                        -- "user", "tenant", "subscription", "feedback"
    target_id     VARCHAR(128),                       -- string for portability
    before_state  JSONB,                              -- snapshot before mutation
    after_state   JSONB,                              -- snapshot after mutation
    ip            VARCHAR(64),
    user_agent    TEXT,
    metadata      JSONB        NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp   ON audit_log(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_action      ON audit_log(action);
CREATE INDEX IF NOT EXISTS idx_audit_log_target      ON audit_log(target_kind, target_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_actor       ON audit_log(actor_user_id);

-- ---------------------------------------------------------------------------
-- developer_tokens — API tokens issued to users for programmatic access.
-- We store sha256(token), never the plaintext. Display shows last 4 chars.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS developer_tokens (
    id            UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID         NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tenant_id     VARCHAR(128) REFERENCES tenants(id) ON DELETE CASCADE,
    name          VARCHAR(255) NOT NULL,             -- user-friendly label
    token_hash    VARCHAR(64)  NOT NULL UNIQUE,      -- sha256 hex of the secret
    last_4        VARCHAR(4)   NOT NULL,             -- "..abc4" for UI listing
    scopes        TEXT[]       NOT NULL DEFAULT '{}',
    created_at    TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    expires_at    TIMESTAMPTZ,
    last_used_at  TIMESTAMPTZ,
    revoked_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_devtok_user        ON developer_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_devtok_tenant      ON developer_tokens(tenant_id);
CREATE INDEX IF NOT EXISTS idx_devtok_active      ON developer_tokens(revoked_at) WHERE revoked_at IS NULL;

-- ---------------------------------------------------------------------------
-- stammdaten — per-tenant per-app config (replaces app-local stammdaten store).
-- JSONB content; app decides schema, bridge only stores + serves.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stammdaten (
    tenant_id   VARCHAR(128) NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    app_id      app_id       NOT NULL,
    data        JSONB        NOT NULL DEFAULT '{}',
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_by  UUID         REFERENCES users(id) ON DELETE SET NULL,
    PRIMARY KEY (tenant_id, app_id)
);
