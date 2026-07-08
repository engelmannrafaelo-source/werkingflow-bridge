"""
Tests for POST /v1/activity/verify-display — cost-display verification.

The endpoint is the accounting-consistency gate: an app reports the per-call
EUR costs it rendered, the Bridge compares against activities.payload.costEur.

Coverage:
- exact match → 200 ok with ledger total
- per-call cost deviation → 409 + 'cost-display-mismatch' incident INSERT
- unknown event id → 409 (unknown-event)
- non-operator probing a foreign row → 409 (not-own-activity), no ledger echo
- empty display → 200 checked=0 (trivially consistent)
- unknown appId → 400
"""
from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import os
os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.activity.routes as routes
from src.config import config as _config


USER_ID = str(uuid.uuid4())
OTHER_USER_ID = str(uuid.uuid4())
EVENT_A = str(uuid.uuid4())
EVENT_B = str(uuid.uuid4())

# The shell env may carry a real BRIDGE_SERVICE_TOKEN (dev-server bootstrap) —
# read whatever the config actually resolved so auth passes in both envs.
SERVICE_HEADERS = {
    "X-Bridge-Service-Token": _config.service_token,
    "X-User-ID": USER_ID,  # scoped proxy — non-operator semantics
}


@pytest.fixture(scope="module")
def client() -> TestClient:
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


def _row(event_id: str, actor: str, cost: float, app_id: str = "werking-energy"):
    return {
        "id": uuid.UUID(event_id),
        "actor_user_id": uuid.UUID(actor),
        "app_id": app_id,
        "payload": {"costEur": cost, "feature": "x"},
    }


def _mock_pool(fetch_rows):
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=fetch_rows)
    conn.execute = AsyncMock(return_value=None)

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool, conn


def _post(client, pool, body, tenant="tenant-x"):
    with (
        patch.object(routes, "get_pool", return_value=pool),
        patch.object(routes, "resolve_tenant_id", AsyncMock(return_value=tenant)),
    ):
        return client.post(
            "/v1/activity/verify-display", json=body, headers=SERVICE_HEADERS
        )


def test_matching_display_passes(client):
    pool, conn = _mock_pool([
        _row(EVENT_A, USER_ID, 0.5619),
        _row(EVENT_B, USER_ID, 0.012382),
    ])
    res = _post(client, pool, {
        "appId": "werking-energy",
        "calls": [
            {"id": EVENT_A, "costEur": 0.5619},
            {"id": EVENT_B, "costEur": 0.012382},
        ],
        "totalCostEur": 0.574282,
    })
    assert res.status_code == 200, res.text
    data = res.json()
    assert data["status"] == "ok"
    assert data["checked"] == 2
    assert abs(data["ledgerTotalEur"] - 0.574282) < 1e-9
    conn.execute.assert_not_called()  # no incident row


def test_cost_deviation_persists_incident_and_409(client):
    pool, conn = _mock_pool([_row(EVENT_A, USER_ID, 0.5619)])
    res = _post(client, pool, {
        "appId": "werking-energy",
        "calls": [{"id": EVENT_A, "costEur": 0.99}],  # displayed wrong value
        "totalCostEur": 0.99,
    })
    assert res.status_code == 409, res.text
    detail = res.json()["detail"]
    assert detail["error"] == "cost-display-mismatch"
    assert detail["mismatches"][0]["reason"] == "cost-differs"
    assert detail["mismatches"][0]["ledgerCostEur"] == 0.5619

    # Incident row persisted
    conn.execute.assert_called_once()
    args = conn.execute.call_args.args
    assert "cost-display-mismatch" in args[0]
    incident_payload = json.loads(args[4])
    assert incident_payload["mismatchCount"] == 1


def test_unknown_event_id_is_mismatch(client):
    pool, _conn = _mock_pool([])  # ledger knows nothing
    res = _post(client, pool, {
        "appId": "werking-energy",
        "calls": [{"id": EVENT_A, "costEur": 0.1}],
        "totalCostEur": 0.1,
    })
    assert res.status_code == 409
    assert res.json()["detail"]["mismatches"][0]["reason"] == "unknown-event"


def test_foreign_row_not_own_activity_and_no_ledger_echo(client):
    pool, _conn = _mock_pool([_row(EVENT_A, OTHER_USER_ID, 0.5)])
    res = _post(client, pool, {
        "appId": "werking-energy",
        "calls": [{"id": EVENT_A, "costEur": 0.5}],
        "totalCostEur": 0.5,
    })
    assert res.status_code == 409
    m = res.json()["detail"]["mismatches"][0]
    assert m["reason"] == "not-own-activity"
    assert "ledgerCostEur" not in m  # foreign values are never echoed


def test_empty_display_trivially_consistent(client):
    pool, conn = _mock_pool([])
    res = _post(client, pool, {
        "appId": "werking-energy",
        "calls": [],
        "totalCostEur": 0.0,
    })
    assert res.status_code == 200
    assert res.json()["checked"] == 0
    conn.fetch.assert_not_called()


def test_unknown_app_id_rejected(client):
    pool, _conn = _mock_pool([])
    res = _post(client, pool, {
        "appId": "not-an-app",
        "calls": [{"id": EVENT_A, "costEur": 0.1}],
        "totalCostEur": 0.1,
    })
    assert res.status_code == 400
