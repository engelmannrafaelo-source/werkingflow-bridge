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
TYPE_INPUT_TOO_LARGE = "input_too_large"
# ADR-0012: the poll reached a bridge that is not the job's home store.
TYPE_JOB_MISDIRECTED = "job_misdirected"

# Reason codes — narrow, stable identifiers for why a 429 (or 500) was emitted.
# These go into `error.reason` and drive panel aggregation / alerting.
# Contract: every worker response is EITHER 2xx OR 429-with-reason.
# 500-class reasons are contract violations — they should page, not be expected.
REASON_TOKEN_BUDGET_FULL = "worker_token_budget_full"
REASON_QUEUE_EXHAUSTED = "worker_queue_exhausted"
# Historical name, deliberately UNCHANGED (panels/alerting aggregate on this
# string). It means "an account limit window is exhausted" — which window is
# now carried honestly in extra.limit_window, because it is very often the
# 5-hour SESSION window and not the weekly one. See account_exhausted_error.
REASON_ACCOUNT_WEEKLY_EXHAUSTED = "worker_account_weekly_exhausted"
REASON_UPSTREAM_ANTHROPIC_ERROR = "claude_upstream_error"
REASON_UPSTREAM_ANTHROPIC_TIMEOUT = "claude_upstream_timeout"
# Vision API direct-key billing exhausted (Anthropic-side, not retryable)
REASON_VISION_BILLING_EXHAUSTED = "vision_billing_exhausted"
REASON_VISION_EMPTY_RESPONSE = "vision_empty_response"
# Anthropic 4xx invalid_request_error — the CALLER's request is malformed
# (bad temperature/top_p, unknown model, oversized payload). Non-retryable:
# retrying the identical request fails forever. Must NOT collapse to 500
# (→ nginx exhausts workers → bogus "at capacity") nor 429 (→ client retry-loop).
REASON_INVALID_REQUEST = "upstream_invalid_request"
# 500-class (contract violation — must be investigated)
REASON_INTERNAL = "worker_internal_error"
REASON_MISCONFIGURED = "worker_misconfigured"
# Bridge-wide input-size gate (src/middleware/input_limit_policy.py). Dormant
# until BRIDGE_INPUT_LIMIT_ENFORCE=true — see that module's docstring for the
# two-stage rollout this reason code belongs to.
REASON_INPUT_LIMIT_EXCEEDED = "bridge_input_limit_exceeded"
# ADR-0012 — async-job identity/routing. All three are LOUD by design; none of
# them may collapse into the 404 that means "unknown id, or expired".
REASON_JOB_ID_MALFORMED = "job_id_malformed"
REASON_JOB_HOME_MISMATCH = "job_home_bridge_mismatch"
REASON_JOB_HOME_UNCONFIGURED = "job_home_unconfigured"


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
    #
    # 401/403 are auth rejections, never throttle events. Labeling them
    # "bridge_throttle" (the generic bridge_internal compat type) sent the
    # 2026-07-06 energy-phase-4 diagnosis down a throttling rabbit hole while
    # the real cause was a credential rejection on the cross-host backup hop.
    # Scoped to 401/403 only: unified-tester relies on type=="bridge_throttle"
    # to detect a 404 route-not-deployed, so other statuses keep the old label.
    if status_code in (401, 403):
        compat_type = "authentication_error"
    elif source == SOURCE_BRIDGE_INTERNAL:
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


def input_limit_exceeded_error(est_tokens: int, limit_tokens: int) -> JSONResponse:
    """Request's estimated input exceeds the central Bridge-wide input limit.

    Non-retryable (400): the identical request fails again — the caller must
    shrink its input (less context, shorter documents) before retrying, a
    backoff does not help. Currently only reachable when
    BRIDGE_INPUT_LIMIT_ENFORCE=true (see src/middleware/input_limit_policy.py);
    the default observe-only stage logs this situation but never builds this
    response.
    """
    return bridge_error(
        source=SOURCE_BRIDGE_INTERNAL,
        error_type=TYPE_INPUT_TOO_LARGE,
        reason=REASON_INPUT_LIMIT_EXCEEDED,
        message=(
            f"Input too large: ~{est_tokens:,} estimated tokens exceeds the "
            f"Bridge-wide limit of {limit_tokens:,}. Reduce the request's "
            f"input size (fewer/shorter context documents) before retrying."
        ),
        status_code=400,
        retryable_override=False,
        extra={
            "est_tokens": est_tokens,
            "limit_tokens": limit_tokens,
        },
    )


