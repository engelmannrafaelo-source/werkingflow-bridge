"""RUSAGE (11.10.2026): research hands out the usage the run reported.

Befund Energy DS3: a research job (/v1/jobs kind=research) returned content,
antwort_quelle and antwort_ende but no usage field, so Energy wrote KOSTENSPUR
`bridge_research_antwort_ohne_usage` and the research costs were missing from
the report. The bridge knew the numbers: _execute_research_impl sums the CLI's
result usage for the ledger (accumulated_*), it only never put them into
ResearchResponse. On an API error inside the CLI the result message carrying
the usage was withheld (CliApiError) and its numbers were lost entirely.

Guarded through the real path: job (registry) → research_executor → HTTP
self-call → /v1/research → _execute_research_impl → run_completion → CLI
stand-in (SDK messages); and the cross-worker relay of /v1/research.
"""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock as _MagicMock

os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

for _mod_name in [
    "claude_code_sdk",
    "claude_code_sdk._errors",
    "claude_code_sdk._internal",
    "claude_code_sdk._internal.client",
]:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = _MagicMock()

import json  # noqa: E402
from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

import src.claude_cli as claude_cli  # noqa: E402
import src.main as main  # noqa: E402
from src.jobs import executors, registry, store_client  # noqa: E402
from tests import sdk_strom as sdk  # noqa: E402

HEX32 = "0123456789abcdef" * 2
JOB_ID = f"job_dev_{HEX32}"

# What the CLI's result message reports (Anthropic semantics: input_tokens is
# the UNCACHED input, cache traffic separate).
CLI_USAGE = {
    "input_tokens": 1200,
    "output_tokens": 3400,
    "cache_creation_input_tokens": 5000,
    "cache_read_input_tokens": 70000,
}
ANTWORT = "Antwort der Recherche. " * 40

_CLI_FEHLER = "API Error: 529 Overloaded"


def _text(t):
    return sdk.AssistantMessage(content=[sdk.TextBlock(text=t)], model="m")


def _erfolg(usage=CLI_USAGE):
    return [sdk.init(), _text(ANTWORT), sdk.result(usage=usage)]


def _api_fehler(usage=CLI_USAGE):
    """The CLI's API-error turn and the result it ends the run with — the
    result carries the usage of everything before the error."""
    turn = _text(_CLI_FEHLER)
    turn._bridge_cli_error = "overloaded_error"
    turn._bridge_is_api_error_message = True
    r = sdk.result("success", is_error=True, usage=usage)
    r.result = _CLI_FEHLER
    r._bridge_api_error_status = 529
    r._bridge_terminal_reason = "api_error"
    return [sdk.init(), _text("Ich beginne. "), turn, r]


def _cli(tmp_path):
    cli = object.__new__(claude_cli.ClaudeCodeCLI)
    cli.timeout = 30
    cli.cwd = tmp_path
    cli.claude_env_vars = {}
    cli.cache_dir = tmp_path
    cli.max_cache_size_mb = 10
    cli.file_discovery = MagicMock()
    cli.file_discovery.discover_files_from_sdk_messages.return_value = []
    cli.file_discovery.discover_files_from_directory_scan.return_value = []
    return cli


@pytest.fixture
def bridge(monkeypatch, tmp_path):
    """The research endpoint with a CLI stand-in; returns a setter for the
    stream the CLI plays."""
    monkeypatch.setenv("INSTANCE_NAME", "worker-rusage")
    monkeypatch.setenv("API_KEY", "")
    monkeypatch.setenv("CLAUDE_SKIP_AUTH", "1")
    monkeypatch.setattr(
        claude_cli, "MessageParseError", type("MessageParseError", (Exception,), {})
    )
    strom: list = []

    async def query(prompt, options):
        for m in strom:
            yield m

    monkeypatch.setattr(claude_cli, "query", query)
    monkeypatch.setattr(
        main.claude_cli, "run_completion", _cli(tmp_path).run_completion
    )
    monkeypatch.setattr("src.claude_cli.detect_quota_exhaustion", lambda *a, **k: False)
    monkeypatch.setattr(
        "src.activity.ai_call_writer.persist_ai_call_activity", AsyncMock()
    )

    real_client = httpx.AsyncClient

    def in_process_client(*args, **kwargs):
        # The self-call (and the relay) reach the same app in-process.
        kwargs["transport"] = httpx.ASGITransport(app=main.app)
        return real_client(*args, **kwargs)

    with (
        patch(
            "src.main.validate_claude_code_auth",
            return_value=(True, {"method": "test"}),
        ),
        patch("src.main.verify_api_key", new_callable=AsyncMock),
        patch("src.main.enforce_pool_admission", new_callable=AsyncMock),
        patch(
            "src.research_cloud.routing.resolve_research_cloud_routing",
            new=AsyncMock(return_value=False),
        ),
        patch("src.auth.auth_manager.get_api_key", return_value="k"),
        patch.object(httpx, "AsyncClient", side_effect=in_process_client),
    ):
        def spiele(messages):
            strom[:] = messages

        yield spiele
    if main.limiter is not None:
        main.limiter.reset()


def _body(**kw):
    return {
        "query": "Wie hoch ist der Grenzwert?",
        "model": "claude-sonnet-4-5",
        "antwort_im_text": True,
        "max_turns": 5,
        **kw,
    }


