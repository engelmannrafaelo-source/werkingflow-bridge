"""Research-cloud executor — Weg C: direct Anthropic Messages API with
server-side web_search/web_fetch tools, pause_turn continuation loop.

Mirrors the eval-validated mechanics from
specs/research-cloud-overflow/eval-research.py:path_c() (cache_control on the
trailing content block — eval-verified factor-5 cost difference; container-id
echo on every pause_turn continuation; max_tokens 20000) as a clean async
executor with typed models instead of the eval script's throwaway dict shape.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Dict, List, Optional

import httpx

from src.research_cloud.models import (
    AnthropicMessagesResponse,
    ResearchCloudConfig,
    ResearchCloudResult,
    ResearchCloudUsage,
)

logger = logging.getLogger(__name__)

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


class ResearchCloudExecutorError(Exception):
    """Fail-loud: the cloud executor refuses to run, or the API call errors.

    Never caught-and-silently-rerouted mid-run — once the cloud path has
    started, a failure here is a job error (kein Silent-Fallback in den Pool).
    """


def _mark_cache_control(messages: List[Dict[str, Any]]) -> None:
    """cache_control on the last content block of the last message.

    Without this the server-side tool loop bills every internal iteration in
    full: the first (uncached) eval run on 2026-07-24 hit 2.86M input tokens /
    ~9 USD for a single recherche; with the marker it dropped to ~1.56 USD —
    a factor of ~5.
    """
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    block.pop("cache_control", None)
    last = messages[-1]
    if isinstance(last["content"], str):
        last["content"] = [{"type": "text", "text": last["content"]}]
    for block in reversed(last["content"]):
        if isinstance(block, dict) and block.get("type") in ("text", "tool_result", "server_tool_use"):
            block["cache_control"] = {"type": "ephemeral"}
            break


def _build_tools(config: ResearchCloudConfig) -> List[Dict[str, Any]]:
    return [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": config.web_search_max_uses},
        {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": config.web_fetch_max_uses},
    ]


async def run_research_cloud(
    query: str,
    system_prompt: str,
    *,
    config: Optional[ResearchCloudConfig] = None,
    api_key: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> ResearchCloudResult:
    """Run one research-cloud job to completion (all pause_turn continuations).

    Fail loud: raises ResearchCloudExecutorError if ANTHROPIC_API_KEY is unset
    or the Messages API returns a non-200, or the loop exhausts
    max_continuations without a terminal stop_reason. Never falls back to the
    worker pool mid-run — a failed cloud call is a job error, not a silent
    reroute (see src/research_cloud — silent fallback is only legitimate
    *before* execution starts, at the cap/routing check).

    ``client``, if given, is used as-is (caller owns its lifecycle) — this is
    the seam tests use to inject a mocked httpx.AsyncClient.
    """
    config = config or ResearchCloudConfig()
    api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ResearchCloudExecutorError(
            "ANTHROPIC_API_KEY not set — refusing to run the research-cloud executor"
        )

    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
    }
    tools = _build_tools(config)
    system: List[Dict[str, Any]] = [
        {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}
    ]
    messages: List[Dict[str, Any]] = [{"role": "user", "content": query}]
    usage = ResearchCloudUsage()
    searches = fetches = 0
    container_id: Optional[str] = None
    iteration = 0
    t0 = time.monotonic()
    parsed: Optional[AnthropicMessagesResponse] = None

    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=config.http_timeout_seconds)
    try:
        for iteration in range(config.max_continuations):
            _mark_cache_control(messages)
            body: Dict[str, Any] = {
                "model": config.model,
                "max_tokens": config.max_tokens,
                "system": system,
                "thinking": {"type": "adaptive"},
                "tools": tools,
                "messages": messages,
            }
            if config.inference_geo:
                body["inference_geo"] = config.inference_geo
            if container_id:
                # web_search/web_fetch _20260209 run code-execution under the
                # hood; on pause_turn the pending tool uses live in this
                # container and the continuation MUST reference it, else 400
                # "container_id is required..." (eval-verified 2026-07-24).
                body["container"] = container_id

            response = await http_client.post(ANTHROPIC_API_URL, headers=headers, json=body)
            if response.status_code != 200:
                raise ResearchCloudExecutorError(
                    f"research-cloud Messages API call failed: HTTP {response.status_code}: "
                    f"{response.text[:500]}"
                )
            parsed = AnthropicMessagesResponse(**response.json())

            if parsed.container:
                container_id = parsed.container.id
            usage.add(
                ResearchCloudUsage(
                    input_tokens=parsed.usage.input_tokens,
                    output_tokens=parsed.usage.output_tokens,
                    cache_read_input_tokens=parsed.usage.cache_read_input_tokens,
                    cache_creation_input_tokens=parsed.usage.cache_creation_input_tokens,
                )
            )
            for block in parsed.content:
                if block.get("type") == "server_tool_use":
                    if block.get("name") == "web_search":
                        searches += 1
                    elif block.get("name") == "web_fetch":
                        fetches += 1

            if parsed.stop_reason != "pause_turn":
                break

            # Do NOT append a synthetic "Continue" user turn — the API
            # detects the trailing server_tool_use block and resumes
            # automatically (shared/tool-use-concepts.md: Stop reasons for
            # server-side tools). Re-send the original user turn + the
            # assistant echo of what just came back.
            messages = [
                {"role": "user", "content": query},
                {"role": "assistant", "content": parsed.content},
            ]
        else:
            raise ResearchCloudExecutorError(
                f"research-cloud executor exceeded max_continuations="
                f"{config.max_continuations} without finishing (still pause_turn)"
            )
    finally:
        if owns_client:
            await http_client.aclose()

    duration = time.monotonic() - t0
    text = "\n\n".join(
        block.get("text", "") for block in parsed.content if block.get("type") == "text"
    )
    return ResearchCloudResult(
        status="success",
        content=text,
        model=parsed.model or config.model,
        usage=usage,
        searches=searches,
        fetches=fetches,
        iterations=iteration + 1,
        stop_reason=parsed.stop_reason,
        duration_seconds=round(duration, 2),
        container_id=container_id,
    )
