"""Unit tests for the app-level provider policy
(src/routing/app_provider_policy.py).

Rafael's decision (2026-08-04): the provider decision (Anthropic pool vs.
Bedrock EU) belongs to the Bridge, keyed by APPLICATION — not by individual
user pins (src.routing.user_provider_override, unchanged/still-precedent).
Precedence: user pin > app rule > global fail-safe default (Bedrock EU).
"""

import os

os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.models import BackendType
from src.routing.app_provider_policy import (
    APP_PROVIDER_RULES,
    GLOBAL_DEFAULT_RULE,
    NON_CUSTOMER_APP_IDS,
    PIN_OVERRIDING_APP_IDS,
    AppProviderPolicyError,
    ProviderRule,
    app_rule_outranks_user_pin,
    apply_app_provider_policy,
    client_requests_bedrock,
    resolve_app_provider_policy,
)


def _request(headers=None, principal=None):
    """Minimal stand-in for a FastAPI Request: .headers (dict-like, lowercase
    lookups match request.headers.get(...) usage) and .state.principal."""
    req = MagicMock()
    req.headers = headers or {}
    req.state = SimpleNamespace(principal=principal)
    return req


def _principal(name, allowed_apps, is_legacy=False):
    return SimpleNamespace(name=name, allowed_apps=allowed_apps, is_legacy=is_legacy)


class TestResolveViaPrincipal:
    """The authenticated principal is the PREFERRED, non-spoofable source."""

    def test_single_scoped_principal_wins_over_header(self):
        """Even a conflicting X-App-ID header does not override the token's
        own scope — the principal is authenticated, the header is not."""
        req = _request(
            headers={"X-App-ID": "werking-energy"},
            principal=_principal("report-vercel-prod", ["werking-report"]),
        )
        rule, app_id = resolve_app_provider_policy(req)
        assert app_id == "werking-report"
        assert rule == APP_PROVIDER_RULES["werking-report"]

    def test_engelmann_principal_resolves_to_anthropic(self):
        req = _request(principal=_principal("engelmann-vercel-prod", ["engelmann"]))
        rule, app_id = resolve_app_provider_policy(req)
        assert app_id == "engelmann"
        assert rule.provider == "anthropic"

    def test_wildcard_principal_falls_back_to_header(self):
        """dev-tooling / legacy carry allowed_apps=['*'] — cannot itself prove
        which app is calling, so the header is consulted (same trust level as
        pre-principals)."""
        req = _request(
            headers={"X-App-ID": "werking-noise"},
            principal=_principal("dev-tooling", ["*"]),
        )
        rule, app_id = resolve_app_provider_policy(req)
        assert app_id == "werking-noise"
        assert rule == APP_PROVIDER_RULES["werking-noise"]

    def test_multi_app_principal_falls_back_to_header(self):
        """cui-* is scoped to two apps (cui, partner-platform) — ambiguous,
        so it cannot itself name a single app either."""
        req = _request(
            headers={"X-App-ID": "werking-report"},
            principal=_principal("cui-prod", ["cui", "partner-platform"]),
        )
        rule, app_id = resolve_app_provider_policy(req)
        assert app_id == "werking-report"

    def test_legacy_principal_falls_back_to_header(self):
        req = _request(
            headers={"X-App-ID": "werking-energy"},
            principal=_principal("legacy", ["*"], is_legacy=True),
        )
        rule, app_id = resolve_app_provider_policy(req)
        assert app_id == "werking-energy"


class TestResolveWithoutPrincipal:
    """BRIDGE_PRINCIPALS_ENABLED off, or no principal resolved — header only,
    same trust level the Bridge has always had for X-App-ID."""

    def test_header_only(self):
        req = _request(headers={"X-App-ID": "werking-report"})
        rule, app_id = resolve_app_provider_policy(req)
        assert app_id == "werking-report"
        assert rule.provider == "anthropic"

    def test_no_app_id_at_all_returns_none(self):
        req = _request()
        rule, app_id = resolve_app_provider_policy(req)
        assert rule is None
        assert app_id is None


