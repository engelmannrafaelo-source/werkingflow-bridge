"""Das Ledger sagt "success" erst, wenn die Antwort auch ankam.

Befund 03.09.2026: Ein Gateway-Fehler NACH dem Modelllauf erzeugte keine
Fehlerzeile — der Worker rechnete den Call fertig, buchte status='success' und
lieferte die Antwort erst danach in eine Verbindung aus, die das Gateway
laengst abgeraeumt hatte. Einmal 2,16 USD fuer eine Antwort, die nie ankam.
Nachgestellt am 05.09. auf der dev-Bridge (Client bricht nach 1,5s ab, Modell
rechnet weiter, Zeile: status=success, 0,034230 EUR).

Was hier abgesichert wird:
  1. Aufrufer weg  -> Zeile traegt status='undelivered' + error_code, und der
     Betrag bleibt drin (das Geld ist ausgegeben, das Modell hat gerechnet).
  2. Aufrufer da   -> unveraendert 'success' (kein Fehlalarm).
  3. Kein Request-Kontext (Spool-Replay, Hintergrundjob) -> unveraendert
     'success'; ohne Aufrufer kann keiner weg sein.
  4. Eine echte Fehlerzeile bleibt eine Fehlerzeile mit Kosten 0 — 'undelivered'
     darf 'error' nicht verwaessern.
"""
from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock, patch

os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

import pytest

from src.activity import delivery
from src.activity.ai_call_writer import persist_ai_call_activity

USER_ID = str(uuid.uuid4())
TENANT_ID = str(uuid.uuid4())


class _FakeReceive:
    """ASGI receive that yields exactly what uvicorn yields for a caller that
    is gone (or, when alive, nothing at all)."""

    def __init__(self, disconnected: bool):
        self._disconnected = disconnected
        self.calls = 0

    async def __call__(self):
        self.calls += 1
        if self._disconnected:
            return {"type": "http.disconnect"}
        # A live connection has no message waiting; starlette/our probe run
        # this inside an already-cancelled scope, so an await that never
        # returns is exactly the "nothing there" case.
        import anyio

        await anyio.sleep_forever()


async def _book(seam, *, status="success", probe=None, model="claude-haiku-4-5-20251001"):
    seam.context = {"tenantId": TENANT_ID, "billingMode": "pay_per_token"}
    token = None
    if probe is not None:
        token = delivery._probe.set(probe)
    try:
        with patch("src.activity.ai_call_writer._deduct_call_cost", new=AsyncMock()):
            await persist_ai_call_activity(
                app_id="werking-report",
                user_id=USER_ID,
                agent_id="research:planning",
                workflow_id=None,
                model=model,
                input_tokens=1000,
                output_tokens=500,
                status=status,
                duration_ms=900,
                app_env="prod",
                provider="research-cloud",
            )
    finally:
        if token is not None:
            delivery._probe.reset(token)
    return seam.row


@pytest.mark.asyncio
async def test_caller_gone_is_not_booked_as_success(ledger_seam):
    probe = delivery.DeliveryProbe(_FakeReceive(disconnected=True))
    row = await _book(ledger_seam, probe=probe)

    assert row["provider_metadata"]["status"] == delivery.STATUS_UNDELIVERED, (
        "Gateway-Fehler nach dem Modelllauf wurde wieder als Erfolg gebucht"
    )
    assert row["provider_metadata"]["error_code"] == delivery.ERROR_CODE_CALLER_GONE
    assert row["provider_metadata"]["error_message"], (
        "eine Fehlerzeile ohne Grund ist der halbe Befund"
    )


@pytest.mark.asyncio
async def test_undelivered_row_keeps_the_money(ledger_seam):
    """Das Geld ist weg, auch wenn die Antwort nie ankam — die Zeile muss es
    zeigen. Auf 'error' gebucht (Kosten 0) waere der Betrag aus jeder
    Kostenauswertung verschwunden."""
    probe = delivery.DeliveryProbe(_FakeReceive(disconnected=True))
    row = await _book(ledger_seam, probe=probe)

    assert row["hypothetical_cost_eur"] > 0, "undelivered call lost its cost"
    assert row["real_cost_eur"] > 0, (
        "research-cloud zahlt pro Token an Anthropic — auch fuer eine Antwort, "
        "die niemand bekommen hat"
    )


@pytest.mark.asyncio
async def test_live_caller_still_books_success(ledger_seam):
    probe = delivery.DeliveryProbe(_FakeReceive(disconnected=False))
    row = await _book(ledger_seam, probe=probe)

    assert row["provider_metadata"]["status"] == "success"
    assert "error_code" not in row["provider_metadata"]


