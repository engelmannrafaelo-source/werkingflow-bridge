"""
bridge_error — Structured error envelope for ALL non-2xx responses from the bridge.

Worker contract (enforced by this module):
    Every worker response is EITHER 2xx OR 429-with-reason — OR 500 as an
    explicit contract-violation alert signal. No 502/503/504 on the wire
    from worker code paths. The one caveat: WorkerUnavailableError still emits
    503 *to nginx* as the failover handshake — see main.py. Phase 2 will
    unify that at the nginx layer.

Sources (error.source):
    bridge_internal     → throttle, queue timeout, internal exception
    bridge_account      → all worker accounts exhausted (weekly/session limit)
    bridge_config       → bridge is misconfigured (missing env, bad creds)
    upstream_anthropic  → Anthropic API returned an error (after our retries)
    upstream_network    → network timeout / connection reset (after our retries)

Types (error.bridge_type, within source):
    throttle            → token-budget cap reached, would-have-blocked
    queue_timeout       → waited for capacity, none freed in time
    account_exhausted   → no routable worker (all accounts at weekly limit)
    upstream_error      → Anthropic 5xx / rate-limit after retries
    upstream_timeout    → network or read timeout after retries
    internal            → unexpected exception in bridge code (contract violation)
    configuration       → bridge misconfigured (contract violation)

Reasons (error.reason) — narrow stable identifiers for panel aggregation:
    worker_token_budget_full         → adaptive limiter cap hit           (429)
    worker_queue_exhausted           → queued but never admitted          (429)
    worker_account_weekly_exhausted  → all accounts at weekly limit       (429)
    claude_upstream_error            → upstream Anthropic error           (429)
    claude_upstream_timeout          → upstream network/read timeout      (429)
    worker_internal_error            → bug / unclassified exception       (500)
    worker_misconfigured             → missing env, bad credential        (500)

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
SOURCE_BRIDGE_CONFIG = "bridge_config"
SOURCE_UPSTREAM_ANTHROPIC = "upstream_anthropic"
SOURCE_UPSTREAM_NETWORK = "upstream_network"

TYPE_THROTTLE = "throttle"
TYPE_QUEUE_TIMEOUT = "queue_timeout"
TYPE_ACCOUNT_EXHAUSTED = "account_exhausted"
TYPE_UPSTREAM_ERROR = "upstream_error"
TYPE_UPSTREAM_TIMEOUT = "upstream_timeout"
TYPE_INTERNAL = "internal"
TYPE_CONFIG = "configuration"

# Reason codes — narrow, stable identifiers for why a 429 (or 500) was emitted.
# These go into `error.reason` and drive panel aggregation / alerting.
# Contract: every worker response is EITHER 2xx OR 429-with-reason.
# 500-class reasons are contract violations — they should page, not be expected.
REASON_TOKEN_BUDGET_FULL = "worker_token_budget_full"
REASON_QUEUE_EXHAUSTED = "worker_queue_exhausted"
REASON_ACCOUNT_WEEKLY_EXHAUSTED = "worker_account_weekly_exhausted"
REASON_UPSTREAM_ANTHROPIC_ERROR = "claude_upstream_error"
REASON_UPSTREAM_ANTHROPIC_TIMEOUT = "claude_upstream_timeout"
# 500-class (contract violation — must be investigated)
REASON_INTERNAL = "worker_internal_error"
REASON_MISCONFIGURED = "worker_misconfigured"


def bridge_error(
    *,
    source: str,
    error_type: str,
    message: str,
    status_code: int = 429,
    retry_after_s: Optional[int] = None,
    extra: Optional[Dict[str, Any]] = None,
    retryable_override: Optional[bool] = None,
    reason: Optional[str] = None,
) -> JSONResponse:
    """
    Build a structured bridge error response.

    Args:
        source:       one of SOURCE_* constants
        error_type:   one of TYPE_* constants
        message:      human-readable explanation (will be prefixed with [Bridge <worker>])
        status_code:  HTTP status (default 429 — every transient/capacity error.
                      500 is reserved for contract-violation bugs only.)
        retry_after_s: hint for client backoff; sets Retry-After header
        extra:        optional additional fields merged into the error body
        reason:       narrow stable identifier (REASON_*) for panel aggregation

    Returns:
        JSONResponse with the structured body.
    """
    if retryable_override is not None:
        retryable = retryable_override
    else:
        retryable = status_code in (408, 425, 429, 500, 502, 503, 504)
    full_message = f"[Bridge {WORKER_NAME}] {message}"
    # Envelope combines OpenAI-compatible fields (message, type, code) with
    # bridge-specific metadata so callers can ALSO discriminate by `source`
    # without breaking existing OpenAI-style error parsers.
    if source == SOURCE_BRIDGE_INTERNAL:
        compat_type = "bridge_throttle"
    elif source == SOURCE_BRIDGE_CONFIG:
        compat_type = "configuration_error"
    else:
        compat_type = "api_error"
    body: Dict[str, Any] = {
        "error": {
            # OpenAI-compat fields — DON'T break existing clients.
            "message": full_message,
            "type": compat_type,
            "code": str(status_code),
            # Bridge-specific discriminators
            "source": source,
            "bridge_type": error_type,
            "reason": reason,
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
        f"bridge_error: source={source} type={error_type} reason={reason} "
        f"status={status_code} retry_after={retry_after_s}s msg={message}"
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
        reason=REASON_TOKEN_BUDGET_FULL,
        message=(
            f"Internal throttle: token-budget at {inflight_tokens:,}/{cap_tokens:,} cap. "
            f"Suggested retry in ~{retry_after_s}s."
        ),
        status_code=429,
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
        reason=REASON_QUEUE_EXHAUSTED,
        message=(
            f"Queued for capacity {waited_s:.1f}s but no slot freed "
            f"(cap={cap_tokens:,}, inflight={inflight_tokens:,}). "
            f"Retry in ~{retry_after_s}s."
        ),
        status_code=429,
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
        reason=REASON_ACCOUNT_WEEKLY_EXHAUSTED,
        message=(
            "All worker accounts have reached their weekly Anthropic limit. "
            f"Retry in ~{retry_after_s // 60} minutes."
        ),
        status_code=429,
        retry_after_s=retry_after_s,
    )


def upstream_error(
    detail: str,
    status_code: int = 429,
    retry_after_s: int = 30,
) -> JSONResponse:
    """
    Anthropic returned an error after our retry attempts.

    Surfaces as 429 by default — from the client's perspective the Bridge
    is temporarily unable to serve the request, and the only correct action
    is to back off and retry. The `source=upstream_anthropic` + `reason`
    fields carry the forensic detail for panel/alerting.
    """
    # Accept legacy 5xx inputs but normalize to 429 at the wire — the
    # contract is: worker never emits 5xx for transient upstream issues.
    if status_code in (500, 502, 503, 504, 529):
        status_code = 429
    return bridge_error(
        source=SOURCE_UPSTREAM_ANTHROPIC,
        error_type=TYPE_UPSTREAM_ERROR,
        reason=REASON_UPSTREAM_ANTHROPIC_ERROR,
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
        reason=REASON_UPSTREAM_ANTHROPIC_TIMEOUT,
        message=f"Upstream network timeout after {waited_s:.1f}s of retries.",
        status_code=429,
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
    Unexpected exception inside the bridge — contract violation.

    These are kept at HTTP 500 on purpose: the worker contract guarantees
    2xx or 429-with-reason. A 500 on the wire is an explicit alert signal
    for monitoring: something escaped classification and must be fixed.
    """
    return bridge_error(
        source=SOURCE_BRIDGE_INTERNAL,
        error_type=TYPE_INTERNAL,
        reason=REASON_INTERNAL,
        message=f"Internal bridge error: {detail}",
        status_code=status_code,
        retry_after_s=10 if status_code >= 500 else None,
    )


