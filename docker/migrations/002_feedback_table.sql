-- Sprint B3.3 — feedback table
CREATE TABLE IF NOT EXISTS feedback (
    id          UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID         REFERENCES users(id) ON DELETE SET NULL,
    tenant_id   VARCHAR(128) REFERENCES tenants(id) ON DELETE SET NULL,
    app_id      app_id,
    rating      INTEGER      CHECK (rating IS NULL OR (rating >= 1 AND rating <= 5)),
    category    VARCHAR(64),
    title       VARCHAR(512),
    body        TEXT         NOT NULL,
    status      VARCHAR(32)  NOT NULL DEFAULT 'open',
    metadata    JSONB        NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_feedback_created_at ON feedback(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_feedback_status     ON feedback(status);
CREATE INDEX IF NOT EXISTS idx_feedback_app_id     ON feedback(app_id);
