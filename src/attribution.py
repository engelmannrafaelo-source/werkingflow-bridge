"""Fail-closed user attribution for user-facing Bridge calls (Geld-Invariante).

Contract (Rafael decision B, 2026-07-02)
----------------------------------------
Every user-facing (= cost-bearing) call MUST carry X-User-ID — EITHER a real
userId OR an explicit marker 'anonymous:<grund>' (e.g. 'anonymous:public-check-
funnel'). Calls with an anonymous marker are booked to a DEDICATED accounting
bucket (separate from real users AND separate from "missing"). Calls with
neither are rejected (400) — but ONLY when BRIDGE_ATTRIBUTION_ENFORCE=true.

Rollout is staged behind that env toggle (default OFF):
  OFF → never reject; count/log unattributed calls per app+path instead. The
        counter is the live measurement instrument showing which apps still
        leak unattributed calls (GET /v1/metrics/attribution, per worker —
        query a specific worker via the nginx /workerN/ prefix).
  ON  → reject unattributed calls with 400 + actionable message. The
        coordinator flips this only once all app call-sites are clean.

Transition alias: werking-report's public funnel still sends '_anonymous'
(and some code paths the literal string 'undefined'); '_anonymous' is treated
as an anonymous marker until report is normalised to the new convention.

Scope: only POSTs to the enforced path set below, and only requests that carry
an Authorization header (a real app/client). Unauthenticated internet-scanner
noise never reaches the counters — it 401s downstream anyway.

Design notes:
  * Pure ASGI middleware (NOT BaseHTTPMiddleware — see the streaming-safety
    note on PerformanceMonitorMiddleware in main.py).
  * Fail open on internal errors: an exception in classification/counting must
    NEVER break live traffic. Only the deliberate 400 reject (toggle ON) stops
    a request.
  * Counters are per-worker in-memory (reset on restart). Structured log lines
    provide the durable trail; the endpoint provides the live view.
"""
import logging
import os
import time
from collections import defaultdict
from typing import Any, Dict, Optional

from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

ANONYMOUS_PREFIX = "anonymous:"

# Legacy spellings that MUST behave like an anonymous marker during the
# transition (werking-report public funnel). Remove once report sends
# 'anonymous:<grund>' natively.
LEGACY_ANONYMOUS_ALIASES = {"_anonymous"}

# User-facing, cost-bearing worker endpoints (POST). This is the enforcement
# AND measurement surface. Read-only/diagnostic endpoints (metrics, debug,
# compatibility, health) are deliberately absent. GET /v1/jobs/* polls are not
# cost-bearing and already carry their own attribution scope guard.
ENFORCED_PATHS = frozenset({
    "/v1/chat/completions",
    "/v1/research",
    "/v1/jobs",
    "/v1/privacy/smart-anonymize",
    "/v1/convert-pdf",
    "/v1/convert-pdf-to-semantic-html",
    "/v1/convert-html-to-docx",
    "/v1/convert-docx-to-html",
    "/v1/convert-html-to-pdf",
    "/v1/convert-html-to-screenshot",
    "/v1/convert-pdf-to-html-direct",
    "/v1/document/convert",
    "/v1/document/convert-and-anonymize",
    "/v1/audio/transcriptions",
})


def attribution_enforce_enabled() -> bool:
    """Read the toggle live (per request — getenv is cheap and this keeps the
    OFF default explicit). Flipping requires a container recreate anyway."""
    return os.getenv("BRIDGE_ATTRIBUTION_ENFORCE", "false").strip().lower() in ("1", "true", "yes")


def anonymous_reason(user_id: Optional[str]) -> Optional[str]:
    """Return the <grund> when user_id is an explicit anonymous marker, else None.

    'anonymous:' with an empty grund is NOT a valid marker (the grund is the
    whole point — it names the call-site) → None, i.e. counts as missing.
    """
    if not user_id or not isinstance(user_id, str):
        return None
    if user_id in LEGACY_ANONYMOUS_ALIASES:
        return "legacy-underscore-alias"
    if user_id.startswith(ANONYMOUS_PREFIX):
        reason = user_id[len(ANONYMOUS_PREFIX):].strip()
        return reason or None
    return None


def classify_user_id(user_id: Optional[str]) -> str:
    """'user' | 'anonymous' | 'missing'.

    'undefined'/'null' are what JS string-interpolation of a missing value
    produces — semantically missing, not a user.
    """
    if not user_id or not str(user_id).strip() or str(user_id).strip().lower() in ("undefined", "null"):
        return "missing"
    if user_id.startswith(ANONYMOUS_PREFIX) or user_id in LEGACY_ANONYMOUS_ALIASES:
        # A marker without a <grund> ('anonymous:') is NOT valid attribution —
        # the grund names the call-site and is the whole point.
        return "anonymous" if anonymous_reason(user_id) is not None else "missing"
    return "user"


# ---------------------------------------------------------------------------
# Per-worker in-memory counters
# ---------------------------------------------------------------------------
_STARTED_AT = time.time()
# (app_id, path) → count of calls with NO usable attribution
_unattributed: Dict[tuple, int] = defaultdict(int)
# (app_id, path, agent_id, client_id) → count. Same events as _unattributed but
# keyed by the caller-identifying headers, so a leak traces to its call-site
# without docker-log forensics (the engelmann burst 2026-07-02 was only
# attributable by grepping worker logs). Cardinality is bounded: worker-local,
# resets on restart, and only LEAKING call-sites ever appear here.
_unattributed_sources: Dict[tuple, int] = defaultdict(int)
# (app_id, reason) → count of calls with an explicit anonymous marker
_anonymous: Dict[tuple, int] = defaultdict(int)
# (app_id, path) → count of rejected calls (toggle ON only)
_rejected: Dict[tuple, int] = defaultdict(int)

