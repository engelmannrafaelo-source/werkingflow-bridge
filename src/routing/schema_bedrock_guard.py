"""Fail-closed expectation for the Schema generator on a Bedrock-capable (production) worker.

Why this exists: the Bedrock pin (``users.provider_config``) only ever *adds* Bedrock. Without
a pin, with a wrong pin, or outside the production environment the Bridge routes to the
internal Anthropic accounts (see routing/app_provider_policy.py "Bedrock is pin-only" and the
non-prod downgrade in user_provider_override.py). For the Schema product the website promises
EU processing, so a silent Anthropic answer is a data-residency breach, not a degradation.

Scope is deliberately narrow:
* only ``werking-schema-generator`` (the authenticated app, see app_provider_policy._resolve_app_id),
* on every bridge whose own origin id is not ``dev`` (``federation.self_origin_id()``, process ENV
  ``BRIDGE_ORIGIN_ID``) AND, on the Dev bridge, for every request that arrives stamped with a non-dev origin
  (``federation.get_request_origin()``, the LB-stamped ``X-Bridge-Origin``). The request origin can only switch
  the guard ON, never off. This closes the Prod-LB failover to the Dev backup: a Prod request that lands on a
  Dev worker (502/timeout/worker outage/retried status) is stamped ``prod`` by the Prod LB, preserved by the Dev LB's
  trusted peer listener, and is rejected there instead of being answered by Anthropic.
* Guard refusals are never retried by the LB: they use 403/424 (the LB retries only 500/502/503/504/429), so the
  guard itself cannot trigger the failover.
* no new configuration variable: the facts used are the app identity, the existing origin id, the LB-stamped request
  origin, the worker's Bedrock credential state (424 if missing) and the already resolved routing result.

The check runs on the RESOLVED routing result (backend/region/model after pin, app policy,
tier resolution and the non-prod downgrade), before dispatch. It therefore also catches a client
``X-App-Env`` that makes the Bridge drop the Bedrock pin. Bedrock runtime errors need no extra
branch: a request that passes here carries the real operator pin, and pinned requests are never
re-routed to another provider (main.py: ``user_provider_pinned``).

Pure functions on purpose (no imports from the Bridge): testable in isolation.
"""
from __future__ import annotations

from typing import Any, Optional

SCHEMA_APP_ID = "werking-schema-generator"
EXPECTED_PROVIDER = "bedrock"
EXPECTED_REGION = "eu-central-1"
EXPECTED_MODEL = "claude-sonnet-5-5"


