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

The lookup is TTL-cached per billing identity so steady-state per-request
cost is a dict lookup, not a round-trip. A pin change via the admin panel
takes effect within the TTL.

Resolution path (ADR-0009 Schritt 2, worker-DB-free reads): a cache miss asks
platform-api (GET /v1/internal/users/{id}/provider-config, see
src/internal_routes.py) instead of querying Postgres directly, with the direct
query kept as an in-call fallback for as long as this process still has
BRIDGE_DB_URL. Same three-stage shape as principals.py and
identity/user_resolver.py.

FAIL-CLOSED, deliberately, and this is the decision that matters here: if
NEITHER channel can answer, this module RAISES. It never degrades to "no pin".
A pin is an operator's compliance decision (EU data residency via Bedrock);
answering "no pin" because a lookup failed would route exactly the user whose
data must stay in the EU onto the default backend — silently, with a 200 and a
normal-looking response. A 503 is recoverable and visible; a wrong-region call
is neither. The one case that is NOT a failure is a process with no configured
channel to the user pool at all (no BRIDGE_DB_URL and no BRIDGE_SERVICE_TOKEN):
that instance has no users, hence no pins — a static deployment fact, which is
what the old `is_db_enabled()` guard expressed back when the direct connection
was the only channel.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

from src.models import BackendType
from src.platform_client import PlatformUnavailable, call_platform

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


class BedrockNonProdRefusedError(RuntimeError):
    """Bedrock reached from a non-production app environment — refused, 403.

    Bedrock ist echtes AWS-Geld. Ein Pin sagt WER auf Bedrock darf, aber nicht
    WOMIT: dieselben Credentials liegen auf beiden Bridges, und jede lokale,
    Staging-, Partner- oder CI-Instanz kann sich als derselbe User anmelden.
    Genau so wurde ein Konvertierungs-Loop einer Partner-Dev-Instanz 6 Tage
    lang gegen das AWS-Konto gebucht (2026-08-04..11). Deshalb gilt: Bedrock
    NUR aus ``app_env == 'prod'``. Non-Prod laeuft ueber die internen
    Anthropic-Accounts (Flatrate, 0 EUR Grenzkosten).

    SEIT 2026-08-12 ist dieser Fehler der SCHMALE Fall. Ein *positiv erkanntes*
    Staging/Local stuft ``apply_user_provider_override`` von sich aus auf
    Anthropic herunter (Rafael: „Bedrock-Pin wird ausserhalb prod ignoriert") —
    dort kommt es also gar nicht mehr hierher. Uebrig bleibt der Fall
    ``app_env is None``: eine Umgebung, die sich nicht ausweist. Die wird
    weiterhin abgewiesen statt umgeleitet, denn ein Produktions-Deployment
    ohne ``X-App-Env`` wuerde sonst still seine Datenresidenz wechseln — und
    genau das darf nicht unbemerkt passieren.
    """


