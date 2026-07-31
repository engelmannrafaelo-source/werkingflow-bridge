"""Tests for the research-cloud executor pause_turn/container mechanics.

External HTTP is mocked with unittest.mock (this repo's convention — no
respx), via dependency-injecting a mock httpx.AsyncClient into
run_research_cloud().
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.research_cloud.executor import (
    ResearchCloudExecutorError,
    _build_tools,
    _mark_cache_control,
    run_research_cloud,
)
from src.research_cloud.library import LibraryConfig, LibraryFetchError
from src.research_cloud.models import ResearchCloudConfig


def _response(status_code: int, body: dict, text: str = "") -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body
    resp.text = text or str(body)
    return resp


def _usage(input_tokens=100, output_tokens=50) -> dict:
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }


@pytest.mark.asyncio
async def test_single_turn_end_turn_no_container():
    """Simple case: one call, stop_reason=end_turn, no container needed."""
    client = MagicMock()
    client.post = AsyncMock(
        return_value=_response(
            200,
            {
                "model": "claude-sonnet-5",
                "stop_reason": "end_turn",
                "usage": _usage(),
                "content": [{"type": "text", "text": "final report"}],
            },
        )
    )

    result = await run_research_cloud(
        "query", "system prompt", api_key="sk-test", client=client
    )

    assert result.status == "success"
    assert result.content == "final report"
    assert result.iterations == 1
    assert result.stop_reason == "end_turn"
    assert client.post.call_count == 1
    # No container in the response -> no container key sent on the (single) call.
    _, kwargs = client.post.call_args
    assert "container" not in kwargs["json"]


@pytest.mark.asyncio
async def test_pause_turn_continuation_requires_container_id():
    """pause_turn continuation must echo back the container id on every
    subsequent request — the eval-verified 400 'container_id is required'
    failure mode this guards against."""
    client = MagicMock()
    client.post = AsyncMock(
        side_effect=[
            _response(
                200,
                {
                    "model": "claude-sonnet-5",
                    "stop_reason": "pause_turn",
                    "container": {"id": "container-abc123"},
                    "usage": _usage(),
                    "content": [
                        {"type": "server_tool_use", "name": "web_search", "id": "t1"},
                    ],
                },
            ),
            _response(
                200,
                {
                    "model": "claude-sonnet-5",
                    "stop_reason": "end_turn",
                    "usage": _usage(),
                    "content": [{"type": "text", "text": "done after continuation"}],
                },
            ),
        ]
    )

    result = await run_research_cloud(
        "query", "system prompt", api_key="sk-test", client=client
    )

    assert result.iterations == 2
    assert result.content == "done after continuation"
    assert result.searches == 1
    assert result.container_id == "container-abc123"

    # Second call MUST carry the container id from the first response.
    _, first_kwargs = client.post.call_args_list[0]
    assert "container" not in first_kwargs["json"]
    _, second_kwargs = client.post.call_args_list[1]
    assert second_kwargs["json"]["container"] == "container-abc123"


@pytest.mark.asyncio
async def test_pause_turn_continuation_replays_user_and_assistant_only():
    """On continuation, messages must be exactly [user(original query),
    assistant(echoed content)] — no synthetic 'Continue' turn appended."""
    client = MagicMock()
    client.post = AsyncMock(
        side_effect=[
            _response(
                200,
                {
                    "model": "claude-sonnet-5",
                    "stop_reason": "pause_turn",
                    "container": {"id": "c1"},
                    "usage": _usage(),
                    "content": [{"type": "text", "text": "partial"}],
                },
            ),
            _response(
                200,
                {
                    "model": "claude-sonnet-5",
                    "stop_reason": "end_turn",
                    "usage": _usage(),
                    "content": [{"type": "text", "text": "final"}],
                },
            ),
        ]
    )

    await run_research_cloud("the query", "system", api_key="sk-test", client=client)

    _, second_kwargs = client.post.call_args_list[1]
    sent_messages = second_kwargs["json"]["messages"]
    assert len(sent_messages) == 2
    assert sent_messages[0] == {"role": "user", "content": "the query"}
    assert sent_messages[1]["role"] == "assistant"
    # _mark_cache_control stamps the last block before the second call — the
    # echoed content is otherwise the untouched parsed.content from turn 1.
    assert sent_messages[1]["content"] == [
        {"type": "text", "text": "partial", "cache_control": {"type": "ephemeral"}}
    ]


@pytest.mark.asyncio
async def test_exhausting_max_continuations_raises():
    client = MagicMock()
    client.post = AsyncMock(
        return_value=_response(
            200,
            {
                "model": "claude-sonnet-5",
                "stop_reason": "pause_turn",
                "container": {"id": "c1"},
                "usage": _usage(),
                "content": [{"type": "text", "text": "still going"}],
            },
        )
    )
    config = ResearchCloudConfig(max_continuations=2)

    with pytest.raises(ResearchCloudExecutorError, match="max_continuations"):
        await run_research_cloud(
            "query", "system", config=config, api_key="sk-test", client=client
        )
    assert client.post.call_count == 2


@pytest.mark.asyncio
async def test_http_error_raises_job_error_not_silent_fallback():
    client = MagicMock()
    client.post = AsyncMock(
        return_value=_response(429, {}, text="rate limited upstream")
    )

    with pytest.raises(ResearchCloudExecutorError, match="429"):
        await run_research_cloud("query", "system", api_key="sk-test", client=client)


@pytest.mark.asyncio
async def test_missing_api_key_fails_loud(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(ResearchCloudExecutorError, match="ANTHROPIC_API_KEY"):
        await run_research_cloud("query", "system", api_key=None, client=MagicMock())


def test_mark_cache_control_places_marker_on_last_block_only():
    messages = [
        {"role": "user", "content": "first turn"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "a", "cache_control": {"type": "ephemeral"}},
                {"type": "server_tool_use", "name": "web_search"},
            ],
        },
    ]
    _mark_cache_control(messages)
    last_content = messages[-1]["content"]
    # Old marker on the first block is removed...
    assert "cache_control" not in last_content[0]
    # ...and the new marker sits on the last eligible block.
    assert last_content[-1]["cache_control"] == {"type": "ephemeral"}


def test_mark_cache_control_wraps_string_content():
    messages = [{"role": "user", "content": "plain string"}]
    _mark_cache_control(messages)
    assert messages[0]["content"] == [
        {"type": "text", "text": "plain string", "cache_control": {"type": "ephemeral"}}
    ]


# ---------------------------------------------------------------------------
# Library tool loop (RESEARCH_LIBRARY_ENABLED) — client tool_use handling.
# ---------------------------------------------------------------------------

_LIBRARY_CFG = LibraryConfig(
    enabled=True,
    endpoint_url="https://fsn1.example.com",
    bucket="research-library",
    access_key_id="key",
    secret_access_key="secret",
)
_LIBRARY_CFG_DISABLED = LibraryConfig()

_INDEX = {"documents": [{"id": "doc-a", "title": "Doc A", "source_url": "https://example.org/a.pdf"}]}


def _tool_use_response(tool_name, tool_input, tool_use_id="tu1"):
    return _response(
        200,
        {
            "model": "claude-sonnet-5",
            "stop_reason": "tool_use",
            "usage": _usage(),
            "content": [
                {"type": "tool_use", "id": tool_use_id, "name": tool_name, "input": tool_input},
            ],
        },
    )


def _end_turn_response(text="done"):
    return _response(
        200,
        {
            "model": "claude-sonnet-5",
            "stop_reason": "end_turn",
            "usage": _usage(),
            "content": [{"type": "text", "text": text}],
        },
    )


def test_build_tools_omits_library_when_disabled():
    tools = _build_tools(ResearchCloudConfig(), _LIBRARY_CFG_DISABLED)
    names = {t["name"] for t in tools}
    assert names == {"web_search", "web_fetch"}


def test_build_tools_includes_library_when_enabled():
    tools = _build_tools(ResearchCloudConfig(), _LIBRARY_CFG)
    names = {t["name"] for t in tools}
    assert names == {"web_search", "web_fetch", "library_index", "library_get"}


@pytest.mark.asyncio
async def test_library_index_tool_call_continues_loop_and_sends_tool_result():
    client = MagicMock()
    client.post = AsyncMock(
        side_effect=[
            _tool_use_response("library_index", {}),
            _end_turn_response("done after library_index"),
        ]
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "src.research_cloud.executor.fetch_library_index",
            AsyncMock(return_value=_INDEX),
        )
        result = await run_research_cloud(
            "query", "system", api_key="sk-test", client=client, library_config=_LIBRARY_CFG
        )

    assert result.content == "done after library_index"
    assert result.iterations == 2
    assert result.library_calls == 1

    _, second_kwargs = client.post.call_args_list[1]
    sent_messages = second_kwargs["json"]["messages"]
    assert len(sent_messages) == 3
    tool_result_msg = sent_messages[2]
    assert tool_result_msg["role"] == "user"
    assert tool_result_msg["content"][0]["type"] == "tool_result"
    assert tool_result_msg["content"][0]["tool_use_id"] == "tu1"
    assert "documents" in tool_result_msg["content"][0]["content"][0]["text"]


@pytest.mark.asyncio
async def test_library_get_tool_call_returns_search_result_block_with_citations():
    client = MagicMock()
    client.post = AsyncMock(
        side_effect=[
            _tool_use_response("library_get", {"id": "doc-a"}, tool_use_id="tu2"),
            _end_turn_response("done after library_get"),
        ]
    )
    doc = {"entry": _INDEX["documents"][0], "text": "Full text of doc A."}

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "src.research_cloud.executor.fetch_library_document",
            AsyncMock(return_value=doc),
        )
        result = await run_research_cloud(
            "query", "system", api_key="sk-test", client=client, library_config=_LIBRARY_CFG
        )

    assert result.content == "done after library_get"
    assert result.library_calls == 1

    _, second_kwargs = client.post.call_args_list[1]
    tool_result = second_kwargs["json"]["messages"][2]["content"][0]
    assert tool_result["tool_use_id"] == "tu2"
    search_result_block = tool_result["content"][0]
    assert search_result_block["type"] == "search_result"
    assert search_result_block["source"] == "https://example.org/a.pdf"
    assert search_result_block["title"] == "Doc A"
    assert search_result_block["content"] == [{"type": "text", "text": "Full text of doc A."}]
    assert search_result_block["citations"] == {"enabled": True}


@pytest.mark.asyncio
async def test_library_tool_fetch_error_is_fail_soft_not_raised():
    """S3 failure inside a library tool call must not abort the research
    run — it becomes a tool_result(is_error=True) and the loop continues."""
    client = MagicMock()
    client.post = AsyncMock(
        side_effect=[
            _tool_use_response("library_get", {"id": "missing-doc"}),
            _end_turn_response("done despite fetch error"),
        ]
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "src.research_cloud.executor.fetch_library_document",
            AsyncMock(side_effect=LibraryFetchError("unknown library document id: 'missing-doc'")),
        )
        result = await run_research_cloud(
            "query", "system", api_key="sk-test", client=client, library_config=_LIBRARY_CFG
        )

    assert result.status == "success"
    assert result.content == "done despite fetch error"

    _, second_kwargs = client.post.call_args_list[1]
    tool_result = second_kwargs["json"]["messages"][2]["content"][0]
    assert tool_result["is_error"] is True
    assert "unknown library document id" in tool_result["content"][0]["text"]


@pytest.mark.asyncio
async def test_unrecognized_client_tool_use_fails_loud():
    client = MagicMock()
    client.post = AsyncMock(return_value=_tool_use_response("some_other_tool", {}))

    with pytest.raises(ResearchCloudExecutorError, match="no recognized tool_use block"):
        await run_research_cloud(
            "query", "system", api_key="sk-test", client=client, library_config=_LIBRARY_CFG
        )


@pytest.mark.asyncio
async def test_exhausting_max_continuations_during_tool_use_reports_stop_reason():
    client = MagicMock()
    client.post = AsyncMock(return_value=_tool_use_response("library_index", {}))
    config = ResearchCloudConfig(max_continuations=1)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "src.research_cloud.executor.fetch_library_index",
            AsyncMock(return_value=_INDEX),
        )
        with pytest.raises(ResearchCloudExecutorError, match="still tool_use"):
            await run_research_cloud(
                "query", "system", config=config, api_key="sk-test",
                client=client, library_config=_LIBRARY_CFG,
            )


@pytest.mark.asyncio
async def test_library_tools_absent_from_request_when_disabled():
    """Feature-off path: no library_config override, no env vars set ->
    tools list must be exactly the pre-existing two server tools, matching
    pre-feature behavior byte-for-byte."""
    client = MagicMock()
    client.post = AsyncMock(return_value=_end_turn_response("plain report"))

    await run_research_cloud(
        "query", "system", api_key="sk-test", client=client, library_config=_LIBRARY_CFG_DISABLED
    )

    _, kwargs = client.post.call_args
    tool_names = {t["name"] for t in kwargs["json"]["tools"]}
    assert tool_names == {"web_search", "web_fetch"}
