"""CACHEFIX (11.10.2026): an API error inside the CLI is an error, never a 200.

Befund DBD2 (Dev, develop 276d146): a chat call with cache boundary came back
HTTP 200 with the content "API Error: 400 messages.0.content.6.cache_control.ttl:
a ttl='1h' cache_control block must not come after a ttl='5m' cache_control block".

When the Messages API answers an error the CLI does not fail. It writes the
error as a synthetic assistant turn and ends the run with a "success" result.
Measured with the worker CLI @anthropic-ai/claude-code 2.1.285 against a local
Messages-API stand-in that answers 400 (stream-json, real binary):

  assistant: {"error": "unknown", "is_api_error_message": true,
              "message": {"model": "<synthetic>",
                          "content": [{"text": "API Error: 400 ..."}]}}
  result:    {"subtype": "success", "is_error": true, "api_error_status": 400,
              "terminal_reason": "api_error", "result": "API Error: 400 ..."}

`is_api_error_message` is the CLI's own flag (SDK schema: "True when this
assistant message wraps an API error (from internal
AssistantMessage.isApiErrorMessage)"), `api_error_status` the HTTP status.
claude-code-sdk drops both; the resilient parser keeps them. The text is never
the signal.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
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

import pytest  # noqa: E402

import src.claude_cli as claude_cli  # noqa: E402
import src.main as main  # noqa: E402
from src.claude_cli import ClaudeCodeCLI  # noqa: E402
from src.middleware.bridge_error import CliApiError, classify_exception  # noqa: E402
from src.sdk_parser import resilient_parse_message  # noqa: E402
from tests import sdk_strom as sdk  # noqa: E402

WORKER = "worker-cachefix"
API_TEXT = (
    "API Error: 400 messages.0.content.6.cache_control.ttl: a ttl='1h' cache_control "
    "block must not come after a ttl='5m' cache_control block"
)


def _api_error_turn(
    text: str = API_TEXT, error: str = "unknown"
) -> sdk.AssistantMessage:
    """The CLI's synthetic API-error turn as the patched SDK parser hands it on."""
    msg = sdk.AssistantMessage(content=[sdk.TextBlock(text=text)], model="<synthetic>")
    msg._bridge_cli_error = error
    msg._bridge_is_api_error_message = True
    return msg


def _api_error_result(status: int = 400, text: str = API_TEXT) -> sdk.ResultMessage:
    r = sdk.result("success", is_error=True, num_turns=1)
    r.result = text
    r._bridge_api_error_status = status
    r._bridge_terminal_reason = "api_error"
    return r


@pytest.fixture(autouse=True)
def umgebung(monkeypatch):
    monkeypatch.setenv("INSTANCE_NAME", WORKER)
    monkeypatch.setattr(
        claude_cli, "MessageParseError", type("MessageParseError", (Exception,), {})
    )


# ── the parser keeps the CLI's flags ────────────────────────────────────────


def test_parser_traegt_api_fehler_ueber_die_sdk():
    roh_a = {
        "type": "assistant",
        "error": "unknown",
        "is_api_error_message": True,
        "message": {
            "content": [{"type": "text", "text": API_TEXT}],
            "model": "<synthetic>",
            "stop_reason": "stop_sequence",
        },
        "parent_tool_use_id": None,
        "session_id": "s",
        "uuid": "u",
    }
    roh_r = {
        "type": "result",
        "subtype": "success",
        "is_error": True,
        "num_turns": 1,
        "api_error_status": 400,
        "terminal_reason": "api_error",
        "result": API_TEXT,
        "duration_ms": 1,
        "duration_api_ms": 0,
        "session_id": "s",
    }

    def sdk_parse(data):
        if data["type"] == "assistant":
            return sdk.AssistantMessage(
                content=[
                    sdk.TextBlock(text=b["text"]) for b in data["message"]["content"]
                ],
                model=data["message"]["model"],
            )
        return sdk.result("success", is_error=True, num_turns=1)

    a = sdk.chunk(resilient_parse_message(roh_a, sdk_parse))
    r = sdk.chunk(resilient_parse_message(roh_r, sdk_parse))
    assert a["is_api_error_message"] is True and a["cli_error"] == "unknown"
    assert r["api_error_status"] == 400 and r["terminal_reason"] == "api_error"


# ── detection: the flag, not the words ─────────────────────────────────────


def test_flag_ist_das_signal():
    d = claude_cli.detect_cli_api_error(_api_error_turn())
    assert d == {"status": None, "cli_error": "unknown", "text": API_TEXT}
    d = claude_cli.detect_cli_api_error(_api_error_result())
    assert d["status"] == 400 and d["text"] == API_TEXT


def test_text_ohne_flag_ist_inhalt():
    """A model answer that quotes "API Error: 400" is content."""
    msg = sdk.AssistantMessage(content=[sdk.TextBlock(text=API_TEXT)], model="m")
    assert claude_cli.detect_cli_api_error(msg) is None
    assert (
        claude_cli.detect_cli_api_error(
            {"type": "assistant", "content": [{"text": API_TEXT}]}
        )
        is None
    )


