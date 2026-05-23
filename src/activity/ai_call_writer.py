"""
AI-call activity writer — the Bridge logs every LLM call itself.

Why this exists (ADR 0007 / token-tracking consolidation):
  Apps used to be responsible for POSTing /v1/activity/log after each AI call.
  That is unenforceable — any code path that calls the Bridge directly and
  forgets to log creates a silent gap in usage tracking. werking-report had
  13 such gaps.

  The Bridge sees *every* LLM call (it is the proxy). So the Bridge logs it,
  exactly once, right where it already records in-memory prompt-metrics. Apps
  no longer log anything — they only send attribution headers (X-App-ID,
  X-User-ID), which a Layer-0 validator enforces.

Contract:
  • Fire-and-forget. A tracking failure must NEVER break the user-facing
    call — every error is swallowed with a warning.
  • tenant_id is resolved from the user (X-User-ID → users.tenant_id).
  • A call with no resolvable tenant (no user, internal Bridge job) is NOT
    written to `activities` — that table is tenant-scoped and NOT NULL.
    Such calls still land in in-memory prompt-metrics. App calls always
    carry X-User-ID (Layer-0 enforced), so in practice every app call is
    persisted.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from src.db.client import get_pool
from src.pricing import cost_eur, PRICING_VERSION

logger = logging.getLogger(__name__)


async def _deduct_call_cost(user_id: str, app_id: str, cost_eur_amount: float) -> None:
    """
    Post-call budget deduction — best-effort, never raises.

    Runs after the activity row is written. The activity row is the
    authoritative usage record; this deduction keeps the user's budget
    `used_eur` as a running tally so the pre-call gate actually has
    something to gate against. A failure here degrades to "no deduction"
    (the prior behaviour) — it can never break the user-facing call.
    """
    try:
        import uuid as _uuid
        from src.budget.plans import find_plan_for_app
        from src.budget.routes import apply_budget_deduction, BudgetDeductionDenied

        plan = find_plan_for_app(app_id)
        if plan is None:
            return  # app not in the plan catalog — not budget-tracked
        try:
            uid = _uuid.UUID(user_id)
        except (ValueError, AttributeError, TypeError):
            return
        try:
            await apply_budget_deduction(uid, plan.id, cost_eur_amount)
        except BudgetDeductionDenied as denied:
            # The call already happened (the gate ran pre-call). A denial
            # here only means the running tally could not fully absorb the
            # cost — the activity row remains the authoritative usage record.
            logger.info(
                "post-call deduction denied (%s) user=%s app=%s",
                denied.reason, user_id, app_id,
            )
    except Exception as e:  # noqa: BLE001 — deduction must never break the call
        logger.warning("post-call budget deduction failed (non-blocking): %s", e)


async def persist_ai_call_activity(
    *,
    app_id: Optional[str],
    user_id: Optional[str],
    agent_id: Optional[str],
    workflow_id: Optional[str],
    model: str,
    input_tokens: int,
    output_tokens: int,
    status: str,
    duration_ms: int,
    error_code: Optional[str] = None,
    app_env: Optional[str] = None,
) -> None:
    """
    Write one ai-call activity row. Never raises — tracking is best-effort.

    status: "success" | "error"
    app_env: already-normalised environment bucket (prod|staging|local) the
        call came from, or None when the app sent no X-App-Env header. The
        caller normalises it (extract_attribution_context); we just persist
        it. Drives the Platform Admin "mode" filter.
    """
    # Cost from the pricing SSoT. Error calls cost nothing (0.0) — only a
    # successful completion consumes budget.
    call_cost_eur = (
        cost_eur(model, input_tokens, output_tokens)
        if status == "success" else 0.0
    )

    # Diagnostic: surface apps that fail to send X-App-Env so the Platform
    # Admin "mode" filter (which depends on app_env) stops being blind.
    # Logged per call (not rate-limited): the noise IS the signal — every
    # unattributed call is a tracking gap that needs an app-side fix.
    # When all callers send the header, the warning disappears entirely.
    if app_env is None and app_id:
        logger.warning(
            "ai_call_writer: missing X-App-Env header app=%s user=%s model=%s "
            "→ usage_events.app_env will be NULL (app-side bug, not bridge). "
            "Fix: include X-App-Env in the app's outbound bridge headers.",
            app_id, user_id, model,
        )

    try:
        if not user_id:
            # No user → no tenant → cannot write a tenant-scoped row.
            # In-memory prompt-metrics still has it; nothing else to do.
            return

        pool = get_pool()
        async with pool.acquire() as conn:
            trow = await conn.fetchrow(
                """
                SELECT u.tenant_id, t.billing_mode
                FROM users u
                JOIN tenants t ON t.id = u.tenant_id
                WHERE u.id = $1
                """,
                user_id,
            )
            tenant_id = trow["tenant_id"] if trow else None
            billing_mode_text = trow["billing_mode"] if trow else "subscription"
            if not tenant_id:
                # Unknown user or user without tenant — skip (see module docstring).
                return

            feature = agent_id or workflow_id or "call"
            event_type = (
                f"ai-call-error:{feature}" if status != "success"
                else f"ai-call:{feature}"
            )
            payload = {
                "feature": feature,
                "model": model,
                "promptTokens": input_tokens,
                "completionTokens": output_tokens,
                "totalTokens": (input_tokens or 0) + (output_tokens or 0),
                "costEur": call_cost_eur,  # priced via src/pricing.py (SSoT)
                "latencyMs": duration_ms,
                "loggedBy": "bridge",  # distinguishes self-log from legacy app POSTs
            }
            if error_code:
                payload["errorCode"] = error_code

            # actor_user_id is a uuid column — only set it when user_id parses.
            actor_uuid = None
            try:
                import uuid as _uuid
                actor_uuid = _uuid.UUID(user_id)
            except (ValueError, AttributeError, TypeError):
                actor_uuid = None

            # Audit record — the activity row is the source of truth for the
            # audit trail (who called what, when, from which app/env).
            await conn.execute(
                """
                INSERT INTO activities
                  (id, timestamp, category, event_type, actor_user_id,
                   target_user_id, tenant_id, app_id, ip, user_agent, payload,
                   app_env)
                VALUES (gen_random_uuid(), NOW(), 'workflow', $1, $2,
                        NULL, $3, $4, NULL, NULL, $5::jsonb, $6::app_env)
                """,
                event_type,
                actor_uuid,
                tenant_id,
                app_id,
                json.dumps(payload),
                app_env,
            )

            # Usage ledger — structured row with dedicated token/cost columns so
            # the Platform Admin can query across workflow + sandbox without JSONB
            # extraction.  billing_mode maps from the tenant's TEXT column:
            #   subscription  → flat_rate_estimated (user pays flat; per-call cost
            #                   is hypothetical only; real_cost = 0)
            #   pay_per_token → pay_per_token (real_cost = hypothetical_cost)
            if billing_mode_text == "pay_per_token":
                bm_enum = "pay_per_token"
                real_cost = call_cost_eur
            else:
                bm_enum = "flat_rate_estimated"
                real_cost = 0.0

            await conn.execute(
                """
                INSERT INTO usage_events (
                    source,
                    user_id, tenant_id,
                    app, app_env, model, provider,
                    input_tokens, output_tokens,
                    billing_mode, real_cost_eur, hypothetical_cost_eur, pricing_version,
                    provider_metadata
                ) VALUES (
                    'workflow',
                    $1, $2,
                    $3, $4::app_env, $5, 'anthropic',
                    $6, $7,
                    $8::billing_mode_enum, $9, $10, $11,
                    $12::jsonb
                )
                """,
                actor_uuid,
                tenant_id,
                app_id,
                app_env,
                model,
                input_tokens or 0,
                output_tokens or 0,
                bm_enum,
                real_cost,
                call_cost_eur,  # hypothetical = priced at pay-per-token rates
                PRICING_VERSION,
                json.dumps({
                    "feature": feature,
                    "agent_id": agent_id,
                    "workflow_id": workflow_id,
                    "status": status,
                }),
            )
    except Exception as e:  # noqa: BLE001 — tracking must never break the call
        logger.warning("persist_ai_call_activity failed (non-blocking): %s", e)

    # Post-call budget deduction. Separate step from the activity write:
    # the activity row is the usage source of truth, the deduction is the
    # running budget tally. Best-effort — _deduct_call_cost never raises.
    if user_id and app_id and call_cost_eur > 0:
        await _deduct_call_cost(user_id, app_id, call_cost_eur)
