"""BR4 (10.10.2026): an account limit is recognised by the FORM of the CLI's
message, at one place (run_completion), and takes the limit path.

Befund (BR2R3): the limit-text check in run_completion tested
`type(message).__name__ == 'AssistantMessage'` AFTER the dict conversion and
never ran. On the streaming chat path the CLI's limit sentence ("You've hit
your limit · resets 3pm", "Anthropic seven_day limit hit") went out as normal
content with HTTP 200 and the exhausted worker stayed in the pool. And a
RateLimitError raised from a rate_limit_event "rejected" was swallowed by
run_completion's generic except (none of its indicators match "... limit
hit") and came out as an error_during_execution chunk.

Signals the CLI really sends (CLI 2.1.295 SDK schema, read from the binary):
  - assistant turn with top-level `"error": "rate_limit" | "billing_error"`
    (SDKAssistantMessageError); claude-code-sdk 0.0.2x drops the field, the
    resilient parser keeps it as `_bridge_cli_error`;
  - rate_limit_event with rate_limit_info.status "rejected";
  - the ResultMessage only says is_error — no limit field survives the SDK.

Streams here use the stand-ins of tests/sdk_strom.py (SDK class names and
fields) and run through the REAL run_completion; only `query` is replaced.
Limits are reproduced, never requested from Claude.
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
import src.middleware.capacity_lock as capacity_lock_mod  # noqa: E402
from src.claude_cli import (  # noqa: E402
    ClaudeCodeCLI,
    RateLimitError,
    detect_account_limit,
    rate_limit_tracker,
)
from src.sdk_parser import RateLimitEvent, resilient_parse_message  # noqa: E402
from tests import sdk_strom as sdk  # noqa: E402

WORKER = "worker-br4"
# Wordings the CLI writes; the second one no phrase list of the bridge knows.
LIMIT_TEXT = "You've hit your limit · resets 3pm (Europe/Berlin)"
LIMIT_TEXT_UNBEKANNT = "Anthropic seven_day limit hit"


def _limit_turn(text: str = LIMIT_TEXT, error: str = "rate_limit") -> sdk.AssistantMessage:
    """The CLI's synthetic limit answer as the patched SDK parser hands it on."""
    msg = sdk.AssistantMessage(content=[sdk.TextBlock(text=text)], model="<synthetic>")
    msg._bridge_cli_error = error
    return msg


def _result_limit(text: str = LIMIT_TEXT) -> sdk.ResultMessage:
    r = sdk.result("success", is_error=True)
    r.result = text
    return r


@pytest.fixture(autouse=True)
def frischer_zustand(tmp_path, monkeypatch):
    monkeypatch.setenv("INSTANCE_NAME", WORKER)
    # Under the MagicMock SDK, MessageParseError is no exception class, and
    # run_completion's `except MessageParseError` would turn any exception
    # passing it into a TypeError. The real SDK error is a plain Exception.
    monkeypatch.setattr(claude_cli, "MessageParseError", type("MessageParseError", (Exception,), {}))
    monkeypatch.setattr(
        capacity_lock_mod.CapacityLock, "PERSIST_PATH", str(tmp_path / "cap_lock.json")
    )
    monkeypatch.setattr(capacity_lock_mod, "_INSTANCE", None)
    rate_limit_tracker._rate_limits.pop(WORKER, None)
    rate_limit_tracker._hard_limits.discard(WORKER)
    yield
    rate_limit_tracker._rate_limits.pop(WORKER, None)
    rate_limit_tracker._hard_limits.discard(WORKER)
    monkeypatch.setattr(capacity_lock_mod, "_INSTANCE", None)


# ── detection: the field, not the words ──────────────────────────────────────

@pytest.mark.parametrize("error", ["rate_limit", "billing_error"])
def test_feld_des_cli_ist_das_signal(error):
    assert detect_account_limit(_limit_turn(error=error)) == f"cli_error:{error}"
    assert detect_account_limit({"type": "assistant", "error": error}) == f"cli_error:{error}"


def test_unbekannter_wortlaut_mit_feld_ist_limit():
    assert detect_account_limit(_limit_turn(LIMIT_TEXT_UNBEKANNT)) == "cli_error:rate_limit"


@pytest.mark.parametrize("error", ["oauth_org_not_allowed", "server_error", "overloaded",
                                   "max_output_tokens", "invalid_request", None])
def test_andere_cli_fehler_sind_kein_limit(error):
    msg = sdk.AssistantMessage(content=[sdk.TextBlock(text="x")], model="<synthetic>")
    if error is not None:
        msg._bridge_cli_error = error
    assert detect_account_limit(msg) is None