def test_ausgabegrenze_bleibt_beim_antwort_ende():
    """max_output_tokens has its own loud path (BRTRUNC antwort_ende)."""
    assert (
        claude_cli.detect_cli_api_error(_api_error_turn("cut", "max_output_tokens"))
        is None
    )
    r = _api_error_result()
    r._bridge_api_error = "max_output_tokens"
    assert claude_cli.detect_cli_api_error(r) is None


def test_gesunder_result_ist_kein_fehler():
    assert claude_cli.detect_cli_api_error(sdk.result()) is None
    assert (
        claude_cli.detect_cli_api_error(sdk.result("error_max_turns", is_error=True))
        is None
    )
    assert (
        claude_cli.detect_cli_api_error({"type": "user", "is_api_error_message": True})
        is None
    )


# ── verdict: status and cause ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "status,wire,retryable",
    [
        (400, 400, False),
        (404, 404, False),
        (413, 413, False),
        (401, 424, False),
        (403, 424, False),
        (429, 429, True),
        (500, 429, True),
        (529, 429, True),
        (None, 429, True),
    ],
)
def test_urteil_traegt_status_und_ursache(status, wire, retryable):
    resp = classify_exception(CliApiError(status, "unknown", API_TEXT, WORKER))
    body = json.loads(resp.body)["error"]
    assert resp.status_code == wire
    assert body["retryable"] is retryable
    assert body["upstream_status"] == status
    assert "cache_control.ttl" in body["message"]
    assert body["source"] == "upstream_anthropic"


# ── the real run_completion ─────────────────────────────────────────────────


def _make_cli(tmp_path: Path) -> ClaudeCodeCLI:
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


async def test_run_completion_wirft_statt_fehlertext_zu_liefern(monkeypatch, tmp_path):
    monkeypatch.setattr(
        claude_cli, "query", _query(sdk.init(), _api_error_turn(), _api_error_result())
    )
    got = []
    with pytest.raises(CliApiError) as ei:
        async for c in _make_cli(tmp_path).run_completion(prompt="Hallo", model="m"):
            got.append(c)
    assert not any("API Error" in str(c) for c in got)
    assert ei.value.status == 400  # from the result that follows the turn
    assert "cache_control.ttl" in str(ei.value)


async def test_run_completion_nur_result_reicht(monkeypatch, tmp_path):
    """A CLI that flags only the result (no synthetic turn) still fails loud."""
    monkeypatch.setattr(
        claude_cli,
        "query",
        _query(sdk.init(), _api_error_result(529, "API Error: 529 overloaded")),
    )
    with pytest.raises(CliApiError) as ei:
        async for _ in _make_cli(tmp_path).run_completion(prompt="Hallo", model="m"):
            pass
    assert ei.value.status == 529


# ── callers: every answer path ──────────────────────────────────────────────


def _echter_cli(monkeypatch, tmp_path, *messages):
    cli = _make_cli(tmp_path)
    monkeypatch.setattr(claude_cli, "query", _query(*messages))
    monkeypatch.setattr(main.claude_cli, "run_completion", cli.run_completion)


def _chat(monkeypatch, tmp_path, stream: bool):
    from starlette.testclient import TestClient

    from src.middleware.adaptive_limiter import adaptive_limit_dependency

    monkeypatch.setenv("API_KEY", "")
    monkeypatch.setenv("CLAUDE_SKIP_AUTH", "1")
    _echter_cli(
        monkeypatch, tmp_path, sdk.init(), _api_error_turn(), _api_error_result()
    )

    async def _kein_limiter():
        return None

    main.app.dependency_overrides[adaptive_limit_dependency] = _kein_limiter
    try:
        with (
            patch(
                "src.main.validate_claude_code_auth",
                return_value=(True, {"method": "test"}),
            ),
            patch("src.main.verify_api_key", new_callable=AsyncMock),
            patch("src.main.enforce_pool_admission", new_callable=AsyncMock),
            patch("src.main._cross_worker_retry", new=AsyncMock(return_value=None)),
            patch(
                "src.providers.fallback.get_fallback_tiers",
                side_effect=lambda t, **kw: [t],
            ),
        ):
            return TestClient(main.app, raise_server_exceptions=False).post(
                "/v1/chat/completions",
                json={
                    "model": "claude-sonnet-4-5",
                    "stream": stream,
                    "messages": [{"role": "user", "content": "ping"}],
                },
            )
    finally:
        main.app.dependency_overrides.clear()
        # slowapi counts chat requests per minute for the whole process; give
        # the ones made here back so later test files keep their quota.
        if main.limiter is not None:
            main.limiter.reset()


@pytest.mark.parametrize("stream", [False, True])
def test_chat_antwortet_400_statt_200(monkeypatch, tmp_path, stream):
    resp = _chat(monkeypatch, tmp_path, stream)
    assert resp.status_code == 400, resp.text[:300]
    err = resp.json()["error"]
    assert err["retryable"] is False and err["upstream_status"] == 400
    assert "cache_control.ttl" in err["message"]


