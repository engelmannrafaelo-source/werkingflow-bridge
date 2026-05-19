"""
Conversation persistence — durable store for the agent-sandbox daemon.

Mounted under /v1/sandbox — only active when BRIDGE_DB_URL is set.

The agent-sandbox daemon writes through here on every conversation mutation
and snapshots the Claude Code transcript; on a fresh session it rehydrates
from these endpoints. Keyed by (user_id, app, resource_id). All endpoints
require the service token — the daemon is trusted infrastructure.

Auth:
  - GET  /v1/sandbox/conversations                       : require_service_token
  - PUT  /v1/sandbox/conversations/:id                   : require_service_token
  - PUT  /v1/sandbox/conversations/:id/transcript        : require_service_token
  - GET  /v1/sandbox/conversations/:id/transcript        : require_service_token
"""
import base64
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from src.api_auth import require_service_token, AuthClaims
from src.db.client import get_pool

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/sandbox", tags=["sandbox-conversations"])


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class ConversationUpsertRequest(BaseModel):
    userId: uuid.UUID
    app: str
    resourceId: str
    ccSessionId: Optional[str] = None
    title: str = ""
    messageCount: int = 0
    archived: bool = False
    isActive: bool = False
    createdAt: Optional[datetime] = None
    lastActivityAt: Optional[datetime] = None


class ConversationRow(BaseModel):
    id: str
    userId: uuid.UUID
    app: str
    resourceId: str
    ccSessionId: Optional[str] = None
    title: str
    messageCount: int
    archived: bool
    isActive: bool
    createdAt: datetime
    lastActivityAt: datetime


class ConversationListResponse(BaseModel):
    conversations: list[ConversationRow]


class TranscriptUpsertRequest(BaseModel):
    jsonlGzBase64: str


class TranscriptUpsertResponse(BaseModel):
    ok: bool
    byteSize: int


class TranscriptResponse(BaseModel):
    conversationId: str
    jsonlGzBase64: str
    byteSize: int
    updatedAt: datetime


def _conversation_row(r) -> ConversationRow:
    return ConversationRow(
        id=r["id"],
        userId=r["user_id"],
        app=r["app"],
        resourceId=r["resource_id"],
        ccSessionId=r["cc_session_id"],
        title=r["title"],
        messageCount=r["message_count"],
        archived=r["archived"],
        isActive=r["is_active"],
        createdAt=r["created_at"],
        lastActivityAt=r["last_activity_at"],
    )


# ---------------------------------------------------------------------------
# GET /v1/sandbox/conversations
# ---------------------------------------------------------------------------

@router.get("/conversations", response_model=ConversationListResponse)
async def list_conversations(
    user_id: uuid.UUID = Query(...),
    app: str = Query(...),
    resource_id: Optional[str] = Query(None),
    include_archived: bool = Query(False),
    _claims: AuthClaims = Depends(require_service_token),
):
    """List a user's conversations. Without resource_id: every conversation of
    the user in this app (cross-resource — e.g. the AI editor's full view)."""
    clauses = ["user_id = $1", "app = $2"]
    params: list = [user_id, app]
    if resource_id is not None:
        params.append(resource_id)
        clauses.append(f"resource_id = ${len(params)}")
    if not include_archived:
        clauses.append("archived = FALSE")

    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"SELECT * FROM sandbox_conversations "
            f"WHERE {' AND '.join(clauses)} "
            f"ORDER BY last_activity_at DESC",
            *params,
        )
    return ConversationListResponse(conversations=[_conversation_row(r) for r in rows])


# ---------------------------------------------------------------------------
# PUT /v1/sandbox/conversations/{id}
# ---------------------------------------------------------------------------

