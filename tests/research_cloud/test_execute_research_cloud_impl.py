"""Tests for src.main._execute_research_cloud_impl — the research-cloud
counterpart to _execute_research_impl. Verifies:
  - formgleiche success response (same ResearchResponse fields as the pool
    path; apps must not be able to distinguish the serving path)
  - fail-loud anonymize refusal -> status="error", never silently proceeds
  - fail-loud executor error -> status="error", never silent pool fallback
  - output_path is still honored (Bridge writes the file itself, no CLI
    filesystem discovery on this path)
"""
import sys
from unittest.mock import AsyncMock, MagicMock as _MagicMock

for _mod_name in [
    "claude_code_sdk",
    "claude_code_sdk._errors",
    "claude_code_sdk._internal",
    "claude_code_sdk._internal.client",
    "src.identity.routes",
    "src.db.client",
]:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = _MagicMock()

from unittest.mock import MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

import src.main  # noqa: E402
from src.models import ResearchResponse  # noqa: E402
from src.research_cloud.anonymize_gate import CloudAnonymizeError  # noqa: E402
from src.research_cloud.executor import ResearchCloudExecutorError  # noqa: E402
from src.research_cloud.models import ResearchCloudResult, ResearchCloudUsage  # noqa: E402


def _make_req(**kwargs):
    defaults = dict(
        query="Was regelt ÖNORM H 5155?",
        model="claude-sonnet-5",
        depth="standard",
        strategy="planning",
        output_path=None,
        research_mode="standard",
    )
    defaults.update(kwargs)
    ns = MagicMock()
    for k, v in defaults.items():
        setattr(ns, k, v)
    return ns


def _cloud_result(**kwargs):
    defaults = dict(
        status="success",
        content="# Report\n\nBefund...",
        model="claude-sonnet-5",
        usage=ResearchCloudUsage(input_tokens=1000, output_tokens=500),
        searches=5,
        fetches=2,
        iterations=1,
        stop_reason="end_turn",
        duration_seconds=12.3,
        container_id="container-abc",
    )
    defaults.update(kwargs)
    return ResearchCloudResult(**defaults)


@pytest.mark.asyncio
async def test_success_response_is_formgleich_to_pool_path():
    req = _make_req()
    with (
        patch(
            "src.research_cloud.anonymize_gate.anonymize_query_for_cloud",
            new=AsyncMock(return_value="ANON_ query"),
        ),
        patch(
            "src.research_cloud.executor.run_research_cloud",
            new=AsyncMock(return_value=_cloud_result()),
        ),
        patch("src.activity.ai_call_writer.persist_ai_call_activity", new=AsyncMock()),
        patch("src.scholarly.scholarly_enabled", return_value=False),
    ):
        result = await src.main._execute_research_cloud_impl(MagicMock(), req, attribution_ctx={})

    assert isinstance(result, ResearchResponse)
    # Same field surface as any other ResearchResponse — apps can't
    # distinguish the serving path structurally.
    assert set(result.model_dump().keys()) == set(ResearchResponse.model_fields.keys())
    assert result.status == "success"
    assert result.query == req.query  # original (non-anonymized) query echoed back
    assert result.model == "claude-sonnet-5"
    assert result.content == "# Report\n\nBefund..."
    assert result.error is None
    assert result.execution_time_seconds == 12.3


@pytest.mark.asyncio
async def test_anonymize_failure_aborts_with_error_status_no_pool_fallback():
    req = _make_req()
    executor_mock = AsyncMock()
    with (
        patch(
            "src.research_cloud.anonymize_gate.anonymize_query_for_cloud",
            new=AsyncMock(side_effect=CloudAnonymizeError("privacy-service down")),
        ),
        patch("src.research_cloud.executor.run_research_cloud", executor_mock),
    ):
        result = await src.main._execute_research_cloud_impl(MagicMock(), req, attribution_ctx={})

    assert result.status == "error"
    assert "anonymization" in result.error
    assert result.query == req.query
    assert result.model == req.model
    executor_mock.assert_not_awaited()  # never reaches the cloud call


