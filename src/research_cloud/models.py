"""Pydantic models for the research-cloud executor (Anthropic Messages API,
server-side web_search/web_fetch). No dict-passthrough — every shape the
executor produces or consumes is a real model.
"""
from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class ResearchCloudUsage(BaseModel):
    """Accumulated token usage across all pause_turn continuations of one run."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0

    def add(self, other: "ResearchCloudUsage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_input_tokens += other.cache_read_input_tokens
        self.cache_creation_input_tokens += other.cache_creation_input_tokens


class ResearchCloudResult(BaseModel):
    """Outcome of one research-cloud executor run — formgleich-mappable onto
    ResearchResponse by the caller (this model does not itself carry the
    query/session_id fields; the research handler fills those in)."""

    status: Literal["success", "error"]
    content: Optional[str] = None
    model: str
    usage: ResearchCloudUsage = Field(default_factory=ResearchCloudUsage)
    searches: int = 0
    fetches: int = 0
    iterations: int = 0
    stop_reason: Optional[str] = None
    duration_seconds: float = 0.0
    error: Optional[str] = None
    # Anthropic container id used for the pause_turn continuation chain — kept
    # for observability/debugging only, never exposed in the public response.
    container_id: Optional[str] = None


class ResearchCloudConfig(BaseModel):
    """Fixed executor configuration — Rafael-Go 2026-07-25 decisions, not
    request-controllable (a caller cannot raise its own cost ceiling)."""

    model: str = "claude-sonnet-5"
    max_tokens: int = 20000
    max_continuations: int = 8
    http_timeout_seconds: float = 900.0
    web_search_max_uses: int = 15
    web_fetch_max_uses: int = 10
    inference_geo: Optional[str] = "eu"


# ---------------------------------------------------------------------------
# Minimal typed views onto the Anthropic Messages API response — only the
# fields the executor actually reads, not a full SDK re-implementation.
# ---------------------------------------------------------------------------

class AnthropicContainer(BaseModel):
    id: str


class AnthropicUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


class AnthropicMessagesResponse(BaseModel):
    """Subset of the Messages API response body used by the pause_turn loop."""

    id: Optional[str] = None
    model: Optional[str] = None
    stop_reason: Optional[str] = None
    container: Optional[AnthropicContainer] = None
    usage: AnthropicUsage = Field(default_factory=AnthropicUsage)
    # Raw content blocks — kept as dicts (not a discriminated union) because
    # they are round-tripped verbatim into the next continuation request's
    # `assistant` message; re-serializing a parsed model risks dropping fields
    # the API expects to see unchanged (e.g. server_tool_use block internals).
    content: List[Dict[str, Any]] = Field(default_factory=list)
