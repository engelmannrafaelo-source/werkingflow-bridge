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
