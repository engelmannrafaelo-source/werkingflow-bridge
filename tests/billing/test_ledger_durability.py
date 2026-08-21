"""
Die Zusage von ADR-0009 Schritt 1, end-to-end durch den echten Writer:

    Heute geht die Abrechnungszeile bei einem DB-Aussetzer verloren.
    Danach nicht mehr.

Das ist der eigenstaendige Gewinn dieses Schritts — er braucht keinen Umzug,
kein nginx und keine Aenderung an der laufenden Bridge, um zu zaehlen.
"""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

for _mod in ["src.db.client", "src.pricing"]:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()
import src.pricing as _pricing_stub  # noqa: E402
_pricing_stub.cost_eur = MagicMock(return_value=0.01)
_pricing_stub.PRICING_VERSION = "test-v1"

from src.activity import ledger_spool as spool  # noqa: E402
import src.activity.ai_call_writer as writer  # noqa: E402
from src.activity.providers import PROVIDER_ANTHROPIC  # noqa: E402

USER_ID = str(uuid.uuid4())
TENANT_ID = str(uuid.uuid4())


@pytest.fixture
def spool_dir(tmp_path, monkeypatch):
    d = tmp_path / "bridge-billing-spool"
    spool.release_own_file()
    monkeypatch.setattr(spool, "SPOOL_DIR", str(d))
    monkeypatch.setattr(spool, "WORKER_NAME", "worker-test")
    monkeypatch.setattr(spool, "_dir_ready", None)
    monkeypatch.setenv("BRIDGE_LEDGER_SPOOL_ENABLED", "true")
    writer._skip_counts.clear()
    yield d
    spool.release_own_file()


@pytest.fixture
def seam(ledger_seam):
    """Der Geldpfad spricht seit ADR-0009 Schritt 2c per HTTP mit platform-api
    statt selbst mit Postgres. Ein Ausfall ist damit kein DB-Fehler mehr,
    sondern eine unbeantwortbare Anfrage — fuer diese Tests derselbe Fall:
    keine definitive Antwort, also bleibt die Zeile geschuldet."""
    ledger_seam.context = {"tenantId": TENANT_ID, "billingMode": "subscription"}
    return ledger_seam


async def _persist(seam, **over):
    args = dict(
        provider=PROVIDER_ANTHROPIC,
        app_id="werking-report",
        user_id=USER_ID,
        agent_id=None,
        workflow_id=None,
        model="claude-sonnet-5",
        input_tokens=1000,
        output_tokens=500,
        status="success",
        duration_ms=800,
        app_env="prod",
    )
    args.update(over)
    with patch.object(writer, "_deduct_call_cost", new=AsyncMock()) as deduct:
        outcome = await writer.persist_ai_call_activity(**args)
    return outcome, deduct


# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_db_outage_no_longer_loses_the_billing_row(spool_dir, seam):
    """DER Test. Die Gegenseite faellt beim Tenant-Lookup aus — vorher war die
    Zeile damit weg (ERROR-Log, nicht abgerechnete Nutzung). Jetzt liegt sie
    auf Platte und wird nachgeholt, sobald wieder jemand antwortet."""
    seam.explode_on = "context"
    outcome, _ = await _persist(seam)
    assert outcome == spool.OUTCOME_FAILED
    assert spool.spool_stats()["pending"] == 1, "die Zeile muss geschuldet bleiben"

    # platform-api ist zurueck — der Nachlaeufer holt nach, ohne Eingriff.
    seam.explode_on = None
    with patch.object(writer, "_deduct_call_cost", new=AsyncMock()):
        stats = await spool.flush_once(writer.persist_ai_call_activity)

    assert stats["written"] == 1
    assert spool.spool_stats()["pending"] == 0
    assert len(seam.ledger_calls) == 1, "genau eine Abrechnungszeile, nicht zwei"


@pytest.mark.asyncio
async def test_successful_write_leaves_nothing_owed(spool_dir, seam):
    """Der Normalfall darf keinen Rueckstand erzeugen."""
    outcome, _ = await _persist(seam)
    assert outcome == spool.OUTCOME_WRITTEN
    assert spool.spool_stats()["pending"] == 0


