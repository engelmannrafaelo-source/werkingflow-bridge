"""
GET /debug/tokens — Autorisierung UND kein Schluesselmaterial.

Live gemessen am 02.09.2026: die Route antwortete unauthentifiziert mit 200 —
ueber nginx aus dem Internet (https://bridge.werking.tools/debug/tokens) — und
lieferte `token_previews`, die ersten 25 Zeichen jedes Claude-OAuth-Tokens,
plus die Pfade der Token-Dateien.

Zwei getrennte Zusagen werden hier geprueft, weil es zwei getrennte Fehler
waren: ohne Service-Token gibt es keine Antwort, und AUCH die berechtigte
Antwort enthaelt kein 'sk-ant-'. Ein Test, der nur die Autorisierung prueft,
haette die Vorschau stillschweigend weiterleben lassen.
"""
import sys
from unittest.mock import MagicMock as _MagicMock

# Schwere Abhaengigkeiten stubben, bevor src.main importiert wird — gleiches
# Muster wie tests/research_cloud/test_anonymize_gate.py.
for _mod_name in [
    "claude_code_sdk",
    "claude_code_sdk._errors",
    "claude_code_sdk._internal",
    "claude_code_sdk._internal.client",
    "src.identity.routes",
    "src.db.client",
]:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = _MagicMock()

import os  # noqa: E402

os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

from unittest.mock import patch  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import src.main  # noqa: E402

SERVICE_TOKEN = os.environ["BRIDGE_SERVICE_TOKEN"]

# Ein Token in der Form, in der der TokenRotator sie haelt — mit dem echten
# Praefix, damit ein Rueckfall auf die Klartext-Vorschau hier auffliegt.
_FAKE_TOKENS = [
    "sk-ant-oat01-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
    "sk-ant-oat01-BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB",
]


@pytest.fixture()
def client() -> TestClient:
    return TestClient(src.main.app)


@pytest.fixture()
def rotator():
    rot = MagicMockRotator()
    with patch("src.auth.token_rotator", rot):
        yield rot


class MagicMockRotator:
    tokens = _FAKE_TOKENS
    current_index = 0
    token_files = ["/run/secrets/claude_token_account1.txt",
                   "/run/secrets/claude_token_account2.txt"]


def test_without_service_token_it_is_401(client, rotator):
    resp = client.get("/debug/tokens")
    assert resp.status_code == 401


def test_with_wrong_service_token_it_is_401(client, rotator):
    resp = client.get("/debug/tokens", headers={"X-Bridge-Service-Token": "falsch"})
    assert resp.status_code == 401


def test_authorized_answer_carries_no_key_material(client, rotator):
    resp = client.get("/debug/tokens", headers={"X-Bridge-Service-Token": SERVICE_TOKEN})

    assert resp.status_code == 200
    body = resp.json()
    assert "token_previews" not in body
    assert "sk-ant" not in resp.text
    # Der diagnostische Zweck bleibt erhalten: Anzahl, Rotationsindex,
    # Dateien und ein vergleichbarer Fingerabdruck je Token.
    assert body["total_tokens"] == 2
    assert len(body["token_fingerprints"]) == 2
    assert all(len(f) == 8 for f in body["token_fingerprints"])
    assert body["token_fingerprints"][0] != body["token_fingerprints"][1]
    assert body["status"] == "ok"


# ---------------------------------------------------------------------------
# GET /license-health — derselbe Leak, zweite Route
# ---------------------------------------------------------------------------
#
# Am 02.09.2026 mit derselben Messung gefunden: unauthentifiziert 200, Feld
# `token_preview` mit denselben 25 Zeichen. Zusaetzlich loest jeder Aufruf einen
# echten Modell-Call aus — unauthentifiziert also fremde Rechnung.
# Geprueft wird der rate_limited-Zweig: er kehrt vor dem Modell-Call zurueck,
# der Test kostet damit nichts und deckt trotzdem die Ausgabe ab.

def test_license_health_without_service_token_is_401(client, rotator):
    resp = client.get("/license-health")
    assert resp.status_code == 401


def test_license_health_answer_carries_no_key_material(client, rotator):
    with patch.object(src.main.rate_limit_tracker, "is_rate_limited", return_value=True), \
         patch.object(src.main.rate_limit_tracker, "get_retry_after", return_value=42), \
         patch.object(src.main.rate_limit_tracker, "get_all_rate_limits", return_value={}):
        resp = client.get(
            "/license-health", headers={"X-Bridge-Service-Token": SERVICE_TOKEN}
        )

    assert resp.status_code == 200
    body = resp.json()
    assert "token_preview" not in body
    assert "sk-ant" not in resp.text
    assert len(body["token_fingerprint"]) == 8


# ---------------------------------------------------------------------------
# POST /v1/debug/request — dritte offene Debug-Route (Befund Konsolidierung
# d376f2e7 / DevOps 57eb62f8, live 02.09.2026 ~09:00Z: HTTP 200 ohne Auth).
# ---------------------------------------------------------------------------

def test_debug_request_without_service_token_is_401(client, rotator):
    resp = client.post("/v1/debug/request", json={"model": "x"})
    assert resp.status_code == 401


def test_debug_request_with_service_token_still_works(client, rotator):
    resp = client.post(
        "/v1/debug/request",
        headers={"X-Bridge-Service-Token": SERVICE_TOKEN},
        json={"model": "x", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    assert "debug_info" in resp.json()
