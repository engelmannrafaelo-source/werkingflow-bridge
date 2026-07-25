"""
Server-side audit-log writer.

The audit HTTP routes (routes.py) let external callers append audit events; this
module lets the Bridge itself append events from inside a request handler (it
holds the DB pool that the separate privacy-pdf container does not). Used to
persist the value-free pseudonymization attestation as the DSGVO
Rechenschaftspflicht record right where anonymization is proxied.

Mirrors the exact INSERT shape of routes.py:log_audit so both writers share one
`audit_log` schema. DB-guarded and UUID-robust: a malformed actor id or an
absent DB never raises out of here — the caller treats the write as
best-effort/non-blocking so an audit hiccup can never fail the anonymization
itself (failures still surface via the caller's warning log → alerting).
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, Optional

from src.db.client import get_pool, is_db_enabled


async def record_audit_event(
    action: str,
    *,
    actor_user_id: Optional[str] = None,
    actor_label: Optional[str] = None,
    target_kind: Optional[str] = None,
    target_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Append one row to ``audit_log``. No-op when the DB is not configured.

    ``actor_user_id`` is coerced to a UUID; a non-UUID id (e.g. an email or an
    app label) is preserved under ``metadata.actor_user_id_raw`` instead of being
    dropped, so the trail is never silently lossy.
    """
    if not is_db_enabled():
        return

    meta = dict(metadata or {})
    actor_uuid: Optional[uuid.UUID] = None
    if actor_user_id:
        try:
            actor_uuid = uuid.UUID(str(actor_user_id))
        except (ValueError, AttributeError, TypeError):
            meta["actor_user_id_raw"] = str(actor_user_id)

    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO audit_log
              (actor_user_id, actor_label, action, target_kind, target_id,
               before_state, after_state, ip, user_agent, metadata)
            VALUES ($1, $2, $3, $4, $5, NULL, NULL, NULL, NULL, $6::jsonb)
            """,
            actor_uuid, actor_label, action, target_kind, target_id,
            json.dumps(meta),
        )
