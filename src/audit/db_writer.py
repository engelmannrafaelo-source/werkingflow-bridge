"""Raw audit_log INSERT (ADR-0009 Schritt 2a, C4).

Runs on platform-api, which holds the DB pool — used by
POST /v1/internal/audit-events (src/internal_routes.py). Workers no longer
call this directly; src/audit/recorder.py is now an HTTP client of that
endpoint instead of a writer of this one.

Mirrors the INSERT shape of src/audit/routes.py:log_audit; kept as a separate
function (not a shared import) because that route derives actor_user_id from
the caller's own JWT, while this one takes it as an explicit field — a
service-token worker call is recording an action on someone else's behalf.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, Optional

from src.db.client import get_pool, is_db_enabled


async def insert_audit_event(
    action: str,
    *,
    actor_user_id: Optional[str] = None,
    actor_label: Optional[str] = None,
    target_kind: Optional[str] = None,
    target_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Append one row to audit_log. No-op when the DB is not configured — same
    contract src.audit.recorder.record_audit_event used to have directly.

    actor_user_id is coerced to a UUID; a non-UUID id (e.g. an email or an app
    label) is preserved under metadata.actor_user_id_raw instead of being
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
