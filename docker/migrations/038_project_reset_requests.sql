-- 038_project_reset_requests.sql
-- Project reset requests — operator-gated one-shot "start this project over" grant.
--
-- Product rule (Rafael, 2026-07-14): a project credit (manual_project_credits)
-- buys ONE project (Energy: EUR 100 API budget, unlimited re-runs while that
-- budget lasts — see 030_project_budgets.sql). When a customer needs to redo an
-- already-finished project from scratch (forgot data, new measurements) AFTER
-- its EUR 100 budget is spent, that is a second EUR 100 of real compute. Instead
-- of silently charging again or auto-detecting "same building" (address
-- heuristics fail loud both ways), the customer REQUESTS a reset with a written
-- justification; an operator APPROVES it in the Platform Admin; the app then
-- REDEEMS the one-shot grant, which resets that project's EUR 100 budget to full
-- (project_budgets_service.reset_budget) WITHOUT consuming another credit slot.
--
-- The grant is PROJECT-bound (project_id == Bridge attribution workflow_id ==
-- the app-side project_id), not user-bound: approving a reset for project P lets
-- exactly P be redone once. It is self-consuming — after redemption the row is
-- 'redeemed' and a further reset needs a fresh request + approval.
--
-- Lifecycle: requested → approved → redeemed   (or requested → rejected)
--
-- Partial UNIQUE (project_id) WHERE status IN ('requested','approved'):
--   at most ONE open request per project at a time — a customer cannot stack
--   requests, and an approved-but-not-yet-redeemed grant blocks a duplicate.
--   'redeemed'/'rejected' rows are excluded, so the next restart can request anew.
--
-- Additive: does NOT touch manual_project_credits or project_budgets schema.
-- Forward-only, idempotent.

CREATE TABLE IF NOT EXISTS project_reset_requests (
    id            UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id       UUID          NOT NULL REFERENCES users(id),
    -- Nullable: stored for display/scoping when the app provides it; the redeem
    -- path keys the budget reset on (user_id, plan_id, project_id) only.
    tenant_id     VARCHAR(128)  REFERENCES tenants(id),
    plan_id       VARCHAR       NOT NULL DEFAULT 'energy-project',
    -- App-side project identifier == Bridge attribution workflow_id (project_budgets.project_id).
    project_id    VARCHAR       NOT NULL,
    -- For Platform Admin filtering / display only.
    app_id        VARCHAR,
    project_name  VARCHAR,
    -- The customer's written justification (required — the whole point of the gate).
    argument      TEXT          NOT NULL,
    status        VARCHAR       NOT NULL DEFAULT 'requested'
                    CHECK (status IN ('requested', 'approved', 'redeemed', 'rejected')),
    requested_at  TIMESTAMPTZ   NOT NULL DEFAULT now(),
    -- Set when an operator approves or rejects.
    decided_at    TIMESTAMPTZ,
    decided_by    VARCHAR,
    -- Set when the app redeems an approved grant (budget reset happened).
    redeemed_at   TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_prr_status  ON project_reset_requests(status);
CREATE INDEX IF NOT EXISTS idx_prr_user    ON project_reset_requests(user_id);
CREATE INDEX IF NOT EXISTS idx_prr_project ON project_reset_requests(project_id);

-- At most one OPEN (requested or approved) request per project.
CREATE UNIQUE INDEX IF NOT EXISTS uq_prr_open_per_project
    ON project_reset_requests(project_id)
    WHERE status IN ('requested', 'approved');
