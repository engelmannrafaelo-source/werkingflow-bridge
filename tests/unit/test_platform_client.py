"""Tests for src/platform_client.py (ADR-0009 Schritt 2a, C1).

Covers: successful round-trip (incl. header + base URL), the "ordinary
response" contract (2xx/4xx never raise, only status/json differ), and the
PlatformUnavailable contract (timeout, connection error, 5xx, missing token).
No real network — httpx.MockTransport stands in for platform-api.
"""
from unittest.mock import patch

import httpx
import pytest

import src.platform_client as platform_client
from src.platform_client import PlatformUnavailable, call_platform


_RealAsyncClient = httpx.AsyncClient


def _patched_client(handler):
    """Swap httpx.AsyncClient for one wired to a MockTransport, keeping the
    real timeout argument so a timeout-raising handler still behaves like one.

    Patching the class object referenced from src.platform_client patches the
    shared httpx module attribute itself (both modules import the same httpx),
    so the factory must call the REAL class captured above, never `httpx.AsyncClient`
    again — that would recurse into the mock it just installed.
    """
    def _factory(*, timeout=None, **_ignored):
        return _RealAsyncClient(transport=httpx.MockTransport(handler), timeout=timeout)
    return patch("src.platform_client.httpx.AsyncClient", side_effect=_factory)


@pytest.fixture(autouse=True)
def _service_token(monkeypatch):
    monkeypatch.setenv("BRIDGE_SERVICE_TOKEN", "test-service-token")
    monkeypatch.delenv("PLATFORM_API_URL", raising=False)
    yield


@pytest.mark.asyncio
async def test_200_returns_response_with_json_body():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "platform-api"
        assert request.headers["x-bridge-service-token"] == "test-service-token"
        return httpx.Response(200, json={"id": "abc", "name": "engelmann"})

    with _patched_client(handler):
        resp = await call_platform("GET", "/v1/internal/principals/deadbeef")
    assert resp.status_code == 200
    assert resp.json == {"id": "abc", "name": "engelmann"}


@pytest.mark.asyncio
async def test_404_is_an_ordinary_response_not_an_exception():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not found"})

    with _patched_client(handler):
        resp = await call_platform("GET", "/v1/internal/principals/unknown")
    assert resp.status_code == 404
    assert resp.json == {"detail": "not found"}


@pytest.mark.asyncio
async def test_empty_body_yields_none_json():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    with _patched_client(handler):
        resp = await call_platform("POST", "/v1/internal/audit-events")
    assert resp.status_code == 204
    assert resp.json is None


@pytest.mark.asyncio
async def test_5xx_raises_platform_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="internal error")

    with _patched_client(handler):
        with pytest.raises(PlatformUnavailable):
            await call_platform("GET", "/v1/internal/principals/x")


@pytest.mark.asyncio
async def test_connection_error_raises_platform_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with _patched_client(handler):
        with pytest.raises(PlatformUnavailable):
            await call_platform("GET", "/v1/internal/principals/x")


@pytest.mark.asyncio
async def test_timeout_raises_platform_unavailable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    with _patched_client(handler):
        with pytest.raises(PlatformUnavailable):
            await call_platform("GET", "/v1/internal/principals/x")


@pytest.mark.asyncio
async def test_missing_service_token_raises_without_any_network_call(monkeypatch):
    monkeypatch.delenv("BRIDGE_SERVICE_TOKEN", raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not reach the network without a service token")

    with _patched_client(handler):
        with pytest.raises(PlatformUnavailable):
            await call_platform("GET", "/v1/internal/principals/x")


@pytest.mark.asyncio
async def test_platform_api_url_env_overrides_default(monkeypatch):
    monkeypatch.setenv("PLATFORM_API_URL", "http://100.79.1.2:8000")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "100.79.1.2"
        assert request.url.port == 8000
        return httpx.Response(200, json={})

    with _patched_client(handler):
        resp = await call_platform("GET", "/v1/internal/principals/x")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_default_base_url_is_docker_dns_name():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith("http://platform-api:8000")
        return httpx.Response(200, json={})

    with _patched_client(handler):
        await call_platform("GET", "/v1/internal/principals/x")


@pytest.mark.asyncio
async def test_json_body_is_sent_on_post():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        import json as _json
        assert _json.loads(request.content) == {"action": "test"}
        return httpx.Response(201, json={"ok": True})

    with _patched_client(handler):
        resp = await call_platform("POST", "/v1/internal/audit-events", json={"action": "test"})
    assert resp.status_code == 201


# --- Retry contract (ADR-0009 Schritt 2, revidiert 2026-08-20) -------------
#
# The point of these: retrying is opt-in, bounded, and NEVER applied to a 5xx.
# The 5xx case is the safety-critical one — platform-api was reached, so a
# replay is a second attempt at an unknown state, not a retry.


@pytest.mark.asyncio
async def test_default_does_not_retry_a_timeout():
    """Default retries=0 keeps the pre-2b behaviour: exactly one attempt.

    This is what makes the opt-in safe for the existing non-idempotent call
    site (POST /v1/internal/audit-events writes audit_log with no dedup key).
    """
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        raise httpx.ReadTimeout("simulated", request=request)

    with _patched_client(handler):
        with pytest.raises(PlatformUnavailable):
            await call_platform("POST", "/v1/internal/audit-events", json={"a": 1})
    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_opt_in_retry_makes_exactly_the_requested_extra_attempts():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        raise httpx.ReadTimeout("simulated", request=request)

    with _patched_client(handler):
        with pytest.raises(PlatformUnavailable) as exc:
            await call_platform(
                "GET", "/v1/internal/budget/user-budget-state",
                retries=1, retry_backoff_s=0,
            )
    assert len(attempts) == 2
    assert "after 2 attempt(s)" in str(exc.value)


@pytest.mark.asyncio
async def test_retry_succeeds_on_the_second_attempt():
    """The case the whole thing exists for: platform-api was restarting and is
    back a moment later — the customer's call survives the blip."""
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            raise httpx.ConnectError("simulated restart", request=request)
        return httpx.Response(200, json={"allowed": True})

    with _patched_client(handler):
        resp = await call_platform(
            "GET", "/v1/internal/budget/user-budget-state",
            retries=1, retry_backoff_s=0,
        )
    assert len(attempts) == 2
    assert resp.status_code == 200
    assert resp.json == {"allowed": True}


@pytest.mark.asyncio
async def test_5xx_is_never_retried_even_when_retries_are_requested():
    """Safety-critical: a 5xx is an ANSWER, not unreachability. platform-api
    was reached and may have had a partial effect; replaying is not a retry."""
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(503, text="upstream down")

    with _patched_client(handler):
        with pytest.raises(PlatformUnavailable):
            await call_platform(
                "POST", "/v1/internal/budget/ensure-trial",
                json={"userId": "u"}, retries=2, retry_backoff_s=0,
            )
    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_4xx_is_not_retried_and_stays_an_ordinary_response():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(404, json=None)

    with _patched_client(handler):
        resp = await call_platform(
            "GET", "/v1/internal/users/lookup-by-email", retries=2, retry_backoff_s=0,
        )
    assert len(attempts) == 1
    assert resp.status_code == 404
