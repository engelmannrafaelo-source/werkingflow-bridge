"""Tests for src/internal_routes.py — the platform-api side of ADR-0009
Schritt 2a (C2 principals, C6 prepaid-vision, C4 audit-events).

Auth is exercised for real (require_service_token, not overridden) so a
missing/wrong X-Bridge-Service-Token is verified to 401 exactly like the
pre-existing /v1/budget/check. Everything below the auth layer is mocked —
this file is about the route contract, not the DB.
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.internal_routes import router as internal_router

SERVICE_TOKEN = os.environ["BRIDGE_SERVICE_TOKEN"]


@pytest.fixture()
def client() -> TestClient:
    app = FastAPI()
    app.include_router(internal_router)
    return TestClient(app)


def _headers(token: str | None = SERVICE_TOKEN) -> dict:
    return {"X-Bridge-Service-Token": token} if token else {}


# ── auth ─────────────────────────────────────────────────────────────────

def test_missing_service_token_is_401(client):
    resp = client.get("/v1/internal/principals/somehash")
    assert resp.status_code == 401


def test_wrong_service_token_is_401(client):
    resp = client.get("/v1/internal/principals/somehash", headers=_headers("wrong-token"))
    assert resp.status_code == 401


# ── C2: GET /v1/internal/principals/{token_hash} ───────────────────────────

def test_principal_found_returns_200_with_row(client):
    row = {
        "id": "11111111-1111-1111-1111-111111111111",
        "name": "engelmann",
        "allowed_apps": ["engelmann"],
        "allowed_paths": ["*"],
        "monthly_cap_eur": None,
    }
    with patch("src.principals.get_principal_row_by_hash", AsyncMock(return_value=row)):
        resp = client.get("/v1/internal/principals/somehash", headers=_headers())
    assert resp.status_code == 200
    assert resp.json() == row


def test_principal_not_found_returns_404(client):
    with patch("src.principals.get_principal_row_by_hash", AsyncMock(return_value=None)):
        resp = client.get("/v1/internal/principals/unknownhash", headers=_headers())
    assert resp.status_code == 404


# ── C6: GET /v1/internal/prepaid-vision/spent-24h ──────────────────────────

def test_prepaid_vision_spend_returns_200_with_amount(client):
    with patch("src.routing.prepaid_cap.query_spent_last_24h_from_db", AsyncMock(return_value=12.5)):
        resp = client.get("/v1/internal/prepaid-vision/spent-24h", headers=_headers())
    assert resp.status_code == 200
    assert resp.json() == {"spent_eur": 12.5}


# ── C4: POST /v1/internal/audit-events ──────────────────────────────────────

def test_audit_event_is_inserted_and_returns_204(client):
    mock_insert = AsyncMock(return_value=None)
    with patch("src.internal_routes.insert_audit_event", mock_insert):
        resp = client.post(
            "/v1/internal/audit-events",
            headers=_headers(),
            json={
                "action": "pii.pseudonymized",
                "actor_user_id": "11111111-1111-1111-1111-111111111111",
                "actor_label": "engelmann",
                "target_kind": "anonymization",
                "target_id": "wf-1",
                "metadata": {"total_entities": 4},
            },
        )
    assert resp.status_code == 204
    mock_insert.assert_awaited_once_with(
        action="pii.pseudonymized",
        actor_user_id="11111111-1111-1111-1111-111111111111",
        actor_label="engelmann",
        target_kind="anonymization",
        target_id="wf-1",
        metadata={"total_entities": 4},
    )


def test_audit_event_missing_action_is_422(client):
    resp = client.post("/v1/internal/audit-events", headers=_headers(), json={})
    assert resp.status_code == 422
