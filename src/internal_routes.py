"""platform-api endpoints for worker-internal reads/writes (ADR-0009 Schritt 2a).

Everything under /v1/internal/* exists so a worker can resolve or record
something it used to reach via a direct Postgres connection, via HTTP instead
(see src/platform_client.py — the client side of this contract). Every route
here is require_service_token-gated, exactly like the pre-existing
/v1/budget/check and /v1/budget/deduct.

Deliberately NOT exposed through the public nginx path (no /v1/internal
location block in docker/routes-platform-api.conf) — a worker on the same
Docker network, or a worker host reachable over Tailscale, talks to
platform-api directly. Nothing here is meant to be reachable from the public
load balancer at all.

Scope for Schritt 2a (see specs/bridge-worker-host-separation/
schritt2-db-ingress-DESIGN-20260820.md, Teil C): principals (C2), prepaid
vision cap (C6), and the audit-event write path (C4). The budget-gate read
chain (C3, incl. trial auto-provisioning) is explicitly Schritt 2b and does
NOT live here yet.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from src.api_auth import require_service_token, AuthClaims
from src.audit.db_writer import insert_audit_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/internal", tags=["internal"])


# ── C2: principals ───────────────────────────────────────────────────────

@router.get("/principals/{token_hash}")
async def get_principal_by_hash(
    token_hash: str,
    _claims: AuthClaims = Depends(require_service_token),
) -> Dict[str, Any]:
    """Mirrors src.principals.get_principal_row_by_hash — the same query a
    worker's own direct-DB fallback runs. 404 is a real, cacheable answer
    ("no active principal with that hash"), not an error."""
    from src.principals import get_principal_row_by_hash

    row = await get_principal_row_by_hash(token_hash)
    if row is None:
        raise HTTPException(status_code=404, detail="no active principal with that token hash")
    return row


# ── C6: prepaid vision cap ───────────────────────────────────────────────

@router.get("/prepaid-vision/spent-24h")
async def get_prepaid_vision_spent_24h(
    _claims: AuthClaims = Depends(require_service_token),
) -> Dict[str, Any]:
    """Mirrors src.routing.prepaid_cap's direct query — rolling-24h prepaid
    vision spend in EUR. No 404 case: absence of spend is 0.0, not "not found"."""
    from src.routing.prepaid_cap import query_spent_last_24h_from_db

    spent = await query_spent_last_24h_from_db()
    return {"spent_eur": spent}


# ── C4: audit-event write path ───────────────────────────────────────────

class AuditEventRequest(BaseModel):
    action: str = Field(..., min_length=1)
    actor_user_id: Optional[str] = None
    actor_label: Optional[str] = None
    target_kind: Optional[str] = None
    target_id: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


@router.post("/audit-events", status_code=204)
async def post_audit_event(
    body: AuditEventRequest,
    _claims: AuthClaims = Depends(require_service_token),
) -> Response:
    """Mirrors POST /v1/audit/log's INSERT, but takes an explicit
    actor_user_id in the body — the JWT-derived actor of /v1/audit/log doesn't
    apply to a service-token worker call recording an action on someone else's
    behalf (e.g. the pseudonymization attestation, see src/audit/recorder.py).
    """
    await insert_audit_event(
        action=body.action,
        actor_user_id=body.actor_user_id,
        actor_label=body.actor_label,
        target_kind=body.target_kind,
        target_id=body.target_id,
        metadata=body.metadata,
    )
    return Response(status_code=204)