def assert_bedrock_is_pinned(
    effective_backend: Any,
    pinned: Optional[str],
    *,
    app_env: Optional[str] = None,
) -> None:
    """Gate: der EFFEKTIVE Backend (nach tier-/backend-Auflösung) darf nur
    dann Bedrock sein, wenn (a) der Operator-Pin ihn gesetzt hat UND (b) der
    Call aus der Produktionsumgebung kommt.

    Auf den aufgelösten Backend prüfen — nicht auf request_body.backend —
    damit auch provider_tier-Pfade (z.B. 'claude-dsgvo') erfasst sind.

    ``pinned`` MUSS der ECHTE Operator-Pin aus ``users.provider_config`` sein
    (das Ergebnis von ``enforce_user_provider_override``). Insbesondere darf
    hier NICHT der von ``app_provider_policy`` synthetisierte Pin durchgereicht
    werden: eine App-Regel ist kein User-Pin, und ein Gate, das den vom
    Aufrufer selbst erzeugten Ausweis prueft, ist kein Gate (genau dieses Leck
    hat den Loop vom 2026-08-04 bezahlt).

    ``app_env`` ist der bereits normalisierte Bucket (prod|staging|local) aus
    ``normalize_app_env``. None (Header fehlt/unbekannt) ist fail-closed: kein
    nachweislicher Prod-Call, also kein Bedrock.
    """
    if effective_backend != BackendType.BEDROCK:
        return
    if pinned != "bedrock":
        raise BedrockPinRequiredError(
            "Bedrock is only reachable via the operator-set per-user pin "
            "(users.provider_config.provider='bedrock', Platform-Admin → Users). "
            "Client-side backend/provider_tier opt-in is not permitted: every "
            "Bedrock call must be attributable to a real user."
        )
    if app_env != "prod":
        raise BedrockNonProdRefusedError(
            "Bedrock is restricted to production traffic, but this call reports "
            f"app_env={app_env!r}. The per-user pin says WHO may use Bedrock, not "
            "from WHICH environment — local, staging, partner and CI instances "
            "authenticate as the same user and would book real AWS spend. Send "
            "X-App-Env: production from a production deployment, or let the call "
            "run on the internal Anthropic accounts."
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
    effective_backend: Any,
    *,
    app_env: Optional[str],
    app_id: Optional[str],
) -> None:
    """Gate: a Bedrock (real-money) call must be FULLY attributed, else fail loud.

    The invariant Bedrock cost has to satisfy: what the user is billed == what
    goes to AWS == what the accounting dashboard shows. That only holds if every
    real-money call names WHO pays, WHICH app, and WHICH environment — otherwise
    the spend lands somewhere no cost view can see it (the €1.30 blind spot,
    2026-07-09). assert_bedrock_is_pinned already guarantees WHO (the pin sits
    only on a real user row); this adds the two dimensions that were slipping
    through as NULL:

      * ``app_env`` — ALREADY-NORMALISED (normalize_app_env of X-App-Env);
        None means the header was absent/unrecognised. Drives the prod/staging/
        local "mode" filter.
      * ``app_id`` — the RESOLVED app id (extract_attribution_context, incl. the
        X-Client-ID fallback); None means the call names no app.

    Pass the SAME resolved values the ledger persists, so the gate rejects
    exactly the calls that would otherwise record un-attributable AWS cost.
    Only Bedrock is gated: the flat-rate Anthropic pool is €0 marginal cost, so
    an absent dimension there stays a non-fatal diagnostic (ai_call_writer warns).
    """
    if effective_backend != BackendType.BEDROCK:
        return
    missing = []
    if not app_env:
        missing.append("X-App-Env (production|preview|development)")
    if not app_id:
        missing.append("X-App-ID")
    if not missing:
        return
    raise BedrockAttributionIncompleteError(
        "Bedrock is real-money AWS spend and must be fully attributable, but "
        "this call is missing: " + ", ".join(missing) + ". Set these headers on "
        "the outbound Bridge request. Refusing to bill AWS cost that would be "
        "invisible / un-attributable in the cost dashboard."
    )


def invalidate_cache(user_key: Optional[str] = None) -> None:
    """Drop cached configs (all, or one identity) — e.g. after an admin PATCH."""
    if user_key is None:
        _cache.clear()
    else:
        _cache.pop(str(user_key).strip(), None)


async def fetch_provider_config_from_db(user_id: Any) -> Optional[dict]:
    """The one DB leaf: users.provider_config for an ALREADY-RESOLVED Bridge
    user id.

    Split out so platform-api can expose exactly this query as an internal
    endpoint (GET /v1/internal/users/{user_id}/provider-config) while this
    module's own direct-DB fallback calls the identical function — the query
    lives in ONE place, not two (same split as
    identity.user_resolver.lookup_user_id_by_email).

    Returns None both for "no such user" and for "user has no pin": to every
    caller those mean the same thing, namely that there is nothing to enforce.
    Raises on a DB error — deciding what an unanswerable lookup means is the
    caller's job, not this leaf's.
    """
    from src.db.client import get_pool

    pool = get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT provider_config FROM users WHERE id = $1", user_id
        )
    if row is None or not row["provider_config"]:
        return None
    raw = row["provider_config"]
    return json.loads(raw) if isinstance(raw, str) else dict(raw)


def _user_pool_channel_configured() -> bool:
    """Does this process have ANY configured route to the user pool?

    Two exist after ADR-0009 Schritt 2: platform-api (which platform_client
    refuses to call without BRIDGE_SERVICE_TOKEN) and the legacy direct
    connection (BRIDGE_DB_URL). Neither configured means this instance runs
    without a user pool at all — no users, therefore no pins. That is a static
    deployment fact, exactly what the pre-Schritt-2 `is_db_enabled()` guard
    said when the direct connection was the only channel.

    Read this predicate strictly: CONFIGURED-BUT-FAILING is never this case. It
    raises. "Nobody can be pinned here" and "I could not find out whether this
    user is pinned" are different answers and must not share a code path.
    """
    from src.db.client import is_db_enabled

    return is_db_enabled() or bool(os.getenv("BRIDGE_SERVICE_TOKEN"))


