"""R15: every effective worker route is inventoried with a REAL enforcement class.

The inventory (docs/service-route-inventory-worker.json) is derived from the app by
scripts/worker_route_inventory.py; this test holds it against the running router:
  * completeness in both directions, per (path, method), including routers that
    FastAPI 0.143 includes lazily (_IncludedRouter.effective_candidates) — no prefix
    filter, no hand-kept list of "interesting" routes;
  * the declared gate is a dependency of the route;
  * enforcement is real: dependency -> enforcing Depends(); handler -> the endpoint
    (or the helper it awaits) contains the verifier; public -> listed with reason in
    test_no_unauthenticated_routes.PUBLIC_ROUTES; none -> carries an "offen" reference;
  * behavioural probe over the real ASGI app: every enforced GET/DELETE and body-less
    POST answers 401/403 without a key and with a wrong key, and the mutation sinks
    stay untouched. POST routes with a body are validated before the handler runs,
    so for them the verifier source check is the evidence.
A new route without a row, a stale row, or a row whose class does not match the code
makes this red. Same import stubbing as test_no_unauthenticated_routes.py.
"""

import inspect
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

from unittest.mock import patch  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import src.main  # noqa: E402
from scripts.worker_route_inventory import (  # noqa: E402
    ENFORCING,
    dependency_names,
    effective_routes,
)
from tests.unit.test_no_unauthenticated_routes import PUBLIC_ROUTES  # noqa: E402

ROOT = Path(__file__).parents[2]
INVENTORY = json.loads((ROOT / "docs/service-route-inventory-worker.json").read_text())
ROWS = {(r["method"], r["path"]): r for r in INVENTORY["routes"]}
CLASSES = {"dependency", "handler", "public", "none"}


def _effective():
    out = {}
    for path, methods, dependant, endpoint, body_field in effective_routes(
        src.main.app
    ):
        for method in methods:
            out[(method, path)] = (dependant, endpoint, body_field)
    return out


def test_inventory_covers_every_effective_route_and_nothing_else():
    live = set(_effective())
    assert sorted(f"{m} {p}" for m, p in live - set(ROWS)) == [], (
        "routes without inventory row"
    )
    assert sorted(f"{m} {p}" for m, p in set(ROWS) - live) == [], (
        "inventory rows without route"
    )
    assert len(live) == len(ROWS) and len(ROWS) == len(INVENTORY["routes"])


@pytest.mark.parametrize("key", sorted(ROWS), ids=lambda k: f"{k[0]} {k[1]}")
def test_row_matches_the_code(key):
    row = ROWS[key]
    dependant, endpoint, _body = _effective()[key]
    deps = set(dependency_names(dependant))
    assert row["enforcement"] in CLASSES, row
    if row["gate"] == "none":
        assert not (deps & (ENFORCING | {"HTTPBearer"})), (key, deps)
    else:
        assert row["gate"] in deps, (key, row["gate"], deps)
    if row["enforcement"] == "dependency":
        assert row["gate"] in ENFORCING, key
    elif row["enforcement"] == "public":
        assert key[1] in PUBLIC_ROUTES, (
            f"{key}: public rows need a reason in PUBLIC_ROUTES"
        )
        assert not (deps & (ENFORCING | {"HTTPBearer"})), key
    elif row["enforcement"] == "handler":
        source = inspect.getsource(endpoint)
        via = row.get("via")
        if via:
            assert f"{via}(" in source, (key, via)
            helper = getattr(sys.modules[endpoint.__module__], via)
            source = inspect.getsource(helper)
        assert row["verifier"] in source, (key, row["verifier"])
    else:  # none
        assert row.get("offen"), (
            f"{key}: unenforced route needs the reference to its fix"
        )
        assert "verify_api_key" not in inspect.getsource(endpoint), (
            f"{key} now enforces — move it out of 'none'"
        )


