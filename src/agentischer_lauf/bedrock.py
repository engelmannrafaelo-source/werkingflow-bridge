"""Bedrock Messages adapter with a pre-call token reservation (ATOK).

Why this transport and not the CLI: the Claude CLI runs its own loop (tool
rounds, compaction, side calls, truncated_response_recovery) and reports usage
only in its final ResultMessage, so nothing in front of it can reserve tokens
before each model request. Here the bridge sends every request itself:

- input: Bedrock CountTokens counts the exact InvokeModel body that is sent
  afterwards, with the provider tokenizer. No cache_control is ever sent, so
  cache-read/-creation cannot occur. complete() refuses any body that was not
  counted first.
- output: max_tokens of one InvokeModel request, enforced by the provider. No
  thinking, no continuation.
- request count: botocore retries are off (total_max_attempts=1) so no hidden
  second, billed attempt exists. The tool loop is Runtime.phase.

Remaining assumption, stated not hidden: CountTokens and billing use the same
counter. Runtime.phase checks reported usage against the reservation and fails
loudly with grenze_tokens if the provider ever reports more.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from contextvars import ContextVar

from .adapter import ModelReply, RunError
from .sandbox import canonical

BEDROCK_VERSION = "bedrock-2023-05-31"

# Technical call shapes of the mediated sandbox (Sandbox.call), no app wording.
TOOL_SCHEMAS = {
    "Read": {
        "description": "Read a UTF-8 file from the run sandbox.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    "Write": {
        "description": "Write a UTF-8 file to a declared output path of the run sandbox.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
    "List": {
        "description": "List all paths in the run sandbox.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
}

THROTTLED = frozenset({"ThrottlingException", "ServiceUnavailableException"})


def boto_config():
    """The only client configuration under which the bound holds."""
    from botocore.config import Config

    return Config(
        connect_timeout=10,
        read_timeout=600,
        retries={"total_max_attempts": 1, "mode": "standard"},
    )


def single_attempt(client) -> bool:
    retries = getattr(getattr(getattr(client, "meta", None), "config", None), "retries", None)
    if not isinstance(retries, dict):
        return False
    if "total_max_attempts" in retries:
        return retries["total_max_attempts"] == 1
    return retries.get("max_attempts") == 0


def to_anthropic(messages, tools):
    """Runtime message list -> Messages API body parts (system, messages, tools)."""
    system = []
    out = []

    def add(role, blocks):
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": list(blocks)})

    for message in messages:
        role = message["role"]
        if role == "system":
            system.append({"type": "text", "text": message["content"]})
        elif role == "user":
            add("user", [{"type": "text", "text": message["content"]}])
        elif role == "assistant":
            blocks = [{"type": "text", "text": message["content"]}] if message.get("content") else []
            blocks += [
                {"type": "tool_use", "id": c["id"], "name": c["name"], "input": c["input"]}
                for c in message.get("tool_calls") or []
            ]
            add("assistant", blocks)
        elif role == "tool":
            value = message["content"]
            text = value if isinstance(value, str) else canonical(value).decode()
            add(
                "user",
                [{"type": "tool_result", "tool_use_id": message["tool_call_id"], "content": text}],
            )
        else:
            raise RunError("protokoll_unvollstaendig")
    specs = []
    for tool in tools:
        if tool["name"] not in TOOL_SCHEMAS:
            raise RunError("faehigkeit_nicht_verfuegbar")
        specs.append({"name": tool["name"], **TOOL_SCHEMAS[tool["name"]]})
    return system, out, specs


def body_for(messages, tools, max_tokens):
    system, converted, specs = to_anthropic(messages, tools)
    body = {"anthropic_version": BEDROCK_VERSION, "messages": converted, "max_tokens": max_tokens}
    if system:
        body["system"] = system
    if specs:
        body["tools"] = specs
    return body


def counted_key(model_id, body):
    rest = {k: v for k, v in body.items() if k != "max_tokens"}
    return hashlib.sha256(canonical({"model": model_id, "body": rest})).hexdigest()


class BedrockBoundedModel:
    """BoundedModel over Bedrock InvokeModel. Registered per model name.

    book(...) is the existing ledger path (ledger_book). Every billed
    InvokeModel is booked once, before its reply is checked, so a run that
    fails afterwards is still accounted.
    """

    tools_are_requests = True

    def __init__(self, client, model_id: str, *, region: str, book):
        if not callable(book):
            raise TypeError("ledger booking required")
        self.client = client
        self.model_id = model_id
        self.region = region
        self.book = book
        self._counted = {}
        # The flag is a statement about THIS client, never a constant.
        self.hard_token_bound = callable(getattr(client, "count_tokens", None)) and single_attempt(
            client
        )

    def _client_error(self, exc):
        code = ((getattr(exc, "response", None) or {}).get("Error") or {}).get("Code", "")
        if code in THROTTLED:
            return RunError("kapazitaet_belegt", retryable=True)
        return RunError("anbieter_fehler")

    async def input_bound(self, model, messages, tools):
        if not self.hard_token_bound:
            raise RunError("faehigkeit_nicht_verfuegbar")
        body = body_for(messages, tools, 1)
        try:
            from botocore.exceptions import ClientError
        except ImportError:  # pragma: no cover - botocore is a runtime dependency
            raise RunError("faehigkeit_nicht_verfuegbar") from None
        try:
            answer = await asyncio.to_thread(
                self.client.count_tokens,
                modelId=self.model_id,
                input={"invokeModel": {"body": json.dumps(body).encode()}},
            )
        except ClientError as exc:
            raise self._client_error(exc) from None
        tokens = answer.get("inputTokens") if isinstance(answer, dict) else None
        if type(tokens) is not int or tokens < 0:
            # No fallback to an estimate: without a count there is no bound.
            raise RunError("faehigkeit_nicht_verfuegbar")
        self._counted[counted_key(self.model_id, body)] = tokens
        return tokens

    async def complete(self, *, model, messages, tools, max_tokens):
        from botocore.exceptions import ClientError

        if type(max_tokens) is not int or max_tokens <= 0:
            raise RunError("grenze_tokens")
        body = body_for(messages, tools, max_tokens)
        if counted_key(self.model_id, body) not in self._counted:
            # Reservation and request must be the same bytes.
            raise RunError("faehigkeit_nicht_verfuegbar")
        started = time.monotonic()

        def invoke():
            response = self.client.invoke_model(
                modelId=self.model_id,
                contentType="application/json",
                accept="application/json",
                body=json.dumps(body),
            )
            return response, json.loads(response["body"].read())

        try:
            response, data = await asyncio.to_thread(invoke)
        except ClientError as exc:
            raise self._client_error(exc) from None
        raw = data.get("usage") if isinstance(data, dict) else None
        if not isinstance(raw, dict):
            raise RunError("protokoll_unvollstaendig")
        usage = {
            "input_tokens": raw.get("input_tokens"),
            "output_tokens": raw.get("output_tokens"),
            # Absent only because no cache_control is ever sent; a reported
            # value is taken as is and checked against the reservation.
            "cache_read_input_tokens": raw.get("cache_read_input_tokens", 0),
            "cache_creation_input_tokens": raw.get("cache_creation_input_tokens", 0),
            "cost_eur": None,
            "cost_source": "unknown",
        }
        request_id = ((response or {}).get("ResponseMetadata") or {}).get("RequestId")
        await self.book(
            model=self.model_id,
            region=self.region,
            usage=usage,
            duration_ms=int((time.monotonic() - started) * 1000),
            aws_request_id=request_id,
        )
        blocks = data.get("content")
        if not isinstance(blocks, list):
            raise RunError("protokoll_unvollstaendig")
        texts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
        calls = [
            {"id": b.get("id"), "name": b.get("name"), "input": b.get("input")}
            for b in blocks
            if isinstance(b, dict) and b.get("type") == "tool_use"
        ]
        chunks = [{"type": "assistant", "content": blocks, "stop_reason": data.get("stop_reason")}]
        return ModelReply(
            chunks=chunks,
            text="".join(texts) if texts else None,
            usage=usage,
            calls=calls,
            complete_log=True,
        )


# Set per request by the HTTP dispatch (extract_attribution_context).
ATTRIBUTION = ContextVar("agentischer_lauf_attribution", default=None)


async def ledger_book(*, model, region, usage, duration_ms, aws_request_id):
    """Existing ledger writer; also prices the call for X-Bridge-Cost-Eur."""
    from src.activity.ai_call_writer import persist_ai_call_activity
    from src.activity.providers import PROVIDER_BEDROCK

    attribution = ATTRIBUTION.get() or {}
    await persist_ai_call_activity(
        provider=PROVIDER_BEDROCK,
        app_id=attribution.get("app_id"),
        user_id=attribution.get("user_id"),
        agent_id=attribution.get("agent_id"),
        workflow_id=attribution.get("workflow_id"),
        app_env=attribution.get("app_env"),
        model=model,
        region=region,
        input_tokens=usage["input_tokens"] if type(usage["input_tokens"]) is int else 0,
        output_tokens=usage["output_tokens"] if type(usage["output_tokens"]) is int else 0,
        cache_read_tokens=usage["cache_read_input_tokens"] or 0,
        cache_creation_tokens=usage["cache_creation_input_tokens"] or 0,
        status="success",
        duration_ms=duration_ms,
        provider_meta={
            "usage_source": "api",
            "endpoint": "agentischer_lauf",
            "aws_request_id": aws_request_id,
        },
    )


def register_bedrock(adapters, models, *, region, book=ledger_book, client=None):
    """Server composition: one bounded adapter per requested model name.

    Adapters.register refuses an adapter whose client cannot hold the bound
    (no CountTokens, retries on), so a wrong client fails at startup.
    """
    from src.model_registry import resolve_model, to_bedrock_model_id

    if client is None:
        import boto3

        client = boto3.client("bedrock-runtime", region_name=region, config=boto_config())
    for name in models:
        resolved, _ = resolve_model(name)
        if not resolved:
            raise ValueError(f"unknown model {name!r}")
        adapters.register(
            name,
            BedrockBoundedModel(
                client, to_bedrock_model_id(resolved, region), region=region, book=book
            ),
        )
    return adapters
