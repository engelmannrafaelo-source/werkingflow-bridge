"""perplexity_search on the pool path (src/research_pool_perplexity.py) and its
wiring into _execute_research_impl.

Same rules as the cloud path: anonymize gate before anything leaves (a failure
ends the run as an error), fail-soft HTTP, budget, cost into the ledger row.
"""
from __future__ import annotations

import sys
from unittest.mock import MagicMock as _MagicMock

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

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

import src.main  # noqa: E402
import src.research_pool_perplexity as rpp  # noqa: E402
from src.research_cloud.perplexity import (  # noqa: E402
    PerplexityAnswer,
    PerplexityCallError,
    PerplexityConfig,
    PerplexitySource,
)

_CFG = PerplexityConfig(enabled=True, api_key="pplx-test")
_ANSWER = PerplexityAnswer(
    text="Antwort [web:1]", quellen=[PerplexitySource(nr=1, titel="Norm", url="https://n.example")], kosten_usd=0.05
)
_REPORT = "Executive Summary. " * 40


def _tool(anonymize=None, max_uses=8):
    return rpp.PoolPerplexityTool(_CFG, anonymize or AsyncMock(return_value="ANON"), MagicMock(), max_uses)


# --- handler --------------------------------------------------------------

@pytest.mark.asyncio
async def test_handler_anonymizes_asks_and_counts():
    anonymize = AsyncMock(return_value="ANON frage")
    t = _tool(anonymize)
    with patch.object(rpp, "ask_perplexity", new=AsyncMock(return_value=_ANSWER)) as ask:
        out = await t.handle({"frage": "Firma Mühl, Wärmepumpe"})
    anonymize.assert_awaited_once_with("Firma Mühl, Wärmepumpe")
    assert ask.await_args.args[0] == "ANON frage"
    assert t.calls == 1 and t.cost_usd == 0.05 and t.gate_error is None
    assert "[1] Norm — https://n.example" in out["content"][0]["text"]


@pytest.mark.asyncio
async def test_gate_failure_sends_nothing_and_locks_the_tool():
    t = _tool(AsyncMock(side_effect=RuntimeError("detector down")))
    with patch.object(rpp, "ask_perplexity", new=AsyncMock()) as ask:
        with pytest.raises(rpp.PoolToolError, match="NICHT gesendet"):
            await t.handle({"frage": "x"})
        with pytest.raises(rpp.PoolToolError, match="gesperrt"):
            await t.handle({"frage": "y"})
    ask.assert_not_called()
    assert "detector down" in t.gate_error


@pytest.mark.asyncio
async def test_gate_empty_text_counts_as_failure():
    t = _tool(AsyncMock(return_value="  "))
    with patch.object(rpp, "ask_perplexity", new=AsyncMock()) as ask:
        with pytest.raises(rpp.PoolToolError):
            await t.handle({"frage": "x"})
    ask.assert_not_called()
    assert t.gate_error


@pytest.mark.asyncio
async def test_http_failure_is_fail_soft():
    t = _tool()
    with patch.object(rpp, "ask_perplexity", new=AsyncMock(side_effect=PerplexityCallError("HTTP 503"))):
        with pytest.raises(rpp.PoolToolError, match="nicht erreichbar"):
            await t.handle({"frage": "x"})
    assert t.gate_error is None and t.calls == 1 and t.cost_usd == 0.0


@pytest.mark.asyncio
async def test_budget_refuses_without_calling():
    t = _tool(max_uses=1)
    with patch.object(rpp, "ask_perplexity", new=AsyncMock(return_value=_ANSWER)) as ask:
        await t.handle({"frage": "a"})
        with pytest.raises(rpp.PoolToolError, match="Budget"):
            await t.handle({"frage": "b"})
    assert ask.await_count == 1


@pytest.mark.asyncio
async def test_missing_cost_is_counted():
    t = _tool()
    with patch.object(rpp, "ask_perplexity", new=AsyncMock(return_value=_ANSWER.model_copy(update={"kosten_usd": None}))):
        await t.handle({"frage": "a"})
    assert t.cost_missing == 1 and t.cost_usd == 0.0


# --- wiring into _execute_research_impl ------------------------------------

def _make_req():
    ns = MagicMock()
    for k, v in dict(
        query="Welche Normausgabe H 5195-1 gilt?", model="claude-sonnet-4-5", depth="quick",
        strategy="planning", max_turns=10, max_hops=None, confidence_threshold=0.7,
        parallel_searches=5, source_filter=None, output_path=None, async_mode=False,
        backend=None, privacy=None, bedrock_region=None, research_mode=None,
    ).items():
        setattr(ns, k, v)
    return ns


