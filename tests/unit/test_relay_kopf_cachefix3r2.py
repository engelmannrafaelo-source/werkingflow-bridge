"""CACHEFIX3R Runde 2 (Codex): the dependency marker survives the relay and
the real executor classifier still parks the job (424). Reviewer test unchanged."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from starlette.requests import Request

from src.jobs.executors import ExecutorHTTPError, _raise_if_dependency_unavailable
from src.platform_client import DEPENDENCY_UNAVAILABLE_HEADER
from tests.unit.test_relay_kopf_cachefix3 import main


async def test_dependency_marker_survives_relay_and_parks_job():
    req = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": [],
        }
    )
    req.state.cached_body_dict = {"messages": [{"role": "user", "content": "fixture"}]}
    upstream = httpx.Response(
        503,
        json={"error": {"retryable": True}},
        headers={DEPENDENCY_UNAVAILABLE_HEADER: "provider-config"},
    )
    with pytest.raises(ExecutorHTTPError) as direct:
        _raise_if_dependency_unavailable(upstream, req.url.path)
    assert direct.value.status_code == 424
    with (
        patch.object(
            main, "_pick_alternative_worker", AsyncMock(return_value="fixture-worker")
        ),
        patch.object(httpx.AsyncClient, "post", AsyncMock(return_value=upstream)),
    ):
        relayed = await main._cross_worker_retry(req, "entry")
    client_response = httpx.Response(
        relayed.status_code, content=relayed.body, headers=relayed.raw_headers
    )
    with pytest.raises(ExecutorHTTPError) as relay:
        _raise_if_dependency_unavailable(client_response, req.url.path)
    assert relay.value.status_code == 424
