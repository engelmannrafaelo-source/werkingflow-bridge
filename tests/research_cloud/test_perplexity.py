"""perplexity_search as a research-cloud client tool (RESEARCH_PERPLEXITY_ENABLED).

Covers the parser (source ids), the HTTP client (retry/backoff), the executor
handler (anonymize gate fail-loud, fail-soft HTTP, budget, unknown tools) and
the cost plumbing. External HTTP is mocked (repo convention, no respx).
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.pricing import cost_usd
from src.research_cloud.executor import ResearchCloudExecutorError, _build_tools, run_research_cloud
from src.research_cloud.library import LibraryConfig
from src.research_cloud.models import ResearchCloudConfig
from src.research_cloud.perplexity import (
    PERPLEXITY_API_URL,
    PerplexityCallError,
    PerplexityConfig,
    ask_perplexity,
    load_perplexity_config,
    parse_perplexity_response,
)
from src.research_cloud.prompt import build_system_prompt

@pytest.fixture(autouse=True)
def _anonymizer_service_on(monkeypatch):
    # Perplexity refuses to arm without the anonymize service (check_perplexity_usable).
    monkeypatch.setenv("BRIDGE_ANONYMIZE_ENABLED", "true")


_ON = PerplexityConfig(enabled=True, api_key="pplx-test", max_retries=2)
_OFF = PerplexityConfig()
_NO_LIBRARY = LibraryConfig()


def _resp(status_code, body=None, headers=None, text=""):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = body or {}
    r.headers = headers or {}
    r.text = text or str(body)
    return r


def _pplx_body(text="Antwort [web:1] und [web:37].", cost=0.05):
    return {
        "status": "completed",
        "model": "sonar",
        "output": [
            {"type": "search_results", "results": [
                {"id": 1, "title": "Norm", "url": "https://norm.example/h5195"},
                {"id": 2, "title": "PDF", "url": "https://hersteller.example/235.pdf"},
            ]},
            {"type": "search_results", "results": [
                {"id": 37, "title": "Norm (nochmal)", "url": "https://norm.example/h5195"},
            ]},
            {"type": "message", "content": [{"type": "output_text", "text": text, "annotations": []}]},
        ],
        "usage": {"cost": {"total_cost": cost, "currency": "USD"}} if cost is not None else {},
    }


def _anthropic(body):
    return _resp(200, {"model": "claude-sonnet-5", "usage": {"input_tokens": 10, "output_tokens": 5}, **body})


def _tool_use(name, inp, tid="tu1"):
    return _anthropic({"stop_reason": "tool_use", "content": [{"type": "tool_use", "id": tid, "name": name, "input": inp}]})


def _end(text="bericht"):
    return _anthropic({"stop_reason": "end_turn", "content": [{"type": "text", "text": text}]})


# --- parser ---------------------------------------------------------------

def test_parse_keeps_every_result_id_even_for_duplicate_urls():
    a = parse_perplexity_response(_pplx_body())
    assert [q.nr for q in a.quellen] == [1, 2, 37]
    assert a.quellen[2].url == "https://norm.example/h5195"
    assert a.warnungen == []
    assert a.kosten_usd == 0.05
    assert a.text.startswith("Antwort")


def test_parse_empty_results_block_is_a_warning_not_a_crash():
    body = _pplx_body(text="nichts")
    body["output"].insert(0, {"type": "search_results"})
    a = parse_perplexity_response(body)
    assert any("without results" in w for w in a.warnungen)


def test_parse_flags_dangling_citations():
    a = parse_perplexity_response(_pplx_body(text="x [web:99]"))
    assert any("99" in w for w in a.warnungen)


def test_parse_incomplete_status_raises():
    with pytest.raises(PerplexityCallError, match="incomplete"):
        parse_perplexity_response({"status": "failed", "error": {"message": "boom"}})


def test_config_repr_never_contains_key():
    assert "pplx-test" not in repr(_ON)


def test_load_config_reads_flag_and_key(monkeypatch):
    monkeypatch.setenv("RESEARCH_PERPLEXITY_ENABLED", "true")
    monkeypatch.setenv("PERPLEXITY_API_KEY", "k")
    cfg = load_perplexity_config()
    assert cfg.enabled and cfg.api_key == "k" and cfg.preset == "medium"


@pytest.mark.asyncio
async def test_enabled_without_anonymizer_service_refuses(monkeypatch):
    monkeypatch.setenv("BRIDGE_ANONYMIZE_ENABLED", "false")
    client = MagicMock()
    client.post = AsyncMock()
    with pytest.raises(ResearchCloudExecutorError, match="BRIDGE_ANONYMIZE_ENABLED"):
        await run_research_cloud(
            "q", "s", api_key="sk", client=client, library_config=_NO_LIBRARY, library_index={},
            perplexity_config=_ON, anonymize=AsyncMock(),
        )
    client.post.assert_not_called()


# --- HTTP client ----------------------------------------------------------

@pytest.mark.asyncio
async def test_ask_retries_429_then_succeeds():
    client = MagicMock()
    client.post = AsyncMock(side_effect=[_resp(429, headers={"retry-after": "3"}), _resp(200, _pplx_body())])
    sleep = AsyncMock()
    a = await ask_perplexity("frage", _ON, client, sleep=sleep)
    assert a.kosten_usd == 0.05
    sleep.assert_awaited_once_with(3.0)
    args, kwargs = client.post.call_args
    assert args[0] == PERPLEXITY_API_URL
    assert kwargs["headers"]["Authorization"] == "Bearer pplx-test"
    assert kwargs["json"] == {"input": "frage", "preset": "medium", "stream": False}


@pytest.mark.asyncio
async def test_ask_gives_up_after_retries():
    client = MagicMock()
    client.post = AsyncMock(return_value=_resp(503))
    with pytest.raises(PerplexityCallError, match="503"):
        await ask_perplexity("f", _ON, client, sleep=AsyncMock())
    assert client.post.await_count == 3


@pytest.mark.asyncio
async def test_ask_does_not_retry_401():
    client = MagicMock()
    client.post = AsyncMock(return_value=_resp(401))
    with pytest.raises(PerplexityCallError, match="401"):
        await ask_perplexity("f", _ON, client, sleep=AsyncMock())
    assert client.post.await_count == 1


# --- tools ----------------------------------------------------------------

def test_build_tools_perplexity_alone_forces_direct_callers():
    tools = _build_tools(ResearchCloudConfig(), _NO_LIBRARY, _ON)
    # fetch_document comes with Perplexity (Perplexity finds, it reads).
    assert {t["name"] for t in tools} == {"web_search", "web_fetch", "perplexity_search", "fetch_document"}
    for t in tools:
        if "type" in t:
            assert t["allowed_callers"] == ["direct"]
    assert "web_fetch" in next(t for t in tools if t["name"] == "perplexity_search")["description"]


def test_build_tools_offers_perplexity_directly_not_deferred():
    # Cloud path: a plain client tool in `tools` — no defer_loading, and no
    # tool-search tool that could hide it (the pool path had exactly that
    # problem, 02.10.2026).
    tools = _build_tools(ResearchCloudConfig(), _NO_LIBRARY, _ON)
    pplx = next(t for t in tools if t["name"] == "perplexity_search")
    assert "type" not in pplx and "defer_loading" not in pplx
    assert not any("tool_search" in (t.get("type") or "") + t["name"] for t in tools)


def test_perplexity_first_for_datasheets_and_norm_editions():
    desc = next(t for t in _build_tools(ResearchCloudConfig(), _NO_LIBRARY, _ON)
                if t["name"] == "perplexity_search")["description"]
    prompt = build_system_prompt("standard", perplexity=True)
    for text in (desc, prompt):
        assert "Hersteller-Datenblätter" in text and "ZUERST" in text and "Norm" in text
    assert "web_fetch" in prompt.split("ZUERST", 1)[1]


def test_build_tools_without_perplexity_unchanged():
    tools = _build_tools(ResearchCloudConfig(), _NO_LIBRARY, _OFF)
    assert {t["name"] for t in tools} == {"web_search", "web_fetch"}
    assert all("allowed_callers" not in t for t in tools)


def test_prompt_section_only_when_on():
    assert "perplexity_search" in build_system_prompt("standard", perplexity=True)
    assert "perplexity_search" not in build_system_prompt("standard")


# --- executor -------------------------------------------------------------

async def _run(anthropic_responses, *, pplx_client=None, anonymize=None, cfg=_ON, config=None):
    client = MagicMock()
    client.post = AsyncMock(side_effect=anthropic_responses)
    result = await run_research_cloud(
        "query", "system", api_key="sk-test", client=client,
        library_config=_NO_LIBRARY, library_index={},
        perplexity_config=cfg, anonymize=anonymize, perplexity_client=pplx_client,
        config=config,
    )
    return result, client


@pytest.mark.asyncio
async def test_perplexity_call_is_anonymized_answered_and_costed():
    pplx = MagicMock()
    pplx.post = AsyncMock(return_value=_resp(200, _pplx_body()))
    anonymize = AsyncMock(return_value="ANONYM frage")
    result, client = await _run(
        [_tool_use("perplexity_search", {"frage": "Mühl GmbH Wärmepumpe"}), _end()],
        pplx_client=pplx, anonymize=anonymize,
    )
    anonymize.assert_awaited_once_with("Mühl GmbH Wärmepumpe")
    assert pplx.post.call_args.kwargs["json"]["input"] == "ANONYM frage"
    assert result.perplexity_calls == 1
    assert result.perplexity_cost_usd == 0.05
    assert result.library_calls == 0
    sent = client.post.call_args_list[1].kwargs["json"]["messages"][-1]["content"][0]
    assert sent["type"] == "tool_result" and not sent.get("is_error")
    text = sent["content"][0]["text"]
    assert "[37] Norm (nochmal) — https://norm.example/h5195" in text
    assert "kein Beleg" in text


@pytest.mark.asyncio
async def test_anonymize_failure_aborts_run_and_sends_nothing():
    pplx = MagicMock()
    pplx.post = AsyncMock()
    anonymize = AsyncMock(side_effect=RuntimeError("detector down"))
    with pytest.raises(ResearchCloudExecutorError, match="anonymize gate failed"):
        await _run([_tool_use("perplexity_search", {"frage": "x"}), _end()], pplx_client=pplx, anonymize=anonymize)
    pplx.post.assert_not_called()


@pytest.mark.asyncio
async def test_anonymize_empty_text_aborts_run():
    pplx = MagicMock()
    pplx.post = AsyncMock()
    with pytest.raises(ResearchCloudExecutorError, match="empty"):
        await _run([_tool_use("perplexity_search", {"frage": "x"})], pplx_client=pplx, anonymize=AsyncMock(return_value=" "))
    pplx.post.assert_not_called()


@pytest.mark.asyncio
async def test_enabled_without_anonymizer_refuses_before_any_call():
    with pytest.raises(ResearchCloudExecutorError, match="no anonymizer"):
        _, client = await _run([_end()], anonymize=None)


@pytest.mark.asyncio
async def test_enabled_without_key_refuses_before_any_call():
    client = MagicMock()
    client.post = AsyncMock()
    with pytest.raises(ResearchCloudExecutorError, match="PERPLEXITY_API_KEY"):
        await run_research_cloud(
            "q", "s", api_key="sk", client=client, library_config=_NO_LIBRARY, library_index={},
            perplexity_config=PerplexityConfig(enabled=True), anonymize=AsyncMock(),
        )
    client.post.assert_not_called()


@pytest.mark.asyncio
async def test_perplexity_http_failure_is_fail_soft():
    pplx = MagicMock()
    pplx.post = AsyncMock(return_value=_resp(401))
    result, client = await _run(
        [_tool_use("perplexity_search", {"frage": "x"}), _end("ohne perplexity")],
        pplx_client=pplx, anonymize=AsyncMock(return_value="x"),
    )
    assert result.content == "ohne perplexity"
    assert result.perplexity_calls == 1 and result.perplexity_cost_usd == 0.0
    sent = client.post.call_args_list[1].kwargs["json"]["messages"][-1]["content"][0]
    assert sent["is_error"] is True


@pytest.mark.asyncio
async def test_missing_cost_is_counted_not_guessed():
    pplx = MagicMock()
    pplx.post = AsyncMock(return_value=_resp(200, _pplx_body(cost=None)))
    result, _ = await _run(
        [_tool_use("perplexity_search", {"frage": "x"}), _end()],
        pplx_client=pplx, anonymize=AsyncMock(return_value="x"),
    )
    assert result.perplexity_cost_usd == 0.0
    assert result.perplexity_cost_missing == 1


@pytest.mark.asyncio
async def test_budget_exhausted_answers_error_without_calling():
    pplx = MagicMock()
    pplx.post = AsyncMock(return_value=_resp(200, _pplx_body()))
    result, client = await _run(
        [_tool_use("perplexity_search", {"frage": "a"}, "t1"),
         _tool_use("perplexity_search", {"frage": "b"}, "t2"), _end()],
        pplx_client=pplx, anonymize=AsyncMock(return_value="x"),
        config=ResearchCloudConfig(perplexity_max_uses=1),
    )
    assert pplx.post.await_count == 1
    assert result.perplexity_calls == 1
    sent = client.post.call_args_list[2].kwargs["json"]["messages"][-1]["content"][0]
    assert sent["is_error"] is True and "Budget" in sent["content"][0]["text"]


@pytest.mark.asyncio
async def test_perplexity_call_while_disabled_is_foreign():
    with pytest.raises(ResearchCloudExecutorError, match="cannot answer client tools"):
        await _run([_tool_use("perplexity_search", {"frage": "x"})], cfg=_OFF)


@pytest.mark.asyncio
async def test_library_call_while_only_perplexity_on_is_foreign():
    with pytest.raises(ResearchCloudExecutorError, match="cannot answer client tools"):
        await _run([_tool_use("library_get", {"id": "x"})], anonymize=AsyncMock(return_value="x"))


# --- cost -----------------------------------------------------------------

def test_cost_usd_adds_extra_tool_fees():
    base = cost_usd("claude-sonnet-5", 1000, 100)
    assert cost_usd("claude-sonnet-5", 1000, 100, extra_cost_usd=0.4) == pytest.approx(base + 0.4)