async def _fetch_via_direct_db(uid: Any) -> Optional[dict]:
    """Direct-DB fallback, used only when platform-api could not answer.

    Raises instead of returning None when there is no DB to fall back to: at
    that point BOTH channels to the user pool have failed, and this module's
    fail-closed promise has to survive the platform-api hop, not just the
    direct query it used to be.
    """
    from src.db.client import is_db_enabled

    if not is_db_enabled():
        raise UserProviderOverrideError(
            "provider_config lookup: platform-api could not answer and this "
            "process has no direct-DB fallback (BRIDGE_DB_URL unset). Refusing "
            "to assume 'not pinned' — that would route a possibly Bedrock/"
            "EU-pinned user onto the default backend and silently change its "
            "data residency. If this worker has moved off production-barrier "
            "(ADR-0009 Schritt 3/4), platform-api reachability is now "
            "load-bearing — investigate why it failed."
        )
    return await fetch_provider_config_from_db(uid)


async def _lookup_provider_config(uid: Any) -> Optional[dict]:
    """Cache-miss resolution: platform-api first, direct DB second.

    Opts into ONE retry: this is a pure read, so replaying it cannot
    double-write (see platform_client.call_platform for why retrying is
    opt-in). The retry earns its keep precisely because this path is
    fail-closed — without it a platform-api restart of a few hundred
    milliseconds would 503 every pinned user's call.
    """
    from src.federation import is_foreign_origin

    try:
        resp = await call_platform(
            "GET", f"/v1/internal/users/{uid}/provider-config", retries=1,
            domain="user",
        )
    except PlatformUnavailable as e:
        if is_foreign_origin():
            # ADR-0011: the pin lives in the HOME users table; the local row
            # for this UUID is a different person or nobody.
            raise
        logger.error(
            "provider_config lookup via platform-api failed (%s) — "
            "falling back to direct DB", e,
        )
        return await _fetch_via_direct_db(uid)

    if resp.status_code == 200 and isinstance(resp.json, dict) and "providerConfig" in resp.json:
        raw = resp.json["providerConfig"]
        return dict(raw) if raw else None

    # An unexpected contract (wrong status, malformed body) is treated like
    # unreachable, NOT like "no pin". A platform-api that has not been deployed
    # yet answers 404 on this route, and reading that as "user is not pinned"
    # would turn a missing deployment into a silent data-residency change.
    if is_foreign_origin():
        raise PlatformUnavailable(
            f"home-bridge provider_config lookup returned unexpected "
            f"status={resp.status_code} — refusing the local-DB fallback for a "
            f"foreign-origin request (ADR-0011)"
        )
    logger.error(
        "provider_config lookup via platform-api returned unexpected "
        "status=%s body=%r — falling back to direct DB",
        resp.status_code, resp.json,
    )
    return await _fetch_via_direct_db(uid)


async def get_user_provider_config(raw_user_id: Any) -> Optional[dict]:
    """Fetch provider_config for the inbound billing identity, TTL-cached.

    Returns None when the user has no override (inherit default). An identity
    that doesn't resolve to a Bridge user simply has no override — attribution
    and billing warn about unresolvable identities separately.

    Raises UserProviderOverrideError when a user pool is reachable in principle
    but the lookup FAILS (platform-api down AND no usable direct connection, or
    a DB error): we then cannot know whether a compliance pin exists, and
    guessing "no pin" would silently break it.
    """
    if raw_user_id is None:
        return None
    key = str(raw_user_id).strip()
    if not key:
        return None

    # ADR-0011: the pin is a fact of the request's HOME bridge — scope the
    # CACHE key (key itself stays the raw identity for resolve_user_id below)
    # so the same identity string cannot cross budget domains.
    from src.federation import cache_scope
    cache_key = f"{cache_scope()}:{key}" if cache_scope() else key

    now = time.monotonic()
    hit = _cache.get(cache_key)
    if hit is not None and hit[0] > now:
        return hit[1]

    # No channel to a user pool at all → no users → no overrides can exist.
    if not _user_pool_channel_configured():
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
            config = await _lookup_provider_config(uid)
    except UserProviderOverrideError:
        # Already classified (and already logged) by the fallback path — re-wrapping
        # it would only bury the specific message under a generic one.
        raise
    except Exception as e:  # noqa: BLE001 — classified below, never swallowed
        logger.error(
            "user_provider_override: provider_config lookup failed "
            "(identity_len=%d): %s", len(key), e,
        )
        raise UserProviderOverrideError(
            "provider_config lookup failed — cannot verify whether this user "
            "is pinned to a specific backend"
        ) from e

    _cache[cache_key] = (now + _CACHE_TTL_SECONDS, config)
    return config