async def _job(body):
    """Run a research job the way /v1/jobs does, record how it ended."""
    mark_done, mark_error = AsyncMock(), AsyncMock()
    with patch.multiple(
        store_client,
        mark_running=AsyncMock(return_value=True),
        heartbeat=AsyncMock(),
        update_progress=AsyncMock(),
        mark_done=mark_done,
        mark_error=mark_error,
        defer_job=AsyncMock(),
        get_job=AsyncMock(return_value={"defer_count": 0}),
    ):
        registry.register_executor("research", executors.research_executor)
        await registry.run_generic_job(JOB_ID, "research", body, None)
    return mark_done, mark_error


def _erwartete_usage():
    prompt = 1200 + 5000 + 70000
    return {
        # chat/completions shape
        "prompt_tokens": prompt,
        "completion_tokens": 3400,
        "total_tokens": prompt + 3400,
        "cache_read_tokens": 70000,
        "cache_write_tokens": 5000,
        # Anthropic names
        "input_tokens": 1200,
        "output_tokens": 3400,
        "cache_creation_input_tokens": 5000,
        "cache_read_input_tokens": 70000,
    }


def _ohne_extras(usage):
    return {k: v for k, v in usage.items() if k != "image_count"}


# --- job → executor → /v1/research -------------------------------------------


async def test_job_erfolg_traegt_usage(bridge):
    bridge(_erfolg())
    mark_done, mark_error = await _job(_body())
    mark_error.assert_not_awaited()
    mark_done.assert_awaited_once()
    result = mark_done.await_args.args[1]
    assert result["status"] == "success", result.get("error")
    assert result["content"].strip() == ANTWORT.strip()
    assert _ohne_extras(result["usage"]) == _erwartete_usage()
    assert result["usage_fehlt"] is None


async def test_job_erfolg_ohne_usage_ist_null_mit_grund(bridge):
    bridge(_erfolg(usage=None))
    mark_done, _ = await _job(_body())
    result = mark_done.await_args.args[1]
    assert result["status"] == "success"
    assert result["usage"] is None  # not a made-up 0
    assert result["usage_fehlt"] == "cli_ohne_usage"


async def test_job_fehler_traegt_usage_des_vorenthaltenen_results(bridge):
    """API error inside the CLI: the answer is withheld, the run was paid for
    — its result's usage reaches the job error."""
    bridge(_api_fehler())
    mark_done, mark_error = await _job(_body())
    mark_done.assert_not_awaited()
    mark_error.assert_awaited_once()
    kw = mark_error.await_args.kwargs
    assert kw["retryable"] is True  # 529: verdict unchanged (CACHEFIX)
    assert _ohne_extras(kw["usage"]) == _erwartete_usage()
    assert kw["usage_fehlt"] is None


async def test_job_fehler_ohne_usage_ist_null_mit_grund(bridge):
    bridge(_api_fehler(usage=None))
    _, mark_error = await _job(_body())
    kw = mark_error.await_args.kwargs
    assert kw["usage"] is None
    assert kw["usage_fehlt"] == "cli_ohne_usage"


# --- /v1/research over the cross-worker relay ---------------------------------


def test_relay_reicht_usage_durch(bridge):
    """The entry worker's account hits its limit (RateLimitError →
    rate_limit_handler → _cross_worker_retry); the relay forwards the body of
    the worker that ran the research — usage included."""
    bridge(_erfolg())
    hops: list = []

    async def admission(request):
        hop = request.headers.get("x-bridge-retry-count")
        hops.append(hop)
        if not hop:
            raise claude_cli.RateLimitError("account limit", retry_after_seconds=60)

    from starlette.testclient import TestClient

    with (
        patch("src.main.enforce_pool_admission", side_effect=admission),
        patch(
            "src.main._pick_alternative_worker",
            new=AsyncMock(return_value="worker1"),
        ),
    ):
        resp = TestClient(main.app, raise_server_exceptions=False).post(
            "/v1/research", json=_body()
        )
    assert hops == [None, "1"], hops  # really relayed
    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert body["status"] == "success", body.get("error")
    assert _ohne_extras(body["usage"]) == _erwartete_usage()


# --- job error row: stored as sent, only when sent ------------------------------


class _Conn:
    def __init__(self):
        self.args = None

    async def execute(self, sql, *args):
        self.args = args
        return "UPDATE 1"


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    def acquire(self):
        conn = self.conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *a):
                return False

        return _Ctx()


@pytest.mark.parametrize(
    "felder, erwartet",
    [
        ({}, {}),
        ({"usage": None, "usage_fehlt": "cli_ohne_usage"},
         {"usage": None, "usage_fehlt": "cli_ohne_usage"}),
        ({"usage": {"input_tokens": 1}, "usage_fehlt": None},
         {"usage": {"input_tokens": 1}, "usage_fehlt": None}),
    ],
)
async def test_store_schreibt_usage_nur_wenn_gesendet(monkeypatch, felder, erwartet):
    from src.jobs import store

    conn = _Conn()
    monkeypatch.setattr(store, "get_pool", lambda: _Pool(conn))
    await store.mark_error(JOB_ID, "boom", code="EXECUTOR_ERROR",
                           retryable=False, retry_after_s=None, **felder)
    error = json.loads(conn.args[1])
    assert {k: error[k] for k in ("usage", "usage_fehlt") if k in error} == erwartet


def test_platform_route_nimmt_usage_an():
    from src.internal_routes import InternalJobError

    body = InternalJobError(message="m", usage={"input_tokens": 1}, usage_fehlt=None)
    assert body.model_dump(include={"usage", "usage_fehlt"}, exclude_unset=True) == {
        "usage": {"input_tokens": 1}, "usage_fehlt": None,
    }
    alt = InternalJobError(message="m")
    assert alt.model_dump(include={"usage", "usage_fehlt"}, exclude_unset=True) == {}
