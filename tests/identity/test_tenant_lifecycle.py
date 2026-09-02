"""
Mandanten-Lebenszyklus — der Gegenpart zur Mandanten-Anlage.

Hintergrund (Prod-Befund 02.09.2026): 13 Mandanten ohne Nutzer, sechs Namen
mehrfach. Ursache verifiziert am Code: jede Kontoanlage legt einen Mandanten an
(register / create_user / sandbox-JIT), die Kontolöschung liess ihn stehen
(`tenants.owner_user_id ON DELETE SET NULL`), die nächste Registrierung derselben
Adresse legte den nächsten an.

Geprüft wird hier beides — dass leere, datenlose Mandanten wirklich mitfallen,
UND dass Mandanten mit Mitgliedern oder Bestand ausdrücklich NICHT fallen. Der
zweite Teil ist der wichtigere: eine Bereinigung, die zu viel löscht, ist
schlimmer als die Hüllen, die sie aufräumt.
"""
from __future__ import annotations

import sys
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional, Sequence
from unittest.mock import AsyncMock, MagicMock, patch

import os
os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

try:
    import asyncpg  # noqa: F401
except ImportError:  # pragma: no cover — mirrors tests/identity/test_self_service.py
    _stub = MagicMock()
    _stub.UniqueViolationError = type("UniqueViolationError", (Exception,), {})
    _stub.ForeignKeyViolationError = type("ForeignKeyViolationError", (Exception,), {})
    _stub.PostgresError = type("PostgresError", (Exception,), {})
    _stub.Connection = MagicMock
    sys.modules["asyncpg"] = _stub

import asyncpg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import config
from src.db.admin_routes import router as admin_db_router
from src.identity import tenant_lifecycle
from src.identity.tenant_lifecycle import (
    TenantCleanupError,
    drop_tenant_if_orphaned,
    strip_email_from_tenant_name,
    tenant_blocking_tables,
    tenant_scoped_tables,
)
from tests.identity.tenant_mocks import DEFAULT_TENANT_TABLES, mock_hard_delete_pool


from datetime import datetime, timezone

_SERVICE_HEADER = {"X-Bridge-Service-Token": config.service_token}
_NOW = datetime.now(timezone.utc)


@pytest.fixture(scope="module")
def client() -> TestClient:
    app = FastAPI()
    app.include_router(admin_db_router)
    return TestClient(app)


# ---------------------------------------------------------------------------
# Fake-Connection für die Modul-Ebene (ohne HTTP)
# ---------------------------------------------------------------------------

class FakeConn:
    def __init__(
        self,
        *,
        schema_tables: Sequence[str] = DEFAULT_TENANT_TABLES,
        members: int = 0,
        blocking: Sequence[str] = (),
        delete_result: str = "DELETE 1",
        delete_raises: Optional[BaseException] = None,
        update_result: str = "UPDATE 1",
    ) -> None:
        self.schema_tables = list(schema_tables)
        self.members = members
        self.blocking = list(blocking)
        self.delete_result = delete_result
        self.delete_raises = delete_raises
        self.update_result = update_result
        self.executed: List[tuple] = []
        self.fetched_sql: List[str] = []

    async def fetch(self, sql: str, *args: Any) -> List[Dict[str, Any]]:
        self.fetched_sql.append(sql)
        if "information_schema.columns" in sql:
            return [{"table_name": t} for t in self.schema_tables]
        return [{"table_name": t} for t in self.blocking]

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return self.members

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append((sql, args))
        if "DELETE FROM tenants" in sql:
            if self.delete_raises is not None:
                raise self.delete_raises
            return self.delete_result
        if "UPDATE tenants" in sql:
            return self.update_result
        raise AssertionError(f"unerwartetes execute: {sql!r}")


# ---------------------------------------------------------------------------
# Katalog-Erkennung
# ---------------------------------------------------------------------------

