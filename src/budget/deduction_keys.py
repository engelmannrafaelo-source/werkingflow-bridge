"""
Idempotency keys for budget deductions (migration 061).

Both deduction endpoints — POST /v1/budget/deduct (monthly pot) and
POST /v1/internal/project-budgets/deduct (project pot) — are a
read-modify-write plus a FIFO draw through the TopUp lots. Without a key a
second attempt after a lost answer charges the customer twice, so the worker
was only allowed ONE unretried attempt, and every lost answer or short
platform-api outage lost the deduction silently (Befund 23.09.2026, dev-Bridge:
a paid werking-report call in the ledger, the monthly pot untouched).

Usage — inside the caller's transaction, BEFORE touching any budget row:

    stored = await claim(conn, key, scope="month", user_id=..., plan_id=..., amount_eur=...)
    if stored is not None:
        return stored            # already applied — nothing deducted this time
    ... deduct ...
    await record(conn, key, result)

The claim is an INSERT on the primary key, so a concurrent second attempt with
the same key blocks until the first transaction ends: it then either sees the
committed row (and returns its result) or, if the first rolled back because the
deduction was refused, claims the key itself. A refused deduction therefore
never burns the key.
"""
from __future__ import annotations

import json
import uuid
from typing import Any, Dict, Optional

# Worker call_uids are UUID4 strings (36 chars); anything much shorter is not a
# key that identifies one call, it is a caller bug.
MIN_KEY_LENGTH = 8
MAX_KEY_LENGTH = 200

_AMOUNT_TOLERANCE_EUR = 1e-6

# Extra transport-level attempts for a KEYED deduction (timeout / connection
# error only — call_platform never retries a 5xx). Keyless calls stay at 0.
DEDUCT_RETRIES_WITH_KEY = 2


class DeductionKeyConflict(ValueError):
    """The key was already used for a DIFFERENT deduction (other user, plan,
    project, pot or amount). Never answered with the stored result — that would
    confirm a charge the caller did not ask for. ValueError so the endpoints map
    it to 400 like every other invalid deduction."""


async def claim(
    conn: Any,
    key: str,
    *,
    scope: str,
    user_id: uuid.UUID,
    plan_id: str,
    amount_eur: float,
    project_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Claim `key` for this deduction. Returns None when the caller must deduct
    now, or the stored result (with ``duplicate: True``) when this exact
    deduction was already applied."""
    inserted = await conn.fetchval(
        """
        INSERT INTO budget_deductions
            (idempotency_key, scope, user_id, plan_id, project_id, amount_eur)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (idempotency_key) DO NOTHING
        RETURNING idempotency_key
        """,
        key, scope, user_id, plan_id, project_id, amount_eur,
    )
    if inserted is not None:
        return None

    row = await conn.fetchrow(
        """
        SELECT scope, user_id, plan_id, project_id, amount_eur, result
          FROM budget_deductions
         WHERE idempotency_key = $1
        """,
        key,
    )
    if row is None:
        # The conflicting row vanished between INSERT and SELECT — only possible
        # if someone deleted it by hand. Refuse rather than deduct a second time.
        raise RuntimeError(
            f"budget_deductions: key {key!r} conflicted on INSERT but is gone on "
            f"SELECT — deduction NOT applied, the key's history is inconsistent"
        )

    same = (
        row["scope"] == scope
        and str(row["user_id"]) == str(user_id)
        and row["plan_id"] == plan_id
        and (row["project_id"] or None) == (project_id or None)
        and abs(float(row["amount_eur"]) - float(amount_eur)) <= _AMOUNT_TOLERANCE_EUR
    )
    if not same:
        raise DeductionKeyConflict(
            f"idempotency key {key!r} was already used for a different deduction "
            f"(scope={row['scope']!r} user={row['user_id']} plan={row['plan_id']!r} "
            f"project={row['project_id']!r} amount={float(row['amount_eur']):.6f})"
        )

    result = row["result"]
    if isinstance(result, str):
        result = json.loads(result)
    if not isinstance(result, dict):
        # Claimed and committed without a result — the claiming transaction
        # wrote the row but not its answer. Cannot happen through claim/record
        # in one transaction; refuse loudly instead of guessing "nothing".
        raise RuntimeError(
            f"budget_deductions: key {key!r} is claimed but carries no result — "
            f"deduction NOT applied a second time"
        )
    return {**result, "duplicate": True}


async def record(conn: Any, key: str, result: Dict[str, Any]) -> None:
    """Store the first attempt's answer under its key (same transaction as the
    deduction and the claim)."""
    await conn.execute(
        "UPDATE budget_deductions SET result = $2::jsonb WHERE idempotency_key = $1",
        key,
        json.dumps(result),
    )
