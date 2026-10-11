"""CACHEFIX3 (11.10.2026): a relayed answer keeps the answering worker's headers.

Befund DBD2 Schritt 4 (Dev, 05d4d3b, 18:01:49-18:02:23Z): the deploy smoke
`chat_cache_boundary` got HTTP 200 without `X-Cache-Boundary` on all three
attempts, although worker1 logged `cache_boundary: status=applied at=727` for
each of them. Worker log (wt-wrapper-logs/app.log, same three seconds):

  [worker3] bridge_error: source=bridge_account type=account_exhausted ...
  [worker3] _cross_worker_retry: cross-worker retry #1: worker3 → worker1
  [worker1] chat_completions: cache_boundary: status=applied at=727

The LB had sent the call to a worker without a free account; the adaptive
limiter raised account_exhausted, `bridge_error_handler` relayed the body to
worker1 via `_cross_worker_retry`, and the relay rebuilt the answer as
`JSONResponse(status_code, content)` — worker1's body, but none of its
headers. Every diagnostic header the answering worker sets
(X-Cache-Boundary, X-Worker-Instance, X-Claude-Session-ID, cost headers)
vanished on that path, and only there.

The test runs the real path: entry app → limiter refusal → bridge_error_handler
→ _cross_worker_retry → httpx → a second pass through the same app (the
"other worker") with the CLI stand-in → relayed answer.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock as _MagicMock

for _mod_name in [
    "claude_code_sdk",
    "claude_code_sdk._errors",
    "claude_code_sdk._internal",
    "claude_code_sdk._internal.client",
]:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = _MagicMock()

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

import src.claude_cli as claude_cli  # noqa: E402
import src.main as main  # noqa: E402
from scripts.bridge_smoke import CACHE_BOUNDARY_SMOKE_BODY  # noqa: E402
from src.claude_cli import ClaudeCodeCLI  # noqa: E402
from src.middleware.bridge_error import raise_account_exhausted  # noqa: E402
from tests import sdk_strom as sdk  # noqa: E402

ENTRY = "worker3"
ANSWERING = "worker1"


def _cli(tmp_path) -> ClaudeCodeCLI:
    cli = object.__new__(ClaudeCodeCLI)
    cli.timeout = 30
    cli.cwd = tmp_path
    cli.claude_env_vars = {}
    cli.cache_dir = tmp_path
    cli.max_cache_size_mb = 10
    cli.file_discovery = MagicMock()
    return cli


def _query(*messages):
    async def query(prompt, options):
        for m in messages:
            yield m

    return query


@pytest.fixture
def relay(monkeypatch, tmp_path):
    """Entry worker refuses (account_exhausted), the other worker answers."""
    monkeypatch.setenv("INSTANCE_NAME", ENTRY)
    monkeypatch.setenv("API_KEY", "")
    monkeypatch.setenv("CLAUDE_SKIP_AUTH", "1")
    monkeypatch.setattr(
        claude_cli, "MessageParseError", type("MessageParseError", (Exception,), {})
    )
    monkeypatch.setattr(
        claude_cli,
        "query",
        _query(
            sdk.init(),
            sdk.AssistantMessage(content=[sdk.TextBlock(text="OK")], model="m"),
            sdk.result(usage={"input_tokens": 7000, "output_tokens": 1}),
        ),
    )
    monkeypatch.setattr(
        main.claude_cli, "run_completion", _cli(tmp_path).run_completion
    )

    calls: list[dict] = []

    async def admission(request):
        # The relayed call carries x-bridge-retry-count; only the entry pass
        # (no such header) is refused, like worker3 at its weekly limit.
        hop = request.headers.get("x-bridge-retry-count")
        calls.append({"hop": hop})
        if not hop:
            raise_account_exhausted(3600)

    real_client = httpx.AsyncClient

    def other_worker_client(*args, **kwargs):
        # http://worker1:8000/... reaches the same app in-process.
        kwargs["transport"] = httpx.ASGITransport(app=main.app)
        return real_client(*args, **kwargs)

    with (
        patch(
            "src.main.validate_claude_code_auth",
            return_value=(True, {"method": "test"}),
        ),
        patch("src.main.verify_api_key", new_callable=AsyncMock),
        patch("src.main.enforce_pool_admission", side_effect=admission),
        patch(
            "src.main._pick_alternative_worker",
            new=AsyncMock(return_value=ANSWERING),
        ),
        patch.object(httpx, "AsyncClient", side_effect=other_worker_client),
        patch(
            "src.providers.fallback.get_fallback_tiers",
            side_effect=lambda t, **kw: [t],
        ),
    ):
        yield calls
    if main.limiter is not None:
        main.limiter.reset()


def _post(body):
    from starlette.testclient import TestClient

    return TestClient(main.app, raise_server_exceptions=False).post(
        "/v1/chat/completions", json=body
    )


def test_relayed_answer_keeps_cache_boundary_header(relay):
    resp = _post(CACHE_BOUNDARY_SMOKE_BODY)

    assert [c["hop"] for c in relay] == [None, "1"], relay  # really relayed
    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert body["x_claude_metadata"]["cache_boundary"] == "applied"
    assert body["usage"]["prompt_tokens"] > 0
    # The header the deploy smoke asserts — lost on 05d4d3b.
    assert resp.headers.get("x-cache-boundary") == "applied"
    # Same loss for every header the answering worker sets.
    assert resp.headers.get("x-worker-instance") == body["x_claude_metadata"][
        "worker_instance"
    ]
    assert resp.headers.get("x-bridge-cost-eur")


def test_relay_does_not_forward_framing_headers(relay):
    resp = _post(CACHE_BOUNDARY_SMOKE_BODY)
    assert resp.status_code == 200
    # One content-length matching the body actually sent, no stale framing.
    assert resp.headers.get_list("content-length") == [str(len(resp.content))]
    assert "transfer-encoding" not in resp.headers
    # The relaying worker answers the caller and says so (ServedByMiddleware).
    assert len(resp.headers.get_list("x-bridge-served-by")) == 1
    # The relaying worker booked nothing; the cost is the answering worker's.
    assert len(resp.headers.get_list("x-bridge-cost-eur")) == 1


def test_smoke_probe_passes_on_relayed_answer(relay, monkeypatch):
    """The deploy probe itself, unweakened, against the relayed path."""
    import requests

    from scripts import bridge_smoke

    def via_app(url, headers=None, json=None, timeout=None):
        resp = _post(json)
        r = requests.Response()
        r.status_code = resp.status_code
        r._content = resp.content
        r.headers.update(resp.headers)
        return r

    monkeypatch.setattr(bridge_smoke.requests, "post", via_app)
    ctx = bridge_smoke.Ctx(
        base_url="http://bridge.test",
        api_key="k",
        extra_header={},
        expect_bridge="",
        served_by_required=False,
    )
    probe = next(p for p in bridge_smoke.PROBES if p.name == "chat_cache_boundary")
    result = probe.fn(ctx)
    assert result.ok, result.detail
