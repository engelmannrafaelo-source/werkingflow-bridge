"""
Project Resets Service — operator-gated one-shot "start this project over" grant.

See docker/migrations/038_project_reset_requests.sql for the product rationale.

Lifecycle:  requested → approved → redeemed   (or requested → rejected)

- create_request : app (Energy) files a reset request with a written argument.
- list_requests  : Platform Admin lists requests (filter by status/app).
- approve/reject : Platform Admin decides.
- redeem         : app redeems an APPROVED grant → resets that project's EUR 100
                   budget (project_budgets_service.reset_budget) and marks the
                   row 'redeemed', atomically. Self-consuming: a second restart
                   needs a fresh request + approval.

Fail-fast:
  - A duplicate OPEN request per project is blocked by the DB partial-unique
    index (uq_prr_open_per_project) → OpenRequestExistsError.
  - redeem() only ever consumes a row that is 'approved' AND matches
    (user_id, project_id); anything else returns redeemed=False (never resets a
    budget without an approved grant).
"""
from __future__ import annotations

import uuid
from typing import Any, Dict, List, Optional

from src.billing.project_budgets_service import reset_budget
from src.db.client import get_pool

# Statuses that count as "open" (block a duplicate request for the same project).
_OPEN_STATUSES = ("requested", "approved")


class OpenRequestExistsError(Exception):
    """An open (requested/approved) reset request already exists for this project."""

    def __init__(self, project_id: str) -> None:
        super().__init__(f"An open reset request already exists for project '{project_id}'")
        self.project_id = project_id


class RequestNotFoundError(Exception):
    """No reset request with the given id (in the required status)."""


def _row_to_dict(r: Any) -> Dict[str, Any]:
    return {
        "id": str(r["id"]),
        "userId": str(r["user_id"]),
        "tenantId": r["tenant_id"],
        "planId": r["plan_id"],
        "projectId": r["project_id"],
        "appId": r["app_id"],
        "projectName": r["project_name"],
        "argument": r["argument"],
        "status": r["status"],
        "requestedAt": r["requested_at"].isoformat() if r["requested_at"] else None,
        "decidedAt": r["decided_at"].isoformat() if r["decided_at"] else None,
        "decidedBy": r["decided_by"],
        "redeemedAt": r["redeemed_at"].isoformat() if r["redeemed_at"] else None,
    }


async def create_request(
    *,
    user_id: uuid.UUID,
    project_id: str,
    argument: str,
    plan_id: str = "energy-project",
    tenant_id: Optional[str] = None,
    app_id: Optional[str] = None,
    project_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Create a reset request (status='requested').

    Raises OpenRequestExistsError if the project already has an open request
    (the partial-unique index makes this race-safe under concurrency).
    """
    argument = (argument or "").strip()
    if not argument:
        raise ValueError("argument is required")
    project_id = (project_id or "").strip()
    if not project_id:
        raise ValueError("project_id is required")

    pool = get_pool()
    async with pool.acquire() as conn:
        try:
            row = await conn.fetchrow(
                """
                INSERT INTO project_reset_requests
                    (user_id, tenant_id, plan_id, project_id, app_id, project_name, argument)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                RETURNING *
                """,
                user_id,
                tenant_id,
                plan_id,
                project_id,
                app_id,
                project_name,
                argument,
            )
        except Exception as e:  # asyncpg.UniqueViolationError → open request exists
            if "uq_prr_open_per_project" in str(e):
                raise OpenRequestExistsError(project_id) from e
            raise
    return _row_to_dict(row)


async def list_requests(
    *,
    status: Optional[str] = None,
    app_id: Optional[str] = None,
    user_id: Optional[uuid.UUID] = None,
    project_id: Optional[str] = None,
    limit: int = 200,
) -> List[Dict[str, Any]]:
    """List reset requests, newest first. Optional filters for the Platform Admin."""
    conds: List[str] = []
    params: List[Any] = []

    def add(cond_tmpl: str, val: Any) -> None:
        params.append(val)
        conds.append(cond_tmpl.format(n=len(params)))

    if status:
        add("status = ${n}", status)
    if app_id:
        add("app_id = ${n}", app_id)
    if user_id:
        add("user_id = ${n}", user_id)
    if project_id:
        add("project_id = ${n}", project_id)

    where = f"WHERE {' AND '.join(conds)}" if conds else ""
    params.append(max(1, min(int(limit), 1000)))
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT * FROM project_reset_requests
            {where}
            ORDER BY requested_at DESC
            LIMIT ${len(params)}
            """,
            *params,
        )
    return [_row_to_dict(r) for r in rows]


async def decide(
    *,
    request_id: uuid.UUID,
    approve: bool,
    operator: Optional[str] = None,
) -> Dict[str, Any]:
    """Approve or reject a 'requested' reset request.

    Only a row still in 'requested' can be decided (idempotent-safe: a second
    approve/reject on an already-decided row raises RequestNotFoundError rather
    than silently re-deciding).
    """
    new_status = "approved" if approve else "rejected"
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            UPDATE project_reset_requests
               SET status = $2, decided_at = NOW(), decided_by = $3
             WHERE id = $1 AND status = 'requested'
            RETURNING *
            """,
            request_id,
            new_status,
            operator,
        )
    if row is None:
        raise RequestNotFoundError(f"No 'requested' reset request with id {request_id}")
    return _row_to_dict(row)


async def redeem(
    *,
    user_id: uuid.UUID,
    project_id: str,
    plan_id: str = "energy-project",
) -> Dict[str, Any]:
    """Redeem an APPROVED grant for (user_id, project_id): reset the project's
    budget to full and mark the grant 'redeemed' — atomically, one-shot.

    Returns {redeemed: bool, budgetReset: bool}. redeemed=False means there was
    no approved grant for this project (the caller must NOT reset anything and
    should surface "no reset unlocked"). Never resets a budget without consuming
    a matching approved grant in the same transaction.
    """
    project_id = (project_id or "").strip()
    pool = get_pool()
    async with pool.acquire() as conn:
        async with conn.transaction():
            grant = await conn.fetchrow(
                """
                SELECT id FROM project_reset_requests
                 WHERE user_id = $1 AND project_id = $2 AND status = 'approved'
                 ORDER BY decided_at ASC
                 LIMIT 1
                   FOR UPDATE
                """,
                user_id,
                project_id,
            )
            if grant is None:
                return {"redeemed": False, "budgetReset": False}

            budget_reset = await reset_budget(
                conn,
                user_id=user_id,
                plan_id=plan_id,
                project_id=project_id,
            )
            await conn.execute(
                """
                UPDATE project_reset_requests
                   SET status = 'redeemed', redeemed_at = NOW()
                 WHERE id = $1
                """,
                grant["id"],
            )
    return {"redeemed": True, "budgetReset": budget_reset}


async def get_open_status(
    *, user_id: uuid.UUID, project_id: str
) -> Dict[str, Any]:
    """Current reset state for one project (for the app to render the right UI).

    Returns {status} where status is the newest row's status among
    requested/approved, or 'none' when there is no open request. 'approved'
    means the app may now redeem (offer the "restart now" action).
    """
    project_id = (project_id or "").strip()
    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT status FROM project_reset_requests
             WHERE user_id = $1 AND project_id = $2
               AND status = ANY($3::text[])
             ORDER BY requested_at DESC
             LIMIT 1
            """,
            user_id,
            project_id,
            list(_OPEN_STATUSES),
        )
    return {"status": row["status"] if row else "none"}
