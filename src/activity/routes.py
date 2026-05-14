"""
Activity-Log Endpoints.

POST /v1/activity/log    Apps melden Events (Auth, Billing, Admin, Workflow, ...)
GET  /v1/activity/query  Admin-Dashboard filtert/durchsucht

Categories aus packages/usage-billing-admin/src/types/activity.ts:
  auth, user, tenant, billing, workflow, admin, storage, security, system
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from src.api_auth import require_jwt_or_service, AuthClaims
from src.db.client import get_pool

router = APIRouter(prefix="/v1/activity", tags=["activity"])

_ALLOWED_CATEGORIES = {
    "auth", "user", "tenant", "billing", "workflow",
    "admin", "storage", "security", "system",
}
_ALLOWED_APP_IDS = {
    "werking-report", "werking-energy", "werking-safety",
    "werking-noise", "engelmann",
}


class LogRequest(BaseModel):
    category: str
    eventType: str
    actorUserId: Optional[str] = None
    targetUserId: Optional[str] = None
    tenantId: Optional[str] = None
    appId: Optional[str] = None
    ip: Optional[str] = None
    userAgent: Optional[str] = None
    payload: Dict[str, Any] = {}


def _to_uuid(s: Optional[str]) -> Optional[uuid.UUID]:
    if not s:
        return None
    try:
        return uuid.UUID(s)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid UUID: {s}")


@router.post("/log")
async def activity_log(
    body: LogRequest,
    _claims: AuthClaims = Depends(require_jwt_or_service),
) -> Dict[str, Any]:
    if body.category not in _ALLOWED_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"Unknown category: {body.category}")
    if body.appId and body.appId not in _ALLOWED_APP_IDS:
        raise HTTPException(status_code=400, detail=f"Unknown appId: {body.appId}")

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO activities
              (id, timestamp, category, event_type, actor_user_id, target_user_id,
               tenant_id, app_id, ip, user_agent, payload)
            VALUES (gen_random_uuid(), NOW(), $1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
            RETURNING id, timestamp
            """,
            body.category, body.eventType,
            _to_uuid(body.actorUserId), _to_uuid(body.targetUserId),
            body.tenantId, body.appId, body.ip, body.userAgent,
            json.dumps(body.payload or {}),
        )
    return {"id": str(row["id"]), "timestamp": row["timestamp"].isoformat()}


@router.get("/query")
async def activity_query(
    tenantId: Optional[str] = None,
    userId: Optional[str] = Query(None, description="matches actor OR target"),
    appId: Optional[str] = None,
    category: Optional[str] = None,
    eventType: Optional[str] = None,
    since: Optional[str] = Query(None, description="ISO timestamp"),
    until: Optional[str] = Query(None, description="ISO timestamp"),
    limit: int = Query(100, ge=1, le=1000),
    mode: Optional[str] = Query(None, description="prod|staging|local — filter by tenant.category"),
    _claims: AuthClaims = Depends(require_jwt_or_service),
) -> Dict[str, Any]:
    where: List[str] = []
    args: List[Any] = []

    def add(cond: str, val: Any) -> None:
        args.append(val)
        where.append(cond.replace("$$", f"${len(args)}"))

    # All activity columns explicitly qualified — `category` is on both
    # activities and tenants and would be ambiguous once we LEFT JOIN tenants.
    if tenantId:
        add("activities.tenant_id = $$", tenantId)
    if userId:
        u = _to_uuid(userId)
        args.append(u)
        where.append(f"(activities.actor_user_id = ${len(args)} OR activities.target_user_id = ${len(args)})")
    if appId:
        if appId not in _ALLOWED_APP_IDS:
            raise HTTPException(status_code=400, detail=f"Unknown appId: {appId}")
        add("activities.app_id = $$", appId)
    if category:
        if category not in _ALLOWED_CATEGORIES:
            raise HTTPException(status_code=400, detail=f"Unknown category: {category}")
        add("activities.category = $$", category)
    if eventType:
        add("activities.event_type = $$", eventType)
    if since:
        add("activities.timestamp >= $$", since)
    if until:
        add("activities.timestamp <= $$", until)

    if mode:
        if mode not in ("prod", "staging", "local"):
            raise HTTPException(status_code=400, detail=f"Invalid mode: {mode}")
        args.append(mode)
        where.append(f"t.category = ${len(args)}::tenant_category")
        join_clause = "LEFT JOIN tenants t ON t.id = activities.tenant_id"
    else:
        join_clause = ""

    sql = f"""
      SELECT activities.id, activities.timestamp, activities.category, activities.event_type,
             activities.actor_user_id, activities.target_user_id,
             activities.tenant_id, activities.app_id, activities.ip, activities.user_agent, activities.payload
        FROM activities {join_clause}
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY activities.timestamp DESC LIMIT $" + str(len(args) + 1)
    args.append(limit)

    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)

    return {
        "activities": [
            {
                "id": str(r["id"]),
                "timestamp": r["timestamp"].isoformat(),
                "category": r["category"],
                "eventType": r["event_type"],
                "actorUserId": str(r["actor_user_id"]) if r["actor_user_id"] else None,
                "targetUserId": str(r["target_user_id"]) if r["target_user_id"] else None,
                "tenantId": r["tenant_id"],
                "appId": r["app_id"],
                "ip": r["ip"],
                "userAgent": r["user_agent"],
                "payload": r["payload"] if isinstance(r["payload"], dict) else json.loads(r["payload"] or "{}"),
            }
            for r in rows
        ],
        "count": len(rows),
    }

# mode_filter applied