class TestTenantScopedTables:

    async def test_excludes_users_and_activity_log(self):
        """
        `users` ist die Mitgliedschaft (getrennt geprüft), `activities` ist
        Protokoll. Beide dürfen eine Löschung nicht blockieren — sonst greift
        die Bereinigung nie, weil praktisch jeder Mandant Protokollzeilen hat.
        """
        conn = FakeConn()
        tables = await tenant_scoped_tables(conn)

        assert "users" not in tables
        assert "activities" not in tables
        # Bestandsträger sind drin — auch die ohne Fremdschlüssel.
        assert "usage_events" in tables
        assert "project_budgets" in tables
        assert "sandbox_usage_events" in tables

    async def test_unknown_new_table_blocks_by_default(self):
        """
        Fail-closed: eine Tabelle, die niemand hier eingeordnet hat, gilt als
        Bestand. Eine gepflegte Positivliste hätte den umgekehrten Fehler —
        stiller Datenverlust bei jeder neuen Tabelle.
        """
        conn = FakeConn(schema_tables=[*DEFAULT_TENANT_TABLES, "brandneue_tabelle"])
        assert "brandneue_tabelle" in await tenant_scoped_tables(conn)

    async def test_missing_users_column_is_fatal(self):
        """Sehen wir users.tenant_id nicht, sehen wir nicht das erwartete Schema."""
        conn = FakeConn(schema_tables=["usage_events", "invoices"])
        with pytest.raises(TenantCleanupError, match="Schema-Invariante"):
            await tenant_scoped_tables(conn)

    async def test_hostile_table_name_is_rejected_not_skipped(self):
        conn = FakeConn(schema_tables=[*DEFAULT_TENANT_TABLES, 'x"; DROP TABLE tenants; --'])
        with pytest.raises(TenantCleanupError, match="Muster"):
            await tenant_blocking_tables(conn, "tenant-1")

    async def test_blocking_check_is_a_single_query(self):
        conn = FakeConn(blocking=["project_budgets"])
        blocking = await tenant_blocking_tables(conn, "tenant-1")

        assert blocking == ["project_budgets"]
        exists_queries = [s for s in conn.fetched_sql if "WHERE EXISTS" in s]
        assert len(exists_queries) == 1
        assert '"activities"' not in exists_queries[0]


# ---------------------------------------------------------------------------
# drop_tenant_if_orphaned
# ---------------------------------------------------------------------------

class TestDropTenantIfOrphaned:

    async def test_deletes_empty_and_dataless_tenant(self):
        conn = FakeConn(members=0, blocking=[])
        result = await drop_tenant_if_orphaned(conn, "tenant-1", reason="test")

        assert result.deleted is True
        assert any("DELETE FROM tenants" in sql for sql, _ in conn.executed)

    async def test_team_tenant_with_remaining_member_is_untouched(self):
        conn = FakeConn(members=2, blocking=[])
        result = await drop_tenant_if_orphaned(conn, "team-tenant", reason="test")

        assert result.deleted is False
        assert result.remaining_members == 2
        assert conn.executed == []
        # Bei Mitgliedern wird der Bestand gar nicht erst gefragt.
        assert not any("WHERE EXISTS" in s for s in conn.fetched_sql)

    async def test_tenant_with_data_is_kept_and_names_what_holds_it(self):
        conn = FakeConn(members=0, blocking=["project_budgets", "purchase_consents"])
        result = await drop_tenant_if_orphaned(conn, "tenant-1", reason="test")

        assert result.deleted is False
        assert result.blocked_by == ["project_budgets", "purchase_consents"]
        assert conn.executed == []

    async def test_vanished_tenant_is_loud_not_silent(self):
        """`DELETE 0` heisst: jemand anderes hat parallel gearbeitet. Melden."""
        conn = FakeConn(members=0, delete_result="DELETE 0")
        with pytest.raises(TenantCleanupError, match="DELETE 1"):
            await drop_tenant_if_orphaned(conn, "tenant-1", reason="test")

    async def test_unexpected_fk_aborts_instead_of_guessing(self):
        """
        Eine Tabelle, die über eine anders benannte Spalte auf tenants zeigt,
        entzieht sich der Katalog-Erkennung. Dann wird nicht weitergelöscht.
        """
        conn = FakeConn(
            members=0,
            delete_raises=asyncpg.ForeignKeyViolationError("violates foreign key"),
        )
        with pytest.raises(TenantCleanupError, match="anders benannte Spalte"):
            await drop_tenant_if_orphaned(conn, "tenant-1", reason="test")

    async def test_empty_tenant_id_is_rejected(self):
        with pytest.raises(TenantCleanupError):
            await drop_tenant_if_orphaned(FakeConn(), "", reason="test")