async def _stream(*chunks):
    for c in chunks:
        yield c


@pytest.fixture
def spy_cli():
    captured = {}
    on_run = []

    def _run(**kwargs):
        captured.update(kwargs)
        for f in on_run:
            f()
        return _stream(
            {"type": "assistant", "content": [{"type": "text", "text": _REPORT}]},
            {"type": "result", "subtype": "success", "usage": {"input_tokens": 100, "output_tokens": 200}},
        )

    with patch.object(src.main.claude_cli, "run_completion", side_effect=_run) as spy:
        yield spy, captured, on_run


@pytest.fixture
def persist():
    with patch("src.activity.ai_call_writer.persist_ai_call_activity", new=AsyncMock()) as m:
        yield m


@pytest.fixture
def pplx_on(monkeypatch):
    monkeypatch.setenv("RESEARCH_PERPLEXITY_ENABLED", "true")
    monkeypatch.setenv("PERPLEXITY_API_KEY", "pplx-test")


@pytest.mark.asyncio
async def test_flag_off_leaves_the_run_unchanged(spy_cli, persist):
    _, captured, _ = spy_cli
    result = await src.main._execute_research_impl(_make_req(), None, request=MagicMock())
    assert result.status == "success"
    assert captured["sdk_mcp_servers"] is None
    assert "perplexity" not in (captured.get("append_system_prompt") or "")
    assert "perplexity_calls" not in persist.await_args.kwargs["provider_meta"]


@pytest.mark.asyncio
async def test_flag_on_hands_the_server_and_prompt_to_the_cli(spy_cli, persist, pplx_on):
    _, captured, _ = spy_cli
    result = await src.main._execute_research_impl(_make_req(), None, request=MagicMock())
    assert result.status == "success"
    assert set(captured["sdk_mcp_servers"]) == {rpp.MCP_SERVER_NAME}
    assert rpp.MCP_TOOL_NAME in captured["append_system_prompt"]
    booked = persist.await_args.kwargs
    assert booked["extra_cost_usd"] == 0.0
    assert booked["provider_meta"]["perplexity_calls"] == 0


@pytest.mark.asyncio
async def test_flag_on_without_key_refuses_before_the_cli(spy_cli, persist, monkeypatch):
    monkeypatch.setenv("RESEARCH_PERPLEXITY_ENABLED", "true")
    spy, _, _ = spy_cli
    result = await src.main._execute_research_impl(_make_req(), None, request=MagicMock())
    assert result.status == "error" and "PERPLEXITY_API_KEY" in result.error
    spy.assert_not_called()


@pytest.mark.asyncio
async def test_flag_on_without_request_refuses(spy_cli, persist, pplx_on):
    spy, _, _ = spy_cli
    result = await src.main._execute_research_impl(_make_req(), None)
    assert result.status == "error" and "anonymize gate" in result.error
    spy.assert_not_called()


@pytest.mark.asyncio
async def test_gate_failure_during_the_run_ends_it_as_error(spy_cli, persist, pplx_on):
    _, _, on_run = spy_cli
    instances = []
    real_init = rpp.PoolPerplexityTool.__init__

    def _init(self, *a, **kw):
        real_init(self, *a, **kw)
        instances.append(self)

    on_run.append(lambda: setattr(instances[-1], "gate_error", "RuntimeError: detector down"))
    with patch.object(rpp.PoolPerplexityTool, "__init__", _init):
        result = await src.main._execute_research_impl(_make_req(), None, request=MagicMock())
    assert result.status == "error"
    assert "Anonymisierungssperre" in result.error


@pytest.mark.asyncio
async def test_cost_of_the_run_reaches_the_ledger_row(spy_cli, persist, pplx_on):
    _, _, on_run = spy_cli
    instances = []
    real_init = rpp.PoolPerplexityTool.__init__

    def _init(self, *a, **kw):
        real_init(self, *a, **kw)
        instances.append(self)

    def _spend():
        instances[-1].calls = 3
        instances[-1].cost_usd = 0.15

    on_run.append(_spend)
    with patch.object(rpp.PoolPerplexityTool, "__init__", _init):
        await src.main._execute_research_impl(_make_req(), None, request=MagicMock())
    booked = persist.await_args.kwargs
    assert booked["extra_cost_usd"] == 0.15
    assert booked["provider_meta"]["perplexity_calls"] == 3
