"""
Monatsreset des Monatstopfs.

Das Modul-Design nennt monthly_budgets "use-it-or-lose-it, Monatsreset",
billing_service dokumentiert "Reset window: 30 days" — umgesetzt war der Reset
nie. resetAt wurde geschrieben und nur fuer die Trial-Ablaufpruefung gelesen;
check_budget/deduct_budget rechneten ausschliesslich limit - used. Ein
"Monatsbudget" war damit ein Lebenszeit-Deckel: einmal leer, dauerhaft zu,
ohne Fehler und ohne Log. Diese Tests halten das Verhalten fest.
"""
from datetime import datetime, timedelta, timezone

from src.budget.calculator import (
    MonthlyBudgetEntry,
    rollover_monthly_if_due,
)

JETZT = datetime(2026, 8, 11, 12, 0, 0, tzinfo=timezone.utc)


def _entry(used: float, reset_at: datetime) -> MonthlyBudgetEntry:
    return MonthlyBudgetEntry(limit_eur=1000.0, used_eur=used, reset_at=reset_at.isoformat())


class TestMonatsreset:
    def test_faelliger_topf_wird_zurueckgesetzt(self):
        e = _entry(1000.0, JETZT - timedelta(days=1))
        neu, gerollt = rollover_monthly_if_due(e, now=JETZT)
        assert gerollt is True
        assert neu.used_eur == 0.0
        assert neu.limit_eur == 1000.0
        assert datetime.fromisoformat(neu.reset_at) > JETZT

    def test_nicht_faelliger_topf_bleibt_unberuehrt(self):
        anker = JETZT + timedelta(days=5)
        e = _entry(742.19, anker)
        neu, gerollt = rollover_monthly_if_due(e, now=JETZT)
        assert gerollt is False
        assert neu.used_eur == 742.19
        assert neu.reset_at == anker.isoformat()

    def test_erschoepfter_topf_ist_nach_reset_wieder_nutzbar(self):
        """Der Fall, der den Gratis-Trichter dauerhaft geschlossen haette."""
        e = _entry(1000.0, JETZT - timedelta(days=2))
        assert e.limit_eur - e.used_eur == 0.0
        neu, _ = rollover_monthly_if_due(e, now=JETZT)
        assert neu.limit_eur - neu.used_eur == 1000.0

    def test_mehrere_verpasste_perioden_ergeben_einen_topf(self):
        """Kein rueckwirkendes Ansammeln — ein Topf pro Periode."""
        e = _entry(1000.0, JETZT - timedelta(days=95))
        neu, gerollt = rollover_monthly_if_due(e, now=JETZT)
        assert gerollt is True
        assert neu.used_eur == 0.0
        assert neu.limit_eur == 1000.0          # nicht 4000
        assert datetime.fromisoformat(neu.reset_at) > JETZT

    def test_anker_bleibt_am_zyklus_statt_am_aufrufzeitpunkt(self):
        """Weiterschieben in Schritten, nicht now + 30d — sonst wandert der Zyklus."""
        anker = JETZT - timedelta(days=1)
        neu, _ = rollover_monthly_if_due(_entry(500.0, anker), now=JETZT, period_days=30)
        assert datetime.fromisoformat(neu.reset_at) == anker + timedelta(days=30)

    def test_grenzfall_genau_faellig(self):
        neu, gerollt = rollover_monthly_if_due(_entry(10.0, JETZT), now=JETZT)
        assert gerollt is True
        assert neu.used_eur == 0.0

    def test_naiver_zeitstempel_gilt_als_utc(self):
        """Altbestand traegt teils Zeitstempel ohne Zone — darf nicht werfen."""
        e = MonthlyBudgetEntry(limit_eur=1000.0, used_eur=1000.0,
                               reset_at="2026-08-01T00:00:00")
        neu, gerollt = rollover_monthly_if_due(e, now=JETZT)
        assert gerollt is True
        assert neu.used_eur == 0.0
