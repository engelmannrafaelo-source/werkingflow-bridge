"""R13: real routers/auth, synthetic DB rows, no network/provider access."""

import ast
import importlib
import json
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from src import principals, principals_routes
from src.api_auth import deps

ROOT = Path(__file__).parents[2]


def all_platform_routers():
    # Discover the real platform mount inventory rather than a hand-picked subset.
    tree = ast.parse((ROOT / "src/platform_main.py").read_text())
    imports = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                imports[alias.asname or alias.name] = (node.module, alias.name)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "include_router"
        ):
            module, name = imports[node.args[0].id]
            yield getattr(importlib.import_module(module), name)


def dependencies(dep):
    return {dep.call} | set().union(
        *(dependencies(child) for child in dep.dependencies)
    )


@pytest.fixture
def scope_client(monkeypatch):
    monkeypatch.setattr(
        deps,
        "config",
        SimpleNamespace(service_token="synthetic-internal", db_enabled=True),
    )
    rows = {}
    for app in ("werking-energy", "werking-report", "werking-noise"):
        rows[principals.hash_token(app)] = dict(
            id=app,
            name=app,
            allowed_apps=[app],
            allowed_paths=["*"],
            scopes=[f"app:{app}"],
            monthly_cap_eur=None,
        )
    rows[principals.hash_token("synthetic-admin")] = dict(
        id="admin",
        name="admin",
        allowed_apps=[],
        allowed_paths=["*"],
        scopes=["bridge:admin"],
        monthly_cap_eur=None,
    )
    lookup = AsyncMock(side_effect=lambda digest: rows.get(digest))
    monkeypatch.setattr(principals, "get_principal_row_by_hash", lookup)
    app = FastAPI()
    for router in all_platform_routers():
        assert isinstance(router, APIRouter)
        app.include_router(router)
    return TestClient(app)


def protected_routes(app):
    # Independent reviewed inventory: removing a dependency must not remove the
    # route from the test. Also discover new protected routes automatically.
    inventory = json.loads((ROOT / "docs/service-route-inventory.json").read_text())
    required = {(method, row["path"]) for row in inventory for method in row["methods"]}
    seen = set()
    for router in all_platform_routers():
        for route in router.routes:
            if not hasattr(route, "dependant"):
                continue
            gates = dependencies(route.dependant)
            keys = {(method, route.path) for method in route.methods}
            if (
                keys & required
                or route.path.startswith("/v1/admin/")
                or gates
                & {
                    deps.require_admin,
                    deps.require_internal_service,
                    deps.require_credential_admin_or_user,
                }
            ):
                seen |= keys
                yield route
    assert required <= seen, required - seen


@pytest.mark.parametrize("token", ["werking-energy", "werking-report", "werking-noise"])
@pytest.mark.parametrize("proxied", [False, True])
def test_every_admin_and_sensitive_route_rejects_app(scope_client, token, proxied):
    checked = []
    headers = {"X-Bridge-Service-Token": token, "X-App-ID": token}
    if proxied:
        headers["X-User-ID"] = "synthetic-user"
    for route in protected_routes(scope_client.app):
        path = re.sub(r"\{[^}]+\}", "synthetic", route.path)
        for method in route.methods:
            response = scope_client.request(method, path, headers=headers, json={})
            assert response.status_code == 403, (
                method,
                path,
                response.status_code,
                response.text,
            )
            checked.append((method, route.path))
    assert len(checked) > 50


def test_admin_namespace_has_explicit_admin_dependency(scope_client):
    routes = [
        r
        for router in all_platform_routers()
        for r in router.routes
        if getattr(r, "path", "").startswith("/v1/admin/")
    ]
    assert len(routes) >= 9
    actual = {
        p for p in scope_client.app.openapi()["paths"] if p.startswith("/v1/admin/")
    }
    assert actual == {r.path for r in routes}
    for route in routes:
        assert deps.require_admin in dependencies(route.dependant), route.path


