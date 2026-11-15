"""Fail-closed expectation for the Schema generator on a Bedrock-capable (production) worker.

Why this exists: the Bedrock pin (``users.provider_config``) only ever *adds* Bedrock. Without
a pin, with a wrong pin, or outside the production environment the Bridge routes to the
internal Anthropic accounts (see routing/app_provider_policy.py "Bedrock is pin-only" and the
non-prod downgrade in user_provider_override.py). For the Schema product the website promises
EU processing, so a silent Anthropic answer is a data-residency breach, not a degradation.

Scope is deliberately narrow:
* only ``werking-schema-generator`` (the authenticated app, see app_provider_policy._resolve_app_id),
* only on a worker that holds Bedrock credentials. Those exist solely on the production worker;
  the Dev bridge and every other app are untouched.
* no new configuration variable: the facts used are the app identity, the worker's own Bedrock
  credential state, and the already resolved routing result.

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


def guard_applies(app_id: Optional[str], bedrock_configured: bool) -> bool:
    """True for the Schema app on a worker that can serve Bedrock (the production worker)."""
    return app_id == SCHEMA_APP_ID and bool(bedrock_configured)


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