@pytest.mark.asyncio
async def test_executor_failure_returns_job_error_not_silent_fallback():
    req = _make_req()
    with (
        patch(
            "src.research_cloud.anonymize_gate.anonymize_query_for_cloud",
            new=AsyncMock(return_value="ANON_ query"),
        ),
        patch(
            "src.research_cloud.executor.run_research_cloud",
            new=AsyncMock(side_effect=ResearchCloudExecutorError("HTTP 429: rate limited")),
        ),
        patch("src.activity.ai_call_writer.persist_ai_call_activity", new=AsyncMock()),
        patch("src.scholarly.scholarly_enabled", return_value=False),
    ):
        result = await src.main._execute_research_cloud_impl(MagicMock(), req, attribution_ctx={})

    assert result.status == "error"
    assert "429" in result.error


@pytest.mark.asyncio
async def test_transient_upstream_status_marks_error_retryable():
    """A capacity-shaped upstream failure (429/5xx) must be recognizable as
    retryable by the app-side classifier (werking-report
    transient-infra-error.ts isTransientInfraError) — Rafael 2026-08-02: a
    surfaced cloud error that isn't recognizable as retryable becomes a hard
    failure instead of a deferred one, which is worse than today's silent
    pool fallback it replaces."""
    req = _make_req()
    with (
        patch(
            "src.research_cloud.anonymize_gate.anonymize_query_for_cloud",
            new=AsyncMock(return_value="ANON_ query"),
        ),
        patch(
            "src.research_cloud.executor.run_research_cloud",
            new=AsyncMock(side_effect=ResearchCloudExecutorError(
                "research-cloud Messages API call failed: HTTP 529: overloaded_error",
                status_code=529,
            )),
        ),
        patch("src.activity.ai_call_writer.persist_ai_call_activity", new=AsyncMock()),
        patch("src.scholarly.scholarly_enabled", return_value=False),
    ):
        result = await src.main._execute_research_cloud_impl(MagicMock(), req, attribution_ctx={})

    assert result.status == "error"
    # 529 itself is not in the app's regex — the Bridge must normalize to a
    # marker the app DOES recognize (the literal digits survive job-layer
    # JSON re-stringification, unlike a quoted "retryable": true substring).
    import re
    assert re.search(r"\b503\b", result.error)


@pytest.mark.asyncio
async def test_permanent_upstream_status_is_not_marked_retryable():
    """A deterministic 400 (bad request / code bug) must stay a hard failure
    — marking it retryable would mask a real bug behind a pointless 20h
    app-side wait loop that will never succeed."""
    req = _make_req()
    with (
        patch(
            "src.research_cloud.anonymize_gate.anonymize_query_for_cloud",
            new=AsyncMock(return_value="ANON_ query"),
        ),
        patch(
            "src.research_cloud.executor.run_research_cloud",
            new=AsyncMock(side_effect=ResearchCloudExecutorError(
                "research-cloud Messages API call failed: HTTP 400: bad request",
                status_code=400,
            )),
        ),
        patch("src.activity.ai_call_writer.persist_ai_call_activity", new=AsyncMock()),
        patch("src.scholarly.scholarly_enabled", return_value=False),
    ):
        result = await src.main._execute_research_cloud_impl(MagicMock(), req, attribution_ctx={})

    assert result.status == "error"
    assert "retryable" not in result.error
    import re
    assert not re.search(r"\b503\b", result.error)


