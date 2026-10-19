"""R14 (g) → R15: the worker inventory and its completeness live in
tests/unit/test_r15_worker_route_inventory.py (derived from the real router, no
prefix filter). Kept here: the /health/reset admin binding introduced in R14.
"""

import sys
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


def test_health_reset_requires_the_isolated_admin_principal():
    route = {r.path: r for r in app.routes if isinstance(r, APIRoute)}["/health/reset"]
    assert "require_admin" in _dependency_names(route)
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
