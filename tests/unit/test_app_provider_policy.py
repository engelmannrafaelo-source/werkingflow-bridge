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
    AppProviderPolicyError,
    ProviderRule,
    apply_app_provider_policy,
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
        assert rule.provider == "bedrock"
        assert rule.region == "eu-central-1"

    def test_no_app_id_at_all_returns_none(self):
        req = _request()
        rule, app_id = resolve_app_provider_policy(req)
        assert rule is None
        assert app_id is None


class TestGlobalFailSafeDefault:
    """An app that is neither ruled to Anthropic nor Bridge-internal falls to
    Bedrock EU — never silently to Anthropic US (Rafael's explicit gap #1)."""

    def test_unknown_app_id_defaults_to_bedrock(self):
        req = _request(headers={"X-App-ID": "some-brand-new-app"})
        rule, app_id = resolve_app_provider_policy(req)
        assert app_id == "some-brand-new-app"
        assert rule is GLOBAL_DEFAULT_RULE
        assert rule.provider == "bedrock"


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

    def test_werking_report_is_bedrock_eu(self):
        assert APP_PROVIDER_RULES["werking-report"] == ProviderRule(
            provider="bedrock", region="eu-central-1"
        )

    def test_werking_energy_is_bedrock_eu(self):
        assert APP_PROVIDER_RULES["werking-energy"] == ProviderRule(
            provider="bedrock", region="eu-central-1"
        )

    def test_werking_noise_is_bedrock_eu(self):
        assert APP_PROVIDER_RULES["werking-noise"] == ProviderRule(
            provider="bedrock", region="eu-central-1"
        )

    def test_engelmann_is_anthropic_pool(self):
        assert APP_PROVIDER_RULES["engelmann"] == ProviderRule(provider="anthropic")


class TestApplyPolicy:
    def test_apply_bedrock_sets_backend_and_region(self):
        body = SimpleNamespace(backend=None, bedrock_region=None, provider_tier="claude-premium")
        applied = apply_app_provider_policy(
            body, ProviderRule(provider="bedrock", region="eu-central-1")
        )
        assert applied == "bedrock"
        assert body.backend == BackendType.BEDROCK
        assert body.bedrock_region == "eu-central-1"
        assert body.provider_tier is None

    def test_apply_anthropic_clears_provider_tier(self):
        """The app rule overrides any client-chosen tier — same as a user pin
        (apply_user_provider_override) already does."""
        body = SimpleNamespace(backend=None, bedrock_region=None, provider_tier="claude-dsgvo")
        applied = apply_app_provider_policy(body, ProviderRule(provider="anthropic"))
        assert applied == "anthropic"
        assert body.backend == BackendType.ANTHROPIC
        assert body.provider_tier is None

    def test_unsupported_provider_raises_loud(self):
        """A typo in APP_PROVIDER_RULES must not silently leave a DSGVO app
        unrouted — fails loud instead."""
        body = SimpleNamespace(backend=None, bedrock_region=None, provider_tier=None)
        with pytest.raises(AppProviderPolicyError, match="bogus"):
            apply_app_provider_policy(body, ProviderRule(provider="bogus"))
