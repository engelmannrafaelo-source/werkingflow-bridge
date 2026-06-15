-- 030_project_budgets.sql
-- Per-project API budget for project-interval plans (e.g. werking-energy).
--
-- Project plans are sold per project (Energy: EUR 1.000/project, EUR 100 API
-- budget included, "beliebig viele Neuberechnungen solange das Budget reicht").
-- Until now the API spend was metered against user_budgets.monthly_budgets —
-- ONE budget per tenant that resets monthly. That contradicts the product:
-- a customer buying 3 projects in a month would share a single EUR 100 cap
-- and get blocked mid-second-project despite having 3 slots, and the budget
-- was never even provisioned by the project purchase.
--
-- This table gives each project its OWN budget, allocated when the project's
-- slot is consumed (job creation) and drawn down by that project's LLM calls.
-- Strictly per project (no monthly reset, no cross-project sharing).
--
-- The project_id mirrors the app's project_id, which is also the Bridge
-- attribution workflow_id — so the post-call deduction can resolve the right
-- budget from the X-Workflow-ID it already carries.
--
-- Monthly subscription plans (Report, Engelmann, ...) are unaffected: they keep
-- using user_budgets.monthly_budgets. Only plans with interval='project' route
-- here.
--
-- Forward-only, idempotent.

CREATE TABLE IF NOT EXISTS project_budgets (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     UUID            NOT NULL,
    tenant_id   VARCHAR         NOT NULL,
    plan_id     VARCHAR         NOT NULL,
    -- App-side project identifier == Bridge attribution workflow_id.
    project_id  VARCHAR         NOT NULL,
    -- Snapshot of plan.api_budget_eur at allocation time (so later price
    -- changes do not retroactively shrink an already-started project's budget).
    limit_eur   NUMERIC(12, 4)  NOT NULL,
    used_eur    NUMERIC(12, 4)  NOT NULL DEFAULT 0,
    -- Which manual_project_credits slot funded this budget (audit trail).
    credit_id   UUID,
    created_at  TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    -- One budget per (user, plan, project). Allocation is idempotent on this.
    CONSTRAINT project_budgets_unique UNIQUE (user_id, plan_id, project_id),
    -- Never let used exceed the allocated limit (DB-level last line of defence;
    -- the deduction caps in code too).
    CONSTRAINT project_budgets_used_within_limit CHECK (used_eur <= limit_eur),
    CONSTRAINT project_budgets_limit_nonneg CHECK (limit_eur >= 0),
    CONSTRAINT project_budgets_used_nonneg CHECK (used_eur >= 0)
);

-- Hot path: resolve a project's budget for the pre-call gate + post-call
-- deduction by (user, plan, project).
CREATE INDEX IF NOT EXISTS idx_project_budgets_lookup
    ON project_budgets (user_id, plan_id, project_id);
