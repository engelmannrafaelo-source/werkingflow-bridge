"""
bridge_error — Structured error envelope for ALL non-2xx responses from the bridge.

Goal: external callers never see ambiguous errors. Every error response carries
a clear `source` discriminator so apps can decide whether to retry, back off,
or surface to the user.

Sources:
    bridge_internal     → throttle, queue timeout, internal exception
    bridge_account      → all worker accounts exhausted (weekly/session limit)
    upstream_anthropic  → Anthropic API returned an error (after our retries)
    upstream_network    → network timeout / connection reset (after our retries)

Types (within source):
    throttle            → token-budget cap reached, would-have-blocked
    queue_timeout       → waited for capacity, none freed in time
    account_exhausted   → no routable worker (all accounts at weekly limit)
    upstream_error      → Anthropic 5xx / rate-limit after retries
    upstream_timeout    → network or read timeout after retries
    internal            → unexpected exception in bridge code (should be rare)

Every response includes a `Retry-After` header when retryable.
"""

import os
import time
import logging
from typing import Optional, Dict, Any
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

WORKER_NAME = os.getenv("INSTANCE_NAME", "unknown")

# Source / type vocab (also documented in the docstring above)
SOURCE_BRIDGE_INTERNAL = "bridge_internal"
SOURCE_BRIDGE_ACCOUNT = "bridge_account"
SOURCE_UPSTREAM_ANTHROPIC = "upstream_anthropic"
SOURCE_UPSTREAM_NETWORK = "upstream_network"

TYPE_THROTTLE = "throttle"
TYPE_QUEUE_TIMEOUT = "queue_timeout"
TYPE_ACCOUNT_EXHAUSTED = "account_exhausted"
TYPE_UPSTREAM_ERROR = "upstream_error"
TYPE_UPSTREAM_TIMEOUT = "upstream_timeout"
TYPE_INTERNAL = "internal"


