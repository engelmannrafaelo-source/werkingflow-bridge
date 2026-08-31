"""Identity-/budget federation — the request's HOME bridge does the billing.

ADR-0011: a request executed by this bridge but ORIGINATED on the peer bridge
(two-tier worker pool, ADR-0010) must have its identity resolved, its budget
gated and its usage recorded against the platform-api of its HOME bridge —
never against the local one. Evaluating a foreign identity locally is how
healthy dev users got 402'd and how `jit-*@…local` shadow users ended up in
the customer database (measured 2026-08-31).

This module owns three small things:

  * the request-scoped ORIGIN (a ContextVar set by OriginMiddleware from the
    LB-stamped `X-Bridge-Origin` header — the LB guarantees the header is
    trustworthy, see docker/nginx.conf `$bridge_origin_out`),
  * the peer registry (FEDERATION_PEERS env, JSON: origin id →
    {"platformUrl": …, "tokenEnv": …}), and
  * the target resolution `resolve_user_domain_target()` that
    platform_client.call_platform(domain="user") uses to pick base URL +
    service token.

Deliberately NOT here: any retry/cache/fail-open policy — that stays with the
call sites (platform_client contract). And no per-endpoint knowledge: the
peer's platform-api speaks the exact same internal API as the local one, so
federation is purely "which base URL, which token".

Fail polarity (ADR-0011, point 5): a FOREIGN origin without a configured peer
is a DEPLOY error, not a transient one — resolve_user_domain_target raises
FederationMisconfigured, and the budget gate maps that to a 503 fail-CLOSED.
Letting such a call through would mean an unbudgeted call plus exactly the
shadow-provisioning this ADR abolishes.
"""
from __future__ import annotations

import json
import logging
import os
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Optional

from starlette.types import ASGIApp, Receive, Scope, Send

logger = logging.getLogger(__name__)

ORIGIN_HEADER = "x-bridge-origin"

# Request-scoped origin. None = header absent (direct worker access, tests,
# pre-middleware code paths) — treated as "own" everywhere: the pre-federation
# behaviour, and the correct one for every request that never crossed the LB.
_request_origin: ContextVar[Optional[str]] = ContextVar("bridge_request_origin", default=None)


class FederationMisconfigured(Exception):
    """A foreign-origin request needs a peer that is not configured (missing
    FEDERATION_PEERS entry, missing token env). Deploy/host configuration
    error — the caller must fail CLOSED, never open (ADR-0011 point 5)."""


@dataclass(frozen=True)
class PlatformTarget:
    base_url: str
    token: str
    origin: str  # whose platform-api this is ("" = local/own)


def self_origin_id() -> str:
    """This bridge's own origin id (matches the LB's ${BRIDGE_ID}: dev/prod).
    Empty string when unset — then NO origin ever counts as foreign, which is
    the safe pre-rollout default: behaviour identical to before ADR-0011."""
    return os.getenv("BRIDGE_ORIGIN_ID", "").strip()


def set_request_origin(origin: Optional[str]) -> None:
    _request_origin.set(origin.strip().lower() if origin else None)


def get_request_origin() -> Optional[str]:
    return _request_origin.get()


def is_foreign_origin() -> bool:
    """True iff the current request carries an origin that is NOT this bridge.

    Both sides must be known to call anything foreign: no self id configured →
    nothing is foreign (pre-rollout default), no request origin → local
    request → not foreign.
    """
    own = self_origin_id()
    origin = get_request_origin()
    return bool(own) and bool(origin) and origin != own.lower()


def cache_scope() -> str:
    """Cache-key prefix for per-user/per-email process caches in the user
    domain. The SAME email resolves to DIFFERENT UUIDs on the two bridges
    (measured 2026-08-31: rafael@werking.tools exists on both with different
    ids) — an origin-blind cache would hand a dev-origin request the prod
    UUID or vice versa. "" for own/absent origin keeps existing keys (and
    their hit rates) untouched."""
    return (get_request_origin() or "") if is_foreign_origin() else ""


def _peers() -> dict:
    raw = os.getenv("FEDERATION_PEERS", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError as e:
        # Malformed config must not silently disable federation — that would
        # reopen the shadow-user path. Loud, and foreign requests fail closed
        # via FederationMisconfigured below.
        raise FederationMisconfigured(f"FEDERATION_PEERS is not valid JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise FederationMisconfigured("FEDERATION_PEERS must be a JSON object (origin id → peer)")
    return {str(k).lower(): v for k, v in parsed.items()}


def resolve_user_domain_target() -> Optional[PlatformTarget]:
    """Where user-domain platform calls of the CURRENT request must go.

    None            → local platform-api (own/absent origin): caller keeps its
                      existing URL/token path, byte-for-byte pre-ADR behaviour.
    PlatformTarget  → the request is foreign: talk to its home platform-api.

    Raises FederationMisconfigured when the origin is foreign but no usable
    peer entry exists — the caller must fail closed.
    """
    if not is_foreign_origin():
        return None

    origin = get_request_origin()
    peer = _peers().get(origin or "")
    if not isinstance(peer, dict) or not peer.get("platformUrl"):
        raise FederationMisconfigured(
            f"request originates from bridge {origin!r} but FEDERATION_PEERS has no "
            f"usable entry for it (needs platformUrl + tokenEnv). Refusing to evaluate "
            f"a foreign identity against the LOCAL database — that is the shadow-user "
            f"bug ADR-0011 exists to prevent."
        )
    token_env = peer.get("tokenEnv") or ""
    token = os.getenv(token_env, "") if token_env else ""
    if not token:
        raise FederationMisconfigured(
            f"FEDERATION_PEERS[{origin!r}].tokenEnv={token_env!r} resolves to no value — "
            f"cannot authenticate to the home bridge's platform-api."
        )
    return PlatformTarget(
        base_url=str(peer["platformUrl"]).rstrip("/"), token=token, origin=origin or ""
    )


class OriginMiddleware:
    """Pure-ASGI middleware: copy the LB-stamped X-Bridge-Origin header into
    the request-scoped ContextVar. Trust lives in nginx ($bridge_origin_out
    resets untrusted senders to the own id); workers are not reachable except
    through the LB, so the header value here is authoritative."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            origin = None
            for name, value in scope.get("headers", []):
                if name == ORIGIN_HEADER.encode("latin-1"):
                    origin = value.decode("latin-1")
                    break
            set_request_origin(origin)
        await self.app(scope, receive, send)
