"""Die 409 beim Hart-Loeschen nennt den Grund, den sie geprueft hat.

Befund 05.09.2026: Ein Wegwerf-Testkonto (@example.com, ein einziger Call)
liess sich nicht loeschen — mit der Begruendung, es habe eine
Abrechnungshistorie. Nachgemessen: 0 subscriptions, 0 credit_purchases;
geblockt hat usage_events. Der except-Zweig fing ForeignKeyViolationError
generisch und schrieb den Grund unbedingt in den Text, ohne ihn je auszuwerten.

Warum das mehr ist als ein Textfehler: die Regel "wer ein Testkonto anlegt,
entfernt es" (Master-CLAUDE.md) wird damit unerfuellbar — der Operator sucht in
der falschen Tabelle und landet stattdessen beim schwereren GDPR-Anonymisieren.
Und eine laengere feste Liste waere derselbe Fehler: gemessen blocken neun
Tabellen, und die zehnte kommt.
"""
import sys
from unittest.mock import MagicMock

# asyncpg ist eine C-Extension und im Unit-Test-Env nicht da — stubben, bevor
# admin_routes importiert wird (gleiches Muster wie test_role_support.py).
try:
    import asyncpg  # noqa: F401
except ImportError:
    _stub = MagicMock()
    _stub.UniqueViolationError = type("UniqueViolationError", (Exception,), {})
    _stub.ForeignKeyViolationError = type("ForeignKeyViolationError", (Exception,), {})
    _stub.PostgresError = type("PostgresError", (Exception,), {})
    _stub.Connection = MagicMock
    sys.modules["asyncpg"] = _stub

from contextlib import asynccontextmanager  # noqa: E402
from unittest.mock import AsyncMock, patch  # noqa: E402

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

import asyncpg  # noqa: E402

from src.db import admin_routes  # noqa: E402


class _Operator:
    """Minimaler AuthClaims-Ersatz: nur das Feld, an dem der Handler den
    Operator vom Kunden unterscheidet."""

    is_operator = True


def _pool_raising(exc):
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value={"tenant_id": "t-1"})
    conn.execute = AsyncMock(side_effect=exc)

    @asynccontextmanager
    async def _txn():
        yield

    conn.transaction = _txn

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = type("Pool", (), {"acquire": staticmethod(_acquire)})()
    return pool


def _fk_error(table: str, constraint: str):
    exc = asyncpg.ForeignKeyViolationError("blocked")
    # asyncpg fuellt diese Felder aus der Postgres-Fehlermeldung. Gemessen
    # 05.09.2026 gegen Postgres 16: TABLE NAME ist die REFERENZIERENDE Tabelle
    # (die, in der die blockierende Zeile liegt) — genau die, die der Operator
    # sucht.
    try:
        exc.table_name = table
        exc.constraint_name = constraint
    except AttributeError:  # echtes asyncpg: Attribute sind read-only
        pytest.skip("asyncpg exception attributes are read-only in this env")
    return exc


async def _delete_expecting_409(exc):
    with patch.object(admin_routes, "get_pool", return_value=_pool_raising(exc)):
        with pytest.raises(HTTPException) as raised:
            await admin_routes.delete_user(
                "0f9d3a6e-1111-4222-8333-444455556666", claims=_Operator()
            )
    assert raised.value.status_code == 409
    return str(raised.value.detail)


@pytest.mark.asyncio
async def test_409_names_the_table_that_actually_blocked():
    detail = await _delete_expecting_409(
        _fk_error("usage_events", "usage_events_user_id_fkey")
    )

    assert "usage_events" in detail, "die blockierende Tabelle wird nicht genannt"
    assert "usage_events_user_id_fkey" in detail, "der Constraint wird nicht genannt"


@pytest.mark.asyncio
async def test_409_does_not_claim_a_billing_history_it_never_checked():
    """Der Ausgangsbefund: ein Konto ohne jede Abrechnungszeile bekam
    'has billing records' zu lesen."""
    detail = await _delete_expecting_409(
        _fk_error("usage_events", "usage_events_user_id_fkey")
    )

    assert "has billing records" not in detail


@pytest.mark.asyncio
async def test_409_still_points_at_the_anonymize_path():
    """Der Ausweg muss drinbleiben — ohne ihn weiss der Operator nicht, wie er
    ein Konto mit echter, aufbewahrungspflichtiger Historie schliesst."""
    detail = await _delete_expecting_409(
        _fk_error("subscriptions", "subscriptions_user_id_fkey")
    )

    assert "/anonymize" in detail
