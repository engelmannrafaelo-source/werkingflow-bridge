"""
BR13: /v1/sessions* und /v1/cli-sessions* verlangen den Bridge-Schluessel.

Befund 10.10.2026 (nur per Code): die neun Routen deklarierten HTTPBearer, riefen
aber nie verify_api_key — test_no_unauthenticated_routes.py sieht das nicht (er
prueft Deklaration, nicht Durchsetzung, siehe dessen Docstring). Ueber
bridge.werking.tools / prod-bridge.werking.tools (nginx `location /` und der
`/worker<NAME>/`-Durchgang) gab GET /v1/cli-sessions die ersten 200 Zeichen
jedes Prompts seit Containerstart heraus, und DELETE /v1/cli-sessions/{id}
brach fremde, bezahlte Laeufe ab.

Geprueft wird das Verhalten ueber HTTP, je Route und Methode:
  * ohne Schluessel und mit falschem Schluessel → 401, und nichts passiert
    (der Lauf laeuft weiter, die Sitzung bleibt);
  * mit dem gemeinsamen Schluessel → unveraendert wie vorher;
  * mit einem App-Principal (BRIDGE_PRINCIPALS_ENABLED) → 403, weil die Routen
    alle Aufrufer zeigen bzw. abbrechen; der gemeinsame Schluessel (Legacy-
    Principal) bleibt durchgelassen.
"""
import sys
from unittest.mock import MagicMock as _MagicMock

# Schwere Abhaengigkeiten stubben, bevor src.main importiert wird — gleiches
# Muster wie tests/unit/test_debug_tokens_endpoint.py.
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

from unittest.mock import AsyncMock, patch  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import src.main  # noqa: E402
from src.cli_session_manager import cli_session_manager  # noqa: E402
from src.principals import Principal  # noqa: E402
from src.session_manager import session_manager  # noqa: E402

BRIDGE_KEY = "test-bridge-key-br13"
CONV_ID = "br13-conv"
GEHEIM = "Kundenbericht Muster GmbH: Energieverbrauch 2025"

# Jede Route mit jeder Methode, so wie sie in src/main.py steht.
ROUTEN = [
    ("GET", "/v1/sessions/stats"),
    ("GET", "/v1/sessions"),
    ("GET", f"/v1/sessions/{CONV_ID}"),
    ("DELETE", f"/v1/sessions/{CONV_ID}"),
    ("GET", "/v1/cli-sessions"),
    ("GET", "/v1/cli-sessions/stats"),
    ("GET", "/v1/cli-sessions/{cli}"),
    ("DELETE", "/v1/cli-sessions/{cli}"),
    ("DELETE", "/v1/cli-sessions"),
]


@pytest.fixture()
def client(monkeypatch) -> TestClient:
    monkeypatch.setenv("BRIDGE_PRINCIPALS_ENABLED", "false")
    with patch("src.auth.auth_manager.get_api_key", return_value=BRIDGE_KEY):
        yield TestClient(src.main.app)


@pytest.fixture()
def laufende_sitzungen():
    """Eine Gespraechssitzung und ein laufender CLI-Lauf mit Kundeninhalt."""
    session_manager.get_or_create_session(CONV_ID)
    cli = cli_session_manager.create_session(prompt=GEHEIM, model="sonnet")
    yield cli.cli_session_id
    session_manager.delete_session(CONV_ID)
    with cli_session_manager.lock:
        cli_session_manager.sessions.pop(cli.cli_session_id, None)


def _pfad(pfad: str, cli_id: str) -> str:
    return pfad.replace("{cli}", cli_id)


@pytest.mark.parametrize("methode,pfad", ROUTEN)
def test_ohne_schluessel_401_und_nichts_passiert(
        client, laufende_sitzungen, methode, pfad):
    resp = client.request(methode, _pfad(pfad, laufende_sitzungen))
    assert resp.status_code == 401, resp.text
    assert GEHEIM not in resp.text
    assert cli_session_manager.get_session(laufende_sitzungen).status == "running"
    assert session_manager.get_session(CONV_ID) is not None


@pytest.mark.parametrize("methode,pfad", ROUTEN)
def test_falscher_schluessel_401(client, laufende_sitzungen, methode, pfad):
    resp = client.request(
        methode, _pfad(pfad, laufende_sitzungen),
        headers={"Authorization": "Bearer falsch"},
    )
    assert resp.status_code == 401, resp.text
    assert cli_session_manager.get_session(laufende_sitzungen).status == "running"
    assert session_manager.get_session(CONV_ID) is not None


@pytest.mark.parametrize("methode,pfad", ROUTEN)
def test_mit_schluessel_unveraendert(client, laufende_sitzungen, methode, pfad):
    resp = client.request(
        methode, _pfad(pfad, laufende_sitzungen),
        headers={"Authorization": f"Bearer {BRIDGE_KEY}"},
    )
    assert resp.status_code == 200, resp.text


def test_mit_schluessel_liefert_und_bricht_ab_wie_vorher(client, laufende_sitzungen):
    auth = {"Authorization": f"Bearer {BRIDGE_KEY}"}
    liste = client.get("/v1/cli-sessions", headers=auth).json()
    assert any(s["cli_session_id"] == laufende_sitzungen for s in liste["cli_sessions"])

    resp = client.delete(f"/v1/cli-sessions/{laufende_sitzungen}", headers=auth)
    assert resp.status_code == 200
    assert cli_session_manager.get_session(laufende_sitzungen).status == "cancelled"

    resp = client.delete(f"/v1/sessions/{CONV_ID}", headers=auth)
    assert resp.status_code == 200
    assert session_manager.get_session(CONV_ID) is None


def test_unbekannte_id_mit_schluessel_bleibt_404(client):
    auth = {"Authorization": f"Bearer {BRIDGE_KEY}"}
    assert client.get("/v1/cli-sessions/gibt-es-nicht", headers=auth).status_code == 404
    assert client.delete("/v1/sessions/gibt-es-nicht", headers=auth).status_code == 404


_APP_PRINCIPAL = Principal(
    id="p-1", name="werking-energy", allowed_apps=["werking-energy"])


@pytest.mark.parametrize("methode,pfad", ROUTEN)
def test_app_principal_403(client, laufende_sitzungen, monkeypatch, methode, pfad):
    monkeypatch.setenv("BRIDGE_PRINCIPALS_ENABLED", "true")
    with patch("src.principals.resolve_principal_by_token",
               new=AsyncMock(return_value=_APP_PRINCIPAL)):
        resp = client.request(
            methode, _pfad(pfad, laufende_sitzungen),
            headers={"Authorization": "Bearer app-token", "X-App-ID": "werking-energy"},
        )
    assert resp.status_code == 403, resp.text
    assert cli_session_manager.get_session(laufende_sitzungen).status == "running"


def test_gemeinsamer_schluessel_bei_principals_an_bleibt_durchgelassen(
        client, laufende_sitzungen, monkeypatch):
    monkeypatch.setenv("BRIDGE_PRINCIPALS_ENABLED", "true")
    resp = client.get(
        "/v1/cli-sessions", headers={"Authorization": f"Bearer {BRIDGE_KEY}"})
    assert resp.status_code == 200, resp.text