# Which Anthropic limit window a capacity lock refers to → the words the client
# sees. Keys are src.middleware.capacity_lock.WorkerLock.reason values.
_LIMIT_WINDOW_PHRASE = {
    "session_window": "their 5-hour Anthropic session limit",
    "weekly_window": "their weekly Anthropic limit",
    "anthropic_explicit": "an Anthropic rate limit",
}
# What we say when nobody could tell us WHICH limit was hit. Saying "weekly"
# anyway is the bug this vocabulary exists to fix (see account_exhausted_error).
_LIMIT_WINDOW_UNKNOWN = "an Anthropic usage limit (which window is unknown here)"


def account_exhausted_error(
    retry_after_s: int = 3600,
    limit_window: Optional[str] = None,
) -> JSONResponse:
    """No routable worker account: a limit window is exhausted. Long backoff.

    `limit_window` names WHICH window, from the capacity lock that caused the
    rejection ("session_window" / "weekly_window" / "anthropic_explicit").
    Pass it whenever the call site knows; omit it only when nothing knows.

    Why this parameter exists: the message used to read "All worker accounts
    have reached their weekly Anthropic limit" unconditionally. On 2026-09-03
    every prod worker emitted exactly that while the accounts sat at 30 % of
    the WEEK and 100 % of the 5-hour session window, nine minutes from reset.
    Two sessions spent the afternoon looking for an architecture defect that
    a truthful sentence — "session limit, resets in 9 minutes" — would have
    turned into "wait". A wrong limit name is not cosmetics; it is a wrong
    instruction about what to do next.

    Unknown stays unknown. There is no default back to "weekly": the caller
    that cannot name the window says so, and the retry hint still carries the
    actionable part.
    """
    phrase = _LIMIT_WINDOW_PHRASE.get(limit_window or "", _LIMIT_WINDOW_UNKNOWN)
    minutes = max(1, retry_after_s // 60)
    return bridge_error(
        source=SOURCE_BRIDGE_ACCOUNT,
        error_type=TYPE_ACCOUNT_EXHAUSTED,
        reason=REASON_ACCOUNT_WEEKLY_EXHAUSTED,
        message=(
            f"All worker accounts have reached {phrase}. "
            f"Retry in ~{minutes} minutes."
        ),
        status_code=429,
        retry_after_s=retry_after_s,
        extra={"limit_window": limit_window or "unknown"},
    )


def job_id_malformed_error(job_id: str, detail: str) -> JSONResponse:
    """The polled id is not a job id at all (ADR-0012). 400, non-retryable.

    Deliberately NOT the 404 "unknown id, or expired": that answer tells a
    caller its job vanished, when in truth it never sent a job id. Retrying
    the identical malformed id can only fail again."""
    return bridge_error(
        source=SOURCE_BRIDGE_INTERNAL,
        error_type=TYPE_JOB_MISDIRECTED,
        reason=REASON_JOB_ID_MALFORMED,
        message=f"Malformed async job id: {detail}",
        status_code=400,
        retryable_override=False,
        extra={"job_id": job_id[:120]},
    )


def job_misdirected_error(job_id: str, job_home: str, this_bridge: str) -> JSONResponse:
    """This bridge is not the job's home store and could not forward the poll.

    421 Misdirected Request, by its definition: the request reached a server
    that cannot produce a response for it. The job may well be alive and
    running — on the OTHER bridge — so answering 404 would report a healthy job
    as gone, which is precisely the failure ADR-0012 removes.

    Reaching this means the load balancer did not route by the id's marker: an
    un-deployed / older LB, a poll that already hopped once (the loop guard
    stops it here on purpose), or an id naming a bridge that does not exist.
    All three are configuration facts worth seeing, not transient conditions,
    hence non-retryable."""
    return bridge_error(
        source=SOURCE_BRIDGE_CONFIG,
        error_type=TYPE_JOB_MISDIRECTED,
        reason=REASON_JOB_HOME_MISMATCH,
        message=(
            f"Async job {job_id} lives on bridge {job_home!r}; this is bridge "
            f"{this_bridge!r} and it holds a different job store (ADR-0009). "
            f"The poll must reach {job_home!r} — the load balancer routes it "
            f"there by the id's marker (ADR-0012); it did not."
        ),
        status_code=421,
        retryable_override=False,
        extra={"job_id": job_id, "job_home": job_home, "this_bridge": this_bridge},
    )


def job_home_unconfigured_error(detail: str) -> JSONResponse:
    """This worker cannot name its own bridge, so it can neither mint a
    routable job id nor decide whether an incoming one is its own.

    503 + fail-closed, the ADR-0011 point-5 polarity: an unset federation
    identity is a DEPLOY error. Minting untagged ids instead would look
    healthy and quietly restore the cross-bridge 404s."""
    return bridge_error(
        source=SOURCE_BRIDGE_CONFIG,
        error_type=TYPE_CONFIG,
        reason=REASON_JOB_HOME_UNCONFIGURED,
        message=f"Async jobs are not deployable on this worker: {detail}",
        status_code=503,
        retryable_override=False,
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


# Anthropic's exact strings for "the prepaid credit is gone", observed in
# production. Kept as ONE list with the predicate below because two readers
# need this answer: the error classifier (what to return to the caller) and
# the ledger write-path (this call must leave a findable trace instead of
# looking like silence — see src/main.py, vision branch).
VISION_BILLING_EXHAUSTED_MARKERS = (
    "credit balance is too low",
    "purchase credits",
    "insufficient_credit",
)


def is_vision_billing_exhausted(message: str) -> bool:
    """True if this upstream error text means the vision prepaid key is out of
    credit."""
    lower = (message or "").lower()
    return any(m in lower for m in VISION_BILLING_EXHAUSTED_MARKERS)


def vision_billing_error(detail: str) -> JSONResponse:
    """Vision API (direct Anthropic key) returned 400 credit-balance-too-low.

    Surfaces as HTTP 402 Payment Required, non-retryable. Retry is sinnless —
    only billing top-up resolves this. The full Anthropic error text is
    preserved in the message so clients can act on it without log diving.

    Contract: this is NOT a worker-internal contract violation; it is a
    legitimate billing-state signal from Anthropic. Distinct from
    `upstream_error` (transient 5xx/429) because retrying does not help.
    """
    return bridge_error(
        source=SOURCE_UPSTREAM_ANTHROPIC,
        error_type=TYPE_UPSTREAM_ERROR,
        reason=REASON_VISION_BILLING_EXHAUSTED,
        message=f"Vision API credit balance exhausted: {detail}",
        status_code=402,
        retryable_override=False,
    )


def vision_empty_response_error(stop_reason: Optional[str]) -> JSONResponse:
    """Die Vision-Antwort (direkter Anthropic-Key) enthielt NULL Text-Bloecke.

    Befund 2026-08-31 (werking-energy schema_analysis): claude-sonnet-5
    verbraucht bei dichten Schema-Bildern das gesamte Output-Budget in
    Thinking-Bloecken; stop_reason ist dann "max_tokens" und die Antwort
    bestand bis zu diesem Fix aus einem stillen 200 mit content="" — der
    Client retryte blind und verbrannte pro Versuch ~15k Prepaid-Tokens.

    422 + non-retryable bei max_tokens: ein unveraenderter Retry kann nie Text
    liefern — der Aufrufer muss max_tokens erhoehen oder das Thinking-Budget
    begrenzen. 502 + retryable sonst (unerwartet leere Upstream-Antwort,
    transient moeglich). In beiden Faellen ist der Aufruf zu diesem Zeitpunkt
    bereits im Ledger verbucht (Kappen-Grundlage) — siehe
    vision_provider.ensure_usable_vision_content, das NACH dem Persist laeuft.
    """
    exhausted = stop_reason == "max_tokens"
    return bridge_error(
        source=SOURCE_UPSTREAM_ANTHROPIC,
        error_type=TYPE_UPSTREAM_ERROR,
        reason=REASON_VISION_EMPTY_RESPONSE,
        message=(
            "Vision-Antwort enthielt keinen Text: das Output-Budget (max_tokens) "
            "wurde vollstaendig von Thinking-Bloecken verbraucht. max_tokens "
            "erhoehen oder Thinking begrenzen — ein unveraenderter Retry kann "
            "nicht gelingen."
            if exhausted else
            "Vision-Antwort enthielt keinen Text (unerwartete Upstream-Antwort "
            f"mit stop_reason={stop_reason!r}). Der Aufruf wurde abgerechnet; "
            "Retry moeglich."
        ),
        status_code=422 if exhausted else 502,
        retryable_override=not exhausted,
        extra={"stop_reason": stop_reason},
    )


def client_request_error(detail: str) -> JSONResponse:
    """Anthropic returned 400 invalid_request_error — the CALLER's request is
    malformed (e.g. temperature out of 0..1 range, unknown model, payload too
    large). Surfaces as HTTP 400 Bad Request, non-retryable.

    WHY THIS EXISTS: without this branch such errors fell through to
    `internal_error` (500). nginx then retried all workers (proxy_next_upstream
    includes http_500), exhausted them, and rewrote the result as
    "Bridge temporarily at capacity" (503) — disguising a bad client parameter
    as a capacity/infra problem. That made a 5-second fix ("your temperature is
    1.5, max is 1") look like a fleet-wide outage. Passing the real 400 through
    with Anthropic's original message makes the whole class self-diagnosing.

    Contract: a legitimate client-side bad-request signal — distinct from
    `upstream_error` (transient 5xx/429, retryable) and from `internal_error`
    (bridge bug). nginx does NOT retry a 400, so it reaches the caller verbatim.
    """
    return bridge_error(
        source=SOURCE_UPSTREAM_ANTHROPIC,
        error_type=TYPE_UPSTREAM_ERROR,
        reason=REASON_INVALID_REQUEST,
        message=f"Anthropic rejected the request as invalid (not retryable): {detail}",
        status_code=400,
        retryable_override=False,
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


class SDKDisconnectError(Exception):
    """
    Raised when claude_code_sdk exits with 0 chunks (CLI died before producing
    any usable output). Treated as a retryable transient failure: the exception
    handler in main.py attempts cross_worker_retry before surfacing 503 to the
    client. Never surfaces as 500 — a zero-chunk exit is a worker-internal
    condition, not a bridge bug.

    Defined here (not in claude_cli) so it can be imported without pulling in
    the full claude_code_sdk dependency chain.
    """
    def __init__(self, error_detail: dict) -> None:
        super().__init__("sdk_disconnect")
        self.error_detail = error_detail


def raise_throttle(cap_tokens: int, inflight_tokens: int, retry_after_s: int = 30) -> None:
    raise BridgeError(throttle_error(cap_tokens, inflight_tokens, retry_after_s))


def raise_queue_timeout(
    cap_tokens: int, inflight_tokens: int, waited_s: float, retry_after_s: int = 60,
) -> None:
    raise BridgeError(queue_timeout_error(cap_tokens, inflight_tokens, waited_s, retry_after_s))


def raise_account_exhausted(
    retry_after_s: int = 3600, limit_window: Optional[str] = None
) -> None:
    raise BridgeError(account_exhausted_error(retry_after_s, limit_window=limit_window))


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

    # 0a. Already-classified BridgeError — honor its envelope verbatim.
    #
    # A BridgeError reaching classify_exception was deliberately classified
    # deeper in the stack (e.g. vision_billing_error -> 402 non-retryable).
    # Re-classifying it is destructive: str(BridgeError) == "bridge_error",
    # which matches no marker and falls through to internal_error (500). nginx
    # then retries http_500 across all workers, exhausts them, and rewrites the
    # result to "503 Bridge temporarily at capacity" -- disguising an empty
    # Vision credit balance (a billing top-up signal) as a capacity/infra
    # outage that retries for ~30 min. Pass the original verdict through.
    if isinstance(exc, BridgeError):
        return exc.response

    # 0b. RateLimitError from claude_cli -- surface as account_exhausted so the
    # BridgeError handler runs _cross_worker_retry. Inline import avoids a
    # module-load cycle (claude_cli does not import bridge_error).
    try:
        from src.claude_cli import RateLimitError as _RLE
        if isinstance(exc, _RLE):
            retry_after = getattr(exc, "retry_after_seconds", None) or 3600
            # Anthropic named the retry time itself; it does not say which of
            # its windows ran out, so we don't claim to know either.
            return account_exhausted_error(
                retry_after_s=int(retry_after), limit_window="anthropic_explicit"
            )
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

    # 3b. Vision API billing exhausted — direct Anthropic 400 with credit-balance
    # message from vision_provider.py. Non-retryable; distinct from 5xx/429
    # transient upstream errors. Anthropic exact strings observed in production:
    #   "Your credit balance is too low to access the Anthropic API."
    #   "Please go to Plans & Billing to upgrade or purchase credits."
    if is_vision_billing_exhausted(msg):
        return vision_billing_error(detail=msg[:200])

    # 3c. Anthropic 400 invalid_request_error — malformed CALLER request (bad
    # temperature/top_p, unknown model, oversized payload). Non-retryable.
    # MUST be classified before the 500 fallthrough: otherwise it becomes a
    # worker_internal_error (500) → nginx retries+exhausts all workers →
    # rewrites to "Bridge temporarily at capacity" (503), disguising a client
    # parameter bug as a capacity/infra outage. "invalid_request_error" is
    # Anthropic's stable, specific error-type token for this class and does not
    # appear in transient (overloaded/rate-limit/5xx) messages.
    if "invalid_request_error" in lower:
        return client_request_error(detail=msg[:300])

    # 4. Fallthrough — bridge-internal unexpected. 500 is deliberate here:
    # it means something slipped past classification. Fix-forward, don't mask.
    return internal_error(detail=msg[:200], status_code=500)


def raise_classified(exc: Exception) -> None:
    """Convenience: classify + raise in one call."""
    raise BridgeError(classify_exception(exc))