def test_scope_introspection_and_legacy_is_not_admin(scope_client):
    for token, scopes in [
        ("werking-energy", ["app:werking-energy"]),
        ("synthetic-admin", ["bridge:admin"]),
        ("synthetic-internal", ["bridge:internal"]),
    ]:
        headers = {"X-Bridge-Service-Token": token}
        r = scope_client.get("/v1/auth/service-scope", headers=headers)
        assert r.status_code == 200
        assert r.json()["scopes"] == scopes
    assert (
        scope_client.get(
            "/v1/admin/service-principals",
            headers={"X-Bridge-Service-Token": "synthetic-internal"},
        ).status_code
        == 403
    )


def test_admin_positive_control_and_app_scope_assignment(scope_client, monkeypatch):
    monkeypatch.setattr(principals_routes, "is_db_enabled", lambda: True)

    async def create(**kwargs):
        return principals.Principal(
            id="new", **{k: v for k, v in kwargs.items() if k != "token"}
        )

    mock = AsyncMock(side_effect=create)
    monkeypatch.setattr(principals, "create_principal", mock)
    headers = {"X-Bridge-Service-Token": "synthetic-admin"}
    r = scope_client.post(
        "/v1/admin/service-principals",
        headers=headers,
        json={"name": "report", "allowed_apps": ["werking-report"]},
    )
    assert r.status_code == 201, r.text
    assert r.json()["scopes"] == ["app:werking-report"]
    for body in [
        dict(name="mixed", allowed_apps=["werking-energy"], scopes=["bridge:admin"]),
        dict(name="wildcard", allowed_apps=["*"]),
        dict(name="multi", allowed_apps=["werking-energy", "werking-report"]),
    ]:
        assert (
            scope_client.post(
                "/v1/admin/service-principals", headers=headers, json=body
            ).status_code
            == 422
        )
    assert mock.await_count == 1


def test_r12_principal_creation_blocked_and_mutant_detected(scope_client, monkeypatch):
    """Original exploit stops before minting; removing the gate must reopen it."""
    monkeypatch.setattr(principals_routes, "is_db_enabled", lambda: True)
    create = AsyncMock(
        return_value=principals.Principal(
            id="minted",
            name="report",
            allowed_apps=["werking-report"],
            scopes=["app:werking-report"],
        )
    )
    monkeypatch.setattr(principals, "create_principal", create)
    body = {"name": "report", "allowed_apps": ["werking-report"]}
    headers = {"X-Bridge-Service-Token": "werking-energy"}
    r = scope_client.post("/v1/admin/service-principals", headers=headers, json=body)
    assert r.status_code == 403
    create.assert_not_awaited()
    scope_client.app.dependency_overrides[deps.require_admin] = (
        deps.require_jwt_or_service
    )
    r = scope_client.post("/v1/admin/service-principals", headers=headers, json=body)
    assert r.status_code == 201
    assert r.json()["token"]
    create.assert_awaited_once()


def test_user_admin_claim_cannot_grant_service_administration():
    claims = deps.AuthClaims(
        kind="user", user_id="u", email=None, tenant_id=None, is_admin=True
    )
    assert not claims.is_operator
    with pytest.raises(Exception) as exc:
        deps.require_admin(claims)
    assert exc.value.status_code == 403


