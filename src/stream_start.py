"""stream_start — the ONE place where a streamed answer begins (BR9c, 10.10.2026).

A streamed answer is a StreamingResponse over an async generator. Starlette
sends status line and headers before the generator runs, so before BR9c
every failure up to the first chunk — OpenAI-compatible upstream != 200 or
network gone after retries, Bedrock model/region mismatch, CLI
WorkerUnavailableError — reached the client as "200, no event: error, no
[DONE]": indistinguishable from a dropped connection, and without a verdict.

event_stream_response() pulls the first chunk inside the route handler:

  * Failure before the first chunk: nothing is sent yet, so the exception
    leaves the route exactly like on the non-streaming path and the app's
    exception handlers answer with a real HTTP status and the error_contract
    fields (WorkerUnavailableError → 429 failover to the next worker,
    HTTPException → bridge_error, anything else → classify_exception).
    ProviderError is the one exception that carries an upstream status no
    handler knows; it is translated here (provider_start_error).
  * Failure after the first chunk: the 200 is out. The stream ends with
    ``event: error`` carrying retryable / retry_after_s (stream_error_event);
    no [DONE] follows.

Headers go out with the first chunk, not before it. A caller waits for the
headers as long as it waits for the first token — never longer than the
non-streaming path waits for the whole answer.
"""
import asyncio
import json
import logging
from typing import Any, AsyncIterator, Dict, Mapping, Optional

from fastapi.responses import Response, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from src.error_contract import (
    UNCLASSIFIED_RETRYABLE,
    envelope_fields,
    fields,
    is_retryable_status,
)

logger = logging.getLogger(__name__)

# Wire status for a provider that rejected the bridge's own credentials: the
# caller's request is fine, the bridge's dependency is not (like Bedrock's
# AccessDenied → 424). Passing 401/403 through would read as "your key".
_PROVIDER_REJECTED_STATUS = 424

# How often the wait for the first chunk asks whether the caller is still
# there (same cadence as the CLI path's disconnect monitor).
_DISCONNECT_POLL_S = 0.5

# nginx's "client closed request". Nobody reads it; it marks the access log.
_CLIENT_CLOSED_STATUS = 499


def provider_retryable(status_code: int) -> bool:
    """Verdict for an OpenAI-compatible provider status (rule 2), including
    the network failure after retries, which has no HTTP status of its own."""
    from src.providers.openai_compatible import NETWORK_ERROR_STATUS

    return status_code == NETWORK_ERROR_STATUS or is_retryable_status(status_code)


def stream_error_verdict(exc: BaseException) -> Dict[str, Any]:
    """retryable / retry_after_s for an exception that ends a stream."""
    from src.claude_cli import OrgSubscriptionDisabledError, WorkerUnavailableError
    from src.middleware.bridge_error import BridgeError
    from src.providers.openai_compatible import ProviderError

    # Rule 1: the raiser's own verdict (BedrockStreamAborted, JobFailure, ...).
    own = getattr(exc, "retryable", None)
    if isinstance(own, bool):
        return fields(own, getattr(exc, "retry_after_s", None))
    if isinstance(exc, ProviderError):
        return fields(provider_retryable(exc.status_code))
    if isinstance(exc, OrgSubscriptionDisabledError):
        # This worker is locked; the lock says when it may serve again.
        return fields(True, exc.lock_seconds)
    if isinstance(exc, WorkerUnavailableError):
        # Another worker can serve it; before the first chunk this is nginx's
        # 429 failover, after it the caller has to ask again.
        return fields(True)
    if isinstance(exc, BridgeError):
        try:
            body = json.loads(exc.response.body)
        except (AttributeError, TypeError, ValueError):
            body = None
        return envelope_fields(body) or fields(UNCLASSIFIED_RETRYABLE)
    if isinstance(exc, StarletteHTTPException):
        return envelope_fields(exc.detail) or fields(is_retryable_status(exc.status_code))
    return fields(UNCLASSIFIED_RETRYABLE)


