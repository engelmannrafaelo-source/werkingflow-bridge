"""
Worker-side audit-log writer (ADR-0009 Schritt 2a, C4).

The audit HTTP routes (routes.py) let external callers append audit events
via platform-api directly; this module lets the WORKER append events from
inside a request handler (used to persist the value-free pseudonymization
attestation as the DSGVO Rechenschaftspflicht record right where anonymization
is proxied — main.py calls this, not routes.py).

record_audit_event now POSTs to platform-api (POST /v1/internal/audit-events,
src/internal_routes.py) instead of writing to Postgres directly — the worker
holds no write path to audit_log anymore. Deliberately no spool and no
direct-DB fallback here (unlike the read paths in principals.py /
prepaid_cap.py — see ADR-0009 Schritt 2a design doc, C4 vs. C5): losing an
audit line is acceptable (Schritt 1, C7 already decided this for the DB-write
version), so a failed POST is logged and dropped, not retried or buffered.

record_audit_event never raises — a failure here must never fail the
anonymization call it accompanies. (The pre-2a version wrapped a raw INSERT
with no try/except despite documenting the same promise; the HTTP call below
makes it true, since call_platform's only failure mode is the
PlatformUnavailable caught here.)
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from src.platform_client import PlatformUnavailable, call_platform

logger = logging.getLogger(__name__)


async def record_audit_event(
    action: str,
    *,
    actor_user_id: Optional[str] = None,
    actor_label: Optional[str] = None,
    target_kind: Optional[str] = None,
    target_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Best-effort audit event, recorded via platform-api. Never raises —
    failures are logged (visible via the same warning-log alerting the old
    no-op-on-missing-DB path relied on) and otherwise swallowed."""
    try:
        await call_platform(
            "POST",
            "/v1/internal/audit-events",
            json={
                "action": action,
                "actor_user_id": actor_user_id,
                "actor_label": actor_label,
                "target_kind": target_kind,
                "target_id": target_id,
                "metadata": metadata or {},
            },
        )
    except PlatformUnavailable as e:
        logger.warning(
            "audit event %r not recorded (platform-api unreachable): %s", action, e
        )
