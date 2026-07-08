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
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from src.api_auth import require_jwt_or_service, AuthClaims, resolve_tenant_id
from src.db.client import get_pool
from src.tenant import get_app_env_from_request, normalize_app_env

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
    request: Request,
    claims: AuthClaims = Depends(require_jwt_or_service),
) -> Dict[str, Any]:
    if body.category not in _ALLOWED_CATEGORIES:
        raise HTTPException(status_code=400, detail=f"Unknown category: {body.category}")
    if body.appId and body.appId not in _ALLOWED_APP_IDS:
        raise HTTPException(status_code=400, detail=f"Unknown appId: {body.appId}")

    # tenant_id is derived from auth context. user-JWT → from JWT.
    # service-token → from body.tenantId, or derived from body.actorUserId
    # (apps logging on behalf of a signed-in user). See ADR 0007.
    tenant_id = await resolve_tenant_id(claims, body.tenantId, body.actorUserId)

    # app_env: the environment the app variant this call came from runs in.
    # Read from the request header, normalised to prod/staging/local. Absent
    # header → NULL (honest un-attributed). Drives the "mode" filter.
    app_env = normalize_app_env(get_app_env_from_request(request))

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO activities
              (id, timestamp, category, event_type, actor_user_id, target_user_id,
               tenant_id, app_id, ip, user_agent, payload, app_env)
            VALUES (gen_random_uuid(), NOW(), $1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb,
                    $10::app_env)
            RETURNING id, timestamp
            """,
            body.category, body.eventType,
            _to_uuid(body.actorUserId), _to_uuid(body.targetUserId),
            tenant_id, body.appId, body.ip, body.userAgent,
            json.dumps(body.payload or {}),
            app_env,
        )
    return {"id": str(row["id"]), "timestamp": row["timestamp"].isoformat()}


def _parse_iso_timestamp(value: str, field: str) -> datetime:
    """ISO-8601 string -> tz-aware datetime for binding against the TIMESTAMPTZ
    `activities.timestamp` column. asyncpg needs a datetime, not a raw string:
    binding the raw query-string raised a PG type error -> 500 for ANY since/until
    (the param is documented as an ISO timestamp). Fail loud with 400 on an
    unparseable value instead of a 500. Mirrors metrics/routes.py:_parse_iso but
    rejects (not silently ignores) an invalid value."""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid {field} timestamp: {value!r} (expected ISO-8601)",
        )
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


_ALLOWED_ACCOUNT_TYPES = {"customer", "test", "internal"}


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
    mode: Optional[str] = Query(None, description="prod|staging|local — filter by app_env (X-App-Env)"),
    account_type: Optional[str] = Query(None, description="customer|test|internal — filter by tenant.account_type (JOIN tenants)"),
    claims: AuthClaims = Depends(require_jwt_or_service),
) -> Dict[str, Any]:
    # Operators (service token without X-User-ID, admin JWT) may query across
    # all users. All others are scoped to their own activity: user JWTs see
    # only their own records; service tokens with X-User-ID see only the
    # proxied user's activity.
    if not claims.is_operator:
        caller_id = claims.effective_user_id
        if userId and userId != caller_id:
            raise HTTPException(status_code=403, detail="Forbidden: can only query own activity")
        userId = caller_id

    # Validate inputs fail-fast before building any SQL.
    if mode and mode not in ("prod", "staging", "local"):
        raise HTTPException(status_code=400, detail=f"Invalid mode: {mode!r}. Must be prod|staging|local")
    if account_type and account_type not in _ALLOWED_ACCOUNT_TYPES:
        raise HTTPException(status_code=400, detail=f"Invalid account_type: {account_type!r}. Must be customer|test|internal")

    where: List[str] = []
    args: List[Any] = []

    def add(cond: str, val: Any) -> None:
        args.append(val)
        where.append(cond.replace("$$", f"${len(args)}"))

    # Activity columns stay explicitly qualified for readability.
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
        add("activities.timestamp >= $$", _parse_iso_timestamp(since, "since"))
    if until:
        add("activities.timestamp <= $$", _parse_iso_timestamp(until, "until"))

    # "mode" filters by the environment the call actually came from
    # (X-App-Env → activities.app_env), NOT by the customer's hand-set
    # tenant.account_type. Rows with NULL app_env (pre-migration / no header)
    # are honestly un-attributed and excluded when a mode is requested.
    if mode:
        add("activities.app_env = $$::app_env", mode)

    # "account_type" filters by the owning tenant's type (customer|test|internal).
    # Uses an INNER JOIN so activities without a tenant_id are excluded when
    # filtering — a tenant-less activity cannot be attributed to an account type.
    join_clause = ""
    if account_type:
        join_clause = "JOIN tenants ON activities.tenant_id = tenants.id"
        add("tenants.account_type = $$::account_type", account_type)

    sql = f"""
      SELECT activities.id, activities.timestamp, activities.category, activities.event_type,
             activities.actor_user_id, activities.target_user_id,
             activities.tenant_id, activities.app_id, activities.ip, activities.user_agent, activities.payload
        FROM activities
        {join_clause}
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


# ---------------------------------------------------------------------------
# Cost-display verification — "what the user sees == what the ledger says"
# ---------------------------------------------------------------------------

import logging

logger = logging.getLogger(__name__)

# Per-call costs are round(x, 6) at write time (ai_call_writer). The display
# must show EXACTLY the ledger value — any per-call difference is a bug.
# The client-side SUM may differ by float addition order only.
_TOTAL_TOLERANCE_EUR = 0.0005

_MAX_VERIFY_CALLS = 500


