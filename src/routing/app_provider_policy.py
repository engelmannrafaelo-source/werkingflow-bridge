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
3. Global default — internal Anthropic accounts. See "Bedrock is pin-only"
   below: an app rule can no longer grant Bedrock at all.

Bedrock is pin-only (Rafael, 2026-08-11)
-----------------------------------------
The 2026-08-04 version of this module pinned the Kunden-Apps to Bedrock by
APP rule. Because callers apply the app rule into the SAME variable a user pin
uses, the Bedrock gate (``assert_bedrock_is_pinned``) then verified a pin this
module had just minted — a caller issuing its own permission. Consequence: any
instance sending ``X-App-ID: werking-report`` booked real AWS money, including
local, staging, partner and CI deployments. A stuck conversion loop on the
Partner-CUI server did exactly that for six days (see the devops memo
``project_bedrock_dokument_summary_minute_loop_20260811``).

Rafael's rule now: **Bedrock is reachable only through an explicit per-user
operator pin (``users.provider_config``), and only from production.**
Everything else runs on the internal Anthropic accounts. This module can
therefore pin ``anthropic`` only; ``apply_app_provider_policy`` raises on any
other provider so the hole cannot be reopened by editing the table.

KNOWN CONSEQUENCE, flagged for Rafael: this reverses gap (1) above. A customer
who is not (yet) pinned no longer lands on Bedrock EU by default but on the
internal Anthropic accounts — i.e. the provisioning gap between signup and
pinning is a data-residency gap again. The pin is now the ONLY thing carrying
the EU-residency promise, so per-user pinning has to be part of provisioning,
not a follow-up step. If that trade is not wanted, the alternative is to refuse
(fail loud) instead of routing unpinned customer-app traffic to Anthropic —
one branch in ``apply_app_provider_policy``.

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
    # Kunden-Apps: internal Anthropic accounts unless the individual USER
    # carries an operator Bedrock pin (see module docstring, Rafael 2026-08-11).
    "werking-report": ProviderRule(provider="anthropic"),
    "werking-energy": ProviderRule(provider="anthropic"),
    "werking-noise": ProviderRule(provider="anthropic"),
}

# Global default: internal Anthropic accounts. An app rule can no longer put
# traffic on Bedrock at all — only a per-user operator pin can (Rafael,
# 2026-08-11). See the "Bedrock is pin-only" section of the module docstring.
GLOBAL_DEFAULT_RULE = ProviderRule(provider="anthropic")

# Bridge-internal / ops callers — not a "Kunden-App" under the data-residency
# promise. Excluded from the global fail-safe (see module docstring). Keep in
# sync with the non-customer principal names in service_principals (cui-*,
# platform-*, dev-tooling) and the KNOWN_APP_IDS entries that are not apps.
NON_CUSTOMER_APP_IDS = {"cui", "platform", "dev-tooling", "partner-platform"}

# Apps whose rule OUTRANKS an existing per-user operator pin (Rafael,
# 2026-08-13). The default precedence — user pin beats app rule — is correct
# for every DSGVO-scoped Kunden-App: the pin IS that customer's EU-residency
# commitment (e.g. the Kainer-AVV contract case) and an app rule must never
# dissolve it. The Engelmann AI Hub is the one documented exception, and the
# reason is stated in this module's own Background section: the Hub
# authenticates with the SAME user accounts as Report/Energy, so a per-user
# Bedrock pin silently dragged Hub traffic onto Bedrock — the exact failure
# the app-keyed policy was written to end. Because the pin cannot distinguish
# "Report call" from "Hub call by the same person", only the app identity can.
#
# Safe by construction, in both directions:
#   * Membership here is per-APP, not per-user — a Kunden-App can never appear
#     in this set without a reviewed change to this file, so no customer's pin
#     is weakened by an admin action or a DB edit.
#   * ``apply_app_provider_policy`` refuses any provider except "anthropic"
#     (see "Bedrock is pin-only"), so an entry here can only ever move traffic
#     OFF Bedrock onto the flat-rate pool — never a user ONTO Bedrock, and
#     never onto per-token AWS spend the user did not opt into.
PIN_OVERRIDING_APP_IDS = {"engelmann"}


def app_rule_outranks_user_pin(app_id: Optional[str]) -> bool:
    """Whether ``app_id``'s rule may override an EXISTING per-user pin.

    False for everything not explicitly listed in ``PIN_OVERRIDING_APP_IDS``
    — including an unresolved (``None``) app identity. Fail-closed on purpose:
    if the Bridge cannot prove which app is calling, it must keep the user's
    pin, because that pin may be a contractual data-residency promise.
    """
    if app_id is None:
        return False
    return app_id in PIN_OVERRIDING_APP_IDS


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


#: Der EINE provider_tier, den diese Regel nicht wegraeumt — siehe
#: ``apply_app_provider_policy``. Bewusst eine Einzelnennung und keine
#: Kategorie: jede weitere Ausnahme soll eine sichtbare Aenderung an dieser
#: Zeile kosten.
GEMINI_VISION_TEST_TIER = "gemini-vision-test"


def client_requests_gemini_vision(request_body: Any) -> bool:
    """Hat der CLIENT selbst den Gemini-Bild-Testweg angefragt?

    Wie ``client_requests_bedrock`` VOR ``enforce_user_provider_override``
    aufzurufen: der Pin raeumt ``provider_tier`` ab, danach ist die Absicht des
    Aufrufers aus dem Body nicht mehr ablesbar.
    """
    return getattr(request_body, "provider_tier", None) == GEMINI_VISION_TEST_TIER