class TestGlobalDefault:
    """Seit 2026-08-11: der Default sind die internen Anthropic-Accounts.
    Bedrock vergibt ausschliesslich der User-Pin, nie eine App-Regel."""

    def test_unknown_app_id_defaults_to_internal_anthropic(self):
        req = _request(headers={"X-App-ID": "some-brand-new-app"})
        rule, app_id = resolve_app_provider_policy(req)
        assert app_id == "some-brand-new-app"
        assert rule is GLOBAL_DEFAULT_RULE
        assert rule.provider == "anthropic"


class TestNonCustomerAppsExcluded:
    """Bridge-internal / ops channels are not subject to the customer
    data-residency promise — excluded from the fail-safe default."""

    @pytest.mark.parametrize("app_id", sorted(NON_CUSTOMER_APP_IDS))
    def test_internal_app_ids_get_no_policy(self, app_id):
        req = _request(headers={"X-App-ID": app_id})
        rule, resolved_app_id = resolve_app_provider_policy(req)
        assert rule is None
        assert resolved_app_id == app_id


class TestAllFourNamedApps:
    """Directly pin down Rafael's 2026-08-04 decision for the four apps."""

    def test_werking_report_is_anthropic_pool(self):
        assert APP_PROVIDER_RULES["werking-report"] == ProviderRule(provider="anthropic")

    def test_werking_energy_is_anthropic_pool(self):
        assert APP_PROVIDER_RULES["werking-energy"] == ProviderRule(provider="anthropic")

    def test_werking_noise_is_anthropic_pool(self):
        assert APP_PROVIDER_RULES["werking-noise"] == ProviderRule(provider="anthropic")

    def test_no_rule_may_grant_bedrock(self):
        """DIE Invariante (Rafael 2026-08-11): eine App-Regel darf Bedrock nicht
        vergeben — Bedrock kommt ausschliesslich vom User-Pin. Wer hier wieder
        provider='bedrock' eintraegt, oeffnet das Leck, das den Loop vom
        2026-08-04 bezahlt hat."""
        for app_id, rule in APP_PROVIDER_RULES.items():
            assert rule.provider == "anthropic", (
                f"APP_PROVIDER_RULES[{app_id!r}] vergibt {rule.provider!r} — "
                f"nur 'anthropic' ist erlaubt"
            )
        assert GLOBAL_DEFAULT_RULE.provider == "anthropic"

    def test_engelmann_is_anthropic_pool(self):
        assert APP_PROVIDER_RULES["engelmann"] == ProviderRule(provider="anthropic")


class TestApplyPolicy:
    def test_apply_bedrock_rule_fails_loud(self):
        """Eine Bedrock-App-Regel ist strukturell verboten — nicht still
        ignoriert, sondern lauter Fehler."""
        body = SimpleNamespace(backend=None, bedrock_region=None, provider_tier="claude-premium")
        with pytest.raises(AppProviderPolicyError, match="only pin 'anthropic'"):
            apply_app_provider_policy(
                body, ProviderRule(provider="bedrock", region="eu-central-1")
            )

    def test_apply_anthropic_clears_provider_tier(self):
        """The app rule overrides any client-chosen tier — same as a user pin
        (apply_user_provider_override) already does."""
        body = SimpleNamespace(backend=None, bedrock_region=None, provider_tier="claude-premium")
        applied = apply_app_provider_policy(body, ProviderRule(provider="anthropic"))
        assert applied == "anthropic"
        assert body.backend == BackendType.ANTHROPIC
        assert body.provider_tier is None

    def test_explicit_bedrock_backend_is_left_for_the_gate(self):
        """Kein silent-redirect: wer explizit Bedrock verlangt, wird nicht
        heimlich auf Anthropic umgebogen (andere Datenresidenz), sondern faellt
        in das Bedrock-Gate und bekommt dort ein lautes 403."""
        body = SimpleNamespace(
            backend=BackendType.BEDROCK, bedrock_region="eu-central-1", provider_tier=None
        )
        assert apply_app_provider_policy(body, ProviderRule(provider="anthropic")) is None
        assert body.backend == BackendType.BEDROCK

    def test_explicit_bedrock_tier_is_left_for_the_gate(self):
        """Gleiches ueber den provider_tier-Pfad ('claude-dsgvo' → Bedrock)."""
        body = SimpleNamespace(backend=None, bedrock_region=None, provider_tier="claude-dsgvo")
        assert apply_app_provider_policy(body, ProviderRule(provider="anthropic")) is None
        assert body.provider_tier == "claude-dsgvo"

    def test_unsupported_provider_raises_loud(self):
        """A typo in APP_PROVIDER_RULES must not silently leave a DSGVO app
        unrouted — fails loud instead."""
        body = SimpleNamespace(backend=None, bedrock_region=None, provider_tier=None)
        with pytest.raises(AppProviderPolicyError, match="bogus"):
            apply_app_provider_policy(body, ProviderRule(provider="bogus"))


