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

logger = logging.getLogger(__name__)


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
    try:
        if not user_id:
            # No user → no tenant → cannot write a tenant-scoped row.
            # In-memory prompt-metrics still has it; nothing else to do.
            return

        pool = get_pool()
        async with pool.acquire() as conn:
            trow = await conn.fetchrow(
                "SELECT tenant_id FROM users WHERE id = $1", user_id
            )
            tenant_id = trow["tenant_id"] if trow else None
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
    except Exception as e:  # noqa: BLE001 — tracking must never break the call
        logger.warning("persist_ai_call_activity failed (non-blocking): %s", e)
