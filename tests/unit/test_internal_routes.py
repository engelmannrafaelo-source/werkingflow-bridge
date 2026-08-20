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


# ══ Schritt 2b: budget-gate chain ═══════════════════════════════════════════
#
# The load-bearing property under test is the ERROR MAPPING. Both fail-loud
# safeguards in this chain must come back as 4xx. If either leaked as a 5xx,
# platform_client would raise PlatformUnavailable, the worker's gate would hit
# its `except Exception -> letting call through`, and a "this would mis-bill"
# alarm would become a silent pass-through.


# ── 2b/C1: POST /v1/internal/users/lookup-by-email ─────────────────────────

def test_email_lookup_found_returns_id(client):
    import uuid
    uid = uuid.uuid4()
    with patch("src.identity.user_resolver.lookup_user_id_by_email",
               new=AsyncMock(return_value=uid)):
        resp = client.post("/v1/internal/users/lookup-by-email",
                           json={"email": "a@b.tld"}, headers=_headers())
    assert resp.status_code == 200
    assert resp.json() == {"id": str(uid)}


def test_email_lookup_unknown_is_404_and_leaks_no_pii(client):
    with patch("src.identity.user_resolver.lookup_user_id_by_email",
               new=AsyncMock(return_value=None)):
        resp = client.post("/v1/internal/users/lookup-by-email",
                           json={"email": "nobody@example.tld"}, headers=_headers())
    assert resp.status_code == 404
    assert "nobody@example.tld" not in resp.text


def test_email_lookup_requires_service_token(client):
    resp = client.post("/v1/internal/users/lookup-by-email", json={"email": "a@b.tld"})
    assert resp.status_code == 401


# ── 2b/C2: GET /v1/internal/project-budgets/allocated-plan-id ──────────────

def test_allocated_plan_id_none_is_200_with_null_not_404(client):
    """"Nothing allocated" is the NORMAL case (every ordinary report call), so
    it must not look like an error to the caller."""
    import uuid
    with patch("src.billing.project_budgets_service.find_allocated_plan_id",
               new=AsyncMock(return_value=None)):
        resp = client.get("/v1/internal/project-budgets/allocated-plan-id",
                          params={"user_id": str(uuid.uuid4()), "project_id": "p1"},
                          headers=_headers())
    assert resp.status_code == 200
    assert resp.json() == {"planId": None}


def test_ambiguous_project_budget_is_409_not_5xx(client):
    """THE regression guard: a 5xx here would be swallowed by the gate's
    fail-open catch-all and mis-bill silently."""
    import uuid
    from src.billing.project_budgets_service import AmbiguousProjectBudget

    with patch("src.billing.project_budgets_service.find_allocated_plan_id",
               new=AsyncMock(side_effect=AmbiguousProjectBudget("two plans"))):
        resp = client.get("/v1/internal/project-budgets/allocated-plan-id",
                          params={"user_id": str(uuid.uuid4()), "project_id": "p1"},
                          headers=_headers())
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "ambiguous_project_budget"


# ── 2b/C4: POST /v1/internal/budget/user-budget-state ──────────────────────

def test_user_budget_state_returns_data_not_a_verdict(client):
    """The monthly leaf must hand back raw state; the verdict stays in the
    worker (calculator.py pure functions)."""
    import uuid
    state = {"userId": "u", "monthlyBudgets": {"engelmann": {"limitEur": 10.0}},
             "topUpLots": []}
    with patch("src.budget.routes.load_user_budget_state",
               new=AsyncMock(return_value=state)):
        resp = client.post("/v1/internal/budget/user-budget-state",
                           json={"user_id": str(uuid.uuid4())}, headers=_headers())
    assert resp.status_code == 200
    assert resp.json() == state
    assert "allowed" not in resp.json()


def test_legacy_topup_balance_is_409_not_5xx(client):
    import uuid
    from src.budget.topup_store import LegacyTopUpBalanceError

    with patch("src.budget.routes.load_user_budget_state",
               new=AsyncMock(side_effect=LegacyTopUpBalanceError(uuid.uuid4(), 4.20))):
        resp = client.post("/v1/internal/budget/user-budget-state",
                           json={"user_id": str(uuid.uuid4())}, headers=_headers())
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "legacy_topup_balance"


# ── 2b/D2: POST /v1/internal/budget/ensure-trial ───────────────────────────

def test_ensure_trial_returns_outcome_and_refreshed_state(client):
    import uuid
    payload = {"provisioned": True, "trialPlanId": "engelmann-trial",
               "state": {"userId": "u", "monthlyBudgets": {}, "topUpLots": []}}
    with patch("src.budget.routes.ensure_trial_provisioned",
               new=AsyncMock(return_value=payload)):
        resp = client.post("/v1/internal/budget/ensure-trial",
                           json={"user_id": str(uuid.uuid4()), "plan_id": "engelmann"},
                           headers=_headers())
    assert resp.status_code == 200
    assert resp.json() == payload


def test_ensure_trial_without_trial_sibling_is_a_normal_answer(client):
    """No trial plan for this app is not an error — the gate then treats the
    user as unlicensed, which is a verdict, not an outage."""
    import uuid
    payload = {"provisioned": False, "trialPlanId": None, "state": None}
    with patch("src.budget.routes.ensure_trial_provisioned",
               new=AsyncMock(return_value=payload)):
        resp = client.post("/v1/internal/budget/ensure-trial",
                           json={"user_id": str(uuid.uuid4()), "plan_id": "no-trial-app"},
                           headers=_headers())
    assert resp.status_code == 200
    assert resp.json()["provisioned"] is False
