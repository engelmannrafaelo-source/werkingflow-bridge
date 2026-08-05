"""usage_events.provider — Vokabular, Backend-Mapping, Fail-Loud-Verhalten.

Warum diese Tests existieren: die Spalte ist der Beleg fuer die Kundenzusage
"keine Uebermittlung an Anthropic USA". Ein stiller Default hat dort jahrelang
Anthropic-Uebermittlungen erfunden (39% der dev-Zeilen) und echte
Dritt-Verarbeiter versteckt. Die Regression, gegen die hier getestet wird, ist
also nicht "falsches Label im Dashboard", sondern "Auditdokument sagt das
Gegenteil der Realitaet".
"""
import logging

import pytest

from src.activity.providers import (
    EXTERNAL_PROVIDERS,
    LEDGER_PROVIDERS,
    PROVIDER_ANTHROPIC,
    PROVIDER_BEDROCK,
    PROVIDER_GEMINI,
    PROVIDER_LOCAL,
    PROVIDER_OPENAI_COMPATIBLE,
    PROVIDER_RESEARCH_CLOUD,
    PROVIDER_UNKNOWN,
    PROVIDER_UNROUTED,
    REAL_COST_PROVIDERS,
    ledger_provider_for_backend,
    normalize_ledger_provider,
)
from src.activity.ai_call_writer import resolve_ledger_cost
from src.models import BackendType


class TestBackendMapping:
    def test_jeder_backend_type_ist_klassifiziert(self):
        """Ein neuer BackendType darf nicht das Label eines anderen Providers
        erben — er muss explizit eingetragen werden."""
        for backend in BackendType:
            provider = ledger_provider_for_backend(backend)
            assert provider in LEDGER_PROVIDERS, f"{backend} -> {provider}"

    @pytest.mark.parametrize("backend,erwartet", [
        (BackendType.ANTHROPIC, PROVIDER_ANTHROPIC),
        (BackendType.ANTHROPIC_DIRECT, PROVIDER_ANTHROPIC),
        (BackendType.BEDROCK, PROVIDER_BEDROCK),
        (BackendType.OPENAI_COMPATIBLE, PROVIDER_OPENAI_COMPATIBLE),
        (BackendType.GEMINI_CLI, PROVIDER_GEMINI),
    ])
    def test_mapping(self, backend, erwartet):
        assert ledger_provider_for_backend(backend) == erwartet

    def test_gemini_und_openrouter_sind_nicht_anthropic(self):
        """Der konkrete Fehler vor dem Fix: Calls an Google/OpenRouter liefen
        ueber den Default und standen als 'anthropic' im Compliance-Ledger."""
        assert ledger_provider_for_backend(BackendType.GEMINI_CLI) != PROVIDER_ANTHROPIC
        assert ledger_provider_for_backend(BackendType.OPENAI_COMPATIBLE) != PROVIDER_ANTHROPIC

    def test_kein_backend_bedeutet_unrouted_nicht_anthropic(self):
        """Vor der Backend-Wahl abgelehnt (402-Gate) = nichts uebertragen."""
        assert ledger_provider_for_backend(None) == PROVIDER_UNROUTED

    def test_unbekannter_backend_faellt_laut_aus(self):
        class FremderBackend:
            pass

        with pytest.raises(ValueError, match="no ledger provider mapping"):
            ledger_provider_for_backend(FremderBackend())


class TestNormalisierung:
    def test_gueltige_werte_bleiben_unveraendert(self):
        for provider in LEDGER_PROVIDERS:
            assert normalize_ledger_provider(provider) == provider

    def test_unbekannter_wert_wird_unknown_niemals_anthropic(self, caplog):
        """Kern der Regression: ein nicht klassifizierbarer Wert darf NICHT
        auf einen plausibel aussehenden Provider zurueckfallen."""
        with caplog.at_level(logging.ERROR):
            ergebnis = normalize_ledger_provider("tippfehler-provider")
        assert ergebnis == PROVIDER_UNKNOWN
        assert ergebnis != PROVIDER_ANTHROPIC
        assert "tippfehler-provider" in caplog.text

    def test_normalisierung_bricht_den_call_nicht(self):
        """Tracking ist best-effort — ein kaputter Wert darf keinen Kundencall
        mit einer Exception abschiessen."""
        assert normalize_ledger_provider(None) == PROVIDER_UNKNOWN
        assert normalize_ledger_provider("") == PROVIDER_UNKNOWN


class TestMengen:
    def test_lokal_und_unrouted_sind_nicht_extern(self):
        """Die Menge, auf die eine Datenschutz-Auswertung filtern muss."""
        assert PROVIDER_LOCAL not in EXTERNAL_PROVIDERS
        assert PROVIDER_UNROUTED not in EXTERNAL_PROVIDERS
        assert PROVIDER_UNKNOWN not in EXTERNAL_PROVIDERS
        assert PROVIDER_ANTHROPIC in EXTERNAL_PROVIDERS
        assert PROVIDER_BEDROCK in EXTERNAL_PROVIDERS

    def test_vokabular_deckt_migration_053_ab(self):
        """Muss mit dem CHECK-Constraint in
        docker/migrations/053_usage_events_provider_truthful.sql
        uebereinstimmen — sonst scheitern Inserts erst in Produktion."""
        aus_migration = {
            "anthropic", "bedrock", "research-cloud", "openai", "aws-sagemaker",
            "openai-compatible", "gemini", "local", "unrouted", "unknown",
        }
        assert set(LEDGER_PROVIDERS) == aus_migration


class TestKostenSemantikUnveraendert:
    """resolve_ledger_cost wurde beim Umbau auf REAL_COST_PROVIDERS umgestellt.
    Das Verhalten muss dabei EXAKT gleich geblieben sein — ein Compliance-Fix
    darf die Abrechnung nicht nebenbei verschieben."""

    def test_bedrock_und_research_cloud_tragen_echte_kosten(self):
        assert REAL_COST_PROVIDERS == {PROVIDER_BEDROCK, PROVIDER_RESEARCH_CLOUD}
        for provider in (PROVIDER_BEDROCK, PROVIDER_RESEARCH_CLOUD):
            modus, kosten = resolve_ledger_cost("flat_rate", provider, 0.42)
            assert (modus, kosten) == ("flat_rate_estimated", 0.42)

    @pytest.mark.parametrize("provider", [PROVIDER_ANTHROPIC, PROVIDER_LOCAL, PROVIDER_UNROUTED])
    def test_uebrige_provider_bleiben_bei_null(self, provider):
        modus, kosten = resolve_ledger_cost("flat_rate", provider, 0.42)
        assert (modus, kosten) == ("flat_rate_estimated", 0.0)

    def test_pay_per_token_unberuehrt(self):
        assert resolve_ledger_cost("pay_per_token", PROVIDER_ANTHROPIC, 0.42) == ("pay_per_token", 0.42)


class TestWriterVertrag:
    def test_provider_ist_pflichtargument(self):
        """Der eigentliche Fail-Fast: ein neuer Call-Site KANN die Frage nicht
        mehr uebergehen. Frueher hat das Weglassen still 'anthropic' gebucht."""
        import inspect

        from src.activity.ai_call_writer import persist_ai_call_activity

        parameter = inspect.signature(persist_ai_call_activity).parameters["provider"]
        assert parameter.default is inspect.Parameter.empty, (
            "provider hat wieder einen Default — damit kann eine Call-Site "
            "unbemerkt eine Anthropic-Uebermittlung ins Compliance-Ledger schreiben."
        )