class VerifyDisplayCall(BaseModel):
    id: str
    costEur: float


class VerifyDisplayRequest(BaseModel):
    appId: str
    calls: List[VerifyDisplayCall]
    totalCostEur: float


@router.post("/verify-display")
async def verify_display(
    body: VerifyDisplayRequest,
    request: Request,
    claims: AuthClaims = Depends(require_jwt_or_service),
) -> Dict[str, Any]:
    """
    An app reports the per-call EUR costs it just RENDERED to the user; the
    Bridge compares them against its own ledger (activities.payload.costEur).

    Any deviation is an accounting inconsistency: it is persisted as a
    'cost-display-mismatch' billing activity (visible in admin panels),
    logged as ERROR, and answered with 409 so the app can fail loud in the
    UI. Displaying costs the Bridge cannot confirm must never pass silently.

    Non-operator callers (user JWT, service token with X-User-ID) may only
    verify their own activity rows.
    """
    if body.appId not in _ALLOWED_APP_IDS:
        raise HTTPException(status_code=400, detail=f"Unknown appId: {body.appId}")
    if len(body.calls) > _MAX_VERIFY_CALLS:
        raise HTTPException(
            status_code=400,
            detail=f"Too many calls to verify: {len(body.calls)} > {_MAX_VERIFY_CALLS}",
        )
    if not body.calls:
        # Nothing displayed, nothing to verify — trivially consistent.
        return {"status": "ok", "checked": 0, "ledgerTotalEur": 0.0}

    ids = [_to_uuid(c.id) for c in body.calls]
    caller_id = claims.effective_user_id

    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, actor_user_id, app_id, payload
            FROM activities
            WHERE id = ANY($1::uuid[])
            """,
            ids,
        )

    by_id: Dict[str, Any] = {str(r["id"]): r for r in rows}

    mismatches: List[Dict[str, Any]] = []
    ledger_total = 0.0
    for call in body.calls:
        row = by_id.get(call.id)
        if row is None:
            mismatches.append({
                "id": call.id, "reason": "unknown-event",
                "displayedCostEur": call.costEur,
            })
            continue
        if row["app_id"] != body.appId:
            mismatches.append({
                "id": call.id, "reason": "wrong-app",
                "ledgerAppId": row["app_id"],
            })
            continue
        if not claims.is_operator:
            actor = str(row["actor_user_id"]) if row["actor_user_id"] else None
            if actor != caller_id:
                # Foreign row probed — treated as mismatch, not as data leak:
                # no ledger values are echoed back for it.
                mismatches.append({"id": call.id, "reason": "not-own-activity"})
                continue
        payload = row["payload"]
        if not isinstance(payload, dict):
            payload = json.loads(payload or "{}")
        ledger_cost = float(payload.get("costEur") or 0.0)
        ledger_total += ledger_cost
        if round(ledger_cost, 6) != round(call.costEur, 6):
            mismatches.append({
                "id": call.id, "reason": "cost-differs",
                "displayedCostEur": call.costEur,
                "ledgerCostEur": ledger_cost,
            })

    total_diff = abs(ledger_total - body.totalCostEur)
    if not mismatches and total_diff > _TOTAL_TOLERANCE_EUR:
        mismatches.append({
            "reason": "total-differs",
            "displayedTotalEur": body.totalCostEur,
            "ledgerTotalEur": round(ledger_total, 6),
        })

    if not mismatches:
        return {
            "status": "ok",
            "checked": len(body.calls),
            "ledgerTotalEur": round(ledger_total, 6),
        }

    # --- Inconsistency: persist + log loud, answer 409 -----------------------
    logger.error(
        "cost-display-mismatch app=%s user=%s checked=%d mismatches=%d "
        "displayedTotal=%.6f ledgerTotal=%.6f first=%r",
        body.appId, caller_id, len(body.calls), len(mismatches),
        body.totalCostEur, ledger_total, mismatches[0],
    )
    try:
        tenant_id = await resolve_tenant_id(claims, None, caller_id)
        if tenant_id:
            app_env = normalize_app_env(get_app_env_from_request(request))
            incident_payload = {
                "checked": len(body.calls),
                "mismatches": mismatches[:20],
                "mismatchCount": len(mismatches),
                "displayedTotalEur": body.totalCostEur,
                "ledgerTotalEur": round(ledger_total, 6),
            }
            pool = get_pool()
            async with pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO activities
                      (id, timestamp, category, event_type, actor_user_id,
                       target_user_id, tenant_id, app_id, ip, user_agent,
                       payload, app_env)
                    VALUES (gen_random_uuid(), NOW(), 'billing',
                            'cost-display-mismatch', $1, NULL, $2, $3, NULL,
                            NULL, $4::jsonb, $5::app_env)
                    """,
                    _to_uuid(caller_id) if caller_id else None,
                    tenant_id, body.appId,
                    json.dumps(incident_payload), app_env,
                )
        else:
            logger.error(
                "cost-display-mismatch: no tenant resolvable for user=%s — "
                "incident NOT persisted (still rejected with 409)", caller_id,
            )
    except Exception as e:  # noqa: BLE001 — persisting the incident must not mask the 409
        logger.error("cost-display-mismatch: incident persist failed: %s", e)

    raise HTTPException(
        status_code=409,
        detail={
            "error": "cost-display-mismatch",
            "message": "Angezeigte Kosten stimmen nicht mit dem Bridge-Ledger überein.",
            "mismatchCount": len(mismatches),
            "mismatches": mismatches[:20],
            "displayedTotalEur": body.totalCostEur,
            "ledgerTotalEur": round(ledger_total, 6),
        },
    )