def config_error(detail: str, status_code: int = 500) -> JSONResponse:
    """
    Bridge is misconfigured (missing env var, invalid credential, etc.).
    NOT retryable — retrying won't help until operator fixes the config.
    Contract violation: surfaces as 500 to page operators.
    """
    return bridge_error(
        source=SOURCE_BRIDGE_CONFIG,
        error_type=TYPE_CONFIG,
        reason=REASON_MISCONFIGURED,
        message=f"Bridge configuration error: {detail}",
        status_code=status_code,
        retry_after_s=None,
        retryable_override=False,
    )


# ----------------------------------------------------------------------
# Exception classifier — maps raw Exceptions from SDK / network layers
# onto the correct bridge error envelope so callers can discriminate
# `upstream_anthropic` vs `upstream_network` vs `bridge_internal`.
# ----------------------------------------------------------------------
_UPSTREAM_TRANSIENT_MARKERS = (
    "overloaded", "overloaded_error", "overload",
    "too many requests", "rate_limit_error", "rate limit",
    "503", "502", "504", "529", "500",
    "service_unavailable", "bad_gateway", "gateway_timeout",
    "temporarily unavailable",
)
_UPSTREAM_NETWORK_MARKERS = (
    "timeout", "timed out", "connection reset", "connection refused",
    "connectionerror", "readtimeout", "connecttimeout",
    "dns", "name resolution", "broken pipe", "eof",
)
_CONFIG_MARKERS = (
    "environment variable not set",
    "environment variable is not set",
    "not configured",
    "api key not set",
    "api_key not set",
    "missing api key",
    "no api key",
    "no credentials",
    "credentials not configured",
)