@pytest.mark.asyncio
async def test_without_request_context_nothing_is_downgraded(ledger_seam):
    """Spool-Replay und Hintergrundjobs laufen ausserhalb jedes Requests.
    Dort gibt es keinen Aufrufer, der weg sein koennte — 'undelivered' zu
    raten wuerde nicht existierende Ausfaelle erfinden."""
    assert delivery.get_delivery_probe() is None
    row = await _book(ledger_seam, probe=None)

    assert row["provider_metadata"]["status"] == "success"


@pytest.mark.asyncio
async def test_error_row_stays_an_error_row_without_cost(ledger_seam):
    """Gegenprobe: ein gescheiterter Modelllauf kostet weiterhin 0 und wird
    nicht durch die neue Auslieferungsfrage angefasst."""
    probe = delivery.DeliveryProbe(_FakeReceive(disconnected=True))
    row = await _book(ledger_seam, status="error", probe=probe)

    assert row["provider_metadata"]["status"] == "error"
    assert row["hypothetical_cost_eur"] == 0.0
    assert row["real_cost_eur"] == 0.0


@pytest.mark.asyncio
async def test_probe_latches_so_a_second_reader_gets_the_same_answer():
    """Der Streaming-Pfad hat bereits einen eigenen Disconnect-Waechter, der
    aus demselben Kanal liest. Die http.disconnect-Nachricht kommt nur einmal —
    ohne Latch wuerde die zweite Frage 'Aufrufer ist da' antworten."""
    receive = _FakeReceive(disconnected=True)
    probe = delivery.DeliveryProbe(receive)

    assert await probe.caller_gone() is True
    assert await probe.caller_gone() is True
    assert receive.calls == 1, "die zweite Frage hat erneut aus receive gelesen"


# ---------------------------------------------------------------------------
# DeliveryProbeMiddleware — installiert die Sonde und merkt sich einen
# Sendefehler NACH der Buchung (das Restfenster, das die Sonde nicht schliesst)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_middleware_installs_the_probe_for_the_handler():
    seen = {}

    async def app(scope, receive, send):
        seen["probe"] = delivery.get_delivery_probe()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    mw = delivery.DeliveryProbeMiddleware(app)
    await mw(
        {"type": "http", "method": "POST", "path": "/v1/chat/completions"},
        _FakeReceive(disconnected=False),
        AsyncMock(),
    )

    assert seen["probe"] is not None, "Handler lief ohne Auslieferungs-Sonde"


@pytest.mark.asyncio
async def test_middleware_names_the_booked_rows_when_the_send_fails(caplog):
    """Bricht die Auslieferung erst NACH der Buchung ab, kann die Zeile nicht
    mehr korrigiert werden — dann muss der Verlust wenigstens laut sein und die
    betroffenen Zeilen benennen, statt anonym zu verschwinden."""

    async def app(scope, receive, send):
        probe = delivery.get_delivery_probe()
        probe.note_booked("call-uid-4711")
        await send({"type": "http.response.start", "status": 200, "headers": []})

    async def broken_send(message):
        raise ConnectionResetError("broken pipe")

    mw = delivery.DeliveryProbeMiddleware(app)
    with caplog.at_level("ERROR"):
        with pytest.raises(ConnectionResetError):
            await mw(
                {"type": "http", "method": "POST", "path": "/v1/research"},
                _FakeReceive(disconnected=False),
                broken_send,
            )

    assert any(
        "call-uid-4711" in r.getMessage() and "could not be sent" in r.getMessage()
        for r in caplog.records
    ), "Sendefehler blieb stumm — genau das war der Ausgangsbefund"


@pytest.mark.asyncio
async def test_middleware_passes_non_http_scopes_through():
    called = {}

    async def app(scope, receive, send):
        called["scope"] = scope["type"]

    mw = delivery.DeliveryProbeMiddleware(app)
    await mw({"type": "lifespan"}, AsyncMock(), AsyncMock())
    assert called["scope"] == "lifespan"


@pytest.mark.asyncio
async def test_probe_is_silent_once_our_own_response_is_out():
    """uvicorn liefert nach abgeschlossener Antwort auf JEDES receive() ein
    http.disconnect. Wer das als "Aufrufer weg" liest, stempelt jede Arbeit
    falsch, die den Request UEBERLEBT — z.B. den async-Recherche-Lauf, der
    sofort 202 antwortet und seine Ledger-Zeile Minuten spaeter schreibt."""
    probe = delivery.DeliveryProbe(_FakeReceive(disconnected=True))
    probe.mark_response_completed()

    assert await probe.caller_gone() is False
