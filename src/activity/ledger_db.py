"""The money path's database leaves (ADR-0009 Schritt 2c).

Everything here runs on platform-api, which holds the DB pool. These are the
statements `src/activity/ai_call_writer.py` used to execute itself; the worker
now reaches them over HTTP (POST /v1/internal/usage/ai-call and the two read
leaves), exactly as src/audit/db_writer.py did for the audit path in Schritt 2a.

The split is deliberate and follows Schritt 2b's rule: **data, not verdicts.**
Nothing in this module decides anything about billing. The pricing (src/pricing),
the billing-mode mapping (`resolve_ledger_cost`), the provider vocabulary, the
app-id normalisation and every skip branch stay in the worker, where they are
pure functions with their own unit tests. What moved is only the part that
genuinely needs a database connection.

Why the usage_events INSERT is safe to reach over a network at all: it carries
`idempotency_key` (UNIQUE, migration 016) and `ON CONFLICT ... DO NOTHING
RETURNING id`. "Did THIS attempt create the row" therefore survives being
answered twice, which is what lets the worker's write-ahead spool replay a call
whose answer got lost without charging for it twice. `RETURNING id` is the whole
protocol between the two sides — see `AiCallWriteResult.created`.

The audit row rides along in the SAME call rather than in a second round trip,
and stays bound to `created`:

  * `activities` has NO unique key. Replaying it unconditionally would duplicate
    the audit trail on every spool retry of a call whose money row already
    landed.
  * Money goes first. The two rows must not share a fate — they used to sit in
    one try block, so a rejected audit INSERT silently took the billing row down
    with it (2026-08-01, app_id="bridge-jobs" vs. the enum: every unattributed
    research job booked nothing). The audit failure is caught, reported back and
    never rolls the money row back.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional

from src.attribution import ANONYMOUS_USER_ID
from src.db.client import get_pool

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AiCallWriteResult:
    """created: this attempt inserted the usage_events row (vs. a replay that
    found it already there). It is the deduplication token for the budget
    deduction — see ai_call_writer._deduct_call_cost on why the deduction must
    be bound to it.

    audit_written: whether the accompanying `activities` row landed. Purely
    informational; a missing audit row never invalidates the money row."""

    created: bool
    audit_written: bool
    audit_error: Optional[str] = None


async def anonymous_identity_present() -> bool:
    """Does the migration-032 anonymous identity row exist?

    Without it an anonymous booking FK-fails. The caller holds the row OWED in
    its spool rather than skipping, so running the migration makes those calls
    arrive instead of having lost them.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT 1 FROM users WHERE id = $1", ANONYMOUS_USER_ID)
    return row is not None


async def load_billing_context(user_id: str) -> Optional[Dict[str, Any]]:
    """users ⋈ tenants → {tenantId, billingMode}, or None.

    The JOIN is inner, so None means EITHER the user id does not exist in
    `users` OR its tenant_id points nowhere. The caller keeps that distinction
    in its log message; this leaf only reports the absence.
    """
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT u.tenant_id, t.billing_mode
            FROM users u
            JOIN tenants t ON t.id = u.tenant_id
            WHERE u.id = $1
            """,
            user_id,
        )
    if row is None or not row["tenant_id"]:
        return None
    return {
        "tenantId": str(row["tenant_id"]),
        "billingMode": row["billing_mode"],
    }


async def insert_ai_call(
    *,
    idempotency_key: str,
    recorded_at: datetime,
    actor_user_id: Optional[str],
    tenant_id: str,
    app: Optional[str],
    app_env: Optional[str],
    model: str,
    provider: str,
    region: Optional[str],
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_creation_tokens: int,
    billing_mode: str,
    real_cost_eur: float,
    hypothetical_cost_eur: float,
    pricing_version: str,
    provider_metadata: Dict[str, Any],
    audit_event_type: str,
    audit_payload: Dict[str, Any],
) -> AiCallWriteResult:
    """Write the authoritative billing row, then (only if it was created here)
    the audit row. Both on one connection, in that order.

    `recorded_at` is the call's ORIGIN time, never NOW(): a row replayed after
    an outage must be recorded in the period it belongs to, or a call from the
    last minute of a month lands in the next one and the invoice quietly moves.
    This is the single reason POST /v1/activity/log cannot serve this path — it
    hardcodes NOW().

    Raises on a failed money row. The caller (the HTTP endpoint) must let that
    become a 5xx so the worker hears "not written" and keeps the call spooled.
    """
    actor_uuid: Optional[uuid.UUID] = None
    if actor_user_id:
        # Callers normalise identity before they get here; a value that still
        # is not a UUID would be rejected by the uuid column mid-INSERT.
        actor_uuid = uuid.UUID(str(actor_user_id))

    # status/error_code are promoted out of provider_metadata into dedicated,
    # indexed columns (migration 059) — ai_call_writer.py already puts both
    # into provider_metadata for every call, so this is a pure extraction, not
    # a new writer contract. provider_metadata keeps carrying them too (no
    # second source of truth to keep in sync, no reader breaks).
    status = str((provider_metadata or {}).get("status") or "success")
    error_code_val = (provider_metadata or {}).get("error_code")
    error_code = str(error_code_val) if error_code_val is not None else None

    pool = get_pool()
    async with pool.acquire() as conn:
        inserted = await conn.fetchrow(
            """
            INSERT INTO usage_events (
                source, recorded_at, idempotency_key,
                user_id, tenant_id,
                app, app_env, model, provider, region,
                input_tokens, output_tokens,
                cache_read_tokens, cache_creation_tokens,
                billing_mode, real_cost_eur, hypothetical_cost_eur, pricing_version,
                provider_metadata, status, error_code
            ) VALUES (
                'workflow', $17, $18,
                $1, $2,
                $3, $4::app_env, $5, $6, $7,
                $8, $9,
                $10, $11,
                $12::billing_mode_enum, $13, $14, $15,
                $16::jsonb, $19, $20
            )
            ON CONFLICT (idempotency_key) DO NOTHING
            RETURNING id
            """,
            actor_uuid,
            tenant_id,
            app,
            app_env,
            model,
            provider,
            region,
            input_tokens or 0,
            output_tokens or 0,
            cache_read_tokens or 0,
            cache_creation_tokens or 0,
            billing_mode,
            real_cost_eur,
            hypothetical_cost_eur,
            pricing_version,
            json.dumps(provider_metadata or {}),
            recorded_at,
            idempotency_key,
            status,
            error_code,
        )
        created = inserted is not None
        if not created:
            return AiCallWriteResult(created=False, audit_written=False)

        try:
            await conn.execute(
                """
                INSERT INTO activities
                  (id, timestamp, category, event_type, actor_user_id,
                   target_user_id, tenant_id, app_id, ip, user_agent, payload,
                   app_env)
                VALUES (gen_random_uuid(), $7, 'workflow', $1, $2,
                        NULL, $3, $4, NULL, NULL, $5::jsonb, $6::app_env)
                """,
                audit_event_type,
                actor_uuid,
                tenant_id,
                app,
                json.dumps(audit_payload or {}),
                app_env,
                recorded_at,
            )
        except Exception as audit_err:  # noqa: BLE001 — the money row already landed
            logger.error(
                "insert_ai_call: AUDIT row failed (app=%s model=%s): %s — the "
                "usage_events row IS written, so the call stays metered; the "
                "audit trail has a gap here",
                app, model, audit_err,
            )
            return AiCallWriteResult(
                created=True, audit_written=False, audit_error=str(audit_err)
            )

    return AiCallWriteResult(created=True, audit_written=True)
