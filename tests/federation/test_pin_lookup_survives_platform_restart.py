"""BR8: a platform-api restart must not end a job for good.

Measured 10.10.2026 (BR7): a dev deploy recreated the dev platform-api at
04:09:18.28Z (port bound 19.53, application ready 21.03). At 04:09:19Z a prod
worker ran a research job with origin dev and asked that platform-api for the
user's provider pin (ADR-0011). Two attempts 0.25 s apart, connection refused
both times. The fail-closed lookup raised, research answered status=error, and
the job ended as EXECUTOR_ERROR. The Energy run went red.

What must hold now:
- fail-closed stays: nothing is guessed, no local-DB fallback for a foreign origin;
- the lookup outlasts one restart (about 3 s) inside the request;
- longer than that, the refusal is RETRYABLE: the job is parked, not burned,
  within a fixed bound, then fails loud;
- real answers (403/404 from the home bridge, unsupported provider, missing
  peer) stay final at once.
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src import platform_client
from src.federation import set_request_origin
from src.jobs import executors, registry, store_client
from src.routing import user_provider_override as upo

HEADER = "X-Bridge-Dependency-Unavailable"


def _main():
    """src.main without the Claude SDK installed: the SDK is stubbed the same
    way tests/test_research_tracking.py does it (setdefault, so a real SDK or
    an earlier stub wins)."""
    import sys
    from unittest.mock import MagicMock

    for name in ("claude_code_sdk", "claude_code_sdk._errors",
                 "claude_code_sdk._internal", "claude_code_sdk._internal.client"):
        sys.modules.setdefault(name, MagicMock())
    from src import main

    return main
UID = "12a312e3-0000-4000-8000-000000000001"


@pytest.fixture(autouse=True)
def _foreign_dev_job_on_prod_worker(monkeypatch):
    """A prod worker running a job whose budget home is dev (the BR7 case)."""
    monkeypatch.setenv("BRIDGE_ORIGIN_ID", "prod")
    monkeypatch.setenv(
        "FEDERATION_PEERS",
        json.dumps({"dev": {"platformUrl": "http://dev-platform:8300",
                            "tokenEnv": "FEDERATION_TOKEN_DEV"}}),
    )
    monkeypatch.setenv("FEDERATION_TOKEN_DEV", "dev-token")
    monkeypatch.setenv("BRIDGE_SERVICE_TOKEN", "prod-token")
    set_request_origin("dev")
    upo.invalidate_cache()
    yield
    set_request_origin(None)
    upo.invalidate_cache()


class _Platform:
    """The dev platform-api as the worker sees it: refused for the first
    ``down`` attempts (the restart gap), then the given answer."""

    def __init__(self, down: int, status: int = 200, body=None):
        self.down = down
        self.status = status
        self.body = {"providerConfig": None} if body is None else body
        self.calls = 0
        self.pauses: list[float] = []

    def install(self, monkeypatch):
        real_client = httpx.AsyncClient

        def handler(request: httpx.Request) -> httpx.Response:
            self.calls += 1
            assert request.url.host == "dev-platform", "must ask the HOME bridge"
            if self.calls <= self.down:
                raise httpx.ConnectError("All connection attempts failed", request=request)
            return httpx.Response(self.status, json=self.body)

        def client_factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real_client(*args, **kwargs)

        async def no_sleep(seconds):
            self.pauses.append(seconds)

        monkeypatch.setattr(platform_client.httpx, "AsyncClient", client_factory)
        monkeypatch.setattr(platform_client.asyncio, "sleep", no_sleep)
        return self


# --- inside the request: one restart is bridged ------------------------------

async def test_restart_gap_is_bridged_inside_the_request(monkeypatch):
    """BR7 replay: three refused attempts (one restart) and then an answer. Before BR8 the
    second refusal already ended the job."""
    platform = _Platform(down=3, body={"providerConfig": {"provider": "anthropic"}}).install(monkeypatch)
    config = await upo.get_user_provider_config(UID)
    assert config == {"provider": "anthropic"}
    assert platform.calls == 4
    # Retries start at 0, 0.5, 1.5, 3.5 s: covers the measured ~3 s gap.
    assert platform.pauses == [0.5, 1.0, 2.0]


async def test_outage_longer_than_a_restart_is_retryable_not_final(monkeypatch):
    platform = _Platform(down=99).install(monkeypatch)
    with pytest.raises(upo.UserProviderOverrideError) as caught:
        await upo.get_user_provider_config(UID)
    # Still fail-closed: refused (it IS a UserProviderOverrideError) ...
    assert isinstance(caught.value, upo.ProviderConfigTemporarilyUnavailable)
    # ... after a bounded number of attempts (fixed upper limit, about 7.5 s of pauses).
    assert platform.calls == 1 + len(platform_client.RESTART_BRIDGING_BACKOFFS_S)
    assert sum(platform.pauses) == pytest.approx(7.5)


async def test_5xx_from_home_platform_is_retryable(monkeypatch):
    _Platform(down=0, status=502, body={"detail": "bad gateway"}).install(monkeypatch)
    with pytest.raises(upo.ProviderConfigTemporarilyUnavailable):
        await upo.get_user_provider_config(UID)


@pytest.mark.parametrize("status", [401, 403, 404])
async def test_real_answers_stay_final_at_once(monkeypatch, status):
    """Forbidden or unknown route is an ANSWER from the home bridge: not
    retryable, and not repeated."""
    platform = _Platform(down=0, status=status, body={"detail": "no"}).install(monkeypatch)
    with pytest.raises(upo.UserProviderOverrideError) as caught:
        await upo.get_user_provider_config(UID)
    assert not isinstance(caught.value, upo.ProviderConfigTemporarilyUnavailable)
    assert platform.calls == 1


async def test_no_pin_is_an_answer_not_an_error(monkeypatch):
    _Platform(down=1, body={"providerConfig": None}).install(monkeypatch)
    assert await upo.get_user_provider_config(UID) is None


async def test_missing_peer_stays_final(monkeypatch):
    monkeypatch.setenv("FEDERATION_PEERS", "{}")
    with pytest.raises(upo.UserProviderOverrideError) as caught:
        await upo.get_user_provider_config(UID)
    assert not isinstance(caught.value, upo.ProviderConfigTemporarilyUnavailable)


async def test_email_identity_in_front_of_the_pin_is_bridged_too(monkeypatch):
    """An email identity is resolved at the same home platform-api first; a
    restart there must not end the job either."""
    from src.identity import user_resolver

    user_resolver.invalidate_email_cache()
    platform = _Platform(down=2, body={"id": UID, "providerConfig": None}).install(monkeypatch)
    assert await upo.get_user_provider_config("kunde@example.com") is None
    assert platform.pauses[:2] == [0.5, 1.0]


# --- the endpoints mark it, the job layer parks it ---------------------------

async def test_research_route_marks_the_refusal_retryable(monkeypatch):
    from starlette.requests import Request

    main = _main()
    from src.models import ResearchRequest

    monkeypatch.setattr(main, "verify_api_key", AsyncMock())
    monkeypatch.setattr(main, "enforce_attribution", lambda request: None)
    monkeypatch.setattr(
        upo, "enforce_user_provider_override",
        AsyncMock(side_effect=upo.ProviderConfigTemporarilyUnavailable("gap")),
    )
    request = Request({"type": "http", "method": "POST", "path": "/v1/research",
                       "headers": [], "query_string": b""})
    response = await main.research(
        ResearchRequest(query="q", model="sonnet"), request, None, None
    )
    assert response.headers[HEADER] == "provider-config"
    body = json.loads(response.body)
    assert body["status"] == "error"
    assert '"retryable": true' in body["error"] and "503" in body["error"]


async def test_research_route_keeps_real_pin_errors_final(monkeypatch):
    from starlette.requests import Request

    main = _main()
    from src.models import ResearchRequest

    monkeypatch.setattr(main, "verify_api_key", AsyncMock())
    monkeypatch.setattr(main, "enforce_attribution", lambda request: None)
    monkeypatch.setattr(
        upo, "enforce_user_provider_override",
        AsyncMock(side_effect=upo.UserProviderOverrideError("provider 'x' not supported")),
    )
    request = Request({"type": "http", "method": "POST", "path": "/v1/research",
                       "headers": [], "query_string": b""})
    response = await main.research(
        ResearchRequest(query="q", model="sonnet"), request, None, None
    )
    assert response.status == "error"
    assert "retryable" not in response.error


async def test_http_error_handler_keeps_the_raisers_headers():
    """The chat path raises HTTPException(503, headers={HEADER: ...}); the
    envelope handler used to drop every header."""
    from fastapi import HTTPException
    from starlette.requests import Request

    main = _main()

    request = Request({"type": "http", "method": "POST", "path": "/v1/chat/completions",
                       "headers": [], "query_string": b""})
    exc = HTTPException(
        503, detail={"error": {"message": "m", "code": "user_provider_override_unavailable"}},
        headers={HEADER: "provider-config"},
    )
    response = await main.http_exception_handler(request, exc)
    assert response.status_code == 503
    assert response.headers[HEADER] == "provider-config"


def _self_call_answer(status: int, body: dict, dependency: str | None):
    headers = {HEADER: dependency} if dependency else {}
    return httpx.Response(status, json=body, headers=headers)


class _Seam:
    def __init__(self, defer_count: int = 0):
        self.defer_job = AsyncMock()
        self.mark_error = AsyncMock()
        self.mark_done = AsyncMock()
        self.defer_count = defer_count


async def _run_research_job(seam: _Seam, answer: httpx.Response):
    async def fake_post(client, path, body, headers):
        return answer, None

    with patch.object(executors, "_post_with_capacity_redispatch", fake_post), \
         patch.object(executors, "_build_headers", lambda attribution: {}), \
         patch.multiple(
             store_client,
             mark_running=AsyncMock(), heartbeat=AsyncMock(), update_progress=AsyncMock(),
             mark_done=seam.mark_done, mark_error=seam.mark_error, defer_job=seam.defer_job,
             get_job=AsyncMock(return_value={"defer_count": seam.defer_count}),
         ):
        registry.register_executor("research", executors.research_executor)
        await registry.run_generic_job(
            "job_prod_f6100749705a4fb2b71c8a46e57c611c", "research",
            {"query": "q"}, {"bridge_origin": "dev"},
        )


async def test_br7_research_job_is_parked_not_burned():
    """The exact 04:09:19Z case at the job layer: research answers 200 with
    status=error because the pin was not verifiable. Before BR8: EXECUTOR_ERROR,
    for good."""
    seam = _Seam()
    await _run_research_job(seam, _self_call_answer(
        200,
        {"status": "error", "query": "q", "model": "m",
         "error": 'user provider pin not verifiable right now (bridge: HTTP 503, "retryable": true)'},
        "provider-config",
    ))
    seam.mark_error.assert_not_awaited()
    seam.defer_job.assert_awaited_once()
    assert seam.defer_job.await_args.args[1] == 30


async def test_parked_job_fails_loud_after_its_bound():
    seam = _Seam(defer_count=30)
    await _run_research_job(seam, _self_call_answer(
        200, {"status": "error", "query": "q", "model": "m", "error": "gap"}, "provider-config",
    ))
    seam.defer_job.assert_not_awaited()
    seam.mark_error.assert_awaited_once()
    assert seam.mark_error.await_args.kwargs["code"] == "UPSTREAM_HTTP_424"


async def test_real_research_error_without_marker_stays_final():
    seam = _Seam()
    await _run_research_job(seam, _self_call_answer(
        200, {"status": "error", "query": "q", "model": "m",
              "error": "user provider pin unservable (no fallback by design): unsupported"},
        None,
    ))
    seam.defer_job.assert_not_awaited()
    assert seam.mark_error.await_args.kwargs["code"] == "EXECUTOR_ERROR"


async def test_chat_job_503_with_marker_is_parked():
    async def fake_post(client, path, body, headers):
        return _self_call_answer(503, {"error": {"message": "m"}}, "provider-config"), None

    with patch.object(executors, "_post_with_capacity_redispatch", fake_post), \
         patch.object(executors, "_build_headers", lambda attribution: {}):
        with pytest.raises(executors.ExecutorHTTPError) as caught:
            await executors.chat_executor({"model": "m"}, None, AsyncMock())
    assert caught.value.status_code == 424
    assert caught.value.dependency == "provider-config"


async def test_chat_job_plain_503_stays_503():
    async def fake_post(client, path, body, headers):
        return _self_call_answer(503, {"error": {"message": "m"}}, None), None

    with patch.object(executors, "_post_with_capacity_redispatch", fake_post), \
         patch.object(executors, "_build_headers", lambda attribution: {}):
        with pytest.raises(executors.ExecutorHTTPError) as caught:
            await executors.chat_executor({"model": "m"}, None, AsyncMock())
    assert caught.value.status_code == 503


async def test_unnamed_dependency_keeps_its_old_patience():
    """The privacy-service 424 (no header) keeps 60 s × 240."""
    seam = _Seam()
    registry.register_executor("dep-test", AsyncMock(
        side_effect=executors.ExecutorHTTPError(424, "privacy service down")))
    with patch.multiple(
        store_client,
        mark_running=AsyncMock(), heartbeat=AsyncMock(), update_progress=AsyncMock(),
        mark_done=seam.mark_done, mark_error=seam.mark_error, defer_job=seam.defer_job,
        get_job=AsyncMock(return_value={"defer_count": 30}),
    ):
        await registry.run_generic_job("job_prod_x", "dep-test", {}, None)
    seam.defer_job.assert_awaited_once()
    assert seam.defer_job.await_args.args[1] == registry.DEPENDENCY_RETRY_DELAY_S