@router.put("/conversations/{conversation_id}", response_model=ConversationRow)
async def upsert_conversation(
    conversation_id: str,
    body: ConversationUpsertRequest,
    _claims: AuthClaims = Depends(require_service_token),
):
    """Create-or-update one conversation index row (write-through from the
    daemon). tenant_id is resolved from the user — fail loud if unknown."""
    now = datetime.now(timezone.utc)
    created_at = body.createdAt or now
    last_activity_at = body.lastActivityAt or now

    pool = get_pool()
    async with pool.acquire() as conn:
        tenant_id = await conn.fetchval(
            "SELECT tenant_id FROM users WHERE id = $1", body.userId
        )
        if tenant_id is None:
            raise HTTPException(
                status_code=404,
                detail={"error": "user_not_found", "userId": str(body.userId)},
            )
        row = await conn.fetchrow(
            """
            INSERT INTO sandbox_conversations
                (id, user_id, tenant_id, app, resource_id, cc_session_id,
                 title, message_count, archived, is_active,
                 created_at, last_activity_at)
            VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
            ON CONFLICT (id) DO UPDATE SET
                cc_session_id    = EXCLUDED.cc_session_id,
                title            = EXCLUDED.title,
                message_count    = EXCLUDED.message_count,
                archived         = EXCLUDED.archived,
                is_active        = EXCLUDED.is_active,
                last_activity_at = EXCLUDED.last_activity_at
            RETURNING *
            """,
            conversation_id, body.userId, tenant_id, body.app, body.resourceId,
            body.ccSessionId, body.title, body.messageCount, body.archived,
            body.isActive, created_at, last_activity_at,
        )
        # Exactly one active conversation per (user, app, resource).
        if body.isActive:
            await conn.execute(
                """
                UPDATE sandbox_conversations SET is_active = FALSE
                WHERE user_id = $1 AND app = $2 AND resource_id = $3
                  AND id <> $4 AND is_active = TRUE
                """,
                body.userId, body.app, body.resourceId, conversation_id,
            )
    return _conversation_row(row)


# ---------------------------------------------------------------------------
# PUT /v1/sandbox/conversations/{id}/transcript
# ---------------------------------------------------------------------------

@router.put("/conversations/{conversation_id}/transcript", response_model=TranscriptUpsertResponse)
async def upsert_transcript(
    conversation_id: str,
    body: TranscriptUpsertRequest,
    _claims: AuthClaims = Depends(require_service_token),
):
    """Store a gzip-compressed Claude Code jsonl snapshot for a conversation."""
    try:
        blob = base64.b64decode(body.jsonlGzBase64, validate=True)
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "invalid_base64", "detail": str(exc)},
        )

    pool = get_pool()
    async with pool.acquire() as conn:
        exists = await conn.fetchval(
            "SELECT 1 FROM sandbox_conversations WHERE id = $1", conversation_id
        )
        if not exists:
            raise HTTPException(
                status_code=404,
                detail={"error": "conversation_not_found", "id": conversation_id},
            )
        await conn.execute(
            """
            INSERT INTO sandbox_conversation_transcripts
                (conversation_id, jsonl_gz, byte_size, updated_at)
            VALUES ($1, $2, $3, now())
            ON CONFLICT (conversation_id) DO UPDATE SET
                jsonl_gz   = EXCLUDED.jsonl_gz,
                byte_size  = EXCLUDED.byte_size,
                updated_at = now()
            """,
            conversation_id, blob, len(blob),
        )
    logger.info(f"transcript stored: conv={conversation_id} bytes={len(blob)}")
    return TranscriptUpsertResponse(ok=True, byteSize=len(blob))


# ---------------------------------------------------------------------------
# GET /v1/sandbox/conversations/{id}/transcript
# ---------------------------------------------------------------------------

@router.get("/conversations/{conversation_id}/transcript", response_model=TranscriptResponse)
async def get_transcript(
    conversation_id: str,
    _claims: AuthClaims = Depends(require_service_token),
):
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT conversation_id, jsonl_gz, byte_size, updated_at "
            "FROM sandbox_conversation_transcripts WHERE conversation_id = $1",
            conversation_id,
        )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "transcript_not_found", "id": conversation_id},
        )
    return TranscriptResponse(
        conversationId=row["conversation_id"],
        jsonlGzBase64=base64.b64encode(row["jsonl_gz"]).decode("ascii"),
        byteSize=row["byte_size"],
        updatedAt=row["updated_at"],
    )
