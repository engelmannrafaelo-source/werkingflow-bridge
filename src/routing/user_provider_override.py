"""Per-user provider override — the enforcement half of ``users.provider_config``.

The users table carries a ``provider_config`` JSONB (admin-managed via
``PATCH /v1/users/{id}``, operator-only) that pins a user's AI traffic to a
specific backend. Until now the column was schema-only; this module is the
single enforcement point, called by the request paths that resolve a backend
(chat completions, research).

Supported shape (all keys optional except ``provider``)::

    {"provider": "bedrock", "region": "eu-central-1"}
    {"provider": "anthropic"}

Semantics:

- ``NULL`` / no ``provider`` key → inherit: the request decides (default
  anthropic), nothing changes.
- ``provider=bedrock`` → force ``backend=bedrock`` (+ region if set). The
  request's own ``backend``/``provider_tier`` fields are OVERRIDDEN — the pin
  is a compliance decision made by the operator, not a client preference.
- Unknown ``provider`` value → error. A typo in an admin-set config must not
  silently route a DSGVO-pinned user to the default backend.

The DB lookup is TTL-cached per billing identity so steady-state per-request
cost is a dict lookup, not a round-trip. A pin change via the admin panel
takes effect within the TTL.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Optional

from src.models import BackendType

logger = logging.getLogger(__name__)

_CACHE_TTL_SECONDS = 60.0
# billing-identity string → (expires_at_monotonic, provider_config-or-None)
_cache: dict[str, tuple[float, Optional[dict]]] = {}

SUPPORTED_PROVIDERS = {"anthropic", "bedrock"}


class UserProviderOverrideError(RuntimeError):
    """The user's provider_config demands something the server cannot honour.

    Deliberately NOT a silent fallback: for a Bedrock-pinned user, proceeding
    on the default backend would break the data-residency promise the pin
    exists for. Callers surface this as 503.
    """


class BedrockPinRequiredError(RuntimeError):
    """Bedrock was requested without an operator pin — refused, 403.

    Bedrock ist pay-per-token gegen das eigene AWS-Konto; jeder Call MUSS
    einem echten User zugeordnet sein (Umlage + 1:1-Audit). Der Operator-Pin
    (users.provider_config) garantiert genau das: er existiert nur auf einem
    echten User-Row, also schreibt das Ledger immer. Ein Client-Opt-in
    (backend=bedrock oder ein Bedrock-provider_tier) ohne Pin könnte dagegen
    anonyme/System-AWS-Kosten erzeugen. Kein silent-redirect auf die normale
    Bridge: der Client hat explizit Bedrock verlangt und bekäme sonst still
    ein anderes Datenresidenz-Verhalten.
    """


def assert_bedrock_is_pinned(effective_backend: Any, pinned: Optional[str]) -> None:
    """Gate: der EFFEKTIVE Backend (nach tier-/backend-Auflösung) darf nur
    dann Bedrock sein, wenn der Operator-Pin ihn gesetzt hat.

    Auf den aufgelösten Backend prüfen — nicht auf request_body.backend —
    damit auch provider_tier-Pfade (z.B. 'claude-dsgvo') erfasst sind.
    """
    if effective_backend != BackendType.BEDROCK:
        return
    if pinned == "bedrock":
        return
    raise BedrockPinRequiredError(
        "Bedrock is only reachable via the operator-set per-user pin "
        "(users.provider_config.provider='bedrock', Platform-Admin → Users). "
        "Client-side backend/provider_tier opt-in is not permitted: every "
        "Bedrock call must be attributable to a real user."
    )


class BedrockAttributionIncompleteError(RuntimeError):
    """A Bedrock (real-money) call lacks a resolvable environment attribution.

    The flat-rate Anthropic pool has €0 marginal cost, so a NULL reporting
    dimension there is merely cosmetic. Bedrock bills real AWS money per token:
    a call recorded with app_env=NULL books real spend where NO mode-filtered
    cost view (the Platform-Admin prod/staging/local filter) can see it — the
    exact €1.30 that was invisible in the Prod dashboard on 2026-07-09. Real
    money must be fully attributable, so we fail loud instead of silently
    booking invisible cost. Surfaced as 400 — the caller must add X-App-Env.
    """


def assert_bedrock_attribution_complete(
    effective_backend: Any, app_env: Optional[str]
) -> None:
    """Gate: a Bedrock call must carry a resolvable X-App-Env (prod/staging/local).

    Complements assert_bedrock_is_pinned (WHO pays) with WHERE the spend is
    booked. ``app_env`` is the ALREADY-NORMALISED value (normalize_app_env of
    the X-App-Env header) — None means the header was absent or unrecognised.
    Only Bedrock is gated: the Anthropic pool is not real-money, so an absent
    app_env there stays a non-fatal diagnostic (ai_call_writer warns).
    """
    if effective_backend != BackendType.BEDROCK:
        return
    if app_env:
        return
    raise BedrockAttributionIncompleteError(
        "Bedrock is real-money AWS spend and must be fully attributable, but "
        "this call carries no resolvable X-App-Env (prod/staging/local). Set "
        "X-App-Env: production|preview|development on the outbound Bridge "
        "request. Refusing to bill AWS cost that would be invisible to the "
        "mode-filtered cost dashboard."
    )


def invalidate_cache(user_key: Optional[str] = None) -> None:
    """Drop cached configs (all, or one identity) — e.g. after an admin PATCH."""
    if user_key is None:
        _cache.clear()
    else:
        _cache.pop(str(user_key).strip(), None)


async def get_user_provider_config(raw_user_id: Any) -> Optional[dict]:
    """Fetch provider_config for the inbound billing identity, TTL-cached.

    Returns None when the user has no override (inherit default). An identity
    that doesn't resolve to a Bridge user simply has no override — attribution
    and billing warn about unresolvable identities separately.

    Raises UserProviderOverrideError when the users table exists but the
    lookup FAILS (DB error): we then cannot know whether a compliance pin
    exists, and guessing "no pin" would silently break it.
    """
    if raw_user_id is None:
        return None
    key = str(raw_user_id).strip()
    if not key:
        return None

    now = time.monotonic()
    hit = _cache.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]

    from src.db.client import is_db_enabled, get_pool

    # No bridge DB on this instance → no user pool → no overrides can exist.
    if not is_db_enabled():
        return None

    config: Optional[dict] = None
    try:
        from src.identity.user_resolver import (
            resolve_user_id,
            UnresolvableUserIdentity,
        )

        try:
            uid = await resolve_user_id(key)
        except UnresolvableUserIdentity:
            uid = None  # anonymous / non-user identity → no override

        if uid is not None:
            pool = get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT provider_config FROM users WHERE id = $1", uid
                )
            if row is not None and row["provider_config"]:
                raw = row["provider_config"]
                config = json.loads(raw) if isinstance(raw, str) else dict(raw)
    except Exception as e:  # noqa: BLE001 — classified below, never swallowed
        logger.error(
            "user_provider_override: provider_config lookup failed "
            "(identity_len=%d): %s", len(key), e,
        )
        raise UserProviderOverrideError(
            "provider_config lookup failed — cannot verify whether this user "
            "is pinned to a specific backend"
        ) from e

    _cache[key] = (now + _CACHE_TTL_SECONDS, config)
    return config


def apply_user_provider_override(request_body: Any, config: dict) -> Optional[str]:
    """Mutate the request to honour the user's pin.

    Returns the pinned provider name when an override was applied, None when
    the config carries no routing directive. Raises UserProviderOverrideError
    on an unsupported provider value.
    """
    provider = (config or {}).get("provider")
    if provider is None:
        return None

    if provider not in SUPPORTED_PROVIDERS:
        raise UserProviderOverrideError(
            f"provider_config.provider={provider!r} is not supported "
            f"(supported: {sorted(SUPPORTED_PROVIDERS)})"
        )

    if config.get("model"):
        # Model pinning is not implemented — the backend router maps the
        # requested Anthropic model to the provider's ID space itself.
        logger.warning(
            "user_provider_override: provider_config.model=%r ignored "
            "(model pinning not implemented; requested model is used)",
            config.get("model"),
        )

    if provider == "bedrock":
        request_body.backend = BackendType.BEDROCK
        region = config.get("region")
        if region:
            request_body.bedrock_region = region
        # A compliance pin also overrides any client-chosen provider tier.
        request_body.provider_tier = None
        return "bedrock"

    # provider == "anthropic": explicit pin to the default backend.
    request_body.backend = BackendType.ANTHROPIC
    request_body.provider_tier = None
    return "anthropic"


async def enforce_user_provider_override(request: Any, request_body: Any) -> Optional[str]:
    """One-call helper for route handlers: look up + apply + mark the request.

    Sets ``request.state.user_provider_pinned`` so downstream error handling
    (cross-provider fallback) can refuse to reroute pinned traffic.
    Returns the pinned provider name or None.
    """
    raw_uid = request.headers.get("X-User-ID")
    config = await get_user_provider_config(raw_uid)
    if not config:
        return None

    pinned = apply_user_provider_override(request_body, config)
    if pinned:
        request.state.user_provider_pinned = pinned
        logger.info(
            "🔒 Per-user provider override active: provider=%s region=%s",
            pinned, getattr(request_body, "bedrock_region", None),
        )
    return pinned