async def test_streaming_nach_erstem_chunk_meldet_ursache(monkeypatch, tmp_path):
    """After the 200 is out: event: error with status and cause, no [DONE]."""
    from src.models import ChatCompletionRequest, Message

    vorher = sdk.AssistantMessage(content=[sdk.TextBlock(text="Teil 1")], model="m")
    _echter_cli(
        monkeypatch,
        tmp_path,
        sdk.init(),
        vorher,
        _api_error_turn(),
        _api_error_result(),
    )
    req = ChatCompletionRequest(
        model="claude-sonnet-5",
        messages=[Message(role="user", content="hi")],
        stream=True,
    )
    out = [c async for c in main.generate_streaming_response(req, "req-cachefix")]
    assert "data: [DONE]\n\n" not in out
    assert not any("API Error" in c for c in out if c.startswith("data: "))
    errs = [c for c in out if c.startswith("event: error\n")]
    assert len(errs) == 1
    err = json.loads(errs[0].split("data: ", 1)[1])["error"]
    assert err["upstream_status"] == 400 and err["retryable"] is False
    assert "cache_control.ttl" in err["message"]


async def test_doc_agent_meldet_fehler(monkeypatch, tmp_path):
    from src.models import DocAgentFile, DocAgentRequest

    _echter_cli(
        monkeypatch, tmp_path, sdk.init(), _api_error_turn(), _api_error_result()
    )
    monkeypatch.setattr(
        "src.activity.ai_call_writer.persist_ai_call_activity", AsyncMock()
    )
    req = DocAgentRequest(
        question="Was steht drin?", files=[DocAgentFile(name="a.txt", content="x")]
    )
    res = await main._execute_doc_agent_impl(req)
    assert (
        res.status == "error"
        and "cache_control.ttl" in res.error
        and "400" in res.error
    )


async def test_research_meldet_fehler(monkeypatch, tmp_path):
    _echter_cli(
        monkeypatch, tmp_path, sdk.init(), _api_error_turn(), _api_error_result()
    )
    monkeypatch.setattr(
        "src.activity.ai_call_writer.persist_ai_call_activity", AsyncMock()
    )
    req = MagicMock()
    for k, v in dict(
        query="Kennwerte von X-100?",
        model="claude-sonnet-4-5",
        depth="quick",
        strategy="planning",
        max_turns=10,
        max_hops=None,
        confidence_threshold=0.7,
        parallel_searches=5,
        source_filter=None,
        output_path=None,
        async_mode=False,
        backend=None,
        privacy=None,
        bedrock_region=None,
        research_mode=None,
        antwort_im_text=True,
    ).items():
        setattr(req, k, v)
    res = await main._execute_research_impl(req, None, request=MagicMock())
    assert res.status == "error"
    assert "cache_control.ttl" in res.error and "400" in res.error
    assert res.fehler_code is None and res.retryable is False


# ── CACHEFIX2: the doc-agent job ends as an error with the chat verdict ─────


@pytest.mark.parametrize(
    "status,retryable,after", [(400, False, None), (529, True, 30)]
)
async def test_doc_agent_job_endet_als_fehler_mit_urteil(
    monkeypatch, tmp_path, status, retryable, after
):
    """Real doc-agent answer → real executor → real job runner: mark_error
    with the CliApiError verdict (as /v1/chat/completions answers it), never
    mark_done."""
    from src.jobs import registry
    from src.jobs.executors import doc_agent_executor
    from src.models import DocAgentFile, DocAgentRequest

    _echter_cli(
        monkeypatch,
        tmp_path,
        sdk.init(),
        _api_error_turn(f"API Error: {status} cache_control.ttl"),
        _api_error_result(status, f"API Error: {status} cache_control.ttl"),
    )
    monkeypatch.setattr(
        "src.activity.ai_call_writer.persist_ai_call_activity", AsyncMock()
    )
    req = DocAgentRequest(
        question="Was steht drin?", files=[DocAgentFile(name="a.txt", content="x")]
    )
    body = (await main._execute_doc_agent_impl(req)).model_dump()
    chat = json.loads(
        classify_exception(CliApiError(status, "unknown", "x", WORKER)).body
    )["error"]
    assert (body["retryable"], body["retry_after_s"]) == (
        chat["retryable"],
        chat["retry_after_s"],
    )

    done, error = AsyncMock(), AsyncMock()
    with (
        patch("src.jobs.executors._self_post_json", AsyncMock(return_value=body)),
        patch.object(registry, "get_executor", return_value=doc_agent_executor),
        patch.object(registry.store_client, "mark_done", done),
        patch.object(registry.store_client, "mark_error", error),
        patch.object(registry.store_client, "heartbeat", AsyncMock()),
        patch.object(registry.store_client, "update_progress", AsyncMock()),
    ):
        await registry._run_body("job-cachefix2", "doc-agent", {}, None)
    assert not done.called
    (job_id, msg), kw = error.call_args
    assert job_id == "job-cachefix2"
    assert f"API Error: {status} cache_control.ttl" in msg
    assert kw["retryable"] is retryable and kw["retry_after_s"] == after
