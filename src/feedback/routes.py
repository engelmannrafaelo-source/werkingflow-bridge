"""
Feedback endpoints.
Mounted under /v1/feedback — only active when BRIDGE_DB_URL is set.

POST /v1/feedback          {appId, body, [rating, category, title, tenantId]}  — require_jwt_or_service
GET  /v1/feedback          [?appId&status&limit]  — require_admin
PATCH /v1/feedback/{id}    {status?, metadata?}  — require_admin
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, List, Optional

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from src.api_auth import require_admin, require_jwt_or_service, AuthClaims
from src.db.client import get_pool

router = APIRouter(prefix="/v1/feedback", tags=["feedback"])

_ALLOWED_STATUS = {"open", "triaged", "resolved", "wontfix"}
_ALLOWED_APPS = {
    "werking-report", "werking-energy", "werking-safety",
    "werking-noise", "engelmann",
}


def _row_to_dict(r: Any) -> Dict[str, Any]:
    payload_raw = r["metadata"]
    if isinstance(payload_raw, str):
        try:
            payload_raw = json.loads(payload_raw)
        except Exception:
            payload_raw = {}
    return {
        "id": str(r["id"]),
        "userId": str(r["user_id"]) if r["user_id"] else None,
        "tenantId": r["tenant_id"],
        "appId": r["app_id"],
        "rating": r["rating"],
        "category": r["category"],
        "title": r["title"],
        "body": r["body"],
        "status": r["status"],
        "metadata": payload_raw or {},
        "createdAt": r["created_at"].isoformat(),
        "updatedAt": r["updated_at"].isoformat(),
    }


class FeedbackCreateRequest(BaseModel):
    appId: str
    body: str = Field(min_length=1, max_length=10000)
    rating: Optional[int] = Field(default=None, ge=1, le=5)
    category: Optional[str] = Field(default=None, max_length=64)
    title: Optional[str] = Field(default=None, max_length=512)
    tenantId: Optional[str] = None
    userId: Optional[str] = None
    metadata: Dict[str, Any] = {}


@router.post("", status_code=201)
async def create_feedback(
    body: FeedbackCreateRequest,
    claims: AuthClaims = Depends(require_jwt_or_service),
) -> Dict[str, Any]:
    if body.appId not in _ALLOWED_APPS:
        raise HTTPException(status_code=400, detail=f"Unknown appId: {body.appId}")

    # Resolve user_id: prefer JWT subject when present; fall back to body.userId
    # for service-token use (e.g. anonymous feedback widgets server-side).
    user_id = claims.user_id if claims.is_user else body.userId
    user_uuid = uuid.UUID(user_id) if user_id else None

    pool = get_pool()
    try:
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO feedback
                  (user_id, tenant_id, app_id, rating, category, title, body, metadata)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
                RETURNING id, user_id, tenant_id, app_id, rating, category, title, body,
                          status, metadata, created_at, updated_at
                """,
                user_uuid,
                body.tenantId,
                body.appId,
                body.rating,
                body.category,
                body.title,
                body.body,
                json.dumps(body.metadata or {}),
            )
    except asyncpg.PostgresError:
        raise HTTPException(status_code=500, detail="Database error")
    return _row_to_dict(row)


@router.get("")
async def list_feedback(
    appId: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    _claims: AuthClaims = Depends(require_admin),
) -> Dict[str, Any]:
    where: List[str] = []
    args: List[Any] = []

    def add(cond: str, val: Any) -> None:
        args.append(val)
        where.append(cond.replace("$$", f"${len(args)}"))

    if appId:
        if appId not in _ALLOWED_APPS:
            raise HTTPException(status_code=400, detail=f"Unknown appId: {appId}")
        add("app_id = $$", appId)
    if status:
        if status not in _ALLOWED_STATUS:
            raise HTTPException(status_code=400, detail=f"Unknown status: {status}")
        add("status = $$", status)

    sql = """
      SELECT id, user_id, tenant_id, app_id, rating, category, title, body,
             status, metadata, created_at, updated_at
        FROM feedback
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY created_at DESC LIMIT $" + str(len(args) + 1)
    args.append(limit)

    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)

    return {
        "items": [_row_to_dict(r) for r in rows],
        "count": len(rows),
    }


class FeedbackUpdate(BaseModel):
    status: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None


@router.patch("/{feedback_id}")
async def update_feedback(
    feedback_id: str,
    body: FeedbackUpdate,
    _claims: AuthClaims = Depends(require_admin),
) -> Dict[str, Any]:
    if body.status is None and body.metadata is None:
        raise HTTPException(status_code=400, detail="At least one of status or metadata required")
    if body.status is not None and body.status not in _ALLOWED_STATUS:
        raise HTTPException(status_code=400, detail=f"Unknown status: {body.status}")

    sets: List[str] = []
    args: List[Any] = []

    def add_set(col: str, val: Any) -> None:
        args.append(val)
        sets.append(f"{col} = ${len(args)}")

    if body.status is not None:
        add_set("status", body.status)
    if body.metadata is not None:
        add_set("metadata", json.dumps(body.metadata))
    sets.append("updated_at = NOW()")

    args.append(uuid.UUID(feedback_id))
    sql = f"""
      UPDATE feedback SET {', '.join(sets)}
      WHERE id = ${len(args)}
      RETURNING id, user_id, tenant_id, app_id, rating, category, title, body,
                status, metadata, created_at, updated_at
    """

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(sql, *args)
    if not row:
        raise HTTPException(status_code=404, detail=f"Feedback {feedback_id} not found")
    return _row_to_dict(row)
