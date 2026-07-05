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
from collections import defaultdict
from typing import Optional

from src.db.client import get_pool
from src.pricing import cost_eur, PRICING_VERSION

logger = logging.getLogger(__name__)

# Per-identifier skip counters — tracks how often each non-UUID/unresolvable
# user_id is seen so warnings show frequency, not just isolated occurrences.
_skip_counts: dict = defaultdict(int)

# Synthetic identity every EXPLICITLY anonymous call books to (fixed UUID,
# migration 032). Anonymous ≠ missing: 'anonymous:<grund>' is a deliberate
# app statement ("this call-site has no logged-in user by design") and gets
# its own accounting bucket; a missing X-User-ID is a leak (counted in
# src/attribution.py metrics, rejected once BRIDGE_ATTRIBUTION_ENFORCE=true).
ANONYMOUS_USER_ID = "00000000-0000-4000-a000-000000000001"
# Set once the identity row is confirmed present — avoids re-querying per call.
_anonymous_identity_verified = False


async def _anonymous_identity_present() -> bool:
    """Check (once, then cached) that the migration-032 identity exists.
    Without it an anonymous booking would FK-fail — warn loudly instead of
    producing a silent tracking gap."""
    global _anonymous_identity_verified
    if _anonymous_identity_verified:
        return True
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT 1 FROM users WHERE id = $1", ANONYMOUS_USER_ID)
    if row:
        _anonymous_identity_verified = True
        return True
    return False


