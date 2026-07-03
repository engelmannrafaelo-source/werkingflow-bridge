"""
Route-shadowing guard — the composed platform-api route table must be
shadow-free.

The defect class this catches (real, 2026-07-03):
    DELETE /v1/users/{user_id} was registered TWICE — hard-delete in
    db/admin_routes AND GDPR anonymize (close_account) in identity/self_service.
    FastAPI matches in registration order, admin_db_router is included before
    self_service_router in platform_main → close_account was UNREACHABLE over
    HTTP, dead code. Its four isolation tests stayed green (they mounted the
    self_service router alone — a composition that never existed in
    production), while every real portal "Konto löschen" died with 403 in the
    shadow handler. No static tool flags this: both handlers exist, both look
    wired, the bug lives only in the composition.

    Same-module shadowing is equally possible (two decorators on one path in
    one router) and equally silent.

This test imports the REAL platform_main composition — the exact app object
production serves — and fails on any (method, path) registered more than once.
There is deliberately NO allowlist: two handlers on one path is never a valid
state (a delegation like delete_user → close_account keeps ONE route and calls
the other as a plain function). If a duplicate is ever intentional, that
design should be argued in review, not silenced here.
"""

import os
from collections import Counter

os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

from src.platform_main import app  # noqa: E402


def _endpoint_name(route) -> str:
    fn = getattr(route, "endpoint", None)
    return f"{fn.__module__}.{fn.__name__}" if fn else "<unknown>"


def test_no_route_is_shadowed():
    seen: dict[tuple[str, str], list[str]] = {}
    for route in app.routes:
        if not hasattr(route, "methods"):
            continue  # mounts, websockets, static
        for method in route.methods:
            key = (method, route.path)
            seen.setdefault(key, []).append(_endpoint_name(route))

    shadowed = {k: v for k, v in seen.items() if len(v) > 1}
    assert not shadowed, (
        "Shadowed routes detected — only the FIRST handler per (method, path) "
        "is reachable; every later one is dead code that isolation tests will "
        f"still happily cover:\n"
        + "\n".join(
            f"  {m} {p}: reachable={handlers[0]}, DEAD={handlers[1:]}"
            for (m, p), handlers in shadowed.items()
        )
    )


def test_users_delete_is_the_delegating_handler():
    """
    Canary for the 2026-07-03 fix: DELETE /v1/users/{user_id} must resolve to
    admin_routes.delete_user (which delegates non-operators to the GDPR
    close_account). If this route ever moves, re-verify that customer
    self-service deletion still reaches the anonymize path.
    """
    handlers = [
        _endpoint_name(r)
        for r in app.routes
        if hasattr(r, "methods") and "DELETE" in r.methods and r.path == "/v1/users/{user_id}"
    ]
    assert handlers == ["src.db.admin_routes.delete_user"], handlers
