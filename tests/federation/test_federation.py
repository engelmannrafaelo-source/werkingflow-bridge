"""ADR-0011 — identity/budget federation: origin context, peer resolution,
fail polarity.

What is deliberately NOT tested here: the peer's platform-api behaviour (it is
the same code as the local one) and nginx's trust decision (that lives in
docker/nginx.conf and is proven on staging via the access log's `origin`
field). This file pins the WORKER-side contract:

  * no origin / own origin  → everything stays local (pre-ADR behaviour),
  * foreign origin + peer   → user-domain calls target the peer, with the
                              peer's token,
  * foreign origin - peer   → FederationMisconfigured, and the budget gate
                              turns that into a fail-CLOSED 503 — never the
                              transient-infra fail-open.
"""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from src import federation
from src.federation import (
    FederationMisconfigured,
    OriginMiddleware,
    cache_scope,
    is_foreign_origin,
    resolve_user_domain_target,
    set_request_origin,
)


@pytest.fixture(autouse=True)
def _clean_context(monkeypatch):
    """Every test starts with no origin and no federation env."""
    set_request_origin(None)
    monkeypatch.delenv("BRIDGE_ORIGIN_ID", raising=False)
    monkeypatch.delenv("FEDERATION_PEERS", raising=False)
    monkeypatch.delenv("FEDERATION_TOKEN_PEER", raising=False)
    yield
    set_request_origin(None)


def _configure(monkeypatch, *, self_id="dev", peers=None, token="s3cret"):
    monkeypatch.setenv("BRIDGE_ORIGIN_ID", self_id)
    if peers is not None:
        monkeypatch.setenv("FEDERATION_PEERS", json.dumps(peers))
    if token is not None:
        monkeypatch.setenv("FEDERATION_TOKEN_PEER", token)


# ── is_foreign_origin / cache_scope ─────────────────────────────────────────

def test_no_origin_is_never_foreign(monkeypatch):
    _configure(monkeypatch)
    assert is_foreign_origin() is False
    assert cache_scope() == ""


def test_own_origin_is_not_foreign(monkeypatch):
    _configure(monkeypatch)
    set_request_origin("dev")
    assert is_foreign_origin() is False
    assert cache_scope() == ""


def test_unset_self_id_means_nothing_is_foreign():
    # Pre-rollout default: a host without BRIDGE_ORIGIN_ID must behave exactly
    # as before ADR-0011, whatever headers arrive.
    set_request_origin("prod")
    assert is_foreign_origin() is False
    assert resolve_user_domain_target() is None


def test_foreign_origin_detected_case_insensitively(monkeypatch):
    _configure(monkeypatch)
    set_request_origin("PROD")
    assert is_foreign_origin() is True
    assert cache_scope() == "prod"


# ── resolve_user_domain_target ──────────────────────────────────────────────

def test_local_request_resolves_to_none(monkeypatch):
    _configure(monkeypatch, peers={"prod": {"platformUrl": "http://p:8300", "tokenEnv": "FEDERATION_TOKEN_PEER"}})
    set_request_origin("dev")
    assert resolve_user_domain_target() is None


def test_foreign_request_resolves_to_peer(monkeypatch):
    _configure(monkeypatch, peers={"prod": {"platformUrl": "http://peer:8300/", "tokenEnv": "FEDERATION_TOKEN_PEER"}})
    set_request_origin("prod")
    target = resolve_user_domain_target()
    assert target is not None
    assert target.base_url == "http://peer:8300"  # trailing slash normalised
    assert target.token == "s3cret"
    assert target.origin == "prod"


def test_foreign_without_peer_entry_fails_closed(monkeypatch):
    _configure(monkeypatch, peers={})
    set_request_origin("prod")
    with pytest.raises(FederationMisconfigured):
        resolve_user_domain_target()


def test_foreign_with_missing_token_fails_closed(monkeypatch):
    _configure(monkeypatch, peers={"prod": {"platformUrl": "http://p:8300", "tokenEnv": "NOT_SET_ANYWHERE"}})
    set_request_origin("prod")
    with pytest.raises(FederationMisconfigured):
        resolve_user_domain_target()


def test_malformed_peers_json_fails_closed(monkeypatch):
    _configure(monkeypatch)
    monkeypatch.setenv("FEDERATION_PEERS", "{not json")
    set_request_origin("prod")
    with pytest.raises(FederationMisconfigured):
        resolve_user_domain_target()


# ── call_platform(domain=…) ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_call_platform_rejects_unknown_domain():
    from src.platform_client import call_platform

    with pytest.raises(ValueError):
        await call_platform("GET", "/x", domain="global")


@pytest.mark.asyncio
async def test_call_platform_user_domain_fails_closed_without_peer(monkeypatch):
    from src.platform_client import call_platform

    _configure(monkeypatch, peers={})
    set_request_origin("prod")
    # Must raise BEFORE any HTTP attempt — a transport error would be
    # PlatformUnavailable, which the gates treat fail-OPEN. The distinction
    # is the whole point (ADR-0011 point 5).
    with pytest.raises(FederationMisconfigured):
        await call_platform("GET", "/v1/internal/users/x/tenant", domain="user")


# ── OriginMiddleware ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_middleware_sets_origin_from_header():
    seen = {}

    async def inner(scope, receive, send):
        seen["origin"] = federation.get_request_origin()

    mw = OriginMiddleware(inner)
    scope = {"type": "http", "headers": [(b"x-bridge-origin", b"Prod")]}
    await mw(scope, None, None)
    assert seen["origin"] == "prod"


@pytest.mark.asyncio
async def test_middleware_clears_origin_when_header_absent():
    set_request_origin("prod")  # stale value from a previous request
    seen = {}

    async def inner(scope, receive, send):
        seen["origin"] = federation.get_request_origin()

    mw = OriginMiddleware(inner)
    await mw({"type": "http", "headers": []}, None, None)
    assert seen["origin"] is None


# ── budget gate: fail polarity ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gate_fails_closed_on_foreign_origin_without_peer(monkeypatch):
    from src.budget.gate import enforce_budget

    _configure(monkeypatch, peers={})
    set_request_origin("prod")
    with pytest.raises(HTTPException) as exc:
        await enforce_budget(
            "3e6bf3df-9917-4969-94ec-a1736c40a783", "werking-report", 0.05
        )
    assert exc.value.status_code == 503
    assert exc.value.detail["error"] == "federation_unconfigured"


@pytest.mark.asyncio
async def test_gate_uncatalogued_app_stays_ungated_even_when_foreign(monkeypatch):
    # An app outside the plan catalog was never budget-gated; a foreign origin
    # must not change that (the pre-flight sits BEHIND the catalog check).
    from src.budget.gate import enforce_budget

    _configure(monkeypatch, peers={})
    set_request_origin("prod")
    assert await enforce_budget("someone", "definitely-not-an-app", 0.05) is None
