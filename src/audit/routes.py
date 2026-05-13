"""
Audit-Log endpoints — admin action history.

Distinct from /v1/activity (workflow / AI calls). audit captures admin-level
mutations: who clicked which button against which object.

POST /v1/audit/log     log an admin action (service-token or admin-JWT)
GET  /v1/audit/query   filter/list (admin only)
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from src.api_auth import require_admin, require_jwt_or_service, AuthClaims
from src.db.client import get_pool

router = APIRouter(prefix="/v1/audit", tags=["audit"])


def _row(r: Any) -> Dict[str, Any]:
    def _maybe_json(v: Any) -> Any:
        if v is None:
            return None
        if isinstance(v, (dict, list)):
            return v
        try:
            return json.loads(v)
        except Exception:
            return v
    return {
        "id": str(r["id"]),
        "timestamp": r["timestamp"].isoformat(),
        "actorUserId": str(r["actor_user_id"]) if r["actor_user_id"] else None,
        "actorLabel": r["actor_label"],
        "action": r["action"],
        "targetKind": r["target_kind"],
        "targetId": r["target_id"],
        "before": _maybe_json(r["before_state"]),
        "after": _maybe_json(r["after_state"]),
        "ip": r["ip"],
        "userAgent": r["user_agent"],
        "metadata": _maybe_json(r["metadata"]) or {},
    }


class AuditLogRequest(BaseModel):
    action: str
    targetKind: Optional[str] = None
    targetId: Optional[str] = None
    before: Optional[Dict[str, Any]] = None
    after: Optional[Dict[str, Any]] = None
    actorLabel: Optional[str] = None
    ip: Optional[str] = None
    userAgent: Optional[str] = None
    metadata: Dict[str, Any] = {}


@router.post("/log", status_code=201)
async def log_audit(
    body: AuditLogRequest,
    claims: AuthClaims = Depends(require_jwt_or_service),
) -> Dict[str, Any]:
    actor_id = uuid.UUID(claims.user_id) if claims.is_user and claims.user_id else None
    actor_label = body.actorLabel or (claims.email if claims.is_user else "service")

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO audit_log
              (actor_user_id, actor_label, action, target_kind, target_id,
               before_state, after_state, ip, user_agent, metadata)
            VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7::jsonb, $8, $9, $10::jsonb)
            RETURNING id, timestamp, actor_user_id, actor_label, action,
                      target_kind, target_id, before_state, after_state,
                      ip, user_agent, metadata
            """,
            actor_id, actor_label, body.action, body.targetKind, body.targetId,
            json.dumps(body.before) if body.before is not None else None,
            json.dumps(body.after) if body.after is not None else None,
            body.ip, body.userAgent,
            json.dumps(body.metadata or {}),
        )
    return _row(row)


@router.get("/query")
async def query_audit(
    action: Optional[str] = None,
    targetKind: Optional[str] = None,
    targetId: Optional[str] = None,
    actorUserId: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = Query(default=100, ge=1, le=1000),
    _claims: AuthClaims = Depends(require_admin),
) -> Dict[str, Any]:
    where: List[str] = []
    args: List[Any] = []

    def add(cond: str, val: Any) -> None:
        args.append(val)
        where.append(cond.replace("$$", f"${len(args)}"))

    if action:      add("action = $$", action)
    if targetKind:  add("target_kind = $$", targetKind)
    if targetId:    add("target_id = $$", targetId)
    if actorUserId: add("actor_user_id = $$", uuid.UUID(actorUserId))
    if since:       add("timestamp >= $$", since)
    if until:       add("timestamp <= $$", until)

    sql = """
      SELECT id, timestamp, actor_user_id, actor_label, action,
             target_kind, target_id, before_state, after_state,
             ip, user_agent, metadata
        FROM audit_log
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY timestamp DESC LIMIT $" + str(len(args) + 1)
    args.append(limit)

    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    return {"items": [_row(r) for r in rows], "count": len(rows)}