# ---------------------------------------------------------------------------
# Mandantenname-Anonymisierung (Art.-17-Pfad)
# ---------------------------------------------------------------------------

class TestStripEmailFromTenantName:

    async def test_replaces_only_the_address_occurrence(self):
        conn = FakeConn()
        renamed = await strip_email_from_tenant_name(
            conn, "tenant-1", "max@firma.at", "deleted+x@werkingflow.invalid",
        )

        assert renamed is True
        sql, args = conn.executed[0]
        assert "UPDATE tenants" in sql
        assert "replace(name" in sql
        # Gebunden an das Vorkommen der Adresse — ein Team-Mandant "Muster GmbH"
        # wird nicht angefasst, weil die Bedingung dort nicht zutrifft.
        assert "position($2 in name) > 0" in sql
        assert args == ("tenant-1", "max@firma.at", "deleted+x@werkingflow.invalid")

    async def test_no_match_reports_false(self):
        conn = FakeConn(update_result="UPDATE 0")
        renamed = await strip_email_from_tenant_name(
            conn, "team", "max@firma.at", "deleted+x@werkingflow.invalid",
        )
        assert renamed is False

    async def test_missing_email_is_rejected(self):
        with pytest.raises(TenantCleanupError):
            await strip_email_from_tenant_name(FakeConn(), "t", "", "x")


# ---------------------------------------------------------------------------
# DELETE /v1/users/{id} — die eigentliche Ursache
# ---------------------------------------------------------------------------

class TestHardDeleteCleansUpTenant:

    def test_personal_tenant_dies_with_its_last_user(self, client: TestClient):
        uid = uuid.uuid4()
        pool, conn = mock_hard_delete_pool(tenant_id="tenant-of-uid")

        with patch("src.db.admin_routes.get_pool", return_value=pool):
            resp = client.delete(f"/v1/users/{uid}", headers=_SERVICE_HEADER)

        assert resp.status_code == 204
        assert len(conn.executed("DELETE FROM users")) == 1
        tenant_deletes = conn.executed("DELETE FROM tenants")
        assert len(tenant_deletes) == 1
        assert tenant_deletes[0][0][1] == "tenant-of-uid"

    def test_team_tenant_survives_one_member_leaving(self, client: TestClient):
        uid = uuid.uuid4()
        pool, conn = mock_hard_delete_pool(tenant_id="team", members_after_delete=3)

        with patch("src.db.admin_routes.get_pool", return_value=pool):
            resp = client.delete(f"/v1/users/{uid}", headers=_SERVICE_HEADER)

        assert resp.status_code == 204
        assert len(conn.executed("DELETE FROM users")) == 1
        assert conn.executed("DELETE FROM tenants") == []

    def test_tenant_with_data_survives(self, client: TestClient):
        """
        Der Fall, den kein Nutzer-Fremdschlüssel abdeckt: project_budgets hat
        keinen FK auf users, seine Zeilen überleben die Nutzerlöschung — und
        müssen den Mandanten festhalten.
        """
        uid = uuid.uuid4()
        pool, conn = mock_hard_delete_pool(
            tenant_id="tenant-1", blocking_tables=["project_budgets"],
        )

        with patch("src.db.admin_routes.get_pool", return_value=pool):
            resp = client.delete(f"/v1/users/{uid}", headers=_SERVICE_HEADER)

        assert resp.status_code == 204
        assert conn.executed("DELETE FROM tenants") == []

    def test_activity_log_alone_does_not_hold_the_tenant(self, client: TestClient):
        """Das Protokoll steht bewusst nicht in der Bestandsabfrage."""
        uid = uuid.uuid4()
        pool, conn = mock_hard_delete_pool(tenant_id="tenant-1")

        with patch("src.db.admin_routes.get_pool", return_value=pool):
            resp = client.delete(f"/v1/users/{uid}", headers=_SERVICE_HEADER)

        assert resp.status_code == 204
        exists_sql = [c[0][0] for c in conn.fetch.call_args_list if "WHERE EXISTS" in c[0][0]]
        assert len(exists_sql) == 1
        assert '"activities"' not in exists_sql[0]
        assert '"usage_events"' in exists_sql[0]

    def test_unknown_user_is_404_and_touches_no_tenant(self, client: TestClient):
        uid = uuid.uuid4()
        pool, conn = mock_hard_delete_pool(tenant_id=None)

        with patch("src.db.admin_routes.get_pool", return_value=pool):
            resp = client.delete(f"/v1/users/{uid}", headers=_SERVICE_HEADER)

        assert resp.status_code == 404
        assert conn.executed("DELETE FROM tenants") == []

    def test_billing_fk_still_409s_before_any_tenant_work(self, client: TestClient):
        uid = uuid.uuid4()
        pool, conn = mock_hard_delete_pool(
            tenant_id="tenant-1",
            delete_user_raises=asyncpg.ForeignKeyViolationError("violates foreign key"),
        )

        with patch("src.db.admin_routes.get_pool", return_value=pool):
            resp = client.delete(f"/v1/users/{uid}", headers=_SERVICE_HEADER)

        assert resp.status_code == 409
        assert "anonymize" in resp.json()["detail"]
        assert conn.executed("DELETE FROM tenants") == []

    def test_unclassifiable_tenant_aborts_the_whole_delete(self, client: TestClient):
        """
        Fail-loud statt halber Vorgang: kann die Bereinigung den Mandanten nicht
        einordnen, wird die Transaktion zurückgerollt — der Nutzer bleibt und der
        Operator erfährt warum, statt eine neue Hülle zu erzeugen.
        """
        uid = uuid.uuid4()
        pool, conn = mock_hard_delete_pool(
            tenant_id="tenant-1",
            delete_tenant_raises=asyncpg.ForeignKeyViolationError("violates foreign key"),
        )

        with patch("src.db.admin_routes.get_pool", return_value=pool):
            resp = client.delete(f"/v1/users/{uid}", headers=_SERVICE_HEADER)

        assert resp.status_code == 500
        assert "NOT deleted" in resp.json()["detail"]