@pytest.mark.asyncio
async def test_anonymize_gate_failure_is_not_marked_retryable():
    """A compliance/config failure (e.g. privacy service refuses the
    anonymize gate) has no status_code to classify as transient — it must
    stay a hard failure, same as before this change."""
    req = _make_req()
    with (
        patch(
            "src.research_cloud.anonymize_gate.anonymize_query_for_cloud",
            new=AsyncMock(side_effect=CloudAnonymizeError("privacy-service down")),
        ),
        patch("src.research_cloud.executor.run_research_cloud", AsyncMock()),
    ):
        result = await src.main._execute_research_cloud_impl(MagicMock(), req, attribution_ctx={})

    assert result.status == "error"
    assert "retryable" not in result.error


@pytest.mark.asyncio
async def test_output_path_is_written_by_bridge(tmp_path):
    out_path = tmp_path / "report.md"
    req = _make_req(output_path=str(out_path))
    with (
        patch(
            "src.research_cloud.anonymize_gate.anonymize_query_for_cloud",
            new=AsyncMock(return_value="ANON_ query"),
        ),
        patch(
            "src.research_cloud.executor.run_research_cloud",
            new=AsyncMock(return_value=_cloud_result(content="written content")),
        ),
        patch("src.activity.ai_call_writer.persist_ai_call_activity", new=AsyncMock()),
        patch("src.scholarly.scholarly_enabled", return_value=False),
    ):
        result = await src.main._execute_research_cloud_impl(MagicMock(), req, attribution_ctx={})

    assert result.output_file == str(out_path)
    assert out_path.read_text() == "written content"
    assert result.file_size_bytes == len("written content".encode("utf-8"))


# ---------------------------------------------------------------------------
# Library resolution happens BEFORE the executor runs (2026-09-05): a library
# that is switched on and unusable stops the research instead of quietly
# producing a web-only answer that reports success.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unusable_library_aborts_before_any_model_call():
    from src.research_cloud.library import LibraryUnavailableError

    req = _make_req()
    executor_mock = AsyncMock()
    with (
        patch(
            "src.research_cloud.anonymize_gate.anonymize_query_for_cloud",
            new=AsyncMock(return_value="ANON_ query"),
        ),
        patch(
            "src.research_cloud.library.load_library_for_run",
            new=AsyncMock(
                side_effect=LibraryUnavailableError(
                    "RESEARCH_LIBRARY_ENABLED is on, but the library is not configured — "
                    "empty or missing: RESEARCH_LIBRARY_S3_SECRET_ACCESS_KEY"
                )
            ),
        ),
        patch("src.research_cloud.executor.run_research_cloud", executor_mock),
        patch("src.scholarly.scholarly_enabled", return_value=False),
    ):
        result = await src.main._execute_research_cloud_impl(MagicMock(), req, attribution_ctx={})

    assert result.status == "error"
    assert "Bibliothek" in result.error
    assert "RESEARCH_LIBRARY_S3_SECRET_ACCESS_KEY" in result.error
    # The point of doing this first: nothing was spent.
    executor_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_loaded_catalogue_reaches_both_the_prompt_and_the_executor():
    """The catalogue in the prompt and the index answering library_index must
    be the same object — a prompt that advertises documents the tool cannot
    serve would be a new silent failure, not a fix for the old one."""
    index = {"documents": [{"id": "doc-a", "title": "Doc A", "jurisdiction": "AT"}]}
    req = _make_req()
    executor_mock = AsyncMock(return_value=_cloud_result())
    with (
        patch(
            "src.research_cloud.anonymize_gate.anonymize_query_for_cloud",
            new=AsyncMock(return_value="ANON_ query"),
        ),
        patch("src.research_cloud.library.load_library_for_run", new=AsyncMock(return_value=index)),
        patch("src.research_cloud.executor.run_research_cloud", executor_mock),
        patch("src.activity.ai_call_writer.persist_ai_call_activity", new=AsyncMock()),
        patch("src.scholarly.scholarly_enabled", return_value=False),
    ):
        await src.main._execute_research_cloud_impl(MagicMock(), req, attribution_ctx={})

    args, kwargs = executor_mock.await_args
    assert kwargs["library_index"] is index
    assert "`doc-a`" in args[1]  # system prompt carries the catalogue