@pytest.mark.asyncio
async def test_replay_writes_the_same_row_not_a_second_one(spool_dir, seam):
    """Idempotenz ist das, was Nachholen ueberhaupt erlaubt: derselbe Call
    traegt beim Nachlauf denselben Schluessel — und die Gegenseite antwortet
    darauf 'duplicate' statt eine zweite Zeile anzulegen.

    Dass auf 'duplicate' auch keine zweite AUDIT-Zeile entsteht, haengt jetzt
    an der Gegenseite (`activities` hat keinen Unique-Schluessel) und wird dort
    festgenagelt: tests/billing/test_ledger_db_rows.py."""
    await _persist(seam)
    schluessel = seam.row["idempotency_key"]

    seam.outcome = "duplicate"
    with patch.object(writer, "_deduct_call_cost", new=AsyncMock()):
        outcome = await writer.persist_ai_call_activity(
            provider=PROVIDER_ANTHROPIC, app_id="werking-report", user_id=USER_ID,
            agent_id=None, workflow_id=None, model="claude-sonnet-5",
            input_tokens=1000, output_tokens=500, status="success",
            duration_ms=800, app_env="prod",
            _call_uid=schluessel, _call_ts=1_700_000_000.0,
        )

    assert outcome == spool.OUTCOME_DUPLICATE
    assert seam.ledger_calls[1]["idempotency_key"] == schluessel


@pytest.mark.asyncio
async def test_replayed_row_is_recorded_in_the_period_it_belongs_to(spool_dir, seam):
    """recorded_at kommt aus der URSPRUNGSZEIT des Calls, nicht aus der Zeit
    des Nachlaufs. Sonst wandert ein Call vom Monatsletzten still in den
    naechsten Monat — und mit ihm ein Teil der Rechnung."""
    ursprung = 1_700_000_000.0  # 2023-11-14T22:13:20Z
    with patch.object(writer, "_deduct_call_cost", new=AsyncMock()):
        await writer.persist_ai_call_activity(
            provider=PROVIDER_ANTHROPIC, app_id="werking-report", user_id=USER_ID,
            agent_id=None, workflow_id=None, model="claude-sonnet-5",
            input_tokens=10, output_tokens=5, status="success",
            duration_ms=10, app_env="prod",
            _call_uid="uid-1", _call_ts=ursprung,
        )

    assert seam.row["recorded_at"] == datetime.fromtimestamp(
        ursprung, tz=timezone.utc
    ).isoformat()


@pytest.mark.asyncio
async def test_correct_skip_is_not_kept_owed(spool_dir, seam):
    """Ein Call ohne aufloesbaren Tenant SOLL keine Zeile haben. Er darf den
    Puffer nicht dauerhaft belegen — sonst verdeckt Rauschen den echten
    Rueckstand."""
    seam.context = None   # kein users+tenants-Treffer

    outcome, _ = await _persist(seam)
    assert outcome.startswith(spool.OUTCOME_SKIPPED)
    assert spool.spool_stats()["pending"] == 0


@pytest.mark.asyncio
async def test_unanswerable_lookup_is_not_mistaken_for_a_skip(spool_dir, seam):
    """Die Kehrseite des Tests darueber, und der Grund, warum
    load_billing_context bei einer unerwarteten Antwort wirft statt None zu
    liefern: 'kein Tenant' ist ein endgueltiger Skip, der die Zeile aus dem
    Puffer entlaesst. Eine nicht erreichbare (oder nicht ausgerollte)
    Gegenseite darf nie so aussehen — sonst wird eine echte Geldzeile als
    'korrekt nicht abgerechnet' abgelegt."""
    seam.explode_on = "context"

    outcome, _ = await _persist(seam)
    assert outcome == spool.OUTCOME_FAILED
    assert spool.spool_stats()["pending"] == 1


@pytest.mark.asyncio
async def test_cancellation_mid_write_no_longer_loses_the_row(spool_dir, seam):
    """asyncio.CancelledError ist eine BaseException und entkam dem
    `except Exception` — ein Client-Abbruch mitten im Schreiben nahm die Zeile
    lautlos mit. Der Abbruch propagiert weiterhin (Cancellation zu schlucken
    waere der naechste Fehler), aber die Zeile liegt da schon auf Platte."""
    import asyncio as _asyncio

    async def _cancelled(_payload):
        raise _asyncio.CancelledError()

    with (
        patch.object(writer.ledger_client, "write_ai_call", new=_cancelled),
        patch.object(writer, "_deduct_call_cost", new=AsyncMock()),
    ):
        with pytest.raises(_asyncio.CancelledError):
            await writer.persist_ai_call_activity(
                provider=PROVIDER_ANTHROPIC, app_id="werking-report",
                user_id=USER_ID, agent_id=None, workflow_id=None,
                model="claude-sonnet-5", input_tokens=1, output_tokens=1,
                status="success", duration_ms=1, app_env="prod",
            )

    assert spool.spool_stats()["pending"] == 1, (
        "der abgebrochene Call muss geschuldet bleiben statt spurlos zu verschwinden"
    )


# ---------------------------------------------------------------------------
# Der Abzug haengt an der Zeile (ADR-0009 Schritt 1, letzter Baustein)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_deduction_without_a_ledger_row(spool_dir, seam):
    """Vorher konnte der Abzug die Zeile ueberleben: er stand hinter dem
    grossen try und lief auch, wenn dieses in den except-Zweig gefallen war.
    Ein Abzug ohne Beleg ist durch nichts nachrechenbar."""
    seam.explode_on = "ledger"
    outcome, deduct = await _persist(seam)
    assert outcome == spool.OUTCOME_FAILED
    deduct.assert_not_called()
    # Verzoegert, nicht verloren: die Zeile ist geschuldet, der Abzug folgt ihr.
    assert spool.spool_stats()["pending"] == 1


