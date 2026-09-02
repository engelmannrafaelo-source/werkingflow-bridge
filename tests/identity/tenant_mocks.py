"""
Test-Double für den Nutzer-Hartlöschpfad inklusive Mandanten-Bereinigung.

Bewusst SQL-erkennend statt reihenfolge-basiert (`side_effect=[...]`): der Pfad
stellt inzwischen mehrere verschiedene Fragen an dieselbe Verbindung
(Mandant des Nutzers, verbleibende Mitglieder, Katalog, Bestand). Eine
Positionsliste würde bei jeder zusätzlichen Frage stillschweigend die falsche
Antwort liefern und Tests grün lassen, die nichts mehr prüfen. Wer hier eine
Frage stellt, die der Double nicht kennt, bekommt einen lauten Fehler.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, Dict, Iterable, List, Optional, Sequence
from unittest.mock import AsyncMock, MagicMock

# Schema-Ausschnitt wie ihn information_schema auf beiden Bridges liefert
# (Stand 02.09.2026, nachgelesen am Katalog). `users` MUSS enthalten sein —
# tenant_scoped_tables bricht sonst absichtlich ab.
DEFAULT_TENANT_TABLES: Sequence[str] = (
    "activities",
    "billing_events",
    "developer_tokens",
    "feedback",
    "invoices",
    "manual_project_credits",
    "pending_orders",
    "project_budgets",
    "project_reset_requests",
    "purchase_consents",
    "sandbox_conversations",
    "sandbox_leases",
    "sandbox_usage_events",
    "stammdaten",
    "tenant_stammdaten",
    "usage_events",
    "users",
    "vat_id_validations",
)


class _Conn:
    """Minimaler asyncpg-Connection-Ersatz, der auf den SQL-Text hört."""

    def __init__(
        self,
        *,
        tenant_id: Optional[str],
        members_after_delete: int,
        blocking_tables: Sequence[str],
        schema_tables: Sequence[str],
        delete_user_result: str,
        delete_user_raises: Optional[BaseException],
        delete_tenant_result: str,
        delete_tenant_raises: Optional[BaseException],
        tenant_exists: bool,
        orphan_rows: Sequence[Dict[str, Any]],
    ) -> None:
        self._tenant_id = tenant_id
        self._members = members_after_delete
        self._blocking = list(blocking_tables)
        self._schema_tables = list(schema_tables)
        self._delete_user_result = delete_user_result
        self._delete_user_raises = delete_user_raises
        self._delete_tenant_result = delete_tenant_result
        self._delete_tenant_raises = delete_tenant_raises
        self._tenant_exists = tenant_exists
        self._orphan_rows = list(orphan_rows)

        self.execute = AsyncMock(side_effect=self._execute)
        self.fetch = AsyncMock(side_effect=self._fetch)
        self.fetchrow = AsyncMock(side_effect=self._fetchrow)
        self.fetchval = AsyncMock(side_effect=self._fetchval)

    # -- Abfragen --------------------------------------------------------
    async def _execute(self, sql: str, *args: Any) -> str:
        if "DELETE FROM users" in sql:
            if self._delete_user_raises is not None:
                raise self._delete_user_raises
            return self._delete_user_result
        if "DELETE FROM tenants" in sql:
            if self._delete_tenant_raises is not None:
                raise self._delete_tenant_raises
            return self._delete_tenant_result
        if "UPDATE tenants" in sql:
            return "UPDATE 1"
        raise AssertionError(f"Unerwartetes execute im Test-Double: {sql!r}")

    async def _fetch(self, sql: str, *args: Any) -> List[Dict[str, Any]]:
        if "information_schema.columns" in sql:
            return [{"table_name": t} for t in self._schema_tables]
        if "UNION ALL" in sql or "WHERE EXISTS" in sql:
            return [{"table_name": t} for t in self._blocking]
        if "FROM tenants t" in sql:
            return list(self._orphan_rows)
        raise AssertionError(f"Unerwartetes fetch im Test-Double: {sql!r}")

    async def _fetchrow(self, sql: str, *args: Any) -> Optional[Dict[str, Any]]:
        if "SELECT tenant_id FROM users" in sql:
            return None if self._tenant_id is None else {"tenant_id": self._tenant_id}
        raise AssertionError(f"Unerwartetes fetchrow im Test-Double: {sql!r}")

    async def _fetchval(self, sql: str, *args: Any) -> Any:
        if "COUNT(*) FROM users" in sql:
            return self._members
        if "SELECT 1 FROM tenants" in sql:
            return 1 if self._tenant_exists else None
        raise AssertionError(f"Unerwartetes fetchval im Test-Double: {sql!r}")

    # -- Transaktion -----------------------------------------------------
    def transaction(self):
        @asynccontextmanager
        async def _tx():
            yield None
        return _tx()

    # -- Auswertung ------------------------------------------------------
    def executed(self, needle: str) -> List[Any]:
        return [c for c in self.execute.call_args_list if needle in c[0][0]]


def mock_hard_delete_pool(
    *,
    tenant_id: Optional[str] = "tenant-1",
    members_after_delete: int = 0,
    blocking_tables: Iterable[str] = (),
    schema_tables: Iterable[str] = DEFAULT_TENANT_TABLES,
    delete_user_result: str = "DELETE 1",
    delete_user_raises: Optional[BaseException] = None,
    delete_tenant_result: str = "DELETE 1",
    delete_tenant_raises: Optional[BaseException] = None,
    tenant_exists: bool = True,
    orphan_rows: Iterable[Dict[str, Any]] = (),
):
    """Pool-Double für `patch("src.db.admin_routes.get_pool", ...)`."""
    conn = _Conn(
        tenant_id=tenant_id,
        members_after_delete=members_after_delete,
        blocking_tables=list(blocking_tables),
        schema_tables=list(schema_tables),
        delete_user_result=delete_user_result,
        delete_user_raises=delete_user_raises,
        delete_tenant_result=delete_tenant_result,
        delete_tenant_raises=delete_tenant_raises,
        tenant_exists=tenant_exists,
        orphan_rows=list(orphan_rows),
    )

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool, conn
