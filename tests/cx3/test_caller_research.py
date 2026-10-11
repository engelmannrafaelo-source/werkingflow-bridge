"""Caller-owned policy, no model calls or service connections."""

import pytest

from src.research_cloud.prompt import build_pool_library_catalogue, build_system_prompt
from src.research_protocol import build_research_execution_prompt

POLICY = "Du katalogisierst Musikinstrumente. Antworte als JSON."
INDEX = {"documents": [{"id": "sample", "title": "Instrumente"}]}


def test_cli_caller_policy_replaces_legacy_report_and_source_rules():
    prompt, turns, depth = build_research_execution_prompt(
        "/sc:research Holzblasinstrumente --depth deep",
        80,
        library_dir="research-library",
        antwort_im_text=True,
        research_instructions=POLICY,
    )
    assert POLICY in prompt
    assert "Holzblasinstrumente" in prompt
    assert depth == "deep" and turns > 0
    for legacy in ["# Research Report", "Offene Lücken", "Normnummern", "Every number"]:
        assert legacy not in prompt


def test_cloud_policy_replaces_all_editorial_defaults():
    prompt = build_system_prompt("deep", INDEX, True, research_instructions=POLICY)
    assert POLICY in prompt and "sample" in prompt
    for legacy in [
        "Rangfolge",
        "Norm",
        "Hersteller",
        "Executive Summary",
        "Zweite Fundstelle",
    ]:
        assert legacy not in prompt


def test_pool_catalogue_is_only_access_information_for_custom_policy():
    prompt = build_pool_library_catalogue(
        INDEX,
        dir_name="research-library",
        research_instructions=POLICY,
    )
    assert "sample" in prompt and "research-library" in prompt
    assert "Rangfolge" not in prompt and "Zitiere" not in prompt


def test_omitted_policy_preserves_existing_protocol():
    prompt, _, _ = build_research_execution_prompt("/sc:research music", 30)
    assert "# Research Report" in prompt
    assert "Rangfolge" in build_system_prompt("deep", INDEX, True)


def test_request_preserves_policy_for_job_roundtrip():
    from src.models import ResearchRequest

    request = ResearchRequest(query="music", research_instructions=POLICY)
    assert (
        ResearchRequest.model_validate(request.model_dump()).research_instructions
        == POLICY
    )
    with pytest.raises(ValueError):
        ResearchRequest(query="music", research_instructions="")


@pytest.mark.asyncio
async def test_job_transports_caller_policy_unchanged():
    from unittest.mock import AsyncMock, patch

    from src.jobs.executors import research_executor

    payload = {"query": "music", "research_instructions": POLICY}
    with patch(
        "src.jobs.executors._self_post_json",
        new=AsyncMock(return_value={"status": "success"}),
    ) as post:
        await research_executor(payload, None, AsyncMock())
    assert post.await_args.args[1]["research_instructions"] == POLICY
    assert payload == {"query": "music", "research_instructions": POLICY}


def test_cloud_tool_descriptions_do_not_leak_legacy_policy():
    from src.research_cloud.executor import _build_tools
    from src.research_cloud.library import LibraryConfig
    from src.research_cloud.models import ResearchCloudConfig
    from src.research_cloud.perplexity import PERPLEXITY_TOOL, PerplexityConfig

    tools = _build_tools(
        ResearchCloudConfig(),
        LibraryConfig(),
        PerplexityConfig(enabled=True, api_key="synthetic"),
        POLICY,
    )
    descriptions = " ".join(t.get("description", "") for t in tools)
    assert "Hersteller" not in descriptions and "Norm" not in descriptions
    assert "Hersteller" in PERPLEXITY_TOOL["description"]  # no global mutation


@pytest.mark.parametrize("value", ["", "   ", 123, False])
def test_empty_or_nontext_policy_is_rejected(value):
    from src.models import ResearchRequest

    with pytest.raises(ValueError):
        ResearchRequest(query="music", research_instructions=value)


def test_pool_tools_offer_neutral_documentation():
    from unittest.mock import AsyncMock, patch

    from src.research_cloud.perplexity import PerplexityConfig
    from src.research_pool_perplexity import PoolDocumentTool, PoolPerplexityTool

    with (
        patch("claude_code_sdk.tool", side_effect=lambda *a: lambda fn: fn) as tool,
        patch("claude_code_sdk.create_sdk_mcp_server", return_value={}),
    ):
        PoolPerplexityTool(PerplexityConfig(), AsyncMock(), None, 3, POLICY).server()
        PoolDocumentTool(3, research_instructions=POLICY).server()
    descriptions = " ".join(call.args[1] for call in tool.call_args_list)
    assert "Hersteller" not in descriptions and "Norm" not in descriptions
    assert "page" in descriptions and "source" in descriptions
