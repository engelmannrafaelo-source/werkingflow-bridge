"""
Kosten je Auftrag (Befund 23.09.2026, werking-report).

Das Ergebnis eines Chat-Jobs trug nur Tokens — kein Aufrufer konnte sagen, was
ein Auftrag gekostet hat. Jetzt geht der Preis, den das Ledger fuer den Aufruf
bucht, als Kopfzeile X-Bridge-Cost-Eur aus der Antwort und landet im
Job-Ergebnis unter usage.cost_eur.

Bewusst NICHT aus `usage` nachgerechnet: prompt_tokens enthaelt dort den
Cache-Verkehr, den das Ledger zu eigenen Saetzen preist — eine zweite Rechnung
waere eine zweite, falsche Wahrheit.
"""
from __future__ import annotations

import os

os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.activity import delivery
from src.jobs.executors import attach_ledger_cost, chat_executor


def _asgi_app(book: list):
    """Minimal ASGI endpoint: books the given (call_uid, cost) pairs on the
    request's probe — as persist_ai_call_activity does — then answers JSON."""
    async def app(scope, receive, send):
        probe = delivery.get_delivery_probe()
        for uid, cost in book:
            probe.note_cost(uid, cost, "test-v1")
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": b'{"usage":{"prompt_tokens":1}}'})
    return delivery.DeliveryProbeMiddleware(app)


async def _get(app) -> httpx.Response:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.post("/v1/chat/completions", json={})


@pytest.mark.asyncio
async def test_antwort_traegt_den_gebuchten_preis_als_kopfzeile():
    resp = await _get(_asgi_app([("call-a", 0.006598), ("call-b", 0.0012)]))
    assert resp.headers[delivery.COST_HEADER] == "0.007798"
    assert resp.headers[delivery.COST_CALLS_HEADER] == "2"
    assert resp.headers[delivery.PRICING_VERSION_HEADER] == "test-v1"


@pytest.mark.asyncio
async def test_derselbe_aufruf_zweimal_gebucht_zaehlt_einmal():
    resp = await _get(_asgi_app([("call-a", 0.5), ("call-a", 0.5)]))
    assert resp.headers[delivery.COST_HEADER] == "0.500000"
    assert resp.headers[delivery.COST_CALLS_HEADER] == "1"


@pytest.mark.asyncio
async def test_ohne_buchung_keine_kopfzeile_statt_null_euro():
    resp = await _get(_asgi_app([]))
    assert delivery.COST_HEADER not in resp.headers


def test_job_ergebnis_uebernimmt_den_ledgerpreis():
    out = attach_ledger_cost(
        {"model": "m", "usage": {"prompt_tokens": 3797, "completion_tokens": 675}},
        httpx.Headers({"x-bridge-cost-eur": "0.006598", "x-bridge-cost-calls": "1",
                       "x-bridge-pricing-version": "v7"}),
    )
    assert out["usage"]["cost_eur"] == pytest.approx(0.006598)
    assert out["usage"]["cost_source"] == "ledger"
    assert out["usage"]["cost_calls"] == 1
    assert out["usage"]["pricing_version"] == "v7"
    assert out["usage"]["prompt_tokens"] == 3797  # Tokens bleiben unberuehrt


def test_fehlender_preis_ist_ausdruecklich_unbekannt_nicht_null():
    out = attach_ledger_cost({"usage": {"prompt_tokens": 1}}, httpx.Headers({}))
    assert out["usage"]["cost_eur"] is None
    assert out["usage"]["cost_source"] == "unbekannt"


def test_unlesbarer_preis_wirft_das_bezahlte_ergebnis_nicht_weg():
    out = attach_ledger_cost({"usage": {}}, httpx.Headers({"x-bridge-cost-eur": "abc"}))
    assert out["usage"]["cost_eur"] is None
    assert out["usage"]["cost_source"] == "unbekannt"


@pytest.mark.asyncio
async def test_chat_job_ergebnis_nennt_die_kosten():
    class _Resp:
        status_code = 200
        text = ""
        headers = httpx.Headers({"x-bridge-cost-eur": "0.012345", "x-bridge-cost-calls": "1"})

        def json(self):
            return {"id": "chatcmpl-x", "model": "m", "usage": {"prompt_tokens": 10, "completion_tokens": 2}}

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            return _Resp()

    with patch("httpx.AsyncClient", return_value=_Client()), \
         patch("src.auth.auth_manager.get_api_key", return_value="k"):
        out = await chat_executor({"messages": []}, {"app_id": "werking-report"}, AsyncMock())

    assert out["usage"]["cost_eur"] == pytest.approx(0.012345)
    assert out["usage"]["cost_source"] == "ledger"
