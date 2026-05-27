"""
Tests for /v1/research token-tracking and budget gate (R1, R2, R3).

IMPORTANT: claude_code_sdk and heavy deps are stubbed at module-level so
this file can run without a Docker/Claude environment.

Coverage:
  R1-happy-sdk  — success + SDK token counts → persist called with those counts
  R1-happy-est  — success + no SDK tokens → persist called with estimated counts
  R1-error      — exception in run_completion → persist called with status=error
  R1-nonfatal   — persist raises → research response still returned
  R2-gate-sync  — trial_expired → enforce_budget raises 402 before run_completion
  R3-gate-async — async_mode + exhausted budget → 402, no job spawn
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# Stub claude_code_sdk and other heavy deps BEFORE any src.* import.
# pytest collects and runs module-level code in order, so stubs placed here
# prevent ModuleNotFoundError during collection.
# ---------------------------------------------------------------------------
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

# Stub deps that would need DB / identity services
for _mod_name in [
    "src.identity.routes",
    "src.db.client",
]:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = _MagicMock()

# ---------------------------------------------------------------------------
# Now safe to import test utilities and src modules
# ---------------------------------------------------------------------------
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

import src.main  # noqa: E402  — import after stubs


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_attr(**kwargs):
    defaults = dict(
        user_id="user-uuid-123",
        app_id="werking-report",
        agent_id=None,
        workflow_id=None,
        app_env="dev",
    )
    defaults.update(kwargs)
    return defaults


def _make_req(**kwargs):
    """Minimal ResearchRequest-like mock."""
    defaults = dict(
        query="test query",
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
    )
    defaults.update(kwargs)
    ns = MagicMock()
    for k, v in defaults.items():
        setattr(ns, k, v)
    return ns


async def _stream(*chunks):
    """Async generator yielding pre-built chunk dicts."""
    for c in chunks:
        yield c


RESULT_WITH_TOKENS = {
    "type": "result",
    "subtype": "success",
    "usage": {"input_tokens": 100, "output_tokens": 200},
    "total_cost_usd": 0.01,
    "duration_ms": 5000,
    "num_turns": 3,
}

RESULT_NO_TOKENS = {
    "type": "result",
    "subtype": "success",
    "total_cost_usd": 0.01,
    "duration_ms": 5000,
    "num_turns": 3,
}

# A minimal assistant chunk — no session_id so file-discovery skips cleanly
ASSISTANT_TEXT_CHUNK = {
    "type": "assistant",
    "content": [{"type": "text", "text": "Research result text."}],
}


# ---------------------------------------------------------------------------
# Shared patch context for _execute_research_impl tests.
#
# We replace:
#   - src.main.claude_cli     → MagicMock with controllable run_completion
#   - src.main.Path           → avoid real filesystem access
#   - shutil.copy2            → avoid real filesystem access
#   - src.claude_cli.detect_quota_exhaustion → always False (no quota hit)
#   - src.activity.ai_call_writer.persist_ai_call_activity → AsyncMock
# ---------------------------------------------------------------------------

def _base_cli_mock(chunks, parsed_text="Research result text."):
    mock = MagicMock()
    mock.run_completion = MagicMock(return_value=_stream(*chunks))
    mock.parse_claude_message = MagicMock(return_value=parsed_text)
    mock.cwd = "/tmp"
    return mock


# ---------------------------------------------------------------------------
# R1-happy: SDK-provided token counts
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_r1_happy_sdk_tokens():
    """Success + SDK usage → persist called with input_tokens=100, output_tokens=200."""
    mock_persist = AsyncMock()
    mock_cli = _base_cli_mock([ASSISTANT_TEXT_CHUNK, RESULT_WITH_TOKENS])

    with (
        patch("src.main.claude_cli", mock_cli),
        patch("src.claude_cli.detect_quota_exhaustion", return_value=False),
        patch("src.activity.ai_call_writer.persist_ai_call_activity", mock_persist),
        # No file discovery (no x_claude_metadata chunk, no session_id → skip dir scan)
        patch("src.main.FileDiscoveryService", MagicMock()),
    ):
        result = await src.main._execute_research_impl(_make_req(), None, attribution_ctx=_make_attr())

    mock_persist.assert_called_once()
    kw = mock_persist.call_args.kwargs
    assert kw["status"] == "success"
    assert kw["input_tokens"] == 100
    assert kw["output_tokens"] == 200
    assert kw["user_id"] == "user-uuid-123"
    assert kw["app_id"] == "werking-report"
    assert kw["agent_id"] == "research:planning"
    assert kw["app_env"] == "dev"


# ---------------------------------------------------------------------------
# R1-happy: estimated tokens when SDK omits usage
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_r1_happy_estimated_tokens():
    """Success + no SDK usage → persist called with estimated (>0) token counts."""
    mock_persist = AsyncMock()
    mock_cli = _base_cli_mock([ASSISTANT_TEXT_CHUNK, RESULT_NO_TOKENS])

    with (
        patch("src.main.claude_cli", mock_cli),
        patch("src.claude_cli.detect_quota_exhaustion", return_value=False),
        patch("src.activity.ai_call_writer.persist_ai_call_activity", mock_persist),
    ):
        result = await src.main._execute_research_impl(_make_req(), None, attribution_ctx=_make_attr())

    mock_persist.assert_called_once()
    kw = mock_persist.call_args.kwargs
    assert kw["status"] == "success"
    assert kw["input_tokens"] > 0, "Expected estimated input tokens > 0"
    assert kw["output_tokens"] > 0, "Expected estimated output tokens > 0"


# ---------------------------------------------------------------------------
# R1-error: exception in run_completion → error activity persisted
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_r1_error_path_persists_error_activity():
    """run_completion raises → persist called with status=error, no exception escapes."""
    mock_persist = AsyncMock()

    async def _boom():
        raise RuntimeError("SDK exploded")
        yield  # make it an async generator

    mock_cli = MagicMock()
    mock_cli.run_completion = MagicMock(return_value=_boom())
    mock_cli.cwd = "/tmp"

    with (
        patch("src.main.claude_cli", mock_cli),
        patch("src.activity.ai_call_writer.persist_ai_call_activity", mock_persist),
    ):
        result = await src.main._execute_research_impl(_make_req(), None, attribution_ctx=_make_attr())

    assert result.status == "error"
    mock_persist.assert_called_once()
    kw = mock_persist.call_args.kwargs
    assert kw["status"] == "error"
    assert kw["error_code"] == "research_error"
    assert kw["user_id"] == "user-uuid-123"


# ---------------------------------------------------------------------------
# R1: tracking failure is non-fatal (best-effort)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_r1_tracking_failure_does_not_break_response():
    """If persist_ai_call_activity raises, the research response is still returned."""
    mock_persist = AsyncMock(side_effect=Exception("DB down"))
    mock_cli = _base_cli_mock([ASSISTANT_TEXT_CHUNK, RESULT_WITH_TOKENS])

    with (
        patch("src.main.claude_cli", mock_cli),
        patch("src.claude_cli.detect_quota_exhaustion", return_value=False),
        patch("src.activity.ai_call_writer.persist_ai_call_activity", mock_persist),
    ):
        result = await src.main._execute_research_impl(_make_req(), None, attribution_ctx=_make_attr())

    # persist was attempted (and raised), but the result is still usable
    mock_persist.assert_called_once()
    assert result is not None


# ---------------------------------------------------------------------------
# R2: budget gate blocks exhausted users (sync path)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_r2_gate_blocks_sync_path():
    """enforce_budget raises 402 → research() propagates it; run_completion not called."""
    from fastapi import HTTPException, Request as FRequest

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/research",
        "headers": [
            (b"x-user-id", b"user-uuid-123"),
            (b"x-app-id", b"werking-report"),
            (b"authorization", b"Bearer test-key"),
        ],
        "query_string": b"",
    }
    fake_request = FRequest(scope)

    mock_run = AsyncMock()

    with (
        patch("src.main.verify_api_key", AsyncMock()),
        patch("src.main.enforce_attribution", MagicMock()),
        patch("src.main.resolve_model", return_value=("claude-sonnet-4-5", "ok")),
        patch("src.main.resolve_backend_config", return_value=None),
        # Gate raises 402 (enforce_budget is a local import inside research(), patch at source)
        patch("src.budget.gate.enforce_budget", AsyncMock(
            side_effect=HTTPException(status_code=402, detail={"error": "budget_exhausted", "reason": "trial_expired"})
        )),
        patch("src.main._execute_research_impl", mock_run),
    ):
        req_body = _make_req(async_mode=False)
        creds = MagicMock()

        with pytest.raises(HTTPException) as exc:
            await src.main.research(req_body, fake_request, credentials=creds)

        assert exc.value.status_code == 402
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# R3: budget gate blocks async job spawn
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_r3_gate_blocks_async_job_spawn():
    """async_mode=True + exhausted budget → 402 before _save_research_job/create_task."""
    from fastapi import HTTPException, Request as FRequest

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/research",
        "headers": [
            (b"x-user-id", b"user-uuid-123"),
            (b"x-app-id", b"werking-report"),
            (b"authorization", b"Bearer test-key"),
        ],
        "query_string": b"",
    }
    fake_request = FRequest(scope)

    mock_save_job = MagicMock()
    mock_create_task = MagicMock()

    with (
        patch("src.main.verify_api_key", AsyncMock()),
        patch("src.main.enforce_attribution", MagicMock()),
        patch("src.main.resolve_model", return_value=("claude-sonnet-4-5", "ok")),
        patch("src.main.resolve_backend_config", return_value=None),
        # Gate raises 402 (enforce_budget is a local import inside research(), patch at source)
        patch("src.budget.gate.enforce_budget", AsyncMock(
            side_effect=HTTPException(status_code=402, detail={"error": "budget_exhausted"})
        )),
        patch("src.main._save_research_job", mock_save_job),
        patch("src.main.asyncio") as mock_asyncio,
    ):
        mock_asyncio.create_task = mock_create_task

        req_body = _make_req(async_mode=True)
        creds = MagicMock()

        with pytest.raises(HTTPException) as exc:
            await src.main.research(req_body, fake_request, credentials=creds)

        assert exc.value.status_code == 402
        mock_save_job.assert_not_called()
        mock_create_task.assert_not_called()