def apply_app_provider_policy(
    request_body: Any,
    rule: ProviderRule,
    *,
    client_requested_bedrock: Optional[bool] = None,
    client_requested_gemini_vision: Optional[bool] = None,
) -> Optional[str]:
    """Mutate the request to honour the app rule. Mirrors
    ``user_provider_override.apply_user_provider_override`` exactly, so every
    downstream consumer that already understands "the Bridge pinned this
    call's provider" (the Bedrock-pin gate, cross-provider fallback refusal)
    keeps working unchanged for app-level pins too.

    Returns the provider name applied, or raises AppProviderPolicyError on an
    unsupported rule (a typo in APP_PROVIDER_RULES must fail loud, not
    silently leave a DSGVO app unrouted).
    """
    if rule.provider != "anthropic":
        # Structural, not cosmetic: an app rule must never be able to hand out
        # Bedrock. Bedrock is real AWS money and is reachable ONLY through an
        # operator pin on a real user row (users.provider_config) — an app rule
        # is a property of the CALLER, and a caller cannot issue its own
        # permission. Re-adding provider="bedrock" to APP_PROVIDER_RULES must
        # therefore fail loud here instead of quietly reopening the hole that
        # funded the 2026-08-04..11 loop.
        raise AppProviderPolicyError(
            f"APP_PROVIDER_RULES entry has provider={rule.provider!r}; app-level "
            f"rules may only pin 'anthropic'. Bedrock is granted exclusively by "
            f"the per-user operator pin (users.provider_config) — see "
            f"src/routing/user_provider_override.assert_bedrock_is_pinned."
        )

    # Never silently re-route a call that explicitly asked for Bedrock: it
    # requested a specific data residency and would otherwise get a different
    # one behind its back. Leave the request untouched and let the Bedrock gate
    # decide — it refuses without an operator pin, loudly (403).
    #
    # WHOSE intent is being read here matters (Rafael, 2026-08-13). By the time
    # this runs, ``enforce_user_provider_override`` may ALREADY have written
    # ``backend=BEDROCK`` into request_body for a pinned user — the body then
    # looks identical to a client that asked for Bedrock itself, and this guard
    # would veto a pin-overriding app rule that is supposed to win (the Hub
    # case). Callers that override an existing pin therefore pass the CLIENT's
    # intent, snapshotted before the pin mutated the body. The default (None =
    # read the body) keeps the original behaviour for every caller that only
    # applies the rule to unpinned users, where body and client intent agree.
    _asked_for_bedrock = (
        _requests_bedrock_explicitly(request_body)
        if client_requested_bedrock is None
        else client_requested_bedrock
    )
    if _asked_for_bedrock:
        return None

    # Zweite Ausnahme, aus demselben Grund wie die erste und mit engerem
    # Umfang: der Gemini-Bild-Testweg (Rafael, 2026-09-03). Diese Regel raeumt
    # ``provider_tier`` ab, um eine Client-Wahl des Backends zu unterbinden —
    # gemeint war damit der Weg auf Bedrock bzw. auf einen fremden Gateway.
    # Auf den Gemini-Testweg angewandt taete sie etwas anderes und Schlimmeres:
    # sie liesse den Aufruf STILL von Anthropic beantworten, waehrend der
    # Aufrufer glaubt, er messe Gemini — also ein falsches Messergebnis PLUS
    # unerwarteter Verbrauch auf dem Anthropic-Prepaid-Schluessel. Genau die
    # Klasse von stillem Umrouten, gegen die dieses Modul geschrieben wurde.
    #
    # Sicher, weil der Testweg seine EIGENE, striktere Sperre mitbringt
    # (src/routing/gemini_vision_gate.py: Master-Flag + Key + nachgewiesenes
    # Nicht-Prod + ausdrueckliche Erklaerung). Auf einem Produktions-Worker
    # scheitert er gleich dreifach — die Ausnahme kann dort also nichts
    # oeffnen. Und sie beruehrt den Zweck dieser Regel nicht: Gemini ist nicht
    # Bedrock, es entsteht kein per-Token-AWS-Verbrauch.
    _asked_for_gemini_vision = (
        client_requests_gemini_vision(request_body)
        if client_requested_gemini_vision is None
        else client_requested_gemini_vision
    )
    if _asked_for_gemini_vision:
        logger.info(
            "app_provider_policy: provider_tier=%r bleibt bestehen — der "
            "Gemini-Bildtestweg hat eine eigene, striktere Sperre; ihn hier "
            "still abzuraeumen hiesse, den Aufruf heimlich von Anthropic "
            "beantworten zu lassen.", GEMINI_VISION_TEST_TIER,
        )
        return None

    request_body.backend = BackendType.ANTHROPIC
    request_body.provider_tier = None
    return "anthropic"


def client_requests_bedrock(request_body: Any) -> bool:
    """Public reading of "did the CLIENT ask for Bedrock?".

    Call this BEFORE ``enforce_user_provider_override`` runs: the pin rewrites
    ``request_body.backend``, after which the body no longer distinguishes the
    client's own request from the Bridge's pin. The snapshot is what
    ``apply_app_provider_policy(..., client_requested_bedrock=...)`` needs.
    """
    return _requests_bedrock_explicitly(request_body)


def _requests_bedrock_explicitly(request_body: Any) -> bool:
    """True if the client itself asked for Bedrock — directly via ``backend``
    or indirectly via a ``provider_tier`` that the registry maps to Bedrock
    (e.g. 'claude-dsgvo')."""
    if getattr(request_body, "backend", None) == BackendType.BEDROCK:
        return True
    tier_id = getattr(request_body, "provider_tier", None)
    if not tier_id:
        return False
    from src.providers.registry import PROVIDERS

    tier = PROVIDERS.get(tier_id)
    return tier is not None and tier.backend == BackendType.BEDROCK
