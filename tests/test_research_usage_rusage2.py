"""RUSAGE2: a cloud round without usage is not measured — neither a made-up 0
nor silently left out of a sum that looks complete."""
from tests.test_research_usage_rusage import CLI_USAGE
from tests.test_rusager_review import _cloud

NULL_USAGE = {"input_tokens": 0, "output_tokens": 0,
              "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}


async def test_cloud_partial_usage_is_named(monkeypatch):
    responses = [
        {"model": "claude-sonnet-5", "stop_reason": "pause_turn", "container": {"id": "c"}, "usage": CLI_USAGE, "content": []},
        {"model": "claude-sonnet-5", "stop_reason": "end_turn", "content": [{"type": "text", "text": "report"}]}]
    result, ledger = await _cloud(monkeypatch, responses)
    assert result.status == "success", result.error
    assert result.usage.input_tokens == CLI_USAGE["input_tokens"]
    assert result.usage.cache_read_input_tokens == CLI_USAGE["cache_read_input_tokens"]
    assert result.usage_fehlt is None
    assert result.usage_unvollstaendig == "research_cloud_ohne_usage: 1 von 2 Runden ohne usage"
    assert ledger.await_args.kwargs["provider_meta"]["usage_runden_ohne_messung"] == 1


async def test_cloud_reported_zero_stays_measured(monkeypatch):
    result, _ = await _cloud(monkeypatch, [
        {"model": "claude-sonnet-5", "stop_reason": "end_turn", "usage": NULL_USAGE,
         "content": [{"type": "text", "text": "report"}]}])
    assert result.status == "success", result.error
    assert result.usage is not None and result.usage.input_tokens == 0
    assert result.usage_fehlt is None and result.usage_unvollstaendig is None
