"""
GDPR / DSGVO Self-Service Endpoints — require_self_or_admin.

POST   /v1/users/{user_id}/change-password   Change own password (old password verified for self-callers)
DELETE /v1/users/{user_id}                   GDPR Art. 17 — anonymization-with-retention
GET    /v1/users/{user_id}/export            GDPR Art. 20 — data portability export
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field

from src.api_auth import require_self_or_admin, AuthClaims
from src.db.client import get_pool
from src.identity.password import hash_password, verify_password

router = APIRouter(tags=["self-service"])


# ---------------------------------------------------------------------------
# POST /v1/users/{user_id}/change-password
# ---------------------------------------------------------------------------

class ChangePasswordRequest(BaseModel):
    oldPassword: str = Field(min_length=1)
    newPassword: str = Field(min_length=8)


@router.post("/v1/users/{user_id}/change-password", status_code=204, response_class=Response)
async def change_password(
    user_id: str,
    body: ChangePasswordRequest,
    claims: AuthClaims = Depends(require_self_or_admin),
) -> Response:
    """
    Self-service password change.

    For self-callers (user JWT or service proxy) the *current* password must be
    supplied and verified before the new one is accepted — a hijacked session
    cannot silently take over an account without the old credential.

    Operators (service token without X-User-ID, admin JWT) bypass old-password
    verification. Operators who need to force-reset a password can also use the
    existing PATCH /v1/users/{user_id} endpoint with `password`.
    """
    uid = uuid.UUID(user_id)
    pool = get_pool()

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT password_hash, anonymized_at FROM users WHERE id = $1", uid
        )

    if not row:
        raise HTTPException(status_code=404, detail="User not found")

    if row["anonymized_at"] is not None:
        raise HTTPException(status_code=410, detail="Account is closed")

    if not claims.is_operator:
        if not row["password_hash"]:
            raise HTTPException(
                status_code=409,
                detail="No password set on this account (SSO-only); contact support",
            )
        if not verify_password(body.oldPassword, row["password_hash"]):
            raise HTTPException(status_code=403, detail="Current password is incorrect")

    new_hash = hash_password(body.newPassword)
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE users SET password_hash = $1, updated_at = NOW() WHERE id = $2",
            new_hash,
            uid,
        )
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# close_account — GDPR Art. 17 anonymization-with-retention
#
# NOT a route. Reached via DELETE /v1/users/{user_id} in db/admin_routes
# (delete_user), which delegates every non-operator caller here after its
# require_self_or_admin check. It used to carry its own @router.delete on the
# identical path — permanently shadowed (admin_db_router registers before
# self_service_router in platform_main), i.e. dead code that four green
# isolation tests "covered" while every real portal call 403'd in the shadow
# handler. One path, one handler: decorator removed 2026-07-03;
# tests/test_route_shadowing.py keeps the route table shadow-free.
# ---------------------------------------------------------------------------

async def close_account(
    user_id: str,
    claims: AuthClaims,
) -> Dict[str, Any]:
    """
    GDPR Art. 17 — right to erasure via anonymization-with-retention.

    PII on the user row (email, name, password_hash) is cleared and replaced
    with anonymous placeholders. Sessions and credentials are revoked
    immediately. Mollie customer records and user stammdaten (which may contain
    full profile PII) are deleted.

    Financial records (invoices, subscriptions, credit_purchases, billing_events)
    are RETAINED with the user_id FK intact. The row itself is preserved
    (not deleted) to satisfy ON DELETE RESTRICT constraints from those tables
    and the HGB/AO ~10-year accounting/tax retention obligation. The anonymized
    user row becomes a legal stub — identifiable only by internal UUID, no
    longer by personal data.

    tenants.owner_user_id is set to NULL if this user was the tenant owner.

    Idempotent: a second call returns the existing anonymized_at without error.
    """
    uid = uuid.UUID(user_id)
    pool = get_pool()

    async with pool.acquire() as conn:
        user_row = await conn.fetchrow(
            "SELECT id, anonymized_at FROM users WHERE id = $1", uid
        )

    if not user_row:
        raise HTTPException(status_code=404, detail="User not found")

    if user_row["anonymized_at"] is not None:
        return {
            "userId": user_id,
            "anonymizedAt": user_row["anonymized_at"].isoformat(),
            "retained": {},
            "alreadyAnonymized": True,
        }

    # Placeholder email satisfies the UNIQUE NOT NULL constraint on users.email
    # while unambiguously marking the row as anonymized.
    placeholder_email = f"deleted+{uid}@werkingflow.invalid"
    now = datetime.now(timezone.utc)

    async with pool.acquire() as conn:
        async with conn.transaction():
            # Count retained records for transparency.
            invoice_count = await conn.fetchval(
                "SELECT COUNT(*) FROM invoices WHERE user_id = $1", uid
            )
            subscription_count = await conn.fetchval(
                "SELECT COUNT(*) FROM subscriptions WHERE user_id = $1", uid
            )
            credit_purchase_count = await conn.fetchval(
                "SELECT COUNT(*) FROM credit_purchases WHERE user_id = $1", uid
            )
            billing_event_count = await conn.fetchval(
                "SELECT COUNT(*) FROM billing_events WHERE user_id = $1", uid
            )

            # Anonymize user row — clear PII, set closure marker.
            await conn.execute(
                """
                UPDATE users
                   SET email         = $1,
                       name          = '[gelöscht]',
                       password_hash = NULL,
                       anonymized_at = $2,
                       updated_at    = $2
                 WHERE id = $3
                """,
                placeholder_email,
                now,
                uid,
            )

            # Revoke all active sessions immediately.
            await conn.execute("DELETE FROM sessions WHERE user_id = $1", uid)

            # Remove Mollie customer record (stores name + email).
            await conn.execute("DELETE FROM mollie_customers WHERE user_id = $1", uid)

            # Remove user stammdaten (opaque JSONB Gutachter-Profil).
            await conn.execute("DELETE FROM user_stammdaten WHERE user_id = $1", uid)

            # Revoke developer tokens (access credentials, not financial records).
            await conn.execute(
                "UPDATE developer_tokens SET revoked_at = $1"
                " WHERE user_id = $2 AND revoked_at IS NULL",
                now,
                uid,
            )

            # Clear tenant ownership — the tenant itself is retained.
            await conn.execute(
                "UPDATE tenants SET owner_user_id = NULL WHERE owner_user_id = $1", uid
            )

    return {
        "userId": user_id,
        "anonymizedAt": now.isoformat(),
        "retained": {
            "invoices": int(invoice_count),
            "subscriptions": int(subscription_count),
            "creditPurchases": int(credit_purchase_count),
            "billingEvents": int(billing_event_count),
        },
    }


# ---------------------------------------------------------------------------
# GET /v1/users/{user_id}/export — GDPR Art. 20 data portability
# ---------------------------------------------------------------------------

def _jsonb_safe(raw: Any) -> Dict[str, Any]:
    """Normalise an asyncpg JSONB value — may arrive as str or dict."""
    if isinstance(raw, str):
        try:
            return json.loads(raw) or {}
        except Exception:
            return {}
    return raw or {}


def _iso(v: Any) -> Any:
    return v.isoformat() if v else None


@router.get("/v1/users/{user_id}/export")
async def export_user_data(
    user_id: str,
    claims: AuthClaims = Depends(require_self_or_admin),
) -> Dict[str, Any]:
    """
    GDPR Art. 20 — right to data portability.

    Returns all personal data the Bridge holds for this user in a single
    machine-readable JSON payload. Includes profile, licenses, stammdaten,
    subscriptions, invoices, billing events, and activity log.

    Activities are capped at 1 000 most-recent entries to keep the response
    size bounded; the full log is available via GET /v1/activity/query.
    """
    uid = uuid.UUID(user_id)
    pool = get_pool()

    async with pool.acquire() as conn:
        profile_row = await conn.fetchrow(
            """
            SELECT id, email, name, tenant_id, role,
                   created_at, updated_at, anonymized_at
            FROM users WHERE id = $1
            """,
            uid,
        )
        if not profile_row:
            raise HTTPException(status_code=404, detail="User not found")

        license_rows = await conn.fetch(
            "SELECT app_id, plan_id, start_date, end_date, seats"
            " FROM app_licenses WHERE user_id = $1 ORDER BY start_date",
            uid,
        )

        stammdaten_row = await conn.fetchrow(
            "SELECT data, updated_at FROM user_stammdaten WHERE user_id = $1", uid
        )

        sub_rows = await conn.fetch(
            "SELECT id, app_id, plan_id, status, started_at, cancelled_at"
            " FROM subscriptions WHERE user_id = $1 ORDER BY started_at",
            uid,
        )

        invoice_rows = await conn.fetch(
            "SELECT id, invoice_number, status, total_eur, currency,"
            "       issued_at, paid_at, created_at"
            " FROM invoices WHERE user_id = $1 ORDER BY created_at",
            uid,
        )

        billing_event_rows = await conn.fetch(
            "SELECT id, timestamp, event_type, amount_eur, source"
            " FROM billing_events WHERE user_id = $1 ORDER BY timestamp",
            uid,
        )

        activity_rows = await conn.fetch(
            """
            SELECT id, timestamp, category, event_type, app_id, ip, user_agent
            FROM activities
            WHERE actor_user_id = $1 OR target_user_id = $1
            ORDER BY timestamp DESC
            LIMIT 1000
            """,
            uid,
        )

        budget_row = await conn.fetchrow(
            "SELECT monthly_budgets, updated_at FROM user_budgets WHERE user_id = $1", uid
        )

        # TopUp-Saldo aus den datierten Lots (aktive, nicht-abgelaufene Lots).
        # Der alte Skalar user_topup_balances ist Legacy und wird nicht mehr gelesen.
        balance_row = await conn.fetchrow(
            """
            SELECT COALESCE(SUM(amount_eur), 0) AS balance_eur,
                   MAX(updated_at) AS updated_at
            FROM user_topup_lots
            WHERE user_id = $1 AND expires_at > NOW() AND amount_eur > 0
            """,
            uid,
        )

    return {
        "exportedAt": datetime.now(timezone.utc).isoformat(),
        "schemaVersion": "1",
        "profile": {
            "id": str(profile_row["id"]),
            "email": profile_row["email"],
            "name": profile_row["name"],
            "tenantId": profile_row["tenant_id"],
            "role": profile_row["role"],
            "createdAt": _iso(profile_row["created_at"]),
            "updatedAt": _iso(profile_row["updated_at"]),
            "anonymizedAt": _iso(profile_row["anonymized_at"]),
        },
        "appLicenses": [
            {
                "appId": r["app_id"],
                "planId": r["plan_id"],
                "startDate": _iso(r["start_date"]),
                "endDate": _iso(r["end_date"]),
                "seats": r["seats"],
            }
            for r in license_rows
        ],
        "stammdaten": _jsonb_safe(stammdaten_row["data"]) if stammdaten_row else {},
        "subscriptions": [
            {
                "id": str(r["id"]),
                "appId": r["app_id"],
                "planId": r["plan_id"],
                "status": r["status"],
                "startedAt": _iso(r["started_at"]),
                "cancelledAt": _iso(r["cancelled_at"]),
            }
            for r in sub_rows
        ],
        "invoices": [
            {
                "id": str(r["id"]),
                "invoiceNumber": r["invoice_number"],
                "status": r["status"],
                "totalEur": str(r["total_eur"]),
                "currency": r["currency"],
                "issuedAt": _iso(r["issued_at"]),
                "paidAt": _iso(r["paid_at"]),
                "createdAt": _iso(r["created_at"]),
            }
            for r in invoice_rows
        ],
        "billingEvents": [
            {
                "id": str(r["id"]),
                "timestamp": _iso(r["timestamp"]),
                "eventType": r["event_type"],
                "amountEur": str(r["amount_eur"]) if r["amount_eur"] is not None else None,
                "source": r["source"],
            }
            for r in billing_event_rows
        ],
        "activities": [
            {
                "id": str(r["id"]),
                "timestamp": _iso(r["timestamp"]),
                "category": r["category"],
                "eventType": r["event_type"],
                "appId": r["app_id"],
                "ip": r["ip"],
                "userAgent": r["user_agent"],
            }
            for r in activity_rows
        ],
        "budget": {
            "monthlyBudgets": _jsonb_safe(budget_row["monthly_budgets"]) if budget_row else {},
            "updatedAt": _iso(budget_row["updated_at"]) if budget_row else None,
        },
        "topupBalance": {
            "balanceEur": str(balance_row["balance_eur"]) if balance_row else "0.00",
            "updatedAt": _iso(balance_row["updated_at"]) if balance_row else None,
        },
    }
