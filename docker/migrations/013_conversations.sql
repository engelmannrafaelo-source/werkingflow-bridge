-- Durable, server-side conversation persistence for the agent-sandbox daemon.
--
-- Until now the conversation index (conversations.json) and the Claude Code
-- transcripts lived ONLY inside the ephemeral per-session dir
-- (sessions/<sid>/). A daemon restart, idle-purge or device change lost them.
-- These tables make the Bridge the durable store, keyed by
-- (user_id, app, resource_id) so the daemon can rehydrate a fresh session.
--
-- Covers all sandbox adapters (rafael/private/business, the app agents,
-- unified-tester): `app` is a free VARCHAR, NOT the app_id enum, because the
-- internal adapters are not enum members.

CREATE TABLE IF NOT EXISTS sandbox_conversations (
    id                  TEXT         PRIMARY KEY,
    user_id             UUID         NOT NULL REFERENCES users(id)   ON DELETE CASCADE,
    tenant_id           VARCHAR(128) NOT NULL REFERENCES tenants(id) ON DELETE RESTRICT,
    app                 VARCHAR(64)  NOT NULL,
    resource_id         VARCHAR(256) NOT NULL,
    cc_session_id       TEXT,
    title               TEXT         NOT NULL DEFAULT '',
    message_count       INTEGER      NOT NULL DEFAULT 0,
    archived            BOOLEAN      NOT NULL DEFAULT FALSE,
    is_active           BOOLEAN      NOT NULL DEFAULT FALSE,
    created_at          TIMESTAMPTZ  NOT NULL DEFAULT now(),
    last_activity_at    TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- "All conversations of a user in an app" — powers cross-resource access
-- (e.g. the engelmann AI editor listing every document's chat).
CREATE INDEX IF NOT EXISTS idx_sandbox_conversations_user_app
    ON sandbox_conversations (user_id, app, last_activity_at DESC);

-- The per-resource list the daemon hydrates on /sandbox/start.
CREATE INDEX IF NOT EXISTS idx_sandbox_conversations_resource
    ON sandbox_conversations (user_id, app, resource_id, last_activity_at DESC);

-- Transcript = the Claude Code jsonl for one conversation, gzip-compressed.
-- One row per conversation, fully replaced on every snapshot.
CREATE TABLE IF NOT EXISTS sandbox_conversation_transcripts (
    conversation_id     TEXT         PRIMARY KEY
                                     REFERENCES sandbox_conversations(id) ON DELETE CASCADE,
    jsonl_gz            BYTEA        NOT NULL,
    byte_size           INTEGER      NOT NULL,
    updated_at          TIMESTAMPTZ  NOT NULL DEFAULT now()
);