def classify_exception(exc: Exception) -> JSONResponse:
    """
    Map an arbitrary Exception to the most specific bridge error envelope.

    Preference order:
      1. Config markers           → config_error     (source=bridge_config, 500, non-retryable)
      2. Network/timeout markers  → upstream_timeout (source=upstream_network, 429)
      3. Upstream HTTP markers    → upstream_error   (source=upstream_anthropic, 429)
      4. Everything else          → internal_error   (source=bridge_internal, 500)

    The caller should `raise BridgeError(classify_exception(e))` — the global
    handler then returns the envelope as the HTTP response.
    """
    msg = str(exc) or exc.__class__.__name__
    lower = msg.lower()

    # 0. RateLimitError from claude_cli -- surface as account_exhausted so the
    # BridgeError handler runs _cross_worker_retry. Inline import avoids a
    # module-load cycle (claude_cli does not import bridge_error).
    try:
        from src.claude_cli import RateLimitError as _RLE
        if isinstance(exc, _RLE):
            retry_after = getattr(exc, "retry_after_seconds", None) or 3600
            return account_exhausted_error(retry_after_s=int(retry_after))
    except ImportError:
        pass

    # 1. Configuration errors — surface loud and clear; not retryable.
    if any(marker in lower for marker in _CONFIG_MARKERS):
        return config_error(detail=msg[:300])

    # 2. Network / timeout — distinct from API-level errors
    if any(marker in lower for marker in _UPSTREAM_NETWORK_MARKERS):
        # Try to extract waited duration from msg; otherwise 0
        import re as _re
        m = _re.search(r"(\d+(?:\.\d+)?)\s*s\b", lower)
        waited = float(m.group(1)) if m else 0.0
        return upstream_timeout_error(waited_s=waited, retry_after_s=15)

    # 3. Upstream API error (Anthropic or other backend) — normalized to 429.
    # The forensic detail (which upstream code we observed) stays in the
    # message; the wire status is 429 per the worker contract.
    if any(marker in lower for marker in _UPSTREAM_TRANSIENT_MARKERS):
        return upstream_error(detail=msg[:200], retry_after_s=15)

    # 4. Fallthrough — bridge-internal unexpected. 500 is deliberate here:
    # it means something slipped past classification. Fix-forward, don't mask.
    return internal_error(detail=msg[:200], status_code=500)


def raise_classified(exc: Exception) -> None:
    """Convenience: classify + raise in one call."""
    raise BridgeError(classify_exception(exc))
