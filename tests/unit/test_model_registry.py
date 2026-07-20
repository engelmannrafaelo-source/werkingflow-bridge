"""Unit Tests für model_registry.py — Resolve-Semantik + Bedrock-Profil-Mapping.

Schützt zwei Invarianten, die 2026-07-05 beide live gebrochen waren:

1. Familien-Defaults sind BEWUSSTE Entscheidungen (is_default=True gewinnt).
   Ein neu in die Registry aufgenommenes, neueres Modell (opus-4-8,
   sonnet-4-6) darf den Default nicht still kippen — sonst werden explizite
   sonnet-4-5-Requests (Plot-Pipeline! matplotlib-Regression #46935) auf
   4.6 force-upgraded. Force-Upgrade gilt nur für ÄLTER-als-Default;
   neuer-als-Default ist ein bewusstes Opt-in und wird exakt bedient.

2. Bedrock-Profil-IDs sind NICHT uniform suffixiert (dated='-v1:0',
   opus-4-6='-v1', 4.7+/sonnet-4-6=ohne) — Raten produziert
   ResourceNotFound. Mapping ist eine explizite Tabelle, unmapped wirft.
"""

import os

os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

import pytest

from src.model_registry import (
    _DEFAULT_BY_FAMILY,
    _MODEL_BY_ID,
    from_bedrock_model_id,
    get_model_info,
    model_supports_temperature,
    resolve_model,
    to_bedrock_model_id,
)


# ============================================================================
# Familien-Defaults — bewusste Entscheidungen, nicht "neuestes Datum"
# ============================================================================

class TestFamilyDefaults:
    def test_deliberate_defaults_hold(self):
        assert _DEFAULT_BY_FAMILY["sonnet"].id == "claude-sonnet-4-5-20250929"
        assert _DEFAULT_BY_FAMILY["opus"].id == "claude-opus-4-7"
        assert _DEFAULT_BY_FAMILY["haiku"].id == "claude-haiku-4-5-20251001"

    def test_default_carries_flag(self):
        """Jeder Familien-Default muss aus einem is_default=True-Eintrag
        kommen — sonst hat ein neu registriertes Modell den Default gekippt."""
        for family, info in _DEFAULT_BY_FAMILY.items():
            assert info.is_default, (
                f"family '{family}' default '{info.id}' has no is_default flag — "
                f"a newly registered model silently took over the default"
            )


# ============================================================================
# resolve_model — exakt / Opt-in / Force-Upgrade / fuzzy
# ============================================================================

class TestResolveModel:
    @pytest.mark.parametrize(
        "requested,expected",
        [
            # Default exakt bedient
            ("claude-sonnet-4-5-20250929", "claude-sonnet-4-5-20250929"),
            ("claude-opus-4-7", "claude-opus-4-7"),
            # Neuer als Default = bewusstes Opt-in, exakt bedient
            ("claude-sonnet-4-6", "claude-sonnet-4-6"),
            ("claude-opus-4-8", "claude-opus-4-8"),
            # Aelter als Default = Force-Upgrade (always-latest fuer Altbestand)
            ("claude-sonnet-4-20250514", "claude-sonnet-4-5-20250929"),
            ("claude-opus-4-20250514", "claude-opus-4-7"),
            ("claude-3-5-haiku-20241022", "claude-haiku-4-5-20251001"),
            # Fuzzy-Familiennamen -> Default
            ("sonnet", "claude-sonnet-4-5-20250929"),
            ("opus", "claude-opus-4-7"),
            ("haiku", "claude-haiku-4-5-20251001"),
        ],
    )
    def test_resolution(self, requested, expected):
        got, _ = resolve_model(requested)
        assert got == expected

    def test_explicit_newer_is_not_downgraded(self):
        """Der Kern der Regression: ein explizites Opt-in in ein neueres
        Modell darf weder auf den Default 'aufgeräumt' noch umgeleitet werden."""
        for opt_in in ("claude-opus-4-8", "claude-sonnet-4-6"):
            got, warning = resolve_model(opt_in)
            assert got == opt_in
            assert warning is None

    def test_unknown_model_returns_error(self):
        got, msg = resolve_model("xyz-unknown-model")
        assert got is None
        assert "not supported" in msg