def test_public_rows_and_public_routes_list_are_the_same_set():
    public = {p for (m, p), r in ROWS.items() if r["enforcement"] == "public"}
    assert public == set(PUBLIC_ROUTES)


def test_unenforced_set_is_exactly_the_br14_metrics_routes():
    """Pinned so that a NEW unenforced route cannot hide among the known ones."""
    none = sorted(
        f"{m} {p}" for (m, p), r in ROWS.items() if r["enforcement"] == "none"
    )
    assert none == [
        "GET /v1/metrics",
        "GET /v1/metrics/attribution",
        "GET /v1/metrics/cc-usage-history",
        "GET /v1/metrics/document-performance",
        "GET /v1/metrics/jobs-retention",
        "GET /v1/metrics/prompt-performance",
        "GET /v1/metrics/prompt-performance/calls",
        "GET /v1/metrics/prompt-performance/timeline",
        "GET /v1/metrics/queue-forecast",
        "GET /v1/metrics/request-log",
        "GET /v1/metrics/throughput",
        "GET /v1/metrics/usage-breakdown",
        "POST /v1/metrics/cc-usage-snapshot",
    ]
    for key in none:
        assert "BR14" in ROWS[tuple(key.split(" ", 1))]["offen"]


# --- behavioural probe over the real ASGI app --------------------------------------

BRIDGE_KEY = "synthetic-bridge-key-r15"
PROBE_ID = "r15-probe-id"


def _probeable():
    for key, row in sorted(ROWS.items()):
        if row["enforcement"] not in {"dependency", "handler"}:
            continue
        _dep, _ep, body_field = _effective()[key]
        if key[0] == "POST" and body_field is not None:
            # body validation runs before the handler: verifier source is the evidence
            continue
        yield key


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("BRIDGE_PRINCIPALS_ENABLED", "false")
    # A wrong Bearer on a jwt-or-service route must be a 401, not a missing-config 500:
    # the deployed worker has the JWT secret, the test process gets a synthetic one.
    monkeypatch.setenv("BRIDGE_JWT_SECRET", "synthetic-jwt-secret-for-r15-probe-only")
    with (
        patch("src.auth.auth_manager.get_api_key", return_value=BRIDGE_KEY),
        patch.object(
            src.main.session_manager, "delete_session", return_value=True
        ) as delete_sink,
        patch(
            "src.providers.fallback.reset_all_provider_health", return_value=0
        ) as reset_sink,
    ):
        c = TestClient(src.main.app)
        c.sinks = (delete_sink, reset_sink)
        yield c


def _url(path):
    return (
        path.replace("{session_id}", PROBE_ID)
        .replace("{cli_session_id}", PROBE_ID)
        .replace("{request_id}", PROBE_ID)
        .replace("{job_id}", PROBE_ID)
        .replace("{bericht_id}", PROBE_ID)
    )


@pytest.mark.parametrize("key", list(_probeable()), ids=lambda k: f"{k[0]} {k[1]}")
@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer synthetic-wrong"}],
    ids=["no-key", "wrong-key"],
)
def test_enforced_route_refuses_without_the_key_before_any_sink(client, key, headers):
    method, path = key
    resp = client.request(
        method, _url(path), headers=headers, json={} if method == "POST" else None
    )
    assert resp.status_code in (401, 403), (key, resp.status_code, resp.text[:200])
    assert all(s.call_count == 0 for s in client.sinks), key


def test_delete_session_sink_reached_only_with_the_shared_key(client):
    auth = {"Authorization": f"Bearer {BRIDGE_KEY}"}
    assert client.delete(f"/v1/sessions/{PROBE_ID}").status_code == 401
    assert (
        client.delete(
            f"/v1/sessions/{PROBE_ID}",
            headers={"Authorization": "Bearer synthetic-wrong"},
        ).status_code
        == 401
    )
    assert client.sinks[0].call_count == 0
    assert client.delete(f"/v1/sessions/{PROBE_ID}", headers=auth).status_code == 200
    assert client.sinks[0].call_count == 1