# ---------------------------------------------------------------------------
# Betriebs-Gegenpart: Inventur + geführte Löschung
# ---------------------------------------------------------------------------

class TestOrphanInventory:

    def test_lists_orphans_with_what_holds_them(self, client: TestClient):
        now = _NOW
        pool, conn = mock_hard_delete_pool(
            blocking_tables=["usage_events"],
            orphan_rows=[
                {"id": "t1", "name": "Personal tenant for a@b.c",
                 "account_type": "customer", "created_at": now},
            ],
        )

        with patch("src.db.admin_routes.get_pool", return_value=pool):
            resp = client.get("/v1/tenants/orphaned", headers=_SERVICE_HEADER)

        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 1
        assert body["deletable"] == 0
        assert body["tenants"][0]["blockedBy"] == ["usage_events"]

    def test_route_is_not_shadowed_by_the_tenant_id_path(self, client: TestClient):
        """'orphaned' darf nicht als tenant_id gelesen werden."""
        pool, _ = mock_hard_delete_pool(orphan_rows=[])
        with patch("src.db.admin_routes.get_pool", return_value=pool):
            resp = client.get("/v1/tenants/orphaned", headers=_SERVICE_HEADER)
        assert resp.status_code == 200


class TestDeleteTenantRoute:

    def test_deletes_empty_dataless_tenant(self, client: TestClient):
        pool, conn = mock_hard_delete_pool()
        with patch("src.db.admin_routes.get_pool", return_value=pool):
            resp = client.delete("/v1/tenants/tenant-1", headers=_SERVICE_HEADER)

        assert resp.status_code == 204
        assert len(conn.executed("DELETE FROM tenants")) == 1

    def test_unknown_tenant_is_404(self, client: TestClient):
        pool, conn = mock_hard_delete_pool(tenant_exists=False)
        with patch("src.db.admin_routes.get_pool", return_value=pool):
            resp = client.delete("/v1/tenants/nope", headers=_SERVICE_HEADER)

        assert resp.status_code == 404
        assert conn.executed("DELETE FROM tenants") == []

    def test_tenant_with_members_is_409(self, client: TestClient):
        pool, conn = mock_hard_delete_pool(members_after_delete=1)
        with patch("src.db.admin_routes.get_pool", return_value=pool):
            resp = client.delete("/v1/tenants/team", headers=_SERVICE_HEADER)

        assert resp.status_code == 409
        assert "user(s)" in resp.json()["detail"]
        assert conn.executed("DELETE FROM tenants") == []

    def test_tenant_with_data_is_409_and_names_the_tables(self, client: TestClient):
        pool, conn = mock_hard_delete_pool(blocking_tables=["invoices", "usage_events"])
        with patch("src.db.admin_routes.get_pool", return_value=pool):
            resp = client.delete("/v1/tenants/tenant-1", headers=_SERVICE_HEADER)

        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert "invoices" in detail and "usage_events" in detail
        assert conn.executed("DELETE FROM tenants") == []

    def test_requires_operator(self, client: TestClient):
        pool, _ = mock_hard_delete_pool()
        with patch("src.db.admin_routes.get_pool", return_value=pool):
            resp = client.delete("/v1/tenants/tenant-1")
        assert resp.status_code in (401, 403)


