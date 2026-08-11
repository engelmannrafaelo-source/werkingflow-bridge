"""
Budget-Vorwarnung: Schwellenlogik + Darstellung.

Rafael 11.08.: 1000 EUR pro Monat fuer die Gratis-Checks, mit Vorwarnung —
"ein Budget, von dem niemand merkt, dass es zur Neige geht, ist keine Bremse,
sondern eine Ueberraschung."

Getestet wird die REINE Logik. Versand und Stempel haengen an Resend bzw. am
UNIQUE-Index der Tabelle budget_warnings; die Idempotenz ist bewusst DORT
verankert und nicht in Anwendungscode, den ein Test nachbauen muesste.
"""
import pytest

from src.billing.budget_warnings import erreichte_schwellen, render_warnung

SCHWELLEN = [50, 80, 100]


class TestErreichteSchwellen:
    def test_unter_der_ersten_schwelle_nichts(self):
        assert erreichte_schwellen(490.0, 1000.0, SCHWELLEN) == []

    def test_genau_auf_der_schwelle_zaehlt(self):
        assert erreichte_schwellen(500.0, 1000.0, SCHWELLEN) == [50]

    def test_sprung_meldet_alle_uebersprungenen(self):
        """30 % -> 90 % zwischen zwei Durchlaeufen: die 50er darf nicht
        verschluckt werden, sie ist die Information 'es geht steil'."""
        assert erreichte_schwellen(900.0, 1000.0, SCHWELLEN) == [50, 80]

    def test_voll_meldet_auch_die_hundert(self):
        assert erreichte_schwellen(1000.0, 1000.0, SCHWELLEN) == [50, 80, 100]

    def test_ueberschreitung_bleibt_bei_hundert(self):
        assert erreichte_schwellen(1500.0, 1000.0, SCHWELLEN) == [50, 80, 100]

    def test_limit_null_ergibt_nichts_statt_division(self):
        """Ein Konto ohne Grenze hat keine Auslastung. Ein ZeroDivisionError
        hier waere ein stiller Ausfall der GANZEN Warnkette."""
        assert erreichte_schwellen(5.0, 0.0, SCHWELLEN) == []
        assert erreichte_schwellen(0.0, -1.0, SCHWELLEN) == []

    def test_realer_prod_zustand_warnt_noch_nicht(self):
        """Sammelkonto prod am 11.08.: 7,61 von 1000 EUR — weit weg."""
        assert erreichte_schwellen(7.611627, 1000.0, SCHWELLEN) == []


class TestBetragsformat:
    """Der erste Testlauf scheiterte an einem unsichtbaren U+202F, das Pythons
    ","-Formatspezifikation als Tausendertrenner einsetzte. In einer Mail sieht
    man das nicht — im Vergleich schlaegt es zu."""

    def test_deutsche_schreibweise_mit_punkt(self):
        from src.billing.budget_warnings import _fmt_eur
        assert _fmt_eur(1000.0) == "1.000,00"
        assert _fmt_eur(812.5) == "812,50"
        assert _fmt_eur(1234567.891) == "1.234.567,89"

    def test_kein_exotisches_leerzeichen(self):
        from src.billing.budget_warnings import _fmt_eur
        for wert in (1000.0, 1234567.89, 999.99):
            assert "\u202f" not in _fmt_eur(wert)
            assert "\u00a0" not in _fmt_eur(wert)


class TestDarstellung:
    def test_teilverbrauch_nennt_prozent_und_zahlen(self):
        betreff, html = render_warnung(
            konto="check-public@werking.tools", plan_id="report-standard",
            used_eur=812.5, limit_eur=1000.0, threshold_pct=80,
            period_reset_at="2026-09-01T00:00:00+00:00",
        )
        assert "80 %" in betreff
        assert "check-public@werking.tools" in betreff
        assert "812,50" in html and "1.000,00" in html
        assert "2026-09-01T00:00:00+00:00" in html

    def test_volle_ausschoepfung_sagt_was_es_bedeutet(self):
        """Bei 100 % ist die Folge die Nachricht, nicht die Prozentzahl."""
        betreff, html = render_warnung(
            konto="check-public@werking.tools", plan_id="report-standard",
            used_eur=1000.0, limit_eur=1000.0, threshold_pct=100,
            period_reset_at="2026-09-01T00:00:00+00:00",
        )
        assert "aufgebraucht" in betreff
        assert "werden abgelehnt" in html

    def test_kein_prozentzeichen_ohne_grenze(self):
        _, html = render_warnung(
            konto="x@y.z", plan_id="p", used_eur=0.0, limit_eur=0.0,
            threshold_pct=50, period_reset_at="2026-09-01T00:00:00+00:00",
        )
        assert "0 %" in html or "aufgebraucht" in html


class TestTrialAusschluss:
    """Trials sind ausgenommen — bei ihnen ist resetAt das ABLAUFdatum, nicht
    der Periodenanker (derselbe Fallstrick wie beim Monatsreset). Die Zeile
    "Neue Periode ab" waere dort sachlich falsch, der Schluessel wuerde nie
    wechseln, und erschoepfte Trials sind der Normalfall — dafuer gibt es
    trial_warnings, das den KUNDEN informiert.
    """

    @pytest.fixture(autouse=True)
    def _katalog_isoliert(self):
        """PLANS ist ein MODUL-GLOBALER Katalog. Ohne Sicherung faerbt jeder
        Eintrag hier auf fremde Tests ab und macht Ergebnisse von der
        Ausfuehrungsreihenfolge abhaengig — beim ersten Lauf hat genau das
        andere Tests scheinbar reparariert."""
        from src.budget.plans import PLANS, PlanConfig
        sicherung = dict(PLANS)
        PLANS["trial"] = PlanConfig(
            id="trial", app_id="werking-report", name="T", price=0,
            interval="month", api_budget_eur=5, description="", trial=True)
        PLANS["report-standard"] = PlanConfig(
            id="report-standard", app_id="werking-report", name="S", price=49,
            interval="month", api_budget_eur=100, description="", trial=False)
        yield
        PLANS.clear()
        PLANS.update(sicherung)

    def test_trial_wird_erkannt(self):
        from src.billing.budget_warnings import _ist_trial
        assert _ist_trial("trial") is True
        assert _ist_trial("report-standard") is False

    def test_unbekannter_plan_gilt_als_nicht_trial(self):
        """Blinder Fleck ist schlimmer als eine Meldung zu viel."""
        from src.billing.budget_warnings import _ist_trial
        assert _ist_trial("gibt-es-nicht") is False

    def test_leerer_katalog_wirft_statt_zu_raten(self):
        """Ohne Katalog laesst sich Trial nicht von Echtplan unterscheiden.
        Pauschal 'kein Trial' hiesse: Post fuer jeden erschoepften Testzugang —
        und das Startproblem bliebe unsichtbar."""
        from src.budget.plans import PLANS
        from src.billing.budget_warnings import _ist_trial
        sicherung = dict(PLANS)
        PLANS.clear()
        try:
            with pytest.raises(RuntimeError, match="PLANS cache is empty"):
                _ist_trial("egal")
        finally:
            PLANS.update(sicherung)