def error_event(
    message: str, error_type: str, code: str, verdict: Mapping[str, Any], **extra: Any,
) -> str:
    """The one wire form of a stream error (BR9d): ``event: error`` with
    ``{"error": {message, type, code, retryable, retry_after_s}}`` — nested,
    like the sync envelope. PC1 (ai-bridge-client core/sse.ts) reads
    retryable only there; a flat ``{"error": "<text>"}`` reads as no verdict.
    ``extra`` adds envelope fields (source, reason, ...) next to them."""
    payload = {"error": {"message": message, "type": error_type, "code": code, **extra, **verdict}}
    return f"event: error\ndata: {json.dumps(payload)}\n\n"


def stream_error_event(exc: BaseException) -> str:
    """``event: error`` that ends a stream after its first chunk."""
    from src.claude_cli import OrgSubscriptionDisabledError
    from src.middleware.bridge_error import (
        REASON_ACCOUNT_ORG_DISABLED,
        SOURCE_BRIDGE_ACCOUNT,
        TYPE_ACCOUNT_EXHAUSTED,
    )

    if isinstance(exc, OrgSubscriptionDisabledError):
        # Same code and reason as its 503 before the first chunk (BR9e).
        return error_event(
            f"[Bridge {exc.worker_id}] {exc}", "api_error",
            REASON_ACCOUNT_ORG_DISABLED, stream_error_verdict(exc),
            source=SOURCE_BRIDGE_ACCOUNT, bridge_type=TYPE_ACCOUNT_EXHAUSTED,
            reason=REASON_ACCOUNT_ORG_DISABLED, bridge_worker=exc.worker_id,
        )
    envelope = _bridge_error_envelope(exc)
    if envelope is not None:
        # A BridgeError (e.g. the vision branch, BR9e) already says what went
        # wrong — keep its message, type, code and reason instead of
        # "bridge_error" / "BridgeError".
        rest = {k: v for k, v in envelope.items()
                if k not in ("message", "type", "code", "retryable", "retry_after_s")}
        return error_event(
            envelope["message"], str(envelope.get("type") or "streaming_error"),
            str(envelope.get("code") or type(exc).__name__), stream_error_verdict(exc), **rest,
        )
    detail = getattr(exc, "detail", None)
    message = detail if isinstance(detail, str) else str(exc) or type(exc).__name__
    return error_event(message, "streaming_error", type(exc).__name__, stream_error_verdict(exc))


def _bridge_error_envelope(exc: BaseException) -> Optional[Dict[str, Any]]:
    """The ``error`` object of a BridgeError's response body, if it has one."""
    from src.middleware.bridge_error import BridgeError

    if not isinstance(exc, BridgeError):
        return None
    try:
        err = json.loads(exc.response.body).get("error")
    except (AttributeError, TypeError, ValueError):
        return None
    if isinstance(err, dict) and isinstance(err.get("message"), str) and err["message"]:
        return err
    return None


def provider_start_error(exc: Any) -> Exception:
    """BridgeError for a ProviderError raised before the first chunk.

    Transient (429/5xx, network after retries) → 429 with Retry-After, the
    worker contract for "come back later". Rejected credentials → 424.
    Any other rejection keeps its 4xx; the rest is 424. All non-transient
    ones say retryable:false explicitly (a provider 501 is not a bridge 500).
    """
    from src.middleware.bridge_error import (
        SOURCE_UPSTREAM_NETWORK,
        SOURCE_UPSTREAM_PROVIDER,
        TYPE_UPSTREAM_ERROR,
        TYPE_UPSTREAM_TIMEOUT,
        BridgeError,
        bridge_error,
    )
    from src.providers.openai_compatible import NETWORK_ERROR_STATUS

    status = exc.status_code
    message = f"OpenAI-compatible provider failed before the first chunk: {exc}"
    if provider_retryable(status):
        network = status == NETWORK_ERROR_STATUS
        return BridgeError(bridge_error(
            source=SOURCE_UPSTREAM_NETWORK if network else SOURCE_UPSTREAM_PROVIDER,
            error_type=TYPE_UPSTREAM_TIMEOUT if network else TYPE_UPSTREAM_ERROR,
            reason="provider_upstream_network" if network else "provider_upstream_error",
            message=message,
            status_code=429,
            retry_after_s=15,
            retryable_override=True,
            extra={"upstream_status": None if network else status},
        ))
    if status in (401, 403):
        wire = _PROVIDER_REJECTED_STATUS
    elif 400 <= status < 500:
        wire = status
    else:
        wire = _PROVIDER_REJECTED_STATUS
    return BridgeError(bridge_error(
        source=SOURCE_UPSTREAM_PROVIDER,
        error_type=TYPE_UPSTREAM_ERROR,
        reason="provider_upstream_rejected",
        message=message,
        status_code=wire,
        retryable_override=False,
        extra={"upstream_status": status},
    ))


