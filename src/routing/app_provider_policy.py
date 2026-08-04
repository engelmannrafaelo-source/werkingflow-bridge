"""Per-application provider routing policy — the Bridge, not the app, decides
*which backend* processes an app's traffic (Rafael, 2026-08-04).

Background
----------
werking.tools promises customers EU-resident processing (AWS Bedrock,
eu-central-1) for the Kunden-Apps (werking-report incl. Check, werking-energy,
werking-noise). Until now that was enforced per-USER via
``users.provider_config`` (:mod:`src.routing.user_provider_override`) — every
user had to be individually pinned. That has two structural gaps this module
closes:

1. A new / JIT-provisioned user defaults to Anthropic US until an operator
   manually pins them — a silent privacy gap between signup and pinning.
2. The Engelmann AI Hub authenticates with the SAME user accounts as
   Report/Energy. A per-user pin cannot distinguish "this call is Hub
   traffic" from "this call is a Report call by the same person" — Hub
   traffic rode along on the Bedrock pin and paid AWS per-token cost for
   traffic that should run on the flat-rate Anthropic account pool.

Rafael's decision: the provider decision belongs to the Bridge, keyed by
*application*, not by user. This module is the enforcement half of that
decision — the routing table (``APP_PROVIDER_RULES``) is a committed,
versioned architecture decision (which apps are DSGVO-scoped customer apps),
not a live-editable ops toggle, so it lives in code rather than a DB table
(cf. CLAUDE.md "Architektur vs Zustand").

Precedence (highest first)
---------------------------
1. Explicit per-user pin (``user_provider_override`` — unchanged, e.g. the
   Kainer-AVV contract case). Callers apply this policy only when the user
   is NOT pinned, mirroring the existing precedence in ``app_tier_policy``.
2. App rule (this module) — resolved from the AUTHENTICATED caller identity
   (``request.state.principal``, set by ``verify_api_key`` when
   ``BRIDGE_PRINCIPALS_ENABLED``) wherever that identity unambiguously names
   one app. ``X-App-ID`` is used only as a fallback for callers that are not
   (yet) principal-scoped to exactly one app — e.g. the legacy shared-key
   principal or a principals-disabled deployment — because a wildcard
   principal or an absent principal cannot itself prove which app is
   calling; the header is attribution, not authentication, in that case.
3. Global default — fail-safe Bedrock EU. An app that is neither ruled to
   Anthropic nor recognised as Bridge-internal falls to the data-residency-
   safe side, never silently to Anthropic US.

Scope: Bridge-internal / ops channels (see ``NON_CUSTOMER_APP_IDS``) are
deliberately excluded from the global fail-safe. They are not subject to the
werking.tools customer data-residency promise, and forcing Bedrock (plus the
strict attribution it requires, see ``assert_bedrock_attribution_complete``)
onto CUI/platform/dev-tooling traffic would be a wide, unreviewed blast
radius unrelated to the actual goal. Flagged for Rafael to confirm; see the
implementation report.

Fail-closed, like ``user_provider_override`` — NOT like ``app_tier_policy``
------------------------------------------------------------------------
This is a compliance decision (data residency), not a cost optimisation.
``resolve_app_provider_policy`` never raises (there is nothing to raise on: a
static dict lookup cannot fail at runtime the way a DB round-trip can), so
"fail-open vs fail-closed" reduces to one question: what happens when no
identity can be resolved at all? Answer: no policy is applied (``None``) and
routing is left untouched — the SAME behaviour as today for calls without any
app attribution. That is not a silent privacy leak: it is unchanged from the
pre-existing default, and ``enforce_attribution`` already separately governs
whether an unattributed call is even allowed through.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

from src.models import BackendType

logger = logging.getLogger(__name__)

# Wildcard marker mirrored from src.principals (kept local — this module must
# not import src.principals just for one constant, and the value is a public
# DB convention, not an implementation detail that could drift silently: a
# change there is a schema change, not a refactor).
_PRINCIPAL_WILDCARD = "*"


@dataclass(frozen=True)
class ProviderRule:
    provider: str  # "anthropic" | "bedrock" — validated at apply-time.
    region: Optional[str] = None


# Committed routing table: app_id -> ProviderRule. This IS the architecture
# decision Rafael made on 2026-08-04. Changing it is a deliberate, reviewed
# code change (PR), not an admin-panel toggle.
APP_PROVIDER_RULES: dict[str, ProviderRule] = {
    # Engelmann AI Hub shares user accounts with the Kunden-Apps but is not
    # itself a DSGVO-pinned customer app — runs on the flat-rate Anthropic
    # account pool, never billed per-token against AWS.
    "engelmann": ProviderRule(provider="anthropic"),
    # Kunden-Apps: werking.tools' EU-data-residency promise.
    "werking-report": ProviderRule(provider="bedrock", region="eu-central-1"),
    "werking-energy": ProviderRule(provider="bedrock", region="eu-central-1"),
    "werking-noise": ProviderRule(provider="bedrock", region="eu-central-1"),
}

# Fail-safe: any app not explicitly ruled to Anthropic falls here, never
# silently to Anthropic US.
GLOBAL_DEFAULT_RULE = ProviderRule(provider="bedrock", region="eu-central-1")

# Bridge-internal / ops callers — not a "Kunden-App" under the data-residency
# promise. Excluded from the global fail-safe (see module docstring). Keep in
# sync with the non-customer principal names in service_principals (cui-*,
# platform-*, dev-tooling) and the KNOWN_APP_IDS entries that are not apps.
NON_CUSTOMER_APP_IDS = {"cui", "platform", "dev-tooling", "partner-platform"}


class AppProviderPolicyError(RuntimeError):
    """An app-level provider rule names a provider this module doesn't know.

    Deliberately not silently ignored — a typo in APP_PROVIDER_RULES must not
    quietly leave a DSGVO-scoped app on the default backend."""


def _resolve_app_id(request: Any) -> tuple[Optional[str], str]:
    """Resolve the calling app's identity, preferring the AUTHENTICATED
    principal over the self-declared ``X-App-ID`` header.

    Returns ``(app_id, source)`` where source is ``"principal"`` (trusted —
    the token itself is scoped to exactly this app) or ``"header"``
    (attribution only — a wildcard/legacy principal or principals disabled,
    same trust level the Bridge has always had for X-App-ID).
    """
    principal = getattr(request.state, "principal", None)
    if principal is not None and not getattr(principal, "is_legacy", False):
        allowed = list(getattr(principal, "allowed_apps", []) or [])
        if len(allowed) == 1 and allowed[0] != _PRINCIPAL_WILDCARD:
            return allowed[0], "principal"

    from src.tenant.middleware import get_app_id_from_request

    return get_app_id_from_request(request), "header"


def resolve_app_provider_policy(request: Any) -> tuple[Optional[ProviderRule], Optional[str]]:
    """Resolve the app-level provider rule for this request, or (None, None).

    Never raises. Returns ``(rule, app_id)`` — ``app_id`` is returned
    alongside for logging even when ``rule`` is the global default.
    """
    app_id, source = _resolve_app_id(request)
    if not app_id:
        return None, None

    if app_id in NON_CUSTOMER_APP_IDS:
        return None, app_id

    rule = APP_PROVIDER_RULES.get(app_id)
    if rule is not None:
        return rule, app_id

    logger.info(
        "app_provider_policy: app_id=%r (via %s) has no explicit rule — "
        "falling back to global default (%s)",
        app_id, source, GLOBAL_DEFAULT_RULE.provider,
    )
    return GLOBAL_DEFAULT_RULE, app_id


def apply_app_provider_policy(request_body: Any, rule: ProviderRule) -> Optional[str]:
    """Mutate the request to honour the app rule. Mirrors
    ``user_provider_override.apply_user_provider_override`` exactly, so every
    downstream consumer that already understands "the Bridge pinned this
    call's provider" (the Bedrock-pin gate, cross-provider fallback refusal)
    keeps working unchanged for app-level pins too.

    Returns the provider name applied, or raises AppProviderPolicyError on an
    unsupported rule (a typo in APP_PROVIDER_RULES must fail loud, not
    silently leave a DSGVO app unrouted).
    """
    if rule.provider not in ("anthropic", "bedrock"):
        raise AppProviderPolicyError(
            f"APP_PROVIDER_RULES entry has unsupported provider={rule.provider!r} "
            f"(supported: anthropic, bedrock)"
        )

    if rule.provider == "bedrock":
        request_body.backend = BackendType.BEDROCK
        if rule.region:
            request_body.bedrock_region = rule.region
        request_body.provider_tier = None
        return "bedrock"

    request_body.backend = BackendType.ANTHROPIC
    request_body.provider_tier = None
    return "anthropic"