async def _deduct_call_cost(
    user_id: str,
    app_id: str,
    cost_eur_amount: float,
    workflow_id: Optional[str] = None,
    tenant_id: Optional[str] = None,
) -> None:
    """
    Post-call budget deduction — best-effort, never raises.

    Runs after the activity row is written. The activity row is the
    authoritative usage record; this deduction keeps the user's budget
    `used_eur` as a running tally so the pre-call gate actually has
    something to gate against. A failure here degrades to "no deduction"
    (the prior behaviour) — it can never break the user-facing call.

    Routing by plan interval:
    - interval='project' (e.g. Energy): draw from a strictly per-project budget
      keyed by project_id (== workflow_id). It self-provisions on the project's
      first call (lazy allocation — the entitling slot was already consumed by
      the app). Project plans NEVER touch the monthly budget.
    - interval='month' (Report, Engelmann, ...): draw from the monthly tenant
      budget as before.
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

        if plan.interval == "project":
            # Project plans are fully per-project; they never fall through to the
            # monthly budget (a real project customer may have no monthly budget).
            if not workflow_id:
                logger.warning(
                    "post-call deduction: project plan %s call without workflow_id "
                    "for user=%s — cannot attribute to a project budget, skipping",
                    plan.id, user_id,
                )
                return
            from src.billing.project_budgets_service import deduct as _deduct_project

            result = await _deduct_project(
                uid,
                plan.id,
                workflow_id,
                cost_eur_amount,
                allocate_limit_eur=float(plan.api_budget_eur),
                tenant_id=tenant_id,
            )
            if not result.get("exists"):
                logger.warning(
                    "post-call deduction: could not resolve/allocate per-project "
                    "budget for project=%s plan=%s user=%s (tenant_id=%r) — call "
                    "NOT metered against any budget",
                    workflow_id, plan.id, user_id, tenant_id,
                )
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
    provider: str = "anthropic",
    provider_meta: Optional[dict] = None,
    region: Optional[str] = None,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> None:
    """
    Write one ai-call activity row. Never raises — tracking is best-effort.

    status: "success" | "error"
    app_env: already-normalised environment bucket (prod|staging|local) the
        call came from, or None when the app sent no X-App-Env header. The
        caller normalises it (extract_attribution_context); we just persist
        it. Drives the Platform Admin "mode" filter.
    provider: which backend served the call ('anthropic' | 'bedrock' | ...).
        Drives usage_events.provider — the reconciliation against the
        provider's own billing (e.g. CloudWatch token counts) keys on it.
    provider_meta: extra provider facts merged into provider_metadata
        (e.g. bedrock_model_id, region, aws_request_id for call-level joins
        with AWS invocation logs).
    input_tokens: UNCACHED input (Anthropic/Bedrock usage semantics). Prompt-
        cache traffic goes into cache_read_tokens (0.1x input price) and
        cache_creation_tokens (1.25x input price) — the physical input of a
        call is the sum of all three.
    """
    # Cost from the pricing SSoT. Error calls cost nothing (0.0) — only a
    # successful completion consumes budget.
    call_cost_eur = (
        cost_eur(
            model,
            input_tokens,
            output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_creation_tokens=cache_creation_tokens,
        )
        if status == "success" else 0.0
    )

    # Resolved from the user row inside the DB block below; initialised here so
    # the post-call deduction (which runs after that block, even if it failed)
    # can always pass it to _deduct_call_cost without a NameError.
    tenant_id: Optional[str] = None

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

    # Explicit anonymous marker ('anonymous:<grund>' or the legacy report
    # alias '_anonymous') → book to the dedicated anonymous identity. Resolved
    # BEFORE the UUID check below, which would otherwise skip these as
    # "non-UUID string" (the pre-032 behaviour = tracking gap).
    from src.attribution import anonymous_reason
    anon_reason = anonymous_reason(user_id)
    if anon_reason is not None:
        try:
            if await _anonymous_identity_present():
                user_id = ANONYMOUS_USER_ID
                agent_id = agent_id or "anonymous"
            else:
                logger.warning(
                    "persist_ai_call_activity: anonymous call (reason=%s app=%s) but "
                    "anonymous identity %s is missing — run migration 032. Activity skipped.",
                    anon_reason, app_id, ANONYMOUS_USER_ID,
                )
                return
        except Exception as e:  # noqa: BLE001 — tracking must never break the call
            logger.warning("persist_ai_call_activity: anonymous identity check failed: %s", e)
            return

    try:
        if not user_id:
            # No user → no tenant → cannot write a tenant-scoped row.
            # In-memory prompt-metrics still has it; nothing else to do.
            return

        import uuid as _uuid

        # Validate user_id is a UUID before hitting the DB. PostgreSQL
        # rejects non-UUID values on uuid-typed columns. Apps (or CUI) may
        # send emails or system strings — resolve emails via users.email;
        # skip non-user strings with a loud warning (Defensive Programming:
        # tracking gaps must never be silent).
        try:
            _uuid.UUID(user_id)
        except (ValueError, AttributeError, TypeError):
            if "@" in str(user_id):
                # Email address → look up the corresponding UUID
                _pool = get_pool()
                async with _pool.acquire() as _conn:
                    _row = await _conn.fetchrow(
                        "SELECT id FROM users WHERE email = $1", user_id
                    )
                if _row:
                    user_id = str(_row["id"])
                else:
                    _skip_counts[user_id] += 1
                    logger.warning(
                        "persist_ai_call_activity: email not found in users "
                        "(tracking gap #%d) user=%s app=%s → activity skipped. "
                        "Fix: caller should send user UUID, not email.",
                        _skip_counts[user_id], user_id, app_id,
                    )
                    return
            else:
                # 'system', 'internal', or other non-user strings — semantically
                # not a real user; nothing to persist in the tenant-scoped table.
                _skip_counts[user_id] += 1
                logger.warning(
                    "persist_ai_call_activity: non-UUID X-User-ID=%r app=%s "
                    "(skip #%d) → activity skipped (not a real user). "
                    "Fix: send user UUID or omit X-User-ID for system calls.",
                    user_id, app_id, _skip_counts[user_id],
                )
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
            if anon_reason is not None:
                # Which call-site declared itself anonymous — the per-<grund>
                # breakdown inside the anonymous bucket.
                payload["anonymousReason"] = anon_reason

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
            # Exception: real_cost = 0 only holds for calls served by our
            # subscription-covered Anthropic accounts. Bedrock invocations are
            # paid per token to AWS regardless of the tenant's plan — the
            # ledger must show that cost, or the 1:1 billing audit reads €0
            # while the AWS invoice grows.
            if billing_mode_text == "pay_per_token":
                bm_enum = "pay_per_token"
                real_cost = call_cost_eur
            else:
                bm_enum = "flat_rate_estimated"
                real_cost = call_cost_eur if provider == "bedrock" else 0.0

            await conn.execute(
                """
                INSERT INTO usage_events (
                    source,
                    user_id, tenant_id,
                    app, app_env, model, provider, region,
                    input_tokens, output_tokens,
                    cache_read_tokens, cache_creation_tokens,
                    billing_mode, real_cost_eur, hypothetical_cost_eur, pricing_version,
                    provider_metadata
                ) VALUES (
                    'workflow',
                    $1, $2,
                    $3, $4::app_env, $5, $6, $7,
                    $8, $9,
                    $10, $11,
                    $12::billing_mode_enum, $13, $14, $15,
                    $16::jsonb
                )
                """,
                actor_uuid,
                tenant_id,
                app_id,
                app_env,
                model,
                provider,
                region,
                input_tokens or 0,
                output_tokens or 0,
                cache_read_tokens or 0,
                cache_creation_tokens or 0,
                bm_enum,
                real_cost,
                call_cost_eur,  # hypothetical = priced at pay-per-token rates
                PRICING_VERSION,
                json.dumps({
                    "feature": feature,
                    "agent_id": agent_id,
                    "workflow_id": workflow_id,
                    "status": status,
                    **({"anonymous_reason": anon_reason} if anon_reason is not None else {}),
                    **(provider_meta or {}),
                }),
            )
    except Exception as e:  # noqa: BLE001 — tracking must never break the call
        logger.warning("persist_ai_call_activity failed (non-blocking): %s", e)

    # Post-call budget deduction. Separate step from the activity write:
    # the activity row is the usage source of truth, the deduction is the
    # running budget tally. Best-effort — _deduct_call_cost never raises.
    # Anonymous calls have no budget semantics (internal bucket, real_cost 0)
    # — deducting would only produce noisy "no budget row" warnings.
    if user_id and app_id and call_cost_eur > 0 and user_id != ANONYMOUS_USER_ID:
        await _deduct_call_cost(user_id, app_id, call_cost_eur, workflow_id, tenant_id)