# ============================================================================
# Bedrock-Profil-Mapping — explizite Tabelle, kein Suffix-Raten
# ============================================================================

class TestBedrockMapping:
    @pytest.mark.parametrize(
        "model,profile",
        [
            ("claude-haiku-4-5-20251001", "eu.anthropic.claude-haiku-4-5-20251001-v1:0"),
            ("claude-sonnet-4-5-20250929", "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"),
            ("claude-sonnet-4-6", "eu.anthropic.claude-sonnet-4-6"),
            ("claude-opus-4-5-20251101", "eu.anthropic.claude-opus-4-5-20251101-v1:0"),
            ("claude-opus-4-6", "eu.anthropic.claude-opus-4-6-v1"),
            ("claude-opus-4-7", "eu.anthropic.claude-opus-4-7"),
            ("claude-opus-4-8", "eu.anthropic.claude-opus-4-8"),
        ],
    )
    def test_profile_ids_and_roundtrip(self, model, profile):
        assert to_bedrock_model_id(model, "eu-central-1") == profile
        assert from_bedrock_model_id(profile) == model

    def test_region_prefix(self):
        assert to_bedrock_model_id("claude-opus-4-8", "us-east-1") == "us.anthropic.claude-opus-4-8"
        assert to_bedrock_model_id("claude-opus-4-8", "ap-northeast-1") == "apac.anthropic.claude-opus-4-8"

    def test_unmapped_model_raises(self):
        """Fail loud statt geratener Profil-ID (ResourceNotFound bei AWS)."""
        with pytest.raises(ValueError, match="No Bedrock inference profile"):
            to_bedrock_model_id("claude-sonnet-4-20250514")

    def test_non_claude_raises(self):
        with pytest.raises(ValueError, match="non-Claude"):
            to_bedrock_model_id("gpt-5")


# ============================================================================
# temperature-Capability — generationsbasiert (4.5+ deprecaten sampling-controls)
# ============================================================================

class TestSupportsTemperature:
    """`temperature` ist eine GENERATIONS-Capability: Anthropic hat die legacy
    sampling-controls (temperature/top_p) ab der 4.5-Generation deprecated;
    Bedrock hard-rejected das Feld mit 400. Verifiziert 2026-07-20 gegen Bedrock:
    sonnet-5 / sonnet-4-5 / opus-4-8 lehnen `temperature` ab. Die Adapter
    (bedrock_service, vision_provider) strippen es am RESOLVED-Modell, damit
    keine App Provider-Quirks kennen muss."""

    @pytest.mark.parametrize("model_id,expected", [
        ("claude-sonnet-5", False),                 # v5
        ("claude-opus-4-8", False),                 # v4.8
        ("claude-sonnet-4-5-20250929", False),      # v4.5 (Grenze)
        ("claude-sonnet-4-20250514", True),         # v4   < 4.5
        ("claude-opus-4-1-20250805", True),         # v4.1 < 4.5
        ("claude-3-5-sonnet-20241022", True),       # v3.5
    ])
    def test_intrinsic_property_is_version_gated(self, model_id, expected):
        assert get_model_info(model_id).supports_temperature is expected

    def test_helper_follows_forced_upgrade(self):
        # Der Helper prüft am WIRKLICH gesendeten (resolved) Modell: ein älteres
        # Modell wird zum 4.5+-Default hochgestuft → temperature MUSS gestrippt
        # werden, obwohl das angefragte Modell es intrinsisch akzeptierte. Genau
        # der V2-Bug: harmonize sendet sonnet-4-5 → Bridge sendet sonnet-5.
        assert model_supports_temperature("claude-sonnet-4-5-20250929") is False
        assert model_supports_temperature("claude-sonnet-4-20250514") is False

    def test_unknown_model_fails_open(self):
        # Fail-open (nie still einen validen Param für ein unbekanntes Modell strippen).
        assert model_supports_temperature("totally-unknown-xyz") is True
