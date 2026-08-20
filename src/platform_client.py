"""Worker → platform-api internal HTTP client (ADR-0009, Schritt 2a, C1).

The shared building block every worker-side "read via platform-api instead of
directly via Postgres" call goes through. Deliberately dumb: no cache, and no
retry unless a call site explicitly asks for one — cache/retry/fallback policy
belongs to the caller (see src/principals.py, src/routing/prepaid_cap.py),
because fail-open vs. fail-closed differs per call site.

The one thing this module does decide is that retrying is OPT-IN (retries=0 by
default): a retry is only safe for an idempotent operation, and this module
cannot know whether the endpoint it is calling is one. See call_platform's
docstring for why the default has to be the side that cannot double-write.

Deliberately NOT routed through the public nginx path: the worker talks to
platform-api directly over Docker DNS (or, once workers move off
production-barrier, over the Tailscale address — same PLATFORM_API_URL knob,
same pattern as BRIDGE_WORKER_TARGETS in ADR-0009 Problem 2). There is no
`/v1/internal/*` location block in docker/routes-platform-api.conf and none is
needed: these endpoints are never meant to be reachable through the public
load balancer, service-token or not — one less thing exposed at the edge.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

import httpx

DEFAULT_PLATFORM_API_URL = "http://platform-api:8000"

# Pause between attempts when a call site opts into retrying. Kept small on
# purpose: the budget gate sits in FRONT of the LLM call, so every wasted
# second here is the customer's wait. See ADR-0009 Schritt 2 design doc for the
# ~4s total budget this is meant to fit into (2 attempts x 2s + one pause).
DEFAULT_RETRY_BACKOFF_S = 0.25

logger = logging.getLogger(__name__)


class PlatformUnavailable(Exception):
    """platform-api could not produce an answer: unreachable, timed out, or
    itself failed (5xx). Never raised for an ordinary response (2xx/4xx) —
    those come back as a PlatformResponse for the caller to interpret, because
    e.g. 404 on a lookup is a real, cacheable answer ("no such row"), not an
    outage. The caller decides fail-open vs. fail-closed on this exception;
    this module never decides that for them."""


@dataclass(frozen=True)
class PlatformResponse:
    status_code: int
    json: Optional[dict[str, Any]]


def _base_url() -> str:
    return os.getenv("PLATFORM_API_URL", DEFAULT_PLATFORM_API_URL).rstrip("/")


def _service_token() -> str:
    """Read BRIDGE_SERVICE_TOKEN lazily (call time, not import time) so a
    worker that never exercises a platform_client call site can still boot
    without it. Whether the token is already provisioned in the worker's
    environment on every host is unverified from the repo (compose env files
    are gitignored) — see ADR-0009 Schritt 2a design doc, offene Frage 3."""
    token = os.getenv("BRIDGE_SERVICE_TOKEN")
    if not token:
        raise PlatformUnavailable(
            "BRIDGE_SERVICE_TOKEN is not set in this process's environment — "
            "cannot authenticate to platform-api for an internal call. This is "
            "a deploy/host configuration gap, not a transient network error."
        )
    return token


async def call_platform(
    method: str,
    path: str,
    *,
    json: Optional[dict[str, Any]] = None,
    params: Optional[dict[str, Any]] = None,
    timeout_s: float = 2.0,
    retries: int = 0,
    retry_backoff_s: float = DEFAULT_RETRY_BACKOFF_S,
) -> PlatformResponse:
    """POST/GET against platform-api, X-Bridge-Service-Token authenticated.

    Raises PlatformUnavailable on timeout, connection error, or a 5xx from
    platform-api itself — i.e. whenever platform-api could not actually
    answer. Any other status (2xx, 4xx) comes back as a PlatformResponse; the
    caller reads .status_code to distinguish e.g. "found" from "not found".

    retries: how many EXTRA attempts to make after a transport failure.
    Default 0 — retrying is opt-in per call site, never a client-wide default.

    Why opt-in and not a default (ADR-0009, decided at implementation time —
    the Schritt-2 design doc originally specified the opposite polarity, a
    client default that unsafe endpoints would have to switch off):

      * A retry is only correct if the operation is IDEMPOTENT. The read leaves
        of the budget gate are pure reads, and _provision_trial's write is
        idempotent by construction (ON CONFLICT ... WHERE ... IS NULL), so they
        opt in. But POST /v1/internal/audit-events (Schritt 2a) writes
        audit_log via a plain INSERT with NO dedup key — a retry there
        duplicates the row.
      * A TIMEOUT is exactly the case where this bites: the request may well
        have reached platform-api and been executed, and only the ANSWER got
        lost. "Unreachable" and "answered, but I didn't hear it" are
        indistinguishable from here.
      * So the safe default has to be the one that cannot double-write. Making
        retry the default would put the burden of remembering on the dangerous
        side of the choice; every new non-idempotent endpoint would silently
        inherit a wrong behaviour. Opt-in puts it on the safe side.

    Retries happen ONLY on transport failures (timeout / connection error) —
    never on a 5xx. A 5xx is an ANSWER: platform-api was reached and something
    inside it failed, possibly after a partial effect. Replaying that blindly
    is not a retry, it is a second attempt at an unknown state.
    """
    url = f"{_base_url()}{path if path.startswith('/') else '/' + path}"
    headers = {"X-Bridge-Service-Token": _service_token()}

    attempts = retries + 1
    for attempt in range(1, attempts + 1):
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                resp = await client.request(
                    method, url, json=json, params=params, headers=headers
                )
            break
        except (httpx.TimeoutException, httpx.TransportError) as e:
            kind = "timeout" if isinstance(e, httpx.TimeoutException) else "unreachable"
            if attempt >= attempts:
                raise PlatformUnavailable(
                    f"platform-api {kind} on {method} {path} "
                    f"after {attempt} attempt(s): {e}"
                ) from e
            logger.warning(
                "platform-api %s on %s %s (attempt %d/%d) — retrying in %.2fs",
                kind, method, path, attempt, attempts, retry_backoff_s,
            )
            await asyncio.sleep(retry_backoff_s)

    if resp.status_code >= 500:
        raise PlatformUnavailable(
            f"platform-api {resp.status_code} on {method} {path}: {resp.text[:200]}"
        )

    try:
        body = resp.json() if resp.content else None
    except ValueError:
        body = None
    return PlatformResponse(status_code=resp.status_code, json=body)
