-- Migration 019 — manual_project_credits: Projekt-Slot-Zähler für manuell freigegebene Projekt-Bestellungen.
--
-- Workflow:
--   release_order (pending_orders_service.py, interval='project'):
--     → INSERT one row (quantity Slots, used=0, order_id UNIQUE als Double-Release-Guard)
--   Energy-App POST /api/workflows/jobs:
--     → GET /v1/users/{id}/project-credits → canCreateProject-Check
--     → POST /v1/users/{id}/project-credits/{plan_id}/consume → decrements used+1
--
-- UNIQUE (order_id): fail-fast gegen Doppel-Release des gleichen Orders.
-- CHECK (used <= quantity): DB-Invariante, verletzt von concurrent over-consume dank
--   SELECT FOR UPDATE in project_credits_service.consume_credit.

CREATE TABLE IF NOT EXISTS manual_project_credits (
    id          UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID            NOT NULL REFERENCES users(id),
    tenant_id   VARCHAR(128)    NOT NULL REFERENCES tenants(id),
    plan_id     VARCHAR         NOT NULL,
    quantity    INTEGER         NOT NULL DEFAULT 1 CHECK (quantity >= 1),
    used        INTEGER         NOT NULL DEFAULT 0 CHECK (used >= 0 AND used <= quantity),
    granted_at  TIMESTAMPTZ     NOT NULL DEFAULT now(),
    order_id    UUID            NOT NULL REFERENCES pending_orders(id) UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_mpc_user   ON manual_project_credits(user_id);
CREATE INDEX IF NOT EXISTS idx_mpc_tenant ON manual_project_credits(tenant_id);
CREATE INDEX IF NOT EXISTS idx_mpc_plan   ON manual_project_credits(plan_id);
