"""
Tests für pending_orders — Rechnungs-Lane (Variante A: manuelle Freigabe).

Coverage:
- create_pending_order: happy path — Invoice generiert, Order Row + Email
- release_order: subscription-plan (report-standard) → subscriptions Row entsteht
- release_order: project-plan (energy-project) → 501 NotImplementedError
- release_order: bereits released → ValueError (→ 409 in Route)
- list_user_pending_orders: filtert nach user_id
- GET /v1/admin/orders/pending: nicht-Operator bekommt 403
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import os
os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.config import config
from src.billing.pending_orders_service import (
    create_pending_order,
    list_user_pending_orders,
    release_order,
)
from src.billing.routes import _admin_orders_router, _pending_router


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def app() -> FastAPI:
    _app = FastAPI()
    _app.include_router(_admin_orders_router)
    _app.include_router(_pending_router)
    return _app


@pytest.fixture(scope="module")
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


_SERVICE_TOKEN_HEADER = {"X-Bridge-Service-Token": config.service_token}
_USER_JWT_HEADER = {"Authorization": "Bearer invalid-user-jwt"}  # rejected → 401


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

def _mock_pool(*fetchrow_results, fetchval_result=None, fetch_result=None):
    """Minimal asyncpg pool mock: fetchrow_results consumed in order."""
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(side_effect=list(fetchrow_results))
    conn.fetchval = AsyncMock(return_value=fetchval_result)
    conn.fetch = AsyncMock(return_value=fetch_result or [])

    @asynccontextmanager
    async def _acquire():
        yield conn

    @asynccontextmanager
    async def _transaction():
        yield

    conn.transaction = _transaction
    pool = MagicMock()
    pool.acquire = _acquire
    return pool, conn


def _order_row(**overrides):
    """Minimal pending_orders row mock."""
    order_id = uuid.uuid4()
    user_id = uuid.uuid4()
    invoice_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    defaults = {
        "id": order_id,
        "user_id": user_id,
        "tenant_id": "default",
        "plan_id": "report-standard",
        "quantity": 1,
        "total_price_eur": Decimal("250.00"),
        "status": "awaiting_payment",
        "invoice_id": invoice_id,
        "created_at": now,
        "released_at": None,
        "released_by": None,
        "release_note": None,
    }
    defaults.update(overrides)
    row = MagicMock()
    row.__getitem__ = lambda self, k: defaults[k]
    row.keys = lambda: defaults.keys()
    return row


def _sub_row(**overrides):
    """Minimal subscriptions row mock for release tests."""
    uid = uuid.uuid4()
    defaults = {
        "id": uuid.uuid4(),
        "user_id": uid,
        "app_id": "werking-report",
        "plan_id": "report-standard",
        "status": "active",
        "mollie_customer_id": "manual-billing",
        "mollie_subscription_id": None,
        "seats": 1,
        "started_at": datetime.now(timezone.utc),
        "cancelled_at": None,
        "suspended_at": None,
        "expired_at": None,
    }
    defaults.update(overrides)
    row = MagicMock()
    row.__getitem__ = lambda self, k: defaults[k]
    row.keys = lambda: defaults.keys()
    return row


# ---------------------------------------------------------------------------
# create_pending_order — service unit tests
# ---------------------------------------------------------------------------

class TestCreatePendingOrder:
    async def test_create_pending_order_generates_invoice(self):
        """
        Happy path: Invoice wird erzeugt, Order-Row geschrieben, Email-Versand gecallt.
        """
        user_id = str(uuid.uuid4())
        invoice_id = str(uuid.uuid4())
        order = _order_row(plan_id="report-standard")

        pool, conn = _mock_pool(order)  # fetchrow → INSERT pending_orders RETURNING

        with (
            patch(
                "src.billing.pending_orders_service.resolve_tenant_for_user",
                new_callable=AsyncMock, return_value="default",
            ),
            patch(
                "src.billing.pending_orders_service._create_order_invoice",
                new_callable=AsyncMock, return_value=invoice_id,
            ),
            patch(
                "src.billing.pending_orders_service._send_order_email",
                new_callable=AsyncMock,
            ) as mock_send,
            patch(
                "src.billing.pending_orders_service.log_billing_event",
                new_callable=AsyncMock,
            ) as mock_log,
            patch("src.billing.pending_orders_service.get_pool", return_value=pool),
        ):
            result = await create_pending_order(user_id, "report-standard", 1)

        assert result["planId"] == "report-standard"
        assert result["status"] == "awaiting_payment"
        mock_send.assert_called_once_with(invoice_id=invoice_id, user_id=user_id)
        mock_log.assert_called_once()
        assert mock_log.call_args[0][0] == "order.created"

    async def test_trial_plan_raises_value_error(self):
        """Trial-Plan kann nicht als Pending-Order bestellt werden."""
        with pytest.raises(ValueError, match="trial"):
            await create_pending_order(str(uuid.uuid4()), "trial", 1)

    async def test_unknown_plan_raises_value_error(self):
        """Unbekannter Plan → ValueError."""
        with pytest.raises(ValueError, match="Unknown plan"):
            await create_pending_order(str(uuid.uuid4()), "no-such-plan", 1)


# ---------------------------------------------------------------------------
# release_order — service unit tests
# ---------------------------------------------------------------------------

class TestReleaseOrder:
    async def test_release_subscription_plan(self):
        """
        Freigabe eines report-standard (interval='month') Orders:
        subscriptions-Row wird eingefügt, Budget provisioniert, Invoice auf 'paid' gesetzt.
        """
        order_id = str(uuid.uuid4())
        operator_id = str(uuid.uuid4())
        order = _order_row(
            id=uuid.UUID(order_id),
            plan_id="report-standard",
            status="awaiting_payment",
        )
        new_sub = _sub_row(plan_id="report-standard")
        released = _order_row(
            id=uuid.UUID(order_id),
            plan_id="report-standard",
            status="released",
            released_by=uuid.UUID(operator_id),
        )

        # fetchrow sequence: ORDER (FOR UPDATE), SUB INSERT RETURNING, RELEASED ORDER
        pool, conn = _mock_pool(order, new_sub, released)

        with (
            patch(
                "src.billing.pending_orders_service._provision_plan_budget",
                new_callable=AsyncMock,
            ) as mock_budget,
            patch(
                "src.billing.pending_orders_service.log_billing_event",
                new_callable=AsyncMock,
            ) as mock_log,
            patch("src.billing.pending_orders_service.get_pool", return_value=pool),
        ):
            result = await release_order(order_id, operator_id, note="Geldeingang OK")

        assert result["status"] == "released"
        mock_budget.assert_called_once()
        mock_log.assert_called_once()
        assert mock_log.call_args[0][0] == "order.released"

    async def test_release_project_plan_inserts_credits(self):
        """
        energy-project (interval='project') → INSERT in manual_project_credits.
        Kein NotImplementedError mehr — Variante 2 implementiert (2026-05-26).
        """
        order_id = str(uuid.uuid4())
        operator_id = str(uuid.uuid4())
        order = _order_row(
            id=uuid.UUID(order_id),
            plan_id="energy-project",
            status="awaiting_payment",
        )
        released = _order_row(
            id=uuid.UUID(order_id),
            plan_id="energy-project",
            status="released",
            released_by=uuid.UUID(operator_id),
        )

        pool, conn = _mock_pool(order, released)

        with (
            patch("src.billing.pending_orders_service.get_pool", return_value=pool),
            patch(
                "src.billing.pending_orders_service.log_billing_event",
                new_callable=AsyncMock,
            ) as mock_log,
        ):
            result = await release_order(order_id, operator_id)

        assert result["status"] == "released"
        # INSERT into manual_project_credits must have been called
        execute_calls = [str(c) for c in conn.execute.call_args_list]
        insert_calls = [c for c in execute_calls if "manual_project_credits" in c]
        assert len(insert_calls) == 1, "Expected exactly one INSERT into manual_project_credits"
        mock_log.assert_called_once()
        assert mock_log.call_args[0][0] == "order.released"

    async def test_release_already_released_returns_value_error(self):
        """
        Order mit status='released' → ValueError.
        Route gibt 409 zurück.
        """
        order_id = str(uuid.uuid4())
        order = _order_row(
            id=uuid.UUID(order_id),
            plan_id="report-standard",
            status="released",
        )

        pool, conn = _mock_pool(order)

        with patch("src.billing.pending_orders_service.get_pool", return_value=pool):
            with pytest.raises(ValueError, match="released"):
                await release_order(order_id, str(uuid.uuid4()))

    async def test_release_not_found_raises_lookup_error(self):
        """Unbekannte order_id → LookupError."""
        pool, conn = _mock_pool(None)  # fetchrow returns None = not found

        with patch("src.billing.pending_orders_service.get_pool", return_value=pool):
            with pytest.raises(LookupError):
                await release_order(str(uuid.uuid4()), str(uuid.uuid4()))


# ---------------------------------------------------------------------------
# list_user_pending_orders — filtert nach user_id
# ---------------------------------------------------------------------------

class TestListUserPendingOrders:
    async def test_list_user_pending_orders_filters_by_user(self):
        """Gibt nur Orders des angegebenen Users zurück (WHERE user_id = $1)."""
        user_id = str(uuid.uuid4())
        order_a = _order_row(user_id=uuid.UUID(user_id), plan_id="report-standard")
        order_b = _order_row(user_id=uuid.UUID(user_id), plan_id="report-standard")

        pool, conn = _mock_pool()
        conn.fetch = AsyncMock(return_value=[order_a, order_b])

        with patch("src.billing.pending_orders_service.get_pool", return_value=pool):
            results = await list_user_pending_orders(user_id)

        assert len(results) == 2
        # verify the correct user_id was passed as query param
        fetch_sql = conn.fetch.call_args[0][0]
        assert "user_id" in fetch_sql
        fetch_arg = conn.fetch.call_args[0][1]
        assert str(fetch_arg) == user_id


# ---------------------------------------------------------------------------
# Route-level auth: GET /v1/admin/orders/pending requires operator
# ---------------------------------------------------------------------------

class TestAdminOrdersAuth:
    def test_list_all_pending_orders_requires_operator(self, client: TestClient):
        """
        Non-Operator (kein Service-Token, kein Admin-JWT) → 401/403.
        Service-Token ohne X-User-ID = Operator → 200 (mit gemocktem Service).
        """
        # Kein Auth → 401
        resp = client.get("/v1/admin/orders/pending")
        assert resp.status_code == 401

        # User-JWT (auch wenn valid) → 403 wenn kein is_admin
        resp = client.get(
            "/v1/admin/orders/pending",
            headers={"Authorization": "Bearer some-user-token"},
        )
        assert resp.status_code in (401, 403)

    def test_operator_service_token_allowed(self, client: TestClient):
        """Service-Token ohne X-User-ID ist Operator → Liste zurückgeben."""
        with patch(
            "src.billing.pending_orders_service.list_all_pending_orders",
            new_callable=AsyncMock,
            return_value=[],
        ):
            resp = client.get(
                "/v1/admin/orders/pending",
                headers=_SERVICE_TOKEN_HEADER,
            )
        assert resp.status_code == 200
        assert resp.json()["items"] == []

    def test_service_token_with_x_user_id_is_not_operator(self, client: TestClient):
        """Service-Token MIT X-User-ID = customer-proxy, KEIN Operator → 403."""
        resp = client.get(
            "/v1/admin/orders/pending",
            headers={
                **_SERVICE_TOKEN_HEADER,
                "X-User-ID": str(uuid.uuid4()),
            },
        )
        assert resp.status_code == 403