def bridge_error(
    *,
    source: str,
    error_type: str,
    message: str,
    status_code: int = 503,
    retry_after_s: Optional[int] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> JSONResponse:
    """
    Build a structured bridge error response.

    Args:
        source:       one of SOURCE_* constants
        error_type:   one of TYPE_* constants
        message:      human-readable explanation (will be prefixed with [Bridge <worker>])
        status_code:  HTTP status (default 503; use 429 for explicit rate-limit semantics)
        retry_after_s: hint for client backoff; sets Retry-After header
        extra:        optional additional fields merged into the error body

    Returns:
        JSONResponse with the structured body.
    """
    retryable = status_code in (408, 425, 429, 500, 502, 503, 504)
    full_message = f"[Bridge {WORKER_NAME}] {message}"
    # Envelope combines OpenAI-compatible fields (message, type, code) with
    # bridge-specific metadata so callers can ALSO discriminate by `source`
    # without breaking existing OpenAI-style error parsers.
    body: Dict[str, Any] = {
        "error": {
            # OpenAI-compat fields — DON'T break existing clients.
            "message": full_message,
            "type": "bridge_throttle" if source == SOURCE_BRIDGE_INTERNAL else "api_error",
            "code": str(status_code),
            # Bridge-specific discriminators
            "source": source,
            "bridge_type": error_type,
            "retryable": retryable,
            "retry_after_s": retry_after_s,
            "bridge_worker": WORKER_NAME,
            "timestamp": int(time.time()),
        }
    }
    if extra:
        body["error"].update(extra)

    headers: Dict[str, str] = {}
    if retry_after_s and retry_after_s > 0:
        headers["Retry-After"] = str(int(retry_after_s))

    # Log at info for retryable, warning for non-retryable — these are normal
    # operational events, not bugs, so don't spam ERROR.
    log_fn = logger.info if retryable else logger.warning
    log_fn(
        f"bridge_error: source={source} type={error_type} status={status_code} "
        f"retry_after={retry_after_s}s msg={message}"
    )

    return JSONResponse(status_code=status_code, content=body, headers=headers)


# Convenience constructors — these are the most common paths

def throttle_error(
    cap_tokens: int,
    inflight_tokens: int,
    retry_after_s: int = 30,
) -> JSONResponse:
    """Token-budget cap reached. Tells caller to back off briefly."""
    return bridge_error(
        source=SOURCE_BRIDGE_INTERNAL,
        error_type=TYPE_THROTTLE,
        message=(
            f"Internal throttle: token-budget at {inflight_tokens:,}/{cap_tokens:,} cap. "
            f"Suggested retry in ~{retry_after_s}s."
        ),
        status_code=503,
        retry_after_s=retry_after_s,
        extra={
            "cap_tokens": cap_tokens,
            "inflight_tokens": inflight_tokens,
        },
    )


def queue_timeout_error(
    cap_tokens: int,
    inflight_tokens: int,
    waited_s: float,
    retry_after_s: int = 60,
) -> JSONResponse:
    """Waited for capacity, none freed in time. Caller should retry later."""
    return bridge_error(
        source=SOURCE_BRIDGE_INTERNAL,
        error_type=TYPE_QUEUE_TIMEOUT,
        message=(
            f"Queued for capacity {waited_s:.1f}s but no slot freed "
            f"(cap={cap_tokens:,}, inflight={inflight_tokens:,}). "
            f"Retry in ~{retry_after_s}s."
        ),
        status_code=503,
        retry_after_s=retry_after_s,
        extra={
            "cap_tokens": cap_tokens,
            "inflight_tokens": inflight_tokens,
            "waited_s": round(waited_s, 2),
        },
    )


def account_exhausted_error(retry_after_s: int = 3600) -> JSONResponse:
    """All worker accounts at weekly limit. Long backoff."""
    return bridge_error(
        source=SOURCE_BRIDGE_ACCOUNT,
        error_type=TYPE_ACCOUNT_EXHAUSTED,
        message=(
            "All worker accounts have reached their weekly Anthropic limit. "
            f"Retry in ~{retry_after_s // 60} minutes."
        ),
        status_code=503,
        retry_after_s=retry_after_s,
    )


def upstream_error(
    detail: str,
    status_code: int = 502,
    retry_after_s: int = 30,
) -> JSONResponse:
    """Anthropic returned an error after our retry attempts."""
    return bridge_error(
        source=SOURCE_UPSTREAM_ANTHROPIC,
        error_type=TYPE_UPSTREAM_ERROR,
        message=f"Upstream Anthropic error after retries: {detail}",
        status_code=status_code,
        retry_after_s=retry_after_s,
    )


def upstream_timeout_error(
    waited_s: float,
    retry_after_s: int = 30,
) -> JSONResponse:
    """Network/read timeout to upstream after retry attempts."""
    return bridge_error(
        source=SOURCE_UPSTREAM_NETWORK,
        error_type=TYPE_UPSTREAM_TIMEOUT,
        message=f"Upstream network timeout after {waited_s:.1f}s of retries.",
        status_code=504,
        retry_after_s=retry_after_s,
    )


# ----------------------------------------------------------------------
# Custom exception — for use inside FastAPI dependencies / handlers, since
# dependencies cannot return JSONResponse directly. Raise this and let the
# global exception handler unwrap it into the structured envelope.
# ----------------------------------------------------------------------
class BridgeError(Exception):
    """
    Raise to abort a request with a structured bridge envelope.

    Usage:
        raise BridgeError(throttle_error(cap, inflight))
        raise BridgeError(queue_timeout_error(cap, inflight, waited_s))

    The global exception handler (registered in main.py) inspects
    self.response and returns it as the HTTP response.
    """
    def __init__(self, response: JSONResponse) -> None:
        super().__init__("bridge_error")
        self.response = response


def raise_throttle(cap_tokens: int, inflight_tokens: int, retry_after_s: int = 30) -> None:
    raise BridgeError(throttle_error(cap_tokens, inflight_tokens, retry_after_s))


def raise_queue_timeout(
    cap_tokens: int, inflight_tokens: int, waited_s: float, retry_after_s: int = 60,
) -> None:
    raise BridgeError(queue_timeout_error(cap_tokens, inflight_tokens, waited_s, retry_after_s))


def raise_account_exhausted(retry_after_s: int = 3600) -> None:
    raise BridgeError(account_exhausted_error(retry_after_s))


def raise_upstream_error(detail: str, status_code: int = 502, retry_after_s: int = 30) -> None:
    raise BridgeError(upstream_error(detail, status_code, retry_after_s))


def raise_upstream_timeout(waited_s: float, retry_after_s: int = 30) -> None:
    raise BridgeError(upstream_timeout_error(waited_s, retry_after_s))


def internal_error(
    detail: str,
    status_code: int = 500,
) -> JSONResponse:
    """
    Unexpected exception inside the bridge — these should be rare and
    investigated. We still return a structured body so callers can tell it
    apart from upstream errors.
    """
    return bridge_error(
        source=SOURCE_BRIDGE_INTERNAL,
        error_type=TYPE_INTERNAL,
        message=f"Internal bridge error: {detail}",
        status_code=status_code,
        retry_after_s=10 if status_code >= 500 else None,
    )
