"""R14 (g): worker routes outside the platform inventory are named, assessed and gated.

Same import stubbing as test_no_unauthenticated_routes.py; no network, no DB.
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock as _MagicMock

for _mod_name in [
    "claude_code_sdk",
    "claude_code_sdk._errors",
    "claude_code_sdk._internal",
    "claude_code_sdk._internal.client",
    "src.identity.routes",
    "src.db.client",
]:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = _MagicMock()

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402

from src.api_auth.deps import AuthClaims, require_admin  # noqa: E402
from src.main import app  # noqa: E402

ROOT = Path(__file__).parents[2]
INVENTORY = json.loads((ROOT / "docs/service-route-inventory-worker.json").read_text())
PLATFORM = json.loads((ROOT / "docs/service-route-inventory.json").read_text())
APP_REACHABLE_GATES = {"require_service_token", "HTTPBearer", "require_admin"}


def _dependency_names(route: APIRoute) -> set:
    found = set()

    def walk(dep):
        if dep.call is not None:
            found.add(getattr(dep.call, "__name__", type(dep.call).__name__))
        for sub in dep.dependencies:
            walk(sub)

    for dep in route.dependant.dependencies:
        walk(dep)
    return found


def _routes():
    return {r.path: r for r in app.routes if isinstance(r, APIRoute)}


def test_every_inventoried_worker_route_exists_with_its_declared_gate():
    routes = _routes()
    for row in INVENTORY["routes"]:
        route = routes.get(row["path"])
        assert route is not None, row["path"]
        assert set(row["methods"]) <= route.methods, row["path"]
        assert row["gate"] in _dependency_names(route), (
            row["path"],
            _dependency_names(route),
        )


def test_health_reset_requires_the_isolated_admin_principal():
    route = _routes()["/health/reset"]
    names = _dependency_names(route)
    assert "require_admin" in names
    app_claims = AuthClaims(
        kind="service",
        user_id=None,
        email=None,
        tenant_id=None,
        is_admin=False,
        acting_user_id=None,
        scopes=("app:werking-energy",),
        allowed_apps=("werking-energy",),
    )
    with pytest.raises(HTTPException) as exc:
        require_admin(app_claims)
    assert exc.value.status_code == 403


def test_no_unassessed_app_reachable_worker_route():
    """A new service-token/bearer worker route must be assessed here
    or in the platform inventory."""
    platform_paths = {row["path"] for row in PLATFORM}
    inventoried = {row["path"] for row in INVENTORY["routes"]}
    unassessed = sorted(
        path
        for path, route in _routes().items()
        if (_dependency_names(route) & APP_REACHABLE_GATES)
        and path not in platform_paths
        and path not in inventoried
        and path.startswith(
            ("/health", "/debug", "/license", "/v1/debug", "/v1/metrics")
        )
    )
    assert unassessed == [], unassessed
