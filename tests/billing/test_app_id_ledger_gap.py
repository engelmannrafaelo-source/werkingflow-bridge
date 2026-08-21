"""Regression tests für die app_id-ENUM-Lücke (2026-08-01).

Der Bug: ``/v1/jobs`` ohne X-App-ID etikettiert seinen Self-Call mit
``bridge-jobs/selfcall`` → app_id="bridge-jobs". Die Spalte
``activities.app_id`` ist ein Postgres-ENUM, das den Wert nicht kennt. Der
INSERT starb, riss im gemeinsamen try-Block den ``usage_events``-INSERT mit
und wurde als WARNING geschluckt. Ergebnis: JEDER un-attribuierte durable Job
buchte gar nichts — research-cloud-Spend (echtes Geld über den 1P-Key) war ab
2026-07-27 unsichtbar.

Die drei Invarianten, die das hier festnagelt:
1. Ein app_id, das keine echte App ist, wird VOR dem INSERT zu NULL normalisiert
   — der Rohwert geht nicht verloren, sondern nach provider_metadata.app_id_raw.
2. Ein fehlgeschlagener Audit-INSERT darf die Abrechnungszeile NICHT kosten.
   Seit ADR-0009 Schritt 2c liegen beide INSERTs auf der platform-api-Seite;
   diese Invariante wird deshalb dort festgenagelt — test_ledger_db_rows.py.
3. Echte Apps bleiben unverändert (keine Regression für den Normalfall).
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import os
os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

import pytest

from src.activity.ai_call_writer import persist_ai_call_activity
from src.activity.app_registry import (
    _REJECTED_TRACKING_CAP,
    _rejected_counts,
    load_known_app_ids,
    normalize_app_id,
    reset_registry_for_tests,
)

# Die realen ENUM-Member (Stand 2026-08-01) — in den Tests explizit übergeben,
# damit die Funktion pur bleibt und kein DB-Zustand mitspielt.
KNOWN = frozenset(
    {"werking-report", "werking-energy", "werking-safety", "werking-noise", "engelmann"}
)


# ---------------------------------------------------------------------------
# normalize_app_id — pure
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("app", sorted(KNOWN))
def test_real_apps_pass_through_untouched(app):
    assert normalize_app_id(app, known=KNOWN) == (app, None)


def test_bridge_jobs_label_is_rejected_not_inserted():
    """Der konkrete Bug-Auslöser."""
    assert normalize_app_id("bridge-jobs", known=KNOWN) == (None, "bridge-jobs")


@pytest.mark.parametrize("raw", ["rafael", "werking-report-coach", "phase10-standalone"])
def test_other_non_app_labels_are_rejected(raw):
    """Client-ID-Segmente und Usernamen sind keine Apps."""
    assert normalize_app_id(raw, known=KNOWN) == (None, raw)


@pytest.mark.parametrize("raw", [None, ""])
def test_absent_app_id_is_not_reported_as_rejected(raw):
    """Kein app_id ist der legitime Normalfall — nichts zu melden."""
    assert normalize_app_id(raw, known=KNOWN) == (None, None)


def test_unloaded_registry_passes_through():
    """Ohne geladenes ENUM (= Instanz ohne DB) durchreichen statt alles zu
    verwerfen. Kein Fallback für einen Worker MIT DB — dort bootet er gar nicht
    erst, siehe test_load_fails_fast_*."""
    reset_registry_for_tests(None)
    try:
        assert normalize_app_id("werking-report") == ("werking-report", None)
        assert normalize_app_id("bridge-jobs") == ("bridge-jobs", None)
    finally:
        reset_registry_for_tests(None)


def test_rejected_label_tracking_is_bounded():
    """Der Zähler wird mit Header-Werten von aussen gefüttert — er darf nicht
    unbegrenzt wachsen (langsames Speicherleck, von aussen steuerbar)."""
    reset_registry_for_tests(KNOWN)
    try:
        for i in range(_REJECTED_TRACKING_CAP + 500):
            app, rejected = normalize_app_id(f"junk-{i}", known=KNOWN)
            # Verhalten bleibt korrekt, auch jenseits des Caps:
            assert app is None and rejected == f"junk-{i}"
        assert len(_rejected_counts) <= _REJECTED_TRACKING_CAP
    finally:
        reset_registry_for_tests(None)


# ---------------------------------------------------------------------------
# load_known_app_ids — Boot-Invariante (fail fast)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_load_returns_none_without_db_and_without_platform_api():
    """Instanz ohne DB UND ohne Zugang zu platform-api: kein Schreibpfad für
    Ledger-Zeilen, also legitim keine Validierung und kein Boot-Fehler."""
    with (
        patch("src.db.client.is_db_enabled", return_value=False),
        patch.dict(os.environ, {"BRIDGE_SERVICE_TOKEN": ""}, clear=False),
    ):
        assert await load_known_app_ids() is None


@pytest.mark.asyncio
async def test_load_uses_platform_api_when_there_is_no_db():
    """Seit ADR-0009 Schritt 2c schreibt ein Worker OHNE eigene DB trotzdem
    Ledger-Zeilen — über platform-api. Die Prämisse "keine DB ⇒ kein INSERT,
    also nichts zu validieren" gilt damit nicht mehr: die Liste muss über die
    Innen-API kommen, sonst segelt ein Label wie "bridge-jobs" ungeprüft in
    eine ENUM-Spalte und die Zeile wird auf der Gegenseite abgelehnt."""
    from src.platform_client import PlatformResponse

    with (
        patch("src.db.client.is_db_enabled", return_value=False),
        patch(
            "src.platform_client.call_platform",
            new=AsyncMock(
                return_value=PlatformResponse(200, {"members": sorted(KNOWN)})
            ),
        ),
    ):
        try:
            assert await load_known_app_ids() == KNOWN
        finally:
            reset_registry_for_tests(None)


@pytest.mark.asyncio
async def test_load_fails_fast_when_platform_api_cannot_answer():
    """Fail fast statt still: ein Worker, der eine echte App nicht von einem
    Aufruf-Label unterscheiden kann, darf keinen Verkehr bedienen."""
    from src.platform_client import PlatformResponse

    with (
        patch("src.db.client.is_db_enabled", return_value=False),
        patch(
            "src.platform_client.call_platform",
            new=AsyncMock(return_value=PlatformResponse(404, None)),
        ),
    ):
        with pytest.raises(RuntimeError, match="APP REGISTRY VIOLATION"):
            await load_known_app_ids()


@pytest.mark.asyncio
async def test_load_fails_fast_when_pool_missing():
    """DIE Falle, die den ersten Wurf unbrauchbar machte: der Aufruf stand vor
    init_pool(), get_pool() warf, ein except schluckte es — und die Validierung
    war dauerhaft aus, während der Build 'erfolgreich' meldete. Jetzt bootet er
    nicht."""
    with patch("src.db.client.is_db_enabled", return_value=True), patch(
        "src.db.client.get_pool", side_effect=RuntimeError("DB pool not initialized")
    ):
        with pytest.raises(RuntimeError, match="pool not initialized"):
            await load_known_app_ids()


@pytest.mark.asyncio
async def test_load_fails_fast_on_empty_enum():
    """Ein ENUM ohne Member würde die ganze Flotte als app=NULL buchen."""
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire

    with patch("src.db.client.is_db_enabled", return_value=True), patch(
        "src.db.client.get_pool", return_value=pool
    ):
        with pytest.raises(RuntimeError, match="ZERO members"):
            await load_known_app_ids()


# ---------------------------------------------------------------------------
# persist_ai_call_activity — der echte Write-Pfad (Naht zu platform-api gemockt)
# ---------------------------------------------------------------------------

USER_ID = str(uuid.uuid4())
TENANT_ID = str(uuid.uuid4())


async def _run(ledger_seam, app_id: str | None):
    ledger_seam.context = {"tenantId": TENANT_ID, "billingMode": "subscription"}
    reset_registry_for_tests(KNOWN)
    try:
        with patch("src.activity.ai_call_writer._deduct_call_cost", new=AsyncMock()):
            await persist_ai_call_activity(
                app_id=app_id,
                user_id=USER_ID,
                agent_id="research-cloud:planning",
                workflow_id=None,
                model="claude-sonnet-5",
                input_tokens=1000,
                output_tokens=500,
                status="success",
                duration_ms=800,
                app_env="prod",
                provider="research-cloud",
            )
    finally:
        reset_registry_for_tests(None)
    return ledger_seam


@pytest.mark.asyncio
async def test_unknown_app_id_still_produces_a_ledger_row(ledger_seam):
    """Kern-Regression: der Job bucht, obwohl sein Label keine App ist."""
    seam = await _run(ledger_seam, "bridge-jobs")

    assert seam.ledger_calls, "keine Geldzeile — genau der Bug von 2026-08-01"
    assert seam.row["app"] is None, "ungültiges app_id muss als NULL gebucht werden"


@pytest.mark.asyncio
async def test_rejected_label_survives_in_provider_metadata(ledger_seam):
    """Der Rohwert darf nicht verschwinden — sonst ist der Aufrufer unauffindbar."""
    seam = await _run(ledger_seam, "bridge-jobs")

    assert seam.row["provider_metadata"].get("app_id_raw") == "bridge-jobs"


@pytest.mark.asyncio
async def test_real_app_is_unchanged(ledger_seam):
    """Kein Kollateralschaden für den Normalfall."""
    seam = await _run(ledger_seam, "werking-report")

    assert seam.row["app"] == "werking-report"
    assert "app_id_raw" not in seam.row["provider_metadata"]
