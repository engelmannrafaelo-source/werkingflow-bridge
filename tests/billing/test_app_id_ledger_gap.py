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
3. Echte Apps bleiben unverändert (keine Regression für den Normalfall).
"""
from __future__ import annotations

import json
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
async def test_load_returns_none_without_db():
    """Instanz ohne DB: Validierung legitim aus, kein Boot-Fehler."""
    with patch("src.db.client.is_db_enabled", return_value=False):
        assert await load_known_app_ids() is None


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
# persist_ai_call_activity — der echte Write-Pfad (DB gemockt)
# ---------------------------------------------------------------------------

USER_ID = str(uuid.uuid4())
TENANT_ID = str(uuid.uuid4())


def _mock_pool(fail_on: str | None = None):
    """fail_on: SQL-Fragment, bei dem execute() wirft (simuliert den ENUM-Reject)."""
    conn = AsyncMock()

    async def _execute(sql, *args):
        if fail_on and fail_on in sql:
            raise RuntimeError('invalid input value for enum app_id: "bridge-jobs"')
        return None

    conn.execute = AsyncMock(side_effect=_execute)
    conn.fetchrow = AsyncMock(
        return_value={"tenant_id": TENANT_ID, "billing_mode": "subscription"}
    )

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool, conn


def _insert_args(conn, table: str):
    for call in conn.execute.call_args_list:
        if f"INSERT INTO {table}" in call.args[0]:
            return call.args[1:]
    return None


async def _run(app_id: str | None, fail_on: str | None = None):
    pool, conn = _mock_pool(fail_on=fail_on)
    reset_registry_for_tests(KNOWN)
    try:
        with patch("src.activity.ai_call_writer.get_pool", return_value=pool), patch(
            "src.activity.ai_call_writer._deduct_call_cost", new=AsyncMock()
        ):
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
    return conn


@pytest.mark.asyncio
async def test_unknown_app_id_still_produces_a_ledger_row():
    """Kern-Regression: der Job bucht, obwohl sein Label keine App ist."""
    conn = await _run("bridge-jobs")

    args = _insert_args(conn, "usage_events")
    assert args is not None, "usage_events INSERT fehlt — genau der Bug von 2026-08-01"
    # Reihenfolge: actor_uuid, tenant_id, app, app_env, ...
    assert args[2] is None, "ungültiges app_id muss als NULL gebucht werden"


@pytest.mark.asyncio
async def test_rejected_label_survives_in_provider_metadata():
    """Der Rohwert darf nicht verschwinden — sonst ist der Aufrufer unauffindbar."""
    conn = await _run("bridge-jobs")

    args = _insert_args(conn, "usage_events")
    meta = json.loads(args[-1])
    assert meta.get("app_id_raw") == "bridge-jobs"


@pytest.mark.asyncio
async def test_audit_failure_does_not_cost_the_ledger_row():
    """Invariante 2: Audit und Abrechnung teilen kein Schicksal mehr."""
    conn = await _run("werking-report", fail_on="INSERT INTO activities")

    assert _insert_args(conn, "usage_events") is not None, (
        "ein gescheiterter Audit-INSERT darf die Abrechnungszeile nicht mitreißen"
    )


@pytest.mark.asyncio
async def test_real_app_is_unchanged():
    """Kein Kollateralschaden für den Normalfall."""
    conn = await _run("werking-report")

    args = _insert_args(conn, "usage_events")
    assert args[2] == "werking-report"
    assert "app_id_raw" not in json.loads(args[-1])