# Log dampening: first occurrence per app, then every Nth — the counter keeps
# the true number, the log keeps the signal without flooding (the dev bridge
# serves thousands of tester/CUI calls a day).
_UNATTRIBUTED_LOG_EVERY = int(os.getenv("BRIDGE_ATTRIBUTION_LOG_EVERY", "100"))


def record_unattributed(
    app_id: Optional[str],
    path: str,
    agent_id: Optional[str] = None,
    client_id: Optional[str] = None,
) -> None:
    key = (app_id or "unknown", path)
    _unattributed[key] += 1
    _unattributed_sources[(app_id or "unknown", path, agent_id or "-", client_id or "-")] += 1
    n = _unattributed[key]
    if n == 1 or n % _UNATTRIBUTED_LOG_EVERY == 0:
        logger.warning(
            "attribution: UNATTRIBUTED call #%d app=%s path=%s agent=%s client=%s — no "
            "X-User-ID and no 'anonymous:<grund>' marker. Fix the app call-site; this "
            "will be rejected once BRIDGE_ATTRIBUTION_ENFORCE=true.",
            n, app_id or "unknown", path, agent_id or "-", client_id or "-",
        )


def record_anonymous(app_id: Optional[str], reason: str) -> None:
    _anonymous[(app_id or "unknown", reason)] += 1


def record_rejected(app_id: Optional[str], path: str) -> None:
    _rejected[(app_id or "unknown", path)] += 1


def snapshot() -> Dict[str, Any]:
    """Live counter view for GET /v1/metrics/attribution (per worker)."""
    def _nest(counter: Dict[tuple, int]) -> Dict[str, Dict[str, int]]:
        out: Dict[str, Dict[str, int]] = {}
        for (a, b), n in sorted(counter.items()):
            out.setdefault(a, {})[b] = n
        return out

    return {
        "enforce": attribution_enforce_enabled(),
        "since_ts": int(_STARTED_AT),
        "uptime_s": int(time.time() - _STARTED_AT),
        "unattributed_total": sum(_unattributed.values()),
        "unattributed_by_app": _nest(_unattributed),
        # Call-site detail for the same events — which X-Agent-ID/X-Client-ID
        # produced each leak (or "-" when the caller sent neither).
        "unattributed_sources": [
            {"app_id": a, "path": p, "agent_id": ag, "client_id": c, "count": n}
            for (a, p, ag, c), n in sorted(_unattributed_sources.items())
        ],
        "anonymous_total": sum(_anonymous.values()),
        "anonymous_by_app": _nest(_anonymous),
        "rejected_total": sum(_rejected.values()),
        "rejected_by_app": _nest(_rejected),
        "enforced_paths": sorted(ENFORCED_PATHS),
    }


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------
def _reject_body(path: str) -> bytes:
    import json
    return json.dumps({
        "error": {
            "message": (
                f"Missing user attribution on {path}: send X-User-ID with either a "
                "real user id or an explicit marker 'anonymous:<grund>' "
                "(e.g. 'anonymous:public-check-funnel')."
            ),
            "type": "attribution_error",
            "code": "missing_user_attribution",
            "source": "bridge_internal",
            "retryable": False,
        }
    }).encode("utf-8")


class AttributionEnforcementMiddleware:
    """Pure ASGI middleware implementing the fail-closed attribution contract.

    Order of checks per request (anything non-matching passes through
    untouched — zero behaviour change for compliant callers):
      1. POST + path in ENFORCED_PATHS + Authorization header present?
      2. classify X-User-ID → user: pass · anonymous: count bucket, pass ·
         missing: count; reject 400 only when BRIDGE_ATTRIBUTION_ENFORCE=true.
    """

    def __init__(self, app: ASGIApp):
        self.app = app
        logger.info(
            "Attribution enforcement middleware active (enforce=%s, %d enforced paths)",
            attribution_enforce_enabled(), len(ENFORCED_PATHS),
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        reject = False
        try:
            if scope.get("method") == "POST" and scope.get("path") in ENFORCED_PATHS:
                headers = {k.decode("latin-1").lower(): v.decode("latin-1")
                           for k, v in scope.get("headers", [])}
                # Only real clients (Bearer-authenticated apps) are measured —
                # unauthenticated scanner noise 401s downstream and must not
                # pollute the leak metric.
                if headers.get("authorization"):
                    user_id = headers.get("x-user-id")
                    app_id = headers.get("x-app-id") or _app_from_client_id(headers.get("x-client-id"))
                    kind = classify_user_id(user_id)
                    if kind == "anonymous":
                        record_anonymous(app_id, anonymous_reason(user_id) or "unknown")
                    elif kind == "missing":
                        record_unattributed(
                            app_id, scope["path"],
                            agent_id=headers.get("x-agent-id"),
                            client_id=headers.get("x-client-id"),
                        )
                        if attribution_enforce_enabled():
                            record_rejected(app_id, scope["path"])
                            reject = True
        except Exception as e:  # noqa: BLE001 — measurement must never break traffic
            logger.warning("attribution middleware check failed (fail-open): %s", e)

        if reject:
            body = _reject_body(scope["path"])
            await send({
                "type": "http.response.start",
                "status": 400,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("latin-1")),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return

        await self.app(scope, receive, send)


def _app_from_client_id(client_id: Optional[str]) -> Optional[str]:
    """First segment of X-Client-ID ('werking-report/check/analyze' →
    'werking-report'), skipping the 'workflow' namespace prefix — same
    convention as extract_attribution_context in main.py."""
    if not client_id:
        return None
    parts = [p for p in client_id.strip().split("/") if p]
    if parts and parts[0] == "workflow":
        parts = parts[1:]
    return parts[0] if parts else None