def test_text_ohne_feld_ist_kein_limit():
    """A model answer that QUOTES the sentence is content, not a limit."""
    msg = sdk.AssistantMessage(content=[sdk.TextBlock(text=LIMIT_TEXT)], model="m")
    assert detect_account_limit(msg) is None
    assert detect_account_limit({"content": [{"type": "text", "text": LIMIT_TEXT}]}) is None


def test_nutzerzug_und_fremde_dicts_zaehlen_nie():
    assert detect_account_limit({"type": "user", "error": "rate_limit"}) is None
    assert detect_account_limit({"error": "rate_limit"}) is None
    assert detect_account_limit(sdk.chunk(sdk.init())) is None
    assert detect_account_limit(sdk.chunk(_result_limit())) is None
    assert detect_account_limit(None) is None


def test_parser_traegt_das_feld_ueber_die_sdk():
    """claude-code-sdk builds AssistantMessage(content, model, parent_tool_use_id)
    and drops `error`; the resilient parser must keep it."""
    roh = {
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": LIMIT_TEXT}], "model": "<synthetic>"},
        "parent_tool_use_id": None, "session_id": "s", "uuid": "u", "error": "rate_limit",
    }

    def sdk_parse(data):
        return sdk.AssistantMessage(
            content=[sdk.TextBlock(text=b["text"]) for b in data["message"]["content"]],
            model=data["message"]["model"], parent_tool_use_id=data.get("parent_tool_use_id"),
        )

    parsed = resilient_parse_message(roh, sdk_parse)
    assert detect_account_limit(parsed) == "cli_error:rate_limit"


# ── the real run_completion ──────────────────────────────────────────────────

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


async def _run(monkeypatch, tmp_path, *messages):
    monkeypatch.setattr(claude_cli, "query", _query(*messages))
    got = []
    with pytest.raises(RateLimitError) as ei:
        async for c in _make_cli(tmp_path).run_completion(prompt="Hallo", model="claude-sonnet-5"):
            got.append(c)
    return got, ei.value


@pytest.mark.parametrize("text", [LIMIT_TEXT, LIMIT_TEXT_UNBEKANNT])
async def test_run_completion_haelt_limittext_zurueck_und_parkt_worker(monkeypatch, tmp_path, text):
    got, err = await _run(monkeypatch, tmp_path, sdk.init(), _limit_turn(text), _result_limit(text))
    assert not any(text.lower() in str(c).lower() for c in got)
    assert got == [sdk.chunk(sdk.init())]  # only the init chunk went out
    assert rate_limit_tracker.is_hard_limited(WORKER)
    assert rate_limit_tracker.should_reject_new_request(WORKER)
    assert 0 < err.retry_after_seconds <= rate_limit_tracker.MAX_COOLDOWN_SECONDS


async def test_run_completion_billing_error(monkeypatch, tmp_path):
    _, err = await _run(monkeypatch, tmp_path, sdk.init(), _limit_turn("Credit balance is too low",
                                                                       "billing_error"))
    assert "billing_error" in str(err)
    assert rate_limit_tracker.is_hard_limited(WORKER)


async def test_rate_limit_event_rejected_kommt_als_limit_an(monkeypatch, tmp_path):
    """Before BR4 the generic except turned this into error_during_execution."""
    event = RateLimitEvent({
        "type": "rate_limit_event", "uuid": "u", "session_id": "s",
        "rate_limit_info": {"status": "rejected", "rateLimitType": "seven_day"},
    })
    got, err = await _run(monkeypatch, tmp_path, sdk.init(), event)
    assert "seven_day" in str(err)
    assert not any(isinstance(c, dict) and c.get("subtype") == "error_during_execution" for c in got)


async def test_gesunder_lauf_der_den_satz_zitiert_bleibt_inhalt(monkeypatch, tmp_path):
    monkeypatch.setattr(claude_cli, "query", _query(
        sdk.init(),
        sdk.AssistantMessage(content=[sdk.TextBlock(text=LIMIT_TEXT)], model="m"),
        sdk.result(),
    ))
    got = [c async for c in _make_cli(tmp_path).run_completion(prompt="Hallo", model="m")]
    assert any(LIMIT_TEXT in str(c) for c in got)
    assert not rate_limit_tracker.is_rate_limited(WORKER)


# ── callers: the limit path, never content ───────────────────────────────────

def _echter_cli(monkeypatch, tmp_path, *messages):
    cli = _make_cli(tmp_path)
    monkeypatch.setattr(claude_cli, "query", _query(*messages))
    monkeypatch.setattr(main.claude_cli, "run_completion", cli.run_completion)


