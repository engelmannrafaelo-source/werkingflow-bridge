"""Tests for the research-cloud executor pause_turn/container mechanics.

External HTTP is mocked with unittest.mock (this repo's convention — no
respx), via dependency-injecting a mock httpx.AsyncClient into
run_research_cloud().
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.research_cloud.executor import (
    ResearchCloudExecutorError,
    _mark_cache_control,
    run_research_cloud,
)
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
