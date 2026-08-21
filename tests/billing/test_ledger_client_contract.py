"""Der Vertrag der Geld-Naht (ADR-0009 Schritt 2c), src/activity/ledger_client.py.

Ein Satz traegt diese Datei: **eine Nicht-Antwort darf nie wie eine Antwort
aussehen.** Auf dem Geldpfad gibt es genau zwei endgueltige Ausgaenge —
"geschrieben" (erlaubt den Abzug) und "war schon da" (erlaubt ihn nicht). Alles
andere muss werfen, damit die Zeile im Puffer geschuldet bleibt.

Der teure Fall, gegen den das schuetzt, ist gemessen und nicht ausgedacht: eine
platform-api, die noch nicht ausgerollt ist, antwortet 404 (nachgemessen auf der
dev-Bridge, 2026-08-20). Wuerde load_billing_context daraus None machen, waere
das ununterscheidbar von "dieser Kunde hat keinen Tenant" — ein endgueltiger
Skip, der die Geldzeile aus dem Puffer ENTLAESST. Aus einem fehlenden Deploy
wuerde damit stillschweigend nicht abgerechnete Nutzung.
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

from src.activity import ledger_client
from src.activity.ledger_client import LedgerWriteRejected
from src.platform_client import PlatformResponse, PlatformUnavailable

ROW = {
    "idempotency_key": "call-uid-1",
    "recorded_at": "2023-11-14T22:13:20+00:00",
    "actor_user_id": "00000000-0000-4000-a000-000000000002",
    "tenant_id": "00000000-0000-4000-a000-000000000003",
    "model": "claude-sonnet-5",
    "provider": "anthropic",
    "billing_mode": "flat_rate_estimated",
    "real_cost_eur": 0.0,
    "hypothetical_cost_eur": 0.01,
    "pricing_version": "test-v1",
    "audit_event_type": "ai-call:call",
}


def _answers(status, body):
    return patch.object(
        ledger_client,
        "call_platform",
        new=AsyncMock(return_value=PlatformResponse(status_code=status, json=body)),
    )


# ── write_ai_call ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_written_is_reported_verbatim():
    with _answers(200, {"outcome": "written", "auditWritten": True}):
        assert await ledger_client.write_ai_call(ROW) == "written"


@pytest.mark.asyncio
async def test_duplicate_is_reported_verbatim():
    with _answers(200, {"outcome": "duplicate", "auditWritten": False}):
        assert await ledger_client.write_ai_call(ROW) == "duplicate"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,body",
    [
        (404, None),                       # platform-api nicht ausgerollt
        (400, {"detail": "bad request"}),  # Vertragsbruch
        (200, {}),                         # Antwort ohne outcome
        (200, {"outcome": "maybe"}),       # unbekannter Ausgang
    ],
)
async def test_anything_but_a_known_outcome_raises(status, body):
    """…und wird damit vom Aufrufer als "noch geschuldet" behandelt."""
    with _answers(status, body):
        with pytest.raises(LedgerWriteRejected):
            await ledger_client.write_ai_call(ROW)


@pytest.mark.asyncio
async def test_unreachable_platform_api_propagates():
    with patch.object(
        ledger_client, "call_platform",
        new=AsyncMock(side_effect=PlatformUnavailable("timeout")),
    ):
        with pytest.raises(PlatformUnavailable):
            await ledger_client.write_ai_call(ROW)


@pytest.mark.asyncio
async def test_the_write_never_retries():
    """Der Puffer IST der Wiederholmechanismus — und der bessere: asynchron,
    begrenzt, sichtbar. Ein zusaetzlicher Inline-Retry wuerde nur die Wartezeit
    des Aufrufers verlaengern."""
    with patch.object(
        ledger_client, "call_platform",
        new=AsyncMock(return_value=PlatformResponse(200, {"outcome": "written"})),
    ) as call:
        await ledger_client.write_ai_call(ROW)

    assert call.await_args.kwargs["retries"] == 0


# ── load_billing_context ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_missing_tenant_is_a_real_answer():
    """None heisst "kein Tenant" — der endgueltige Skip. Nur bei 200 + dem
    erwarteten Feld."""
    with _answers(200, {"context": None}):
        assert await ledger_client.load_billing_context("u-1") is None


@pytest.mark.asyncio
async def test_context_is_passed_through():
    ctx = {"tenantId": "t-1", "billingMode": "pay_per_token"}
    with _answers(200, {"context": ctx}):
        assert await ledger_client.load_billing_context("u-1") == ctx


@pytest.mark.asyncio
@pytest.mark.parametrize("status,body", [(404, None), (200, {}), (500, None)])
async def test_a_non_answer_is_never_read_as_no_tenant(status, body):
    """DER Test dieser Datei — siehe Modul-Docstring."""
    with _answers(status, body):
        with pytest.raises(LedgerWriteRejected):
            await ledger_client.load_billing_context("u-1")


# ── anonymous_identity_present ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_anonymous_absence_is_an_answer_but_is_not_cached():
    """Eine fehlende Migration-032-Identitaet darf nicht bis zum Neustart
    haengenbleiben: sonst wuerde das Ausfuehren der Migration erst nach einem
    Worker-Restart wirken, waehrend die Zeilen weiter geschuldet auflaufen."""
    ledger_client._anonymous_identity_verified = False
    with _answers(200, {"present": False}):
        assert await ledger_client.anonymous_identity_present() is False
    assert ledger_client._anonymous_identity_verified is False

    with _answers(200, {"present": True}):
        assert await ledger_client.anonymous_identity_present() is True
    assert ledger_client._anonymous_identity_verified is True
    ledger_client._anonymous_identity_verified = False


@pytest.mark.asyncio
async def test_unanswerable_anonymous_probe_raises():
    """…statt "nicht vorhanden" zu melden, was den Call verwerfen wuerde."""
    ledger_client._anonymous_identity_verified = False
    with _answers(503, None):
        with pytest.raises(LedgerWriteRejected):
            await ledger_client.anonymous_identity_present()
