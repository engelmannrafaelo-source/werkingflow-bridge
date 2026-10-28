-- One coordinator owns reservations for BOTH bridges. No prompts or secrets.
CREATE TABLE IF NOT EXISTS anthropic_credit_reservations (
    id UUID PRIMARY KEY,
    account_id TEXT NOT NULL,
    grant_id TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens BIGINT NOT NULL CHECK (input_tokens >= 0),
    max_tokens BIGINT NOT NULL CHECK (max_tokens > 0),
    reserved_usd NUMERIC(18,9) NOT NULL CHECK (reserved_usd > 0),
    charged_usd NUMERIC(18,9) NOT NULL CHECK (charged_usd >= 0),
    state TEXT NOT NULL DEFAULT 'reserved' CHECK (state IN ('reserved','settled','released')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    settled_at TIMESTAMPTZ,
    usage JSONB
);
CREATE INDEX IF NOT EXISTS anthropic_credit_grant_idx
    ON anthropic_credit_reservations(grant_id);
