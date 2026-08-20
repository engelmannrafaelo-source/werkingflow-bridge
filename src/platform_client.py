"""Worker → platform-api internal HTTP client (ADR-0009, Schritt 2a, C1).

The shared building block every worker-side "read via platform-api instead of
directly via Postgres" call goes through. Deliberately dumb: it does exactly
one HTTP round-trip, with no cache and no retry — cache/retry/fallback policy
belongs to the caller (see src/principals.py, src/routing/prepaid_cap.py),
because fail-open vs. fail-closed differs per call site.

Deliberately NOT routed through the public nginx path: the worker talks to
platform-api directly over Docker DNS (or, once workers move off
production-barrier, over the Tailscale address — same PLATFORM_API_URL knob,
same pattern as BRIDGE_WORKER_TARGETS in ADR-0009 Problem 2). There is no
`/v1/internal/*` location block in docker/routes-platform-api.conf and none is
needed: these endpoints are never meant to be reachable through the public
load balancer, service-token or not — one less thing exposed at the edge.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

import httpx

DEFAULT_PLATFORM_API_URL = "http://platform-api:8000"


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
    timeout_s: float = 2.0,
) -> PlatformResponse:
    """POST/GET against platform-api, X-Bridge-Service-Token authenticated.

    Raises PlatformUnavailable on timeout, connection error, or a 5xx from
    platform-api itself — i.e. whenever platform-api could not actually
    answer. Any other status (2xx, 4xx) comes back as a PlatformResponse; the
    caller reads .status_code to distinguish e.g. "found" from "not found".
    No retry here — a caller that wants one implements its own policy.
    """
    url = f"{_base_url()}{path if path.startswith('/') else '/' + path}"
    headers = {"X-Bridge-Service-Token": _service_token()}
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.request(method, url, json=json, headers=headers)
    except httpx.TimeoutException as e:
        raise PlatformUnavailable(f"platform-api timeout on {method} {path}: {e}") from e
    except httpx.TransportError as e:
        raise PlatformUnavailable(f"platform-api unreachable on {method} {path}: {e}") from e

    if resp.status_code >= 500:
        raise PlatformUnavailable(
            f"platform-api {resp.status_code} on {method} {path}: {resp.text[:200]}"
        )

    try:
        body = resp.json() if resp.content else None
    except ValueError:
        body = None
    return PlatformResponse(status_code=resp.status_code, json=body)