#: Positiv erkannte Nicht-Produktions-Umgebungen (aus ``normalize_app_env``).
#: ``None`` gehoert BEWUSST nicht dazu — siehe apply_user_provider_override.
NON_PROD_APP_ENVS = frozenset({"staging", "local"})


def apply_user_provider_override(
    request_body: Any, config: dict, *, app_env: Optional[str] = None
) -> Optional[str]:
    """Mutate the request to honour the user's pin.

    Returns the pinned provider name when an override was applied, None when
    the config carries no routing directive. Raises UserProviderOverrideError
    on an unsupported provider value.

    ``app_env`` ist der normalisierte Bucket (prod|staging|local|None) des
    Aufrufs. Ein BEDROCK-Pin gilt nur in ``prod`` (Rafael, 2026-08-12):

    * ``prod``               → Pin wird angewendet.
    * ``staging`` / ``local`` → Pin wird IGNORIERT, der Call laeuft auf den
      internen Anthropic-Konten. Der Pin traegt die EU-Datenresidenz echter
      Kundendaten — die liegen per Definition nicht auf einer Staging- oder
      Entwicklungs-Instanz. Dort ist er nur noch eine Sperre gegen das eigene
      Testen: die Dev-Bridge hat keine AWS-Zugangsdaten, der Bedrock-Backend
      liess sich also gar nicht bauen, und der Aufruf starb mit einem
      irrefuehrenden 503 ``user_provider_override_unavailable``, das nginx
      obendrein als ``capacity_busy`` etikettierte.
    * ``None`` (Header fehlt/unbekannt) → Pin wird ANGEWENDET, nicht
      heruntergestuft. Das ist der Unterschied, auf den es ankommt: ein
      Produktions-Deployment, das ``X-App-Env`` vergisst, wuerde sonst STILL
      auf die Anthropic-Konten ausweichen — eine unbemerkte Aenderung der
      Datenresidenz. Angewendet laeuft es stattdessen in
      ``assert_bedrock_is_pinned`` und wird dort laut abgewiesen.
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
        if app_env in NON_PROD_APP_ENVS:
            logger.warning(
                "user_provider_override: Bedrock-Pin in app_env=%r ignoriert — "
                "Call laeuft auf den internen Anthropic-Konten. Der Pin gilt nur "
                "in prod (Rafael 2026-08-12); ausserhalb liegen keine echten "
                "Kundendaten, fuer die er die EU-Residenz tragen muesste.",
                app_env,
            )
            request_body.backend = BackendType.ANTHROPIC
            request_body.provider_tier = None
            return "anthropic"

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
    Returns the pinned provider name or None — bei einem in Nicht-Produktion
    heruntergestuften Bedrock-Pin also ``"anthropic"``, nicht ``"bedrock"``.
    Das ist gewollt: der zurueckgegebene Wert ist der TATSAECHLICH gesetzte
    Backend, und genau darauf verlassen sich die Aufrufer (Fallback-Sperre,
    Bedrock-Gate, Research-Ausnahme).

    Die Umgebung wird hier aufgeloest statt vom Aufrufer verlangt: beide
    Aufrufstellen (Chat, Research) haetten sie sonst einzeln nachreichen
    muessen, und eine vergessene waere ein stiller Rueckfall auf das alte
    Verhalten.
    """
    raw_uid = request.headers.get("X-User-ID")
    config = await get_user_provider_config(raw_uid)
    if not config:
        return None

    # Lokaler Import wie bei get_user_provider_config' DB-Zugriff: haelt das
    # Routing-Modul frei von einer Modul-Ebenen-Abhaengigkeit auf src.tenant.
    from src.tenant import get_app_env_from_request, normalize_app_env

    app_env = normalize_app_env(get_app_env_from_request(request))

    pinned = apply_user_provider_override(request_body, config, app_env=app_env)
    if pinned:
        request.state.user_provider_pinned = pinned
        logger.info(
            "🔒 Per-user provider override active: provider=%s region=%s",
            pinned, getattr(request_body, "bedrock_region", None),
        )
    return pinned
