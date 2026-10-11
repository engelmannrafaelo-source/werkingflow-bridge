from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src import main
from src.middleware.bridge_error import CliApiError
from src.models import ResearchRequest
from tests import test_research_usage_rusage
from tests.research_cloud import test_luecken_pool
from tests.test_research_usage_rusage import (
    CLI_USAGE,
    _api_fehler,
    _body,
    _erfolg,
    _job,
)

# pytest fixtures used by name in the tests below
bridge = test_research_usage_rusage.bridge
persist = test_luecken_pool.persist
pplx_on = test_luecken_pool.pplx_on


@pytest.mark.parametrize("failure", [False, True])
async def test_pool_ledger_equals_response(bridge, failure):
    bridge(_api_fehler() if failure else _erfolg())
    with patch("src.activity.ai_call_writer.persist_ai_call_activity", new_callable=AsyncMock) as ledger:
        done, error = await _job(_body())
    result = error.await_args.kwargs if failure else done.await_args.args[1]
    usage = result["usage"]
    ledger.assert_awaited_once()
    booked = ledger.await_args.kwargs
    assert booked["input_tokens"] == usage["input_tokens"]
    assert booked["output_tokens"] == usage["output_tokens"]
    assert booked.get("cache_read_tokens", 0) == usage["cache_read_input_tokens"]
    assert booked.get("cache_creation_tokens", 0) == usage["cache_creation_input_tokens"]

async def test_gap_error_retains_withheld_usage(monkeypatch, tmp_path):
    usage = {"input_tokens": 11, "output_tokens": 22, "cache_read_tokens": 33, "cache_creation_tokens": 44}
    async def stream(**kwargs):
        err = CliApiError(529, "overloaded_error", "API Error: 529", "review")
        err.usage = usage
        raise err
        yield
    monkeypatch.setattr(main.claude_cli, "run_completion", stream)
    checker = SimpleNamespace(rueckrunde_fehler=None)
    with patch("src.research_cloud.luecken.baue_rueckrunden_auftrag", return_value="repair"):
        content, measured = await main._pool_luecken_rueckrunde(
            nachkontrolle=checker, offene=["gap"], bericht="original report",
            erste_chunks=[], cli_resume_id="review", workdir=tmp_path,
            report_file=None, output_file=None,
            pool_pplx=SimpleNamespace(max_uses=3, calls=0), pool_doc=None,
            sdk_mcp_servers=None, append_system_prompt=None,
            request_body=ResearchRequest(**_body()), backend_config=None)
    assert content == "original report"
    assert checker.rueckrunde_fehler
    assert measured == usage

async def _cloud(monkeypatch, responses):
    from src.research_cloud import executor
    client = MagicMock()
    def response(body):
        r = MagicMock(status_code=200)
        r.json.return_value = body
        return r
    client.post = AsyncMock(side_effect=[response(r) for r in responses])
    original = executor.run_research_cloud
    async def run(*args, **kwargs):
        return await original(*args, **kwargs, api_key="review-fake", client=client)
    monkeypatch.setattr(executor, "run_research_cloud", run)
    with patch("src.research_cloud.anonymize_gate.anonymize_query_for_cloud", new=AsyncMock(return_value="query")), patch("src.activity.ai_call_writer.persist_ai_call_activity", new_callable=AsyncMock) as ledger:
        result = await main._execute_research_cloud_impl(MagicMock(), ResearchRequest(**_body()))
    return result, ledger

async def test_cloud_missing_usage_is_null(monkeypatch):
    result, _ = await _cloud(monkeypatch, [{"model": "claude-sonnet-5", "stop_reason": "end_turn", "content": [{"type":"text", "text":"report"}]}])
    assert result.status == "success", result.error
    assert result.usage is None, result.usage
    assert result.usage_fehlt

async def test_cloud_sum_matches_ledger(monkeypatch):
    responses = [
        {"model": "claude-sonnet-5", "stop_reason": "pause_turn", "container":{"id":"review-container"}, "usage":CLI_USAGE, "content":[]},
        {"model": "claude-sonnet-5", "stop_reason": "end_turn", "usage":CLI_USAGE, "content":[{"type":"text", "text":"report"}]}]
    result, ledger = await _cloud(monkeypatch, responses)
    assert result.status == "success", result.error
    ledger.assert_awaited_once()
    for external, internal in [("input_tokens","input_tokens"),("output_tokens","output_tokens"),("cache_read_input_tokens","cache_read_tokens"),("cache_creation_input_tokens","cache_creation_tokens")]:
        assert getattr(result.usage, external) == CLI_USAGE[external] * 2
        assert ledger.await_args.kwargs[internal] == getattr(result.usage, external)


async def test_repair_and_gap_rounds_counted_once(tmp_path, persist, pplx_on):
    from tests.research_cloud.test_luecken_pool import FakeCli, _run
    result = await _run(FakeCli(tmp_path))
    assert result.status == "success", result.error
    assert result.usage.input_tokens == 100 + 20 + 30
    assert result.usage.output_tokens == 200 + 10 + 40
    assert persist.await_args.kwargs["input_tokens"] == result.usage.input_tokens
    assert persist.await_args.kwargs["output_tokens"] == result.usage.output_tokens

async def test_cloud_failure_reports_missing_reason(monkeypatch):
    from src.research_cloud.executor import ResearchCloudExecutorError
    with patch("src.research_cloud.executor.run_research_cloud", new=AsyncMock(side_effect=ResearchCloudExecutorError("failure", status_code=529))), patch("src.research_cloud.anonymize_gate.anonymize_query_for_cloud", new=AsyncMock(return_value="query")), patch("src.activity.ai_call_writer.persist_ai_call_activity", new_callable=AsyncMock):
        result = await main._execute_research_cloud_impl(MagicMock(), ResearchRequest(**_body()))
    assert result.status == "error"
    assert result.retryable is True
    assert result.usage is None
    assert result.usage_fehlt == "research_cloud_fehler"
