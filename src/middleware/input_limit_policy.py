"""
input_limit_policy — single, ENV-configurable Bridge-wide input-size gate.

Motivation (Rafael-Entscheid 2026-08-25, siehe devops-Memory
project_zentrales_input_token_limit_20260825): app-side input-budget literals
(context-char caps, prompt-size caps) are scattered across every app and
drift. The Bridge itself already silently truncates oversized inputs today —
the SDK sub-process compaction cuts the prompt before it reaches the worker,
and neither the app nor the user ever finds out. This module replaces that
silent squeeze with ONE central limit, deliberately the SAME for every app
and every model: as long as calls run over the Bridge worker pool, the
worker lane is the practical ceiling, not the model's context window. A
bigger window only becomes real once a call runs on a direct provider lane
(e.g. Bedrock) — that is a future per-environment tuning knob, not a
per-model split (see the ENV config below).

Two-stage rollout — THIS module implements stage 1 (observe only):
  1. Observe (default, this wave): log loudly whenever a request's estimated
     input would exceed the limit. Nothing is rejected. The logged
     distribution across apps is what lets Rafael pick the real number.
  2. Enforce (future — flip BRIDGE_INPUT_LIMIT_ENFORCE=true once the number
     is set): the reject path below already exists and is tested, it just
     stays dormant until then. Turning it on converts today's silent
     compaction into a loud, structured, translatable error instead.

Config — both ENV-only, retunable per environment without a code change:
  BRIDGE_MAX_INPUT_TOKENS      int, default _DEFAULT_LIMIT_TOKENS below.
  BRIDGE_INPUT_LIMIT_ENFORCE   bool ("1"/"true"/"yes"/"on"), default off.
"""
from __future__ import annotations

import logging
import os

from fastapi import Request

from src.middleware.bridge_error import BridgeError, input_limit_exceeded_error

logger = logging.getLogger(__name__)

_LIMIT_ENV = "BRIDGE_MAX_INPUT_TOKENS"
_ENFORCE_ENV = "BRIDGE_INPUT_LIMIT_ENFORCE"

# Placeholder starting point for the observation phase — NOT a calibrated
# number. Sonnet's context window is ~200k tokens; the worker lane usually
# squeezes well before that, so this starts deliberately below the model
# ceiling (Rafael: "die Tokens nicht zu hoch ansetzen"). Retune per
# environment via BRIDGE_MAX_INPUT_TOKENS once the observation phase has
# delivered real numbers — e.g. production behind direct provider lanes can
# go higher than a dev/staging bridge running everything over the worker pool.
_DEFAULT_LIMIT_TOKENS = 100_000


def _limit_tokens() -> int:
    raw = os.getenv(_LIMIT_ENV)
    if not raw:
        return _DEFAULT_LIMIT_TOKENS
    try:
        val = int(raw)
    except ValueError:
        logger.warning(
            "input_limit_policy: %s=%r is not an int — falling back to default %d",
            _LIMIT_ENV, raw, _DEFAULT_LIMIT_TOKENS,
        )
        return _DEFAULT_LIMIT_TOKENS
    return val if val > 0 else _DEFAULT_LIMIT_TOKENS


def _enforce_enabled() -> bool:
    return os.getenv(_ENFORCE_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def observe_input_limit(request: Request) -> None:
    """Compare the request's pre-flight token estimate against the central
    limit. Logs loudly when exceeded; raises BridgeError only when the
    (currently dormant) enforce flag is on.

    Called from cache_request_body_dependency() right after it sets
    request.state.adaptive_est_tokens (src/middleware/adaptive_limiter.py) —
    that estimate is reused here rather than recomputed. A missing estimate
    means the body was unreadable; that failure is already logged at the
    call site, so this function has nothing to compare and no-ops.

    NOTE for the future enforce rollout: cache_request_body_dependency() is
    also used by endpoints that resolve pool-vs-cloud routing AFTER this
    dependency runs (e.g. /v1/research). Rejecting here applies the input
    ceiling to those requests too, before routing is known — that is
    intentional (input size is an architecture ceiling, independent of which
    lane serves the call), but re-check this reasoning when flipping
    BRIDGE_INPUT_LIMIT_ENFORCE=true for the first time.
    """
    est = getattr(request.state, "adaptive_est_tokens", None)
    if est is None:
        return

    limit = _limit_tokens()
    if est <= limit:
        return

    app_id = request.headers.get("X-App-ID") or request.headers.get("x-app-id")
    agent_id = request.headers.get("X-Agent-ID") or request.headers.get("x-agent-id")
    enforce = _enforce_enabled()
    logger.warning(
        "input_limit_policy: est_tokens=%d exceeds limit=%d (app=%s agent=%s) — %s",
        est, limit, app_id, agent_id,
        "REJECTING (enforce on)" if enforce else "observe-only, allowing through",
    )
    if enforce:
        raise BridgeError(input_limit_exceeded_error(est, limit))