def test_r12_png_chain_with_real_create_resolve_and_egress(monkeypatch):
    from fastapi import Depends, Request

    from src import auth
    from src.middleware.bild_policy_middleware import ImagePolicyMiddleware
    from src.routing import bild_policy as bp

    rows = {
        principals.hash_token("energy-app"): dict(
            id="energy",
            name="energy",
            allowed_apps=["werking-energy"],
            allowed_paths=["*"],
            monthly_cap_eur=None,
            scopes=["app:werking-energy"],
        )
    }

    class Conn:
        async def fetchrow(self, sql, *args):
            if sql.lstrip().startswith("SELECT"):
                return rows.get(args[0])
            name, digest, prefix, apps, paths, cap, scopes = args
            row = dict(
                id="minted-report",
                name=name,
                allowed_apps=apps,
                allowed_paths=paths,
                monthly_cap_eur=cap,
                scopes=scopes,
            )
            rows[digest] = row
            return row

    class Acquire:
        async def __aenter__(self):
            return Conn()

        async def __aexit__(self, *args):
            pass

    async def platform(method, path):
        return SimpleNamespace(
            status_code=200, json={"principal": rows.get(path.rsplit("/", 1)[-1])}
        )

    monkeypatch.setenv("BRIDGE_PRINCIPALS_ENABLED", "true")
    monkeypatch.setattr(
        deps,
        "config",
        SimpleNamespace(service_token="synthetic-internal", db_enabled=True),
    )
    monkeypatch.setattr(principals_routes, "is_db_enabled", lambda: True)
    monkeypatch.setattr(
        principals, "get_pool", lambda: SimpleNamespace(acquire=Acquire)
    )
    monkeypatch.setattr(principals, "call_platform", platform)
    monkeypatch.setattr(
        auth.auth_manager, "get_api_key", lambda: "synthetic-shared-api"
    )
    monkeypatch.setattr(auth.auth_manager, "env_api_key_previous", None)
    principals.invalidate_cache()
    app = FastAPI()
    app.include_router(principals_routes.router)
    app.add_middleware(ImagePolicyMiddleware)
    sink = []

    @app.post("/v1/chat/completions")
    async def completion(request: Request, credentials=Depends(auth.security)):
        await auth.verify_api_key(request, credentials)
        payload = await request.json()
        bp.enforce_image_egress(
            app_id=bp.CURRENT_APP.get(),
            messages=payload["messages"],
            has_vision=True,
            backend_is_gemini=False,
            router_sends_to_gemini=False,
        )
        bp.guard_provider_payload("anthropic", payload)
        sink.append(bp.CURRENT_APP.get())
        return {"local_sink": True}

    client = TestClient(app)
    body = {"name": "report", "allowed_apps": ["werking-report"]}
    headers = {"X-Bridge-Service-Token": "energy-app"}
    assert (
        client.post(
            "/v1/admin/service-principals", headers=headers, json=body
        ).status_code
        == 403
    )
    assert len(rows) == 1 and not sink
    # Isolated rollback mutant: deleting the admin guard reopens the complete
    # original chain. Actual minting, lookup, ingress and egress remain in use.
    app.dependency_overrides[deps.require_admin] = deps.require_jwt_or_service
    made = client.post("/v1/admin/service-principals", headers=headers, json=body)
    assert made.status_code == 201
    payload = {
        "model": "sonnet",
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": (
                                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
                                "AAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
                            ),
                        },
                    }
                ],
            }
        ],
    }
    response = client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": "Bearer " + made.json()["token"],
            "X-App-ID": "werking-report",
        },
        json=payload,
    )
    assert response.status_code == 200, response.text
    assert sink == ["werking-report"]
    principals.invalidate_cache()


@pytest.mark.parametrize("where", ["header", "query", "body"])
def test_app_scope_cannot_be_relabelled(scope_client, where):
    headers = {"X-Bridge-Service-Token": "werking-energy"}
    params, body = {}, {}
    if where == "header":
        headers["X-App-ID"] = "werking-report"
    elif where == "query":
        params["app"] = "werking-report"
    else:
        body["appId"] = "werking-report"
    r = scope_client.request(
        "GET", "/v1/auth/service-scope", headers=headers, params=params, json=body
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_dbless_worker_resolves_explicit_app_grants(monkeypatch):
    monkeypatch.setattr(
        deps,
        "config",
        SimpleNamespace(service_token="synthetic-internal", db_enabled=False),
    )
    lookup = AsyncMock(
        return_value=principals.Principal(
            id="energy",
            name="energy",
            allowed_apps=["werking-energy"],
            scopes=["app:werking-energy"],
        )
    )
    monkeypatch.setattr(principals, "resolve_principal_by_token", lookup)
    claims = await deps._verify_service_token("synthetic-app")
    lookup.assert_awaited_once_with("synthetic-app")
    assert not claims.is_operator
    with pytest.raises(Exception) as exc:
        deps.require_admin(claims)
    assert exc.value.status_code == 403