@pytest.mark.parametrize("text", [LIMIT_TEXT, LIMIT_TEXT_UNBEKANNT])
async def test_streaming_chat_meldet_limit_statt_inhalt(monkeypatch, tmp_path, text):
    from src.models import ChatCompletionRequest, Message

    _echter_cli(monkeypatch, tmp_path, sdk.init(), _limit_turn(text), _result_limit(text))
    metrics = MagicMock()
    monkeypatch.setattr("src.middleware.rolling_metrics.get_rolling_metrics", lambda: metrics)
    req = ChatCompletionRequest(model="claude-sonnet-5",
                                messages=[Message(role="user", content="hi")], stream=True)
    out = [c async for c in main.generate_streaming_response(req, "req-br4")]

    assert not any(text in c for c in out)
    assert "data: [DONE]\n\n" not in out
    errs = [c for c in out if c.startswith("event: error\n")]
    assert len(errs) == 1
    err = json.loads(errs[0].split("data: ", 1)[1])["error"]
    assert err["bridge_type"] == "account_exhausted" and err["code"] == "429"
    assert err["retryable"] is True and err["retry_after_s"] > 0
    assert err["bridge_worker"] == WORKER
    metrics.record_rate_limit.assert_called_once_with(WORKER)
    assert rate_limit_tracker.is_hard_limited(WORKER)


async def test_doc_agent_wirft_limit_statt_status_error(monkeypatch, tmp_path):
    from src.models import DocAgentFile, DocAgentRequest

    _echter_cli(monkeypatch, tmp_path, sdk.init(), _limit_turn(), _result_limit())
    monkeypatch.setattr("src.activity.ai_call_writer.persist_ai_call_activity", AsyncMock())
    req = DocAgentRequest(question="Was steht drin?", files=[DocAgentFile(name="a.txt", content="x")])
    with pytest.raises(RateLimitError):
        await main._execute_doc_agent_impl(req)


async def test_research_wirft_limit_statt_status_error(monkeypatch, tmp_path):
    _echter_cli(monkeypatch, tmp_path, sdk.init(), _limit_turn(), _result_limit())
    monkeypatch.setattr("src.activity.ai_call_writer.persist_ai_call_activity", AsyncMock())
    req = MagicMock()
    for k, v in dict(
        query="Kennwerte von X-100?", model="claude-sonnet-4-5", depth="quick",
        strategy="planning", max_turns=10, max_hops=None, confidence_threshold=0.7,
        parallel_searches=5, source_filter=None, output_path=None, async_mode=False,
        backend=None, privacy=None, bedrock_region=None, research_mode=None,
    ).items():
        setattr(req, k, v)
    with pytest.raises(RateLimitError):
        await main._execute_research_impl(req, None, request=MagicMock())


@pytest.mark.parametrize("text", [LIMIT_TEXT, LIMIT_TEXT_UNBEKANNT])
def test_sync_chat_antwortet_429_statt_200(monkeypatch, tmp_path, text):
    """Sync chat: run_completion raises, the chat handler classifies it as
    account_exhausted (classify_exception) — 429-class, retryable, no sentence."""
    from starlette.testclient import TestClient

    from src.middleware.adaptive_limiter import adaptive_limit_dependency

    monkeypatch.setenv("API_KEY", "")
    monkeypatch.setenv("CLAUDE_SKIP_AUTH", "1")
    _echter_cli(monkeypatch, tmp_path, sdk.init(), _limit_turn(text), _result_limit(text))

    async def _kein_limiter():
        return None

    main.app.dependency_overrides[adaptive_limit_dependency] = _kein_limiter
    try:
        with (
            patch("src.main.validate_claude_code_auth", return_value=(True, {"method": "test"})),
            patch("src.main.verify_api_key", new_callable=AsyncMock),
            patch("src.main.enforce_pool_admission", new_callable=AsyncMock),
            patch("src.main._cross_worker_retry", new=AsyncMock(return_value=None)),
            patch("src.providers.fallback.get_fallback_tiers", side_effect=lambda t, **kw: [t]),
        ):
            resp = TestClient(main.app, raise_server_exceptions=False).post(
                "/v1/chat/completions",
                json={"model": "claude-sonnet-4-5", "messages": [{"role": "user", "content": "ping"}]},
            )
    finally:
        main.app.dependency_overrides.clear()

    assert resp.status_code == 429, resp.text[:300]
    assert text not in resp.text
    err = resp.json()["error"]
    assert err["bridge_type"] == "account_exhausted" and err["retryable"] is True
    assert rate_limit_tracker.is_hard_limited(WORKER)
