"""HTTP regression of the independent R11 lease exploit; no provider transport."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api_auth import broker, deps
from src.sandbox import routes


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("SANDBOX_BROKER_TOKEN", "synthetic-broker")
    config = SimpleNamespace(service_token="synthetic-energy-service", db_enabled=True)
    monkeypatch.setattr(broker, "config", config)
    monkeypatch.setattr(deps, "config", config)

    class Acquire:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr(routes, "get_pool", lambda: SimpleNamespace(acquire=Acquire))
    monkeypatch.setattr(routes, "resolve_user_id", AsyncMock(return_value=uuid4()))
    monkeypatch.setattr(routes, "enforce_budget", AsyncMock())
    monkeypatch.setattr(
        routes._ls,
        "get_tenant_info",
        AsyncMock(
            return_value={"billing_mode": "subscription", "tenant_id": str(uuid4())}
        ),
    )
    monkeypatch.setattr(
        routes._ls, "get_recent_lease_counts", AsyncMock(return_value={})
    )
    monkeypatch.setattr(
        routes._ar,
        "pick_account",
        AsyncMock(return_value=SimpleNamespace(account_id="synthetic", tier="test")),
    )
    monkeypatch.setattr(routes._ls, "read_oauth_token", lambda _: "synthetic-provider")
    monkeypatch.setattr(routes._ls, "create_lease", AsyncMock(return_value=uuid4()))
    app = FastAPI()
    app.include_router(routes.router)
    return TestClient(app)


def request(client, headers, app="werking-energy"):
    return client.post(
        "/v1/sandbox/lease-token",
        headers=headers,
        json={"userId": str(uuid4()), "app": app},
    )


@pytest.mark.parametrize(
    "app", ["werking-energy", "werking-report", "sandbox-broker", "*"]
)
def test_app_cannot_obtain_credential_even_by_claiming_broker(client, app):
    for headers in [
        {"X-Bridge-Service-Token": "synthetic-energy-service"},
        {"X-Sandbox-Broker-Token": "synthetic-energy-service"},
        {"X-Sandbox-Broker-Token": "wrong"},
    ]:
        response = request(client, headers, app)
        assert response.status_code == 403
        assert "synthetic-provider" not in response.text


def test_broker_only_scope_and_no_shared_secret(client, monkeypatch):
    assert (
        request(client, {"X-Sandbox-Broker-Token": "synthetic-broker"}).status_code
        == 200
    )
    monkeypatch.setenv("SANDBOX_BROKER_TOKEN", "synthetic-energy-service")
    assert (
        request(
            client, {"X-Sandbox-Broker-Token": "synthetic-energy-service"}
        ).status_code
        == 403
    )


def test_old_dependency_mutant_reproduces_provider_credential_leak(client):
    client.app.dependency_overrides[broker.require_credential_broker] = (
        deps.require_service_token
    )
    response = request(client, {"X-Bridge-Service-Token": "synthetic-energy-service"})
    assert response.status_code == 200
    assert response.json()["oauthToken"] == "synthetic-provider"


def test_response_model_inventory_requires_broker():
    # Recursively inventory all exported response models in this lease router.
    found = []
    for route in routes.router.routes:
        model = route.response_model
        schema = str(model.model_json_schema()) if model else ""
        if "oauthToken" in schema:
            found.append(route.path)
            assert broker.require_credential_broker in [
                dep.call for dep in route.dependant.dependencies
            ]
    assert found == ["/v1/sandbox/lease-token"]


@pytest.mark.asyncio
async def test_broker_is_not_a_general_app_service_token(client, monkeypatch):
    from src import principals

    monkeypatch.setattr(
        principals, "get_principal_row_by_hash", AsyncMock(return_value=None)
    )
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as rejected:
        await deps._verify_service_token("synthetic-broker")
    assert rejected.value.status_code == 401
    claims = broker.require_credential_broker("synthetic-broker")
    assert claims.principal == "sandbox-broker"
    assert claims.scope == "provider-credential:read"


def test_source_inventory_of_provider_credential_routes():
    import ast
    from pathlib import Path

    found = []
    for path in (Path(__file__).parents[2] / "src").rglob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            route = any(
                isinstance(d, ast.Call)
                and isinstance(d.func, ast.Attribute)
                and d.func.attr in {"get", "post", "put", "patch", "delete"}
                for d in node.decorator_list
            )
            if not route:
                continue
            body = ast.unparse(node)
            if "oauthToken" in body or "read_oauth_token(" in body:
                found.append(
                    (path.relative_to(Path(__file__).parents[2]).as_posix(), node.name)
                )
                assert "Depends(require_credential_broker)" in body
    assert found == [("src/sandbox/routes.py", "lease_token")]