# ---------------------------------------------------------------------------
# POST /v1/users — die zweite Quelle: Mandant ohne Nutzer nach einem 409
# ---------------------------------------------------------------------------

class _RecordingConn:
    """Protokolliert Reihenfolge UND Transaktionsgrenzen."""

    def __init__(self, *, user_insert_raises: Optional[BaseException] = None) -> None:
        self.log: List[str] = []
        self._user_insert_raises = user_insert_raises

    def transaction(self):
        log = self.log

        @asynccontextmanager
        async def _tx():
            log.append("BEGIN")
            try:
                yield None
            except BaseException:
                log.append("ROLLBACK")
                raise
            else:
                log.append("COMMIT")

        return _tx()

    async def fetchrow(self, sql: str, *args: Any):
        if "SELECT id FROM tenants" in sql:
            self.log.append("SELECT tenants")
            return None
        if "INSERT INTO users" in sql:
            self.log.append("INSERT users")
            if self._user_insert_raises is not None:
                raise self._user_insert_raises
            return {
                "id": uuid.uuid4(), "email": "a@b.c", "name": "A",
                "tenant_id": "t", "role": "user", "provider_config": None,
                "created_at": _NOW, "updated_at": _NOW,
            }
        raise AssertionError(f"unerwartetes fetchrow: {sql!r}")

    async def execute(self, sql: str, *args: Any) -> str:
        if "INSERT INTO tenants" in sql:
            self.log.append("INSERT tenants")
            return "INSERT 0 1"
        raise AssertionError(f"unerwartetes execute: {sql!r}")


def _recording_pool(conn: _RecordingConn):
    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool


class TestCreateUserLeavesNoOrphan:

    def test_duplicate_email_rolls_the_fresh_tenant_back(self, client: TestClient):
        """
        Der 409-Fall legte bis 02.09.2026 erst "Auto-tenant for <mail>" an und
        scheiterte dann am UNIQUE auf users.email — Mandant blieb, Nutzer nie
        entstanden. Ohne jede Kontolöschung.
        """
        conn = _RecordingConn(
            user_insert_raises=asyncpg.UniqueViolationError("users_email_key")
        )

        with patch("src.db.admin_routes.get_pool", return_value=_recording_pool(conn)):
            resp = client.post(
                "/v1/users",
                headers=_SERVICE_HEADER,
                json={"email": "schon@da.at", "name": "Zweiter"},
            )

        assert resp.status_code == 409
        # Der Mandanten-Insert liegt innerhalb der Transaktion, die zurückrollt.
        assert conn.log.index("BEGIN") < conn.log.index("INSERT tenants")
        assert conn.log[-1] == "ROLLBACK"

    def test_success_commits_tenant_and_user_together(self, client: TestClient):
        conn = _RecordingConn()

        with patch("src.db.admin_routes.get_pool", return_value=_recording_pool(conn)):
            resp = client.post(
                "/v1/users",
                headers=_SERVICE_HEADER,
                json={"email": "neu@da.at", "name": "Erster"},
            )

        assert resp.status_code == 201
        assert conn.log == ["BEGIN", "SELECT tenants", "INSERT tenants",
                            "INSERT users", "COMMIT"]