async def _rest(first: Optional[str], gen: AsyncIterator[str]) -> AsyncIterator[str]:
    try:
        if first is None:
            return
        yield first
        async for chunk in gen:
            yield chunk
    except Exception as exc:  # the 200 is out: end as event: error
        logger.error(f"Stream failed after its first chunk: {type(exc).__name__}: {exc}")
        yield stream_error_event(exc)
    finally:
        await gen.aclose()


class _CallerGone(Exception):
    """The caller left while the route waited for the first chunk."""


async def _first_chunk_while_caller_present(gen: AsyncIterator[str]) -> str:
    """``gen.__anext__()``, abandoned as soon as the caller is gone (BR9d).

    Before BR9c Starlette's listen_for_disconnect cancelled the generator the
    moment the caller left. Since the first chunk is pulled inside the route,
    nobody listens until it exists — an OpenAI-compatible or Bedrock call
    (with ``thinking``: the whole paid thinking phase) ran on for a caller
    who was no longer there. So the wait for the first chunk asks the
    request's shared delivery probe; on disconnect the pending ``__anext__``
    is cancelled (the provider call inside it with it) and ``gen`` closed.

    The SHARED probe, not Request.is_disconnected(): ``http.disconnect``
    arrives once, and the ledger asks the same probe whether the answer was
    delivered (src/activity/delivery.py). Without a probe (no request
    context) the caller counts as present."""
    from src.activity import delivery

    # BR9e (BR9dR MUSS): ``__anext__`` stays in THIS task. A generator that
    # enters ``asyncio.timeout`` before its first yield (the CLI path's
    # MAX_TIMEOUT around run_completion) binds that timeout to the task that
    # runs this step. Pulled in a helper task, the timeout later cancelled a
    # task that had already finished — and never fired. So the watcher runs
    # beside the route and cancels the route itself when the caller is gone.
    me = asyncio.current_task()
    gone = False

    async def watch() -> None:
        nonlocal gone
        while not await delivery.caller_gone():
            await asyncio.sleep(_DISCONNECT_POLL_S)
        gone = True
        me.cancel()

    watcher = asyncio.ensure_future(watch())
    try:
        chunk = await gen.__anext__()
    except asyncio.CancelledError:
        # Ours only if nobody else cancelled the route as well; otherwise the
        # route's own cancellation goes on (the provider call is gone either way).
        if not (gone and me.uncancel() == 0):
            raise
        await gen.aclose()
        logger.warning("Caller disconnected before the first chunk: stream generator closed")
        raise _CallerGone()
    finally:
        watcher.cancel()
    if gone:
        # The generator swallowed the cancel and produced a chunk anyway.
        me.uncancel()
        await gen.aclose()
        logger.warning("Caller disconnected before the first chunk: stream generator closed")
        raise _CallerGone()
    return chunk


async def event_stream_response(
    gen: AsyncIterator[str],
    *,
    headers: Optional[Mapping[str, str]] = None,
    media_type: str = "text/event-stream",
) -> StreamingResponse:
    """StreamingResponse that starts only once ``gen`` produced its first
    chunk. A failure before that leaves the route as an exception (HTTP error
    with verdict); a failure after it ends the stream as ``event: error``.
    A caller who leaves before the first chunk gets the generator closed."""
    from src.providers.openai_compatible import ProviderError

    try:
        first: Optional[str] = await _first_chunk_while_caller_present(gen)
    except _CallerGone:
        return Response(status_code=_CLIENT_CLOSED_STATUS)
    except StopAsyncIteration:
        first = None
    except ProviderError as exc:
        raise provider_start_error(exc) from exc
    return StreamingResponse(
        _rest(first, gen), media_type=media_type,
        headers=dict(headers) if headers else None,
    )