class TestPinPrecedence:
    """Which apps may override an EXISTING per-user operator pin.

    The per-user pin is a customer's EU-residency commitment; only the Engelmann
    Hub — which shares those very accounts but is not itself a DSGVO-scoped
    customer app — is allowed to outrank it (Rafael, 2026-08-13).
    """

    def test_hub_may_override_a_user_pin(self):
        assert app_rule_outranks_user_pin("engelmann") is True

    @pytest.mark.parametrize("app_id", ["werking-report", "werking-energy", "werking-noise"])
    def test_customer_apps_never_override_a_user_pin(self, app_id):
        """The regression this guards: a blanket "app rule beats pin" would have
        moved every Bedrock-pinned customer onto Anthropic on their next
        Report/Energy/Noise call — silently breaking the AVV promise."""
        assert app_rule_outranks_user_pin(app_id) is False

    def test_unknown_and_unresolved_apps_never_override(self):
        """Fail-closed: if the Bridge cannot prove which app is calling, the
        user's pin stands."""
        assert app_rule_outranks_user_pin(None) is False
        assert app_rule_outranks_user_pin("some-new-app") is False

    def test_every_pin_overriding_app_can_only_pin_anthropic(self):
        """Structural guarantee: an override can only ever move traffic OFF
        Bedrock. If someone adds an app here whose rule is Bedrock, this fails."""
        for app_id in PIN_OVERRIDING_APP_IDS:
            assert APP_PROVIDER_RULES[app_id].provider == "anthropic"

    def test_no_customer_app_is_pin_overriding(self):
        """Guards the set itself, not just today's members."""
        customer_apps = set(APP_PROVIDER_RULES) - {"engelmann"} - NON_CUSTOMER_APP_IDS
        assert customer_apps and not (customer_apps & PIN_OVERRIDING_APP_IDS)


class TestClientIntentSnapshot:
    """A pinned user's body already says backend=BEDROCK by the time the app
    rule runs — the rule must read the CLIENT's intent, not the mutated body,
    or the Hub override silently does nothing (the bug found on 2026-08-13)."""

    def test_pin_written_bedrock_does_not_veto_the_override(self):
        """Body looks like "Bedrock requested" because the PIN wrote it. With
        the client's real intent passed in, the Hub rule applies."""
        body = SimpleNamespace(
            backend=BackendType.BEDROCK, bedrock_region="eu-central-1", provider_tier=None
        )
        applied = apply_app_provider_policy(
            body, ProviderRule(provider="anthropic"), client_requested_bedrock=False
        )
        assert applied == "anthropic"
        assert body.backend == BackendType.ANTHROPIC

    def test_client_asked_for_bedrock_still_vetoes(self):
        """The guard keeps its original purpose: a caller that really demanded
        Bedrock is never silently re-routed to a different data residency."""
        body = SimpleNamespace(
            backend=BackendType.BEDROCK, bedrock_region="eu-central-1", provider_tier=None
        )
        assert apply_app_provider_policy(
            body, ProviderRule(provider="anthropic"), client_requested_bedrock=True
        ) is None
        assert body.backend == BackendType.BEDROCK

    def test_default_still_reads_the_body(self):
        """Callers that only apply the rule to UNPINNED users pass nothing and
        keep the original behaviour."""
        body = SimpleNamespace(
            backend=BackendType.BEDROCK, bedrock_region="eu-central-1", provider_tier=None
        )
        assert apply_app_provider_policy(body, ProviderRule(provider="anthropic")) is None

    def test_snapshot_helper_reads_client_intent(self):
        assert client_requests_bedrock(
            SimpleNamespace(backend=BackendType.BEDROCK, provider_tier=None)
        ) is True
        assert client_requests_bedrock(
            SimpleNamespace(backend=None, provider_tier=None)
        ) is False
