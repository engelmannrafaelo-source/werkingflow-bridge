"""Boundary for providers which can reserve tokens BEFORE each model call.

Legacy CLI streams cannot enforce a total input+output budget. They are not
registered here. An adapter must bound all four disjoint usage counters and
must return model tool *requests*, never execute tools inside the provider.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


class RunError(Exception):
    def __init__(self, code, *, retryable=False, retry_after_s=None):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.retry_after_s = retry_after_s


@dataclass
class ModelReply:
    # Original BRTRUNC-compatible stream; never infer end_turn from success.
    chunks: list[dict[str, Any]]
    text: str | None
    usage: dict[str, Any]
    calls: list[dict[str, Any]] = field(default_factory=list)
    complete_log: bool = False


class BoundedModel(Protocol):
    hard_token_bound: bool
    tools_are_requests: bool

    async def input_bound(self, model: str, messages: list, tools: list) -> int:
        """Guaranteed upper bound of input+cache-read+cache-creation, not estimate."""
        ...

    async def complete(
        self, *, model: str, messages: list, tools: list, max_tokens: int
    ) -> ModelReply:
        """One response; strict output cap; no automatic continuation or tools."""
        ...


class Adapters:
    def __init__(self):
        self._models = {}

    def register(self, name: str, model: BoundedModel):
        if not model.hard_token_bound or not model.tools_are_requests:
            raise RunError("faehigkeit_nicht_verfuegbar")
        self._models[name] = model

    def require(self, name: str) -> BoundedModel:
        if name not in self._models:
            raise RunError("faehigkeit_nicht_verfuegbar")
        return self._models[name]