class SchemaBedrockExpectationError(RuntimeError):
    """The Schema call would not run on the expected Bedrock EU path. ``code`` is machine-readable."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


#: Origin id of the Dev bridge (deploy config ``BRIDGE_ORIGIN_ID=dev``; see federation.self_origin_id()).
DEV_ORIGIN_ID = "dev"


def guard_applies(app_id: Optional[str], self_origin_id: Optional[str], request_origin: Optional[str] = None) -> bool:
    """True for the Schema app unless BOTH this bridge is ``dev`` AND the request carries no non-dev origin.

    Fail-closed: an unset/empty/unknown own origin counts as NOT dev (guard active). The request origin only
    switches the guard ON: a stamp that is empty or ``dev`` changes nothing, a non-dev stamp (``prod`` or any
    foreign id) activates it even on the Dev bridge. It never switches the guard off, so a forged or stripped
    stamp cannot weaken it.
    """
    if app_id != SCHEMA_APP_ID:
        return False
    if (self_origin_id or "").strip().lower() != DEV_ORIGIN_ID:
        return True
    return (request_origin or "").strip().lower() not in ("", DEV_ORIGIN_ID)


#: Schema-EU route stamp (Root review R3, path B). The LB sets it ONLY in ``/__schema_local`` (local workers,
#: no cross-bridge entry) and clears every inbound copy (``more_clear_input_headers``). A Schema call on a
#: non-dev bridge without it did not come through the no-backup route and is refused before dispatch.
SCHEMA_ROUTE_HEADER = "x-bridge-schema-route"
SCHEMA_ROUTE_VALUE = "local"


def assert_schema_route(self_origin_id: Optional[str], route_stamp: Optional[str]) -> None:
    """Raise unless a Schema call on a non-dev bridge carries the LB's Schema-EU route stamp.

    Called only where ``guard_applies`` is True. On the Dev bridge (own origin ``dev``) nothing is required:
    under path B a production Schema request never reaches dev (no peer entry in ``claude_schema_local``, and
    the Dev LB refuses hopped Schema requests with 421). Fail-closed: unset own origin counts as NOT dev.
    The stamp can only add a refusal; forging it does not bypass the Bedrock checks that follow.
    """
    if (self_origin_id or "").strip().lower() == DEV_ORIGIN_ID:
        return
    if (route_stamp or "").strip().lower() != SCHEMA_ROUTE_VALUE:
        raise SchemaBedrockExpectationError(
            "schema_route_required",
            "Schema generator calls on this bridge must arrive via the Schema-EU LB route (no cross-bridge backup).",
        )


#: HTTP status used for "this bridge cannot serve Schema on Bedrock" / "guard refused": NOT in the LB's
#: ``proxy_next_upstream`` set (500, 502, 503, 504, 429), so the Prod LB never fails over to the Dev backup because of it.
UNAVAILABLE_STATUS = 424
REFUSAL_STATUS = 403


#: Schema mini contract: chat completions and ONE proxy target. No research, no other job kinds.
SCHEMA_JOB_KINDS = frozenset({"chat", "proxy"})
SCHEMA_PROXY_PATH = "/v1/privacy/smart-anonymize"
_SCHEMA_PROXY_PAYLOAD_KEYS = frozenset({"path", "body"})


def assert_schema_job_allowed(
    *,
    authenticated_app: Optional[str],
    claimed_app: Optional[str],
    kind: str,
    payload: Any,
) -> None:
    """Gate for POST /v1/jobs: a Schema job may only be ``chat`` or ``proxy`` -> smart-anonymize.

    Applies when the authenticated app OR the claimed (attribution) app is the Schema app. The job's
    self-call runs with the worker key and takes its X-App-ID from the attribution, so a Schema
    principal must not be able to relabel the job (``schema_job_attribution_mismatch``), and a caller
    that claims to be Schema gets the same narrow contract.
    """
    if SCHEMA_APP_ID not in (authenticated_app, claimed_app):
        return
    if authenticated_app == SCHEMA_APP_ID and claimed_app not in (None, "", SCHEMA_APP_ID):
        raise SchemaBedrockExpectationError(
            "schema_job_attribution_mismatch",
            f"Schema jobs must carry app_id {SCHEMA_APP_ID!r}, attribution claims {claimed_app!r}.",
        )
    if kind not in SCHEMA_JOB_KINDS:
        raise SchemaBedrockExpectationError(
            "schema_job_kind_not_allowed",
            f"Schema generator jobs may only be {sorted(SCHEMA_JOB_KINDS)}, got kind={kind!r} (no research, no other kinds).",
        )
    if kind == "proxy":
        if not isinstance(payload, dict) or set(payload) - _SCHEMA_PROXY_PAYLOAD_KEYS:
            raise SchemaBedrockExpectationError(
                "schema_job_payload_not_allowed",
                "Schema proxy jobs take exactly {'path','body'}.",
            )
        if payload.get("path") != SCHEMA_PROXY_PATH:
            raise SchemaBedrockExpectationError(
                "schema_job_proxy_path_not_allowed",
                f"Schema proxy jobs may only target {SCHEMA_PROXY_PATH!r}, got {payload.get('path')!r}.",
            )


_SCHEMA_IDENTITY_KEYS = ("app_id", "user_id", "tenant_id")


def bind_schema_job_attribution(body_attribution: Optional[dict], derived_attribution: Optional[dict]) -> dict:
    """Pin the identity of a Schema job to the VALIDATED request (headers/principal), not to the body.

    ``derived_attribution`` is what the server extracts from the authenticated request. The body may add
    non-identity fields, but app_id/user_id/tenant_id must be equal to the derived values; a missing or
    differing value is refused (``schema_job_attribution_mismatch``). The returned attribution always
    carries the derived identity, so the worker-key self-call cannot bill or route as someone else.
    """
    derived = dict(derived_attribution or {})
    body = dict(body_attribution or {})
    if derived.get("app_id") != SCHEMA_APP_ID:
        raise SchemaBedrockExpectationError(
            "schema_job_attribution_mismatch",
            "Schema jobs need the request identity (X-App-ID/principal) to be the Schema app.",
        )
    for key in _SCHEMA_IDENTITY_KEYS:
        if key in body and body[key] != derived.get(key):
            raise SchemaBedrockExpectationError(
                "schema_job_attribution_mismatch",
                f"attribution.{key} differs from the authenticated request identity.",
            )
    out = {**body}
    for key in _SCHEMA_IDENTITY_KEYS:
        if derived.get(key) is not None:
            out[key] = derived[key]
    return out


def assert_schema_research_refused(app_id: Optional[str]) -> None:
    """The direct /v1/research dispatcher may switch Bedrock to Anthropic by design: refuse it for Schema."""
    if app_id == SCHEMA_APP_ID:
        raise SchemaBedrockExpectationError(
            "schema_research_not_allowed",
            "The Schema generator may not call /v1/research (it can leave Bedrock EU).",
        )


#: Vertex EU (Rafael 17.11.2026: Homepage "fuers Erste non-Bedrock", EU-Vorgabe unveraendert). Claude-Modelle laufen
#: auf Vertex nur ueber den EU-Multi-Region-Endpunkt mit EU-Verarbeitung (Google "Data residency", Partnermodell-Tabelle:
#: Sonnet 5.5 EU multi-region ja, europe-west1/-west4 nein). Daher exakt Location ``eu`` und exakt dieser Host.
VERTEX_EU_PROVIDER = "vertex_eu"
VERTEX_EU_REGION = "eu"
VERTEX_EU_ENDPOINT_HOST = "aiplatform.eu.rep.googleapis.com"

#: Zugelassene Schema-Routen: Backend -> Region. Der OPERATOR-PIN waehlt genau eine davon; es gibt keinen
#: Wechsel zwischen ihnen zur Laufzeit (kein Bedrock-Rueckfall fuer einen Vertex-Pin und umgekehrt).
ALLOWED_SCHEMA_ROUTES = {EXPECTED_PROVIDER: EXPECTED_REGION, VERTEX_EU_PROVIDER: VERTEX_EU_REGION}


def _host_exact(url: Optional[str]) -> Optional[str]:
    from urllib.parse import urlsplit
    try:
        u = urlsplit((url or "").strip())
    except ValueError:
        return None
    if u.scheme != "https" or u.port is not None or u.username or u.password or u.path not in ("", "/") or u.query:
        return None
    return u.hostname


#: PRIVATER NICHT-EU-TESTWEG (Rafael 17.11.2026, nur synthetische Daten, nur seine Test-Identitaet): Anthropic
#: Messages-API per API-Key (Pin ``anthropic_direct``, Tier claude-direct-notools). KEINE EU-Route und deshalb NICHT
#: in ALLOWED_SCHEMA_ROUTES. Zugelassen nur, wenn ZWEI unabhaengige Schloesser stimmen: (1) Operator-Pin
#: users.provider_config.provider='anthropic_direct' am Benutzer und (2) dieser Benutzer steht in der Worker-Env
#: SCHEMA_NONEU_TEST_USER_IDS (kommagetrennte UUIDs). Leer/fehlend -> jeder Aufruf auf diesem Weg wird abgewiesen.
#: DRITTES Schloss (Pruefung R1 A1, 18962da3/Kanzler 17.11.): der Aufruf muss von einem EIGENEN Test-Principal kommen
#: (``request.state.principal.name``, bei Jobs der serverseitig gespeicherte Anleger-Principal), dessen Name in
#: SCHEMA_NONEU_TEST_PRINCIPALS steht. Der oeffentliche Homepage-Principal und der Legacy-Schluessel oeffnen den Weg
#: nie, auch wenn X-User-ID die Test-UUID traegt. Leer/fehlend -> zu.
NONEU_TEST_PROVIDER = "anthropic_direct"
NONEU_TEST_USERS_ENV = "SCHEMA_NONEU_TEST_USER_IDS"
NONEU_TEST_PRINCIPALS_ENV = "SCHEMA_NONEU_TEST_PRINCIPALS"
_NIE_TEST_PRINCIPAL = frozenset({"legacy", "*"})


def noneu_test_principals(raw: Optional[str]) -> frozenset:
    """Parse the principal allowlist strictly; 'legacy' and '*' are never accepted."""
    import re
    out = set()
    for part in (raw or "").split(","):
        v = part.strip()
        if v and v.lower() not in _NIE_TEST_PRINCIPAL and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", v):
            out.add(v)
    return frozenset(out)


def assert_noneu_test_allowed(
    *,
    operator_pin: Optional[str],
    model_id: Optional[str],
    user_id: Optional[str],
    noneu_test_users: frozenset,
    principal_name: Optional[str],
    noneu_test_principals: frozenset,
) -> None:
    """All three locks of the private non-EU test route, AND-linked: operator pin, user allowlist, own test principal.
    Used by the Schema guard AND again by the strict dispatch (also where the guard is off, e.g. the Dev bridge)."""
    if operator_pin != NONEU_TEST_PROVIDER:
        raise SchemaBedrockExpectationError(
            "schema_requires_matching_operator_pin",
            f"The non-EU test route needs the operator pin {NONEU_TEST_PROVIDER!r}; pin={operator_pin!r}.",
        )
    if not user_id or str(user_id).strip().lower() not in noneu_test_users:
        raise SchemaBedrockExpectationError(
            "schema_noneu_test_user_not_allowed",
            f"The non-EU test route is reserved for the users in {NONEU_TEST_USERS_ENV}; this user is not listed.",
        )
    if (not principal_name or principal_name.lower() in _NIE_TEST_PRINCIPAL
            or principal_name not in noneu_test_principals):
        raise SchemaBedrockExpectationError(
            "schema_noneu_test_principal_not_allowed",
            f"The non-EU test route is reserved for the principals in {NONEU_TEST_PRINCIPALS_ENV}; "
            "this caller's principal is not listed.",
        )
    if model_id != EXPECTED_MODEL:
        raise SchemaBedrockExpectationError(
            "schema_route_model_mismatch",
            f"Schema generator calls must use model {EXPECTED_MODEL!r}, resolved model={model_id!r}.",
        )


def noneu_test_users(raw: Optional[str]) -> frozenset:
    """Parse the allowlist strictly: only canonical lowercase-comparable 36-char UUIDs; anything else is ignored."""
    import re
    out = set()
    for part in (raw or "").split(","):
        v = part.strip().lower()
        if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", v):
            out.add(v)
    return frozenset(out)


def assert_schema_expected_route(
    *,
    effective_backend: Any,
    operator_pin: Optional[str],
    region: Optional[str],
    model_id: Optional[str],
    vertex_endpoint: Optional[str] = None,
    user_id: Optional[str] = None,
    noneu_test_users: frozenset = frozenset(),
    principal_name: Optional[str] = None,
    noneu_test_principals: frozenset = frozenset(),
) -> None:
    """Raise unless the resolved route is one of ALLOWED_SCHEMA_ROUTES, chosen by the matching operator pin,
    in its EU region, with the approved model. For Vertex additionally the exact EU multi-region host.

    Fail-closed: unknown backend, pin != backend (no fallback across routes), wrong region/model/host -> refused.
    ``vertex_endpoint`` is the endpoint the Vertex client will call (``VERTEX_EU_ENDPOINT`` or its default)."""
    backend = getattr(effective_backend, "value", effective_backend)
    if backend == NONEU_TEST_PROVIDER:
        assert_noneu_test_allowed(operator_pin=operator_pin, model_id=model_id, user_id=user_id,
                                  noneu_test_users=noneu_test_users, principal_name=principal_name,
                                  noneu_test_principals=noneu_test_principals)
        return
    if backend not in ALLOWED_SCHEMA_ROUTES:
        raise SchemaBedrockExpectationError(
            "schema_route_not_allowed",
            f"Schema generator calls must run on an approved EU route {sorted(ALLOWED_SCHEMA_ROUTES)}, "
            f"but this call resolved to backend={backend!r}. No fallback is attempted.",
        )
    if operator_pin != backend:
        raise SchemaBedrockExpectationError(
            "schema_requires_matching_operator_pin",
            f"Schema generator calls need the operator pin of the resolved route (users.provider_config.provider="
            f"{backend!r}); pin={operator_pin!r}. No switching between routes.",
        )
    if region != ALLOWED_SCHEMA_ROUTES[backend]:
        raise SchemaBedrockExpectationError(
            "schema_route_region_mismatch",
            f"Schema generator on {backend!r} must use region {ALLOWED_SCHEMA_ROUTES[backend]!r}, resolved region={region!r}.",
        )
    if model_id != EXPECTED_MODEL:
        raise SchemaBedrockExpectationError(
            "schema_route_model_mismatch",
            f"Schema generator calls must use model {EXPECTED_MODEL!r}, resolved model={model_id!r}.",
        )
    if backend == VERTEX_EU_PROVIDER and _host_exact(vertex_endpoint) != VERTEX_EU_ENDPOINT_HOST:
        raise SchemaBedrockExpectationError(
            "schema_vertex_endpoint_not_eu",
            f"Schema generator on Vertex must call exactly https://{VERTEX_EU_ENDPOINT_HOST} (EU multi-region).",
        )


def assert_schema_expected_bedrock(
    *,
    effective_backend: Any,
    operator_pin: Optional[str],
    region: Optional[str],
    model_id: Optional[str],
) -> None:
    """Raise unless the resolved route is Bedrock, operator-pinned, EU Frankfurt, approved model.

    ``effective_backend`` is the resolved BackendType (compared by value, so the enum is not imported).
    ``operator_pin`` must be the REAL operator pin from users.provider_config, never the app-rule pin.
    """
    backend = getattr(effective_backend, "value", effective_backend)
    if backend != EXPECTED_PROVIDER:
        raise SchemaBedrockExpectationError(
            "schema_requires_bedrock",
            f"Schema generator calls must run on Bedrock EU, but this call resolved to backend={backend!r} "
            "(missing/wrong provider pin or non-production environment). No Anthropic fallback is attempted.",
        )
    if operator_pin != EXPECTED_PROVIDER:
        raise SchemaBedrockExpectationError(
            "schema_requires_operator_bedrock_pin",
            "Schema generator calls need the operator-set Bedrock pin (users.provider_config.provider='bedrock').",
        )
    if region != EXPECTED_REGION:
        raise SchemaBedrockExpectationError(
            "schema_bedrock_region_mismatch",
            f"Schema generator calls must use region {EXPECTED_REGION!r}, resolved region={region!r}.",
        )
    if model_id != EXPECTED_MODEL:
        raise SchemaBedrockExpectationError(
            "schema_bedrock_model_mismatch",
            f"Schema generator calls must use model {EXPECTED_MODEL!r}, resolved model={model_id!r}.",
        )
