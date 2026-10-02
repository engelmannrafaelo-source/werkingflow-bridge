"""Research-cloud executor — Weg C: direct Anthropic Messages API with
server-side web_search/web_fetch tools, pause_turn continuation loop.

Mirrors the eval-validated mechanics from
specs/research-cloud-overflow/eval-research.py:path_c() (cache_control on the
trailing content block — eval-verified factor-5 cost difference; container-id
echo on every pause_turn continuation; max_tokens 20000) as a clean async
executor with typed models instead of the eval script's throwaway dict shape.

Additionally handles the two client-side library tools (library_index,
library_get — specs/research-library-tool/DESIGN.md), flag-gated via
RESEARCH_LIBRARY_ENABLED. Research (bridge-research.py, 2026-07-31,
platform.claude.com/docs/en/build-with-claude/handling-stop-reasons):
a response with a pending *client* tool_use always has stop_reason
"tool_use", never "pause_turn", even when server_tool_use blocks are also
present in the same response — so the pause_turn continuation branch below
is untouched, and library tool calls are a second, independent branch.

A third client tool, perplexity_search (perplexity.py), is flag-gated via
RESEARCH_PERPLEXITY_ENABLED (Rafael 2026-10-02). Its query goes to a third
party and therefore passes the caller-supplied anonymizer first — the same
fail-closed gate as the research prompt; if that gate fails, the run aborts.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx

from src.research_cloud.library import (
    LibraryConfig,
    LibraryFetchError,
    LibraryUnavailableError,
    fetch_library_document,
    library_enabled,
    load_library_config,
    load_library_for_run,
)
from src.research_cloud.perplexity import (
    PERPLEXITY_TOOL,
    PERPLEXITY_TOOL_NAME,
    PerplexityCallError,
    PerplexityConfig,
    PerplexityUnavailableError,
    ask_perplexity,
    check_perplexity_usable,
    format_tool_result_text,
    load_perplexity_config,
    perplexity_enabled,
)
from src.research_cloud.models import (
    AnthropicMessagesResponse,
    ResearchCloudConfig,
    ResearchCloudResult,
    ResearchCloudUsage,
)

logger = logging.getLogger(__name__)

# Once per process — the answer cannot change without a container restart.
_key_lane_logged = False


def _log_key_lane_once(api_key: str) -> None:
    """Say out loud whether the research lane pays from its own key.

    Measured, not assumed: the worker holds BOTH keys, so it can compare them
    instead of trusting a variable name. On 04.09.2026 the two were the same
    key under two names (sha256 identical in all four dev workers), which is
    why a single research run could empty the image lane without warning while
    the budget guard still showed headroom. RESEARCH_CLOUD_ANTHROPIC_KEY exists
    to end that; this line is how anyone can tell whether it took effect —
    without printing a secret.
    """
    global _key_lane_logged
    if _key_lane_logged:
        return
    _key_lane_logged = True
    vision_key = os.environ.get("ANTHROPIC_VISION_API_KEY")
    if vision_key and vision_key == api_key:
        logger.warning(
            "research-cloud key lane: SHARED with the image lane — one key pays "
            "for both (ANTHROPIC_VISION_API_KEY == RESEARCH_CLOUD_API_KEY). A "
            "single research run can empty the image lane. Set "
            "RESEARCH_CLOUD_ANTHROPIC_KEY in docker/.env to separate them."
        )
    else:
        logger.info(
            "research-cloud key lane: dedicated (separate from the image lane)"
        )

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"

_LIBRARY_INDEX_TOOL: Dict[str, Any] = {
    "name": "library_index",
    "description": (
        "Zeigt das Verzeichnis einer kuratierten, privaten Dokumentbibliothek "
        "(Volltexte ausgewählter Quellen). Nutze library_get, um ein einzelnes "
        "Dokument daraus als Volltext zu laden."
    ),
    "input_schema": {"type": "object", "properties": {}},
}

_LIBRARY_GET_TOOL: Dict[str, Any] = {
    "name": "library_get",
    "description": "Lädt den Volltext eines Dokuments aus der kuratierten Bibliothek anhand seiner id (siehe library_index).",
    "input_schema": {
        "type": "object",
        "properties": {"id": {"type": "string", "description": "Dokument-id aus library_index"}},
        "required": ["id"],
    },
}


def _log_library_call(block: Dict[str, Any], result: Dict[str, Any], seconds: float) -> None:
    """One INFO line per library tool call — the only place tool usage is
    observable outside the model transcript (the jobs projection carries the
    aggregate counter, not the per-call detail)."""
    name = block.get("name")
    doc_id = (block.get("input") or {}).get("id", "")
    is_error = bool(result.get("is_error"))
    size = sum(
        len(c.get("text", "")) if c.get("type") == "text"
        else sum(len(cc.get("text", "")) for cc in c.get("content", []))
        for c in result.get("content", [])
        if isinstance(c, dict)
    )
    logger.info(
        f"research-cloud library call: {name}({doc_id!r}) -> "
        f"{'ERROR' if is_error else 'ok'}, {size} chars, {seconds*1000:.0f}ms"
    )


class ResearchCloudExecutorError(Exception):
    """Fail-loud: the cloud executor refuses to run, or the API call errors.

    Never caught-and-silently-rerouted mid-run — once the cloud path has
    started, a failure here is a job error (kein Silent-Fallback in den Pool).

    `status_code` is set only when the failure came from a non-200 Messages
    API response — it carries the upstream HTTP status so the caller can
    decide retryability without parsing the message text (config/protocol
    failures below leave it None).
    """

    def __init__(self, message: str, *, status_code: Optional[int] = None):
        super().__init__(message)
        self.status_code = status_code


# Anthropic Messages API statuses that mean "try again later, nothing is
# wrong with the request itself" — 429 rate-limit and every 5xx (incl. 529
# overloaded_error, which is not a documented HTTP code but observed live).
# Anything else (400/401/403/404/...) is a rejected or malformed request;
# retrying it changes nothing.
TRANSIENT_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504, 508, 509, 529})


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


def _build_tools(
    config: ResearchCloudConfig,
    library_cfg: LibraryConfig,
    perplexity_cfg: Optional[PerplexityConfig] = None,
) -> List[Dict[str, Any]]:
    tools = [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": config.web_search_max_uses},
        {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": config.web_fetch_max_uses},
    ]
    with_library = library_enabled(library_cfg)
    with_perplexity = perplexity_cfg is not None and perplexity_enabled(perplexity_cfg)
    if with_library or with_perplexity:
        # With client tools in the mix, the web tools' dynamic filtering
        # (code execution under the hood) breaks the turn structure: once a
        # client tool_use interleaves with code-exec-backed server tools, a
        # later continuation 400s with "container_id is required when there
        # are pending tool uses generated by code execution with tools" —
        # while NO response in that flow ever carries a top-level `container`
        # to echo (live-reproduced 2026-08-01, /tmp/resp1-3.json; docs:
        # server-tools.md § Mixing server tools and client tools). Forcing
        # direct invocation disables the internal code execution entirely —
        # deterministic, and per docs also the ZDR-eligible configuration.
        # This holds for ANY client tool, not just the library — Perplexity
        # alone triggers the same interleaving.
        for t in tools:
            t["allowed_callers"] = ["direct"]
    # Copies, not the module-level dicts: anything downstream that mutates
    # a tool entry (marker stamping, future per-request tweaks) must never
    # bleed into other requests via shared globals.
    if with_library:
        tools.append(copy.deepcopy(_LIBRARY_INDEX_TOOL))
        tools.append(copy.deepcopy(_LIBRARY_GET_TOOL))
    if with_perplexity:
        tools.append(copy.deepcopy(PERPLEXITY_TOOL))
    return tools


def _client_tool_names(tools: List[Dict[str, Any]]) -> frozenset:
    """Names of the client tools actually offered on this request — the only
    ones the loop may answer."""
    return frozenset(t["name"] for t in tools if "type" not in t)


async def _handle_perplexity_tool_call(
    block: Dict[str, Any],
    perplexity_cfg: PerplexityConfig,
    anonymize: Callable[[str], Awaitable[str]],
    client: httpx.AsyncClient,
) -> tuple:
    """Execute one perplexity_search tool_use. Returns (tool_result, answer|None).

    Anonymization failure is NOT fail-soft: it raises ResearchCloudExecutorError
    and aborts the run — the query is never sent in clear text, and a gate that
    silently let the model continue would hide a broken privacy path. Everything
    after the gate (HTTP, incomplete answer) is fail-soft like library_get.
    """
    tool_use_id = block.get("id")
    frage = ((block.get("input") or {}).get("frage") or "").strip()
    if not frage:
        return (
            {"type": "tool_result", "tool_use_id": tool_use_id, "is_error": True,
             "content": [{"type": "text", "text": "perplexity_search braucht den Parameter 'frage'."}]},
            None,
        )
    try:
        anonymized = await anonymize(frage)
    except Exception as e:
        raise ResearchCloudExecutorError(
            f"perplexity_search: anonymize gate failed — refusing to send the query to Perplexity: {e}"
        ) from e
    if not anonymized or not anonymized.strip():
        raise ResearchCloudExecutorError(
            "perplexity_search: anonymize gate returned empty text — refusing to send the query"
        )
    try:
        answer = await ask_perplexity(anonymized, perplexity_cfg, client)
    except PerplexityCallError as e:
        logger.warning(f"research-cloud: perplexity_search failed (fail-soft): {e}")
        return (
            {"type": "tool_result", "tool_use_id": tool_use_id, "is_error": True,
             "content": [{"type": "text", "text": f"Perplexity nicht erreichbar: {e}. Weiter mit web_search."}]},
            None,
        )
    for w in answer.warnungen:
        logger.warning(f"research-cloud: perplexity_search: {w}")
    return (
        {"type": "tool_result", "tool_use_id": tool_use_id,
         "content": [{"type": "text", "text": format_tool_result_text(answer)}]},
        answer,
    )


async def _handle_library_tool_call(
    block: Dict[str, Any], library_cfg: LibraryConfig, index: Dict[str, Any]
) -> Dict[str, Any]:
    """Execute one client-side library tool_use block against ``index``, the
    index loaded once at the start of this run.

    Fail-soft: any LibraryFetchError (unknown id, catalogue-only entry, network
    error on a single document) becomes a tool_result with is_error=True — the
    model sees the failure and can continue the research without that document.
    The other kind of failure, "the library as a whole does not work", is not
    handled here at all: it aborts the run before it starts
    (library.load_library_for_run).
    """
    name = block.get("name")
    tool_use_id = block.get("id")
    try:
        if name == "library_index":
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                "content": [{"type": "text", "text": json.dumps(index, ensure_ascii=False)}],
            }
        if name == "library_get":
            doc_id = (block.get("input") or {}).get("id")
            if not doc_id:
                raise LibraryFetchError("library_get called without an 'id'")
            doc = await fetch_library_document(doc_id, library_cfg, index=index)
            entry = doc["entry"]
            source = entry.get("source_url") or entry.get("publisher") or doc_id
            title = entry.get("title") or doc_id
            return {
                "type": "tool_result",
                "tool_use_id": tool_use_id,
                # search_result content block (build-with-claude/search-results,
                # "Method 1: from tool calls") — source/title carry the
                # institution, not "the tool", into the model's citations.
                "content": [
                    {
                        "type": "search_result",
                        "source": source,
                        "title": title,
                        "content": [{"type": "text", "text": doc["text"]}],
                        "citations": {"enabled": True},
                    }
                ],
            }
        raise LibraryFetchError(f"unknown library tool: {name!r}")
    except LibraryFetchError as e:
        logger.warning(f"research-cloud: library tool {name!r} failed (fail-soft): {e}")
        return {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "content": [{"type": "text", "text": str(e)}],
            "is_error": True,
        }


async def run_research_cloud(
    query: str,
    system_prompt: str,
    *,
    config: Optional[ResearchCloudConfig] = None,
    api_key: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
    library_config: Optional[LibraryConfig] = None,
    library_index: Optional[Dict[str, Any]] = None,
    perplexity_config: Optional[PerplexityConfig] = None,
    anonymize: Optional[Callable[[str], Awaitable[str]]] = None,
    perplexity_client: Optional[httpx.AsyncClient] = None,
) -> ResearchCloudResult:
    """Run one research-cloud job to completion (all pause_turn continuations).

    Fail loud: raises ResearchCloudExecutorError if ANTHROPIC_API_KEY is unset
    or the Messages API returns a non-200, or the loop exhausts
    max_continuations without a terminal stop_reason. Never falls back to the
    worker pool mid-run — a failed cloud call is a job error, not a silent
    reroute. Nor before the run: the routing check refuses a cloud-pinned
    caller when the lane is capped or switched off, instead of quietly serving
    them from the pool (src/research_cloud/routing.py). Pool fallback survives
    only where the pool is genuinely the caller's home lane — overflow that
    was merely opportunistic, and a routing probe that failed before any
    commitment to the cloud was made.

    ``client``, if given, is used as-is (caller owns its lifecycle) — this is
    the seam tests use to inject a mocked httpx.AsyncClient.
    """
    config = config or ResearchCloudConfig()
    # Workers deliberately never carry ANTHROPIC_API_KEY — claude_cli.py
    # fatals on it, because the CLI would otherwise silently bill the API
    # instead of the subscription pool. The executor reads its own
    # RESEARCH_CLOUD_API_KEY (docker-compose maps it from
    # RESEARCH_CLOUD_ANTHROPIC_KEY in docker/.env).
    #
    # KEIN Rueckfall auf ANTHROPIC_API_KEY mehr (Rafael, 08.09.2026): "wenn dann
    # soll sie nur ueber ihren eigenen schluessel laufen aber nicht ueber den
    # vision schluessel". Der Rueckfall war genau der Weg, auf dem ein einziger
    # Recherche-Lauf die Bildanalyse-Kasse leeren konnte, ohne dass es jemand
    # sah — der Wert stand unter zwei Namen im selben Container. Fehlt der
    # eigene Schluessel, ist das jetzt ein lauter Konfigurationsfehler und kein
    # stilles Umbuchen auf die Kunden-Bildanalyse.
    api_key = api_key or os.environ.get("RESEARCH_CLOUD_API_KEY")
    if not api_key:
        raise ResearchCloudExecutorError(
            "RESEARCH_CLOUD_API_KEY not set — refusing to run the "
            "research-cloud executor. Set RESEARCH_CLOUD_ANTHROPIC_KEY in "
            "docker/.env (the research lane pays from its OWN key; falling "
            "back to the image lane's key is no longer allowed)."
        )
    _log_key_lane_once(api_key)

    headers = {
        "Content-Type": "application/json",
        "x-api-key": api_key,
        "anthropic-version": ANTHROPIC_VERSION,
    }
    library_cfg = library_config or load_library_config()
    # The caller normally resolves the library first (it needs the index for the
    # prompt catalogue) and hands it in. When it did not, resolve it here — the
    # executor must never run with the library merely *assumed* to work.
    if library_index is None:
        try:
            library_index = await load_library_for_run(library_cfg)
        except LibraryUnavailableError as e:
            raise ResearchCloudExecutorError(str(e)) from e
    if library_index is None and library_enabled(library_cfg):
        # Only reachable when a caller passes library_index=None explicitly for
        # a library that IS on. Refuse rather than run a "library-less" research
        # under a config that promises one.
        raise ResearchCloudExecutorError(
            "research library is enabled but no index was loaded for this run"
        )
    perplexity_cfg = perplexity_config or load_perplexity_config()
    try:
        check_perplexity_usable(perplexity_cfg)
    except PerplexityUnavailableError as e:
        raise ResearchCloudExecutorError(str(e)) from e
    if perplexity_enabled(perplexity_cfg) and anonymize is None:
        # No anonymizer, no third-party tool — never "send it raw this once".
        raise ResearchCloudExecutorError(
            "perplexity_search is enabled but no anonymizer was handed to the executor — "
            "refusing to offer a tool whose queries would leave unanonymized"
        )
    tools = _build_tools(config, library_cfg, perplexity_cfg)
    client_tools = _client_tool_names(tools)
    system: List[Dict[str, Any]] = [
        {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}
    ]
    # base_messages = all COMPLETED turns (user query + per client-tool-round
    # assistant/tool_result pairs); messages = base + in-progress assistant echo.
    base_messages: List[Dict[str, Any]] = [{"role": "user", "content": query}]
    messages: List[Dict[str, Any]] = list(base_messages)
    usage = ResearchCloudUsage()
    searches = fetches = library_calls = perplexity_calls = 0
    perplexity_cost_usd = 0.0
    perplexity_cost_missing = 0
    container_id: Optional[str] = None
    iteration = 0
    t0 = time.monotonic()
    parsed: Optional[AnthropicMessagesResponse] = None

    owns_client = client is None
    http_client = client or httpx.AsyncClient(timeout=config.http_timeout_seconds)
    owns_pplx_client = perplexity_client is None and perplexity_enabled(perplexity_cfg)
    pplx_client = perplexity_client or (httpx.AsyncClient() if owns_pplx_client else None)
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
                    f"{response.text[:500]}",
                    status_code=response.status_code,
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

            if parsed.stop_reason == "tool_use":
                # A pending client tool_use always yields stop_reason
                # "tool_use", never "pause_turn" — even if server_tool_use
                # blocks are also present in this same response (bridge-research
                # 2026-07-31, platform.claude.com/docs/.../handling-stop-reasons).
                # Only the client tools offered on THIS request are answerable;
                # anything else is a tool the model was never given (fail
                # loud, not a silent skip).
                tool_use_blocks = [b for b in parsed.content if b.get("type") == "tool_use"]
                if not tool_use_blocks:
                    raise ResearchCloudExecutorError(
                        "research-cloud executor got stop_reason=tool_use with no "
                        f"client tool_use block in content: {parsed.content!r}"
                    )
                # Fail fast on anything we cannot answer — an unanswered
                # client tool_use would otherwise surface later as an opaque
                # API 400 ("tool_use ids were found without tool_result").
                foreign = [b.get("name") for b in tool_use_blocks if b.get("name") not in client_tools]
                if foreign:
                    raise ResearchCloudExecutorError(
                        f"research-cloud executor cannot answer client tools {foreign!r} — "
                        f"only {sorted(client_tools)} are defined on this request"
                    )
                # Programmatic tool calling (caller != direct) would mean the
                # call comes from paused code in a server-side container whose
                # id we would have to echo back — a flow this executor
                # deliberately disables via allowed_callers=["direct"]. If it
                # shows up anyway, the API contract changed: stop loudly.
                ptc = [
                    b.get("name") for b in tool_use_blocks
                    if (b.get("caller") or {}).get("type") not in (None, "direct")
                ]
                if ptc:
                    raise ResearchCloudExecutorError(
                        f"research-cloud executor got programmatic (non-direct) tool calls "
                        f"{ptc!r} despite allowed_callers=['direct'] — refusing to continue "
                        f"a container flow whose id the API did not expose"
                    )
                tool_results = []
                for tool_block in tool_use_blocks:
                    t_start = time.monotonic()
                    if tool_block.get("name") == PERPLEXITY_TOOL_NAME:
                        if perplexity_calls >= config.perplexity_max_uses:
                            tool_results.append({
                                "type": "tool_result", "tool_use_id": tool_block.get("id"), "is_error": True,
                                "content": [{"type": "text", "text": (
                                    f"perplexity_search: Budget dieser Recherche erschöpft "
                                    f"({config.perplexity_max_uses} Aufrufe). Weiter mit web_search/web_fetch."
                                )}],
                            })
                            continue
                        perplexity_calls += 1
                        tool_result, answer = await _handle_perplexity_tool_call(
                            tool_block, perplexity_cfg, anonymize, pplx_client
                        )
                        if answer is not None:
                            if answer.kosten_usd is None:
                                perplexity_cost_missing += 1
                                logger.error(
                                    "research-cloud: perplexity answer carries no usage.cost.total_cost — "
                                    "this call is NOT in the booked cost"
                                )
                            else:
                                perplexity_cost_usd += answer.kosten_usd
                        logger.info(
                            f"research-cloud perplexity call -> "
                            f"{'ERROR' if tool_result.get('is_error') else 'ok'}, "
                            f"{len(answer.quellen) if answer else 0} sources, "
                            f"cost_usd={answer.kosten_usd if answer else None}, "
                            f"{(time.monotonic() - t_start) * 1000:.0f}ms"
                        )
                        tool_results.append(tool_result)
                        continue
                    tool_result = await _handle_library_tool_call(
                        tool_block, library_cfg, library_index or {}
                    )
                    library_calls += 1
                    _log_library_call(tool_block, tool_result, time.monotonic() - t_start)
                    tool_results.append(tool_result)
                # The client tool_result ends this assistant turn — fold it into
                # the retained history. Completed turns MUST stay in the request:
                # a later server_tool_use (web_fetch) may reference a source tool
                # (web_search) from an EARLIER turn, and the API 400s with
                # "source tool ... not found" if that turn was dropped
                # (live-verified job_a2c433bd, 2026-07-31).
                base_messages = base_messages + [
                    {"role": "assistant", "content": parsed.content},
                    {"role": "user", "content": tool_results},
                ]
                messages = list(base_messages)
                continue

            if parsed.stop_reason == "max_tokens":
                # A report cut off at the token ceiling is not a report, and it
                # cannot be continued: assistant prefill is removed on Sonnet 5
                # (400), so there is no way to resume a truncated turn. Returning
                # it as status="success" is the same silent-degradation class as
                # the library that quietly switched itself off — worse here,
                # because the missing part is typically the tail: the source list
                # and the caveats. Measured 2026-09-05 on the first
                # catalogue-enabled run, whose source list ended mid-entry.
                raise ResearchCloudExecutorError(
                    f"research-cloud run hit max_tokens={config.max_tokens} — the report "
                    f"is truncated and cannot be resumed (no assistant prefill on "
                    f"{config.model}). Refusing to return a partial report as a finished one."
                )

            if parsed.stop_reason != "pause_turn":
                break

            # Do NOT append a synthetic "Continue" user turn — the API
            # detects the trailing server_tool_use block and resumes
            # automatically (shared/tool-use-concepts.md: Stop reasons for
            # server-side tools). parsed.content is CUMULATIVE within the
            # current assistant turn, so the in-progress turn is replaced,
            # while all completed turns (base_messages) are kept.
            messages = base_messages + [
                {"role": "assistant", "content": parsed.content},
            ]
        else:
            raise ResearchCloudExecutorError(
                f"research-cloud executor exceeded max_continuations="
                f"{config.max_continuations} without finishing (still {parsed.stop_reason})"
            )
    finally:
        if owns_client:
            await http_client.aclose()
        if owns_pplx_client and pplx_client is not None:
            await pplx_client.aclose()

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
        library_calls=library_calls,
        perplexity_calls=perplexity_calls,
        perplexity_cost_usd=round(perplexity_cost_usd, 6),
        perplexity_cost_missing=perplexity_cost_missing,
        iterations=iteration + 1,
        stop_reason=parsed.stop_reason,
        duration_seconds=round(duration, 2),
        container_id=container_id,
    )