@pytest.mark.asyncio
async def test_deduction_follows_the_row_when_it_lands(spool_dir, seam):
    """Und beim Nachlauf wird dann abgezogen — genau einmal."""
    seam.explode_on = "ledger"
    await _persist(seam)

    seam.explode_on = None
    with patch.object(writer, "_deduct_call_cost", new=AsyncMock()) as deduct:
        await spool.flush_once(writer.persist_ai_call_activity)

    assert deduct.await_count == 1


@pytest.mark.asyncio
async def test_replay_of_an_existing_row_never_deducts_twice(spool_dir, seam):
    """Die harte Randbedingung des ganzen Entwurfs: apply_budget_deduction ist
    ein read-modify-write ohne Dedup-Schluessel. Wuerde ein Nachlauf ihn
    wiederholen, waere jeder Wiederholungsversuch eine zweite Belastung.

    Ueber HTTP ist die Bindung dieselbe wie vorher ueber die DB: 'written' gibt
    es pro idempotency_key hoechstens einmal."""
    seam.outcome = "duplicate"
    with patch.object(writer, "_deduct_call_cost", new=AsyncMock()) as deduct:
        outcome = await writer.persist_ai_call_activity(
            provider=PROVIDER_ANTHROPIC, app_id="werking-report", user_id=USER_ID,
            agent_id=None, workflow_id=None, model="claude-sonnet-5",
            input_tokens=1000, output_tokens=500, status="success",
            duration_ms=800, app_env="prod",
            _call_uid="schon-gebucht", _call_ts=1_700_000_000.0,
        )

    assert outcome == spool.OUTCOME_DUPLICATE
    deduct.assert_not_called()


@pytest.mark.asyncio
async def test_deduction_carries_the_calls_origin_time(spool_dir, seam):
    """Damit ein Abzug, der in einem anderen Monat landet als der Call, sich
    ueberhaupt bemerken KANN."""
    with patch.object(writer, "_deduct_call_cost", new=AsyncMock()) as deduct:
        await writer.persist_ai_call_activity(
            provider=PROVIDER_ANTHROPIC, app_id="werking-report", user_id=USER_ID,
            agent_id=None, workflow_id=None, model="claude-sonnet-5",
            input_tokens=10, output_tokens=5, status="success",
            duration_ms=10, app_env="prod",
            _call_uid="uid-2", _call_ts=1_700_000_000.0,
        )

    assert deduct.await_args.args[5] == 1_700_000_000.0


@pytest.mark.asyncio
async def test_cross_month_deduction_is_shouted_about(spool_dir, caplog):
    """Ein nachgeholter Abzug zieht aus dem Topf, der JETZT aktuell ist — die
    Zeile ist aber im Zeitraum des Calls verbucht. Selten und begrenzt, aber
    nie still: aus den Zahlen allein laesst sich das hinterher nicht mehr
    rekonstruieren, und genau daran scheitert dann eine Rechnungsdiskussion."""
    import logging

    plan = MagicMock()
    plan.id = "report-standard"
    plan.interval = "month"

    with (
        patch("src.budget.plan_resolution.resolve_billing_plan",
              new=AsyncMock(return_value=plan)),
        patch("src.budget.routes.apply_budget_deduction_via_platform", new=AsyncMock()),
        caplog.at_level(logging.ERROR, logger="src.activity.ai_call_writer"),
    ):
        await writer._deduct_call_cost(
            USER_ID, "werking-report", 0.42, None, TENANT_ID,
            call_ts=1_700_000_000.0,   # 2023-11 — sicher nicht der laufende Monat
        )

    assert any("CROSSES A MONTH BOUNDARY" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_same_month_deduction_stays_quiet(spool_dir, caplog):
    """Der Normalfall darf kein Rauschen erzeugen — sonst nutzt der Alarm nichts."""
    import logging
    import time as _time

    plan = MagicMock()
    plan.id = "report-standard"
    plan.interval = "month"

    with (
        patch("src.budget.plan_resolution.resolve_billing_plan",
              new=AsyncMock(return_value=plan)),
        patch("src.budget.routes.apply_budget_deduction_via_platform", new=AsyncMock()),
        caplog.at_level(logging.ERROR, logger="src.activity.ai_call_writer"),
    ):
        await writer._deduct_call_cost(
            USER_ID, "werking-report", 0.42, None, TENANT_ID, call_ts=_time.time(),
        )

    assert not any("CROSSES A MONTH BOUNDARY" in r.message for r in caplog.records)
