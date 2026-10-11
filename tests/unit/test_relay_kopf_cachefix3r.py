"""CACHEFIX3R (11.10.2026): reviewer tests (Codex) plus the allowlist contract
of the cross-worker relay headers. Reviewer tests unchanged below."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest
from starlette.requests import Request

from tests.unit.test_relay_kopf_cachefix3 import main


@pytest.mark.parametrize(
    "headers,forbidden",
    [
        ([("Set-Cookie", "session=fixture")], "set-cookie"),
        ([("Authorization", "Bearer fixture")], "authorization"),
        ([("X-Internal-Token", "fixture")], "x-internal-token"),
        (
            [("Connection", "X-Private-Hop"), ("X-Private-Hop", "fixture")],
            "x-private-hop",
        ),
    ],
)
async def test_sensitive_headers_not_relayed(headers, forbidden):
    req = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": [],
        }
    )
    req.state.cached_body_dict = {"messages": [{"role": "user", "content": "fixture"}]}
    response = httpx.Response(200, json={"ok": True}, headers=headers)
    with (
        patch.object(
            main, "_pick_alternative_worker", AsyncMock(return_value="fixture-worker")
        ),
        patch.object(httpx.AsyncClient, "post", AsyncMock(return_value=response)),
    ):
        result = await main._cross_worker_retry(req, "entry")
    assert forbidden not in result.headers


async def test_stream_skips_relay():
    req = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": [],
        }
    )
    req.state.cached_body_dict = {"stream": True}
    with patch.object(main, "_pick_alternative_worker", AsyncMock()) as pick:
        assert await main._cross_worker_retry(req, "entry") is None
        pick.assert_not_awaited()


def test_singleton_header_not_duplicated():
    response = httpx.Response(
        200,
        json={},
        headers=[("X-Cache-Boundary", "applied"), ("x-cache-boundary", "ignored")],
    )
    out = main._relay_headers(response)
    assert len([k for k, v in out if k == b"x-cache-boundary"]) <= 1


# ── CACHEFIX3 Runde 2: allowlist contract ─────────────────────────────────────


def _upstream(headers):
    return httpx.Response(200, json={}, headers=headers)


def test_connection_named_header_dropped_case_insensitive():
    out = dict(
        main._relay_headers(
            _upstream(
                [
                    ("Connection", "keep-alive, X-CACHE-BOUNDARY"),
                    ("x-Cache-Boundary", "applied"),
                    ("X-Worker-Instance", "worker1"),
                ]
            )
        )
    )
    assert b"x-cache-boundary" not in out
    assert out[b"x-worker-instance"] == b"worker1"


def test_identical_duplicate_passed_once():
    out = main._relay_headers(
        _upstream(
            [
                ("X-Cache-Boundary", "applied"),
                ("x-cache-boundary", "applied"),
            ]
        )
    )
    assert out == [(b"x-cache-boundary", b"applied")]


def test_contradictory_duplicate_dropped_and_logged(caplog):
    out = main._relay_headers(
        _upstream(
            [
                ("X-Cache-Boundary", "applied"),
                ("x-cache-boundary", "lost:empty_prefix"),
                ("Retry-After", "30"),
            ]
        )
    )
    assert out == [(b"retry-after", b"30")]
    assert "contradictory x-cache-boundary" in caplog.text


def test_only_allowlisted_headers_pass():
    out = dict(
        main._relay_headers(
            _upstream(
                [
                    ("X-Cache-Boundary", "applied"),
                    ("X-Claude-Session-ID", "s1"),
                    ("X-Bridge-Cost-EUR", "0.1"),
                    ("Retry-After", "5"),
                    ("X-Bridge-Served-By", "dev/worker1"),
                    ("Set-Cookie", "a=b"),
                    ("X-Something-New", "x"),
                    ("Content-Length", "2"),
                    ("Server", "uvicorn"),
                ]
            )
        )
    )
    assert set(out) == {
        b"x-cache-boundary",
        b"x-claude-session-id",
        b"x-bridge-cost-eur",
        b"retry-after",
    }
