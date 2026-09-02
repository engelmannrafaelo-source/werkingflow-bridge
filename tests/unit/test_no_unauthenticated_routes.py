"""
Default-deny fuer Routen: eine NEUE Route ohne Auth-Deklaration macht diesen Test rot.

Warum es das gibt (02.09.2026): Autorisierung ist auf dem Bridge-Worker pro Route
Opt-in — es gibt keine erzwingende Middleware. Drei Routen waren dadurch oeffentlich,
zwei davon gaben die ersten 25 Zeichen JEDES Claude-OAuth-Tokens ins Internet
(/debug/tokens, /license-health) und die dritte spiegelte Requests (/v1/debug/request).
Gefunden wurden sie NICHT durch die Meldung und nicht durch Suche nach '/debug/*' —
die teuerste hiess /license-health. Gefunden wurden sie erst, als jemand ALLE Routen
ohne Auth-Dependency aufgelistet hat.

Genau diese Auflistung ist hier festgeschrieben. Wer eine Route hinzufuegt, muss sich
entscheiden: Auth deklarieren oder unten mit Begruendung eintragen. Beides ist in
Ordnung; stillschweigend oeffentlich ist es nicht.

GRENZE DIESES TESTS — bewusst benannt, damit niemand mehr hineinliest als drinsteht:
Er prueft, dass eine Route Auth DEKLARIERT, nicht dass sie sie DURCHSETZT. Routen mit
HTTPBearer (auto_error=False) bekommen `None`, wenn kein Header kommt, und muessen im
Handler selbst ablehnen. Das kann dieser Test nicht sehen. Er schliesst die Luecke
"Route komplett ohne Auth", nicht "Auth falsch implementiert".
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

import pytest  # noqa: E402
from fastapi.routing import APIRoute  # noqa: E402

from src.main import app  # noqa: E402


# Dependencies, die eine Route als "Auth deklariert" ausweisen.
# require_service_token setzt durch; HTTPBearer reicht die Anmeldedaten an den
# Handler weiter, der sie pruefen MUSS (siehe Grenze im Modul-Docstring).
AUTH_DEPENDENCIES = {"require_service_token", "HTTPBearer"}

# Bewusst oeffentliche Routen. Jede Zeile ist eine Entscheidung mit Grund, kein
# Sammelbecken. Aufnahme nur, wenn die Route ohne Anmeldung erreichbar SEIN SOLL
# und ihr Handler kein Schluesselmaterial und keine Kundendaten herausgibt
# (fuer diese elf am 02.09.2026 einzeln im Handler nachgesehen).
PUBLIC_ROUTES = {
    "/health": "Lebendigkeitspruefung des Lastverteilers — muss ohne Anmeldung gehen.",
    "/ready": "Bereitschaftspruefung des Lastverteilers — muss ohne Anmeldung gehen.",
    "/stats": "Aggregierte Zaehler ohne Kundenbezug; von Betriebs-Dashboards gelesen.",
    "/rate-limits": "Aktuelle Limits — Clients richten ihr Tempo danach.",
    "/worker-capacity": "Freie Kapazitaet fuer die Pool-Steuerung; nur Zahlen.",
    "/v1/providers": "Liste der verfuegbaren Anbieter — oeffentlicher Katalog.",
    "/v1/privacy/status": "Zustand der Anonymisierungsschicht; nur an/aus und Zaehler.",
    "/v1/auth/status": "Sagt, OB Auth konfiguriert ist — gibt keine Anmeldedaten preis.",
    "/v1/usage/status": "Aggregierter Verbrauchszustand ohne Nutzerbezug.",
    "/v1/compatibility": "Aushandlung der Protokollversion, vor jeder Anmeldung noetig.",
    "/v1/metrics/account-pool-state": "Pool-Zustand fuer Betriebsmetriken; nur Zaehler.",
}


def _dependency_names(route: APIRoute) -> set:
    """Alle Dependency-Namen einer Route, auch verschachtelte."""
    found = set()

    def walk(dep):
        if dep.call is not None:
            found.add(getattr(dep.call, "__name__", type(dep.call).__name__))
        for sub in dep.dependencies:
            walk(sub)

    for dep in route.dependant.dependencies:
        walk(dep)
    return found


def _api_routes():
    return [r for r in app.routes if isinstance(r, APIRoute)]


def test_keine_route_ohne_auth_oder_begruendung():
    """Jede Route deklariert Auth — oder steht mit Grund in PUBLIC_ROUTES."""
    ungeschuetzt = []
    for route in _api_routes():
        if _dependency_names(route) & AUTH_DEPENDENCIES:
            continue
        if route.path in PUBLIC_ROUTES:
            continue
        methoden = ",".join(sorted(route.methods - {"HEAD", "OPTIONS"}))
        ungeschuetzt.append(f"{methoden} {route.path}")

    assert not ungeschuetzt, (
        "Diese Routen sind ohne Anmeldung erreichbar und nicht als oeffentlich "
        "begruendet:\n  " + "\n  ".join(sorted(ungeschuetzt)) + "\n\n"
        "Zwei zulaessige Wege — such dir einen aus:\n"
        "  1. Auth deklarieren: `_claims: AuthClaims = Depends(require_service_token)`\n"
        "  2. Ist sie absichtlich oeffentlich: in PUBLIC_ROUTES eintragen, MIT Grund, "
        "und vorher im Handler nachsehen, dass sie kein Schluesselmaterial und keine "
        "Kundendaten herausgibt.\n"
        "Der zweite Weg ist kein Formalismus: genau diese Pruefung hat am 02.09.2026 "
        "gefehlt, als /license-health OAuth-Token-Vorschauen ins Internet gab."
    )


def test_public_routes_liste_verrottet_nicht():
    """Kein Eintrag in PUBLIC_ROUTES ohne Route — und keiner, der laengst Auth hat.

    Eine Allowlist, die niemand aufraeumt, deckt irgendwann eine Route ab, die
    gar nicht mehr gemeint war. Deshalb muss jeder Eintrag eine existierende,
    tatsaechlich dependency-freie Route bezeichnen.
    """
    pfade = {r.path for r in _api_routes()}
    verwaist = sorted(p for p in PUBLIC_ROUTES if p not in pfade)
    assert not verwaist, (
        "PUBLIC_ROUTES nennt Routen, die es nicht mehr gibt — bitte entfernen:\n  "
        + "\n  ".join(verwaist)
    )

    inzwischen_geschuetzt = sorted(
        r.path
        for r in _api_routes()
        if r.path in PUBLIC_ROUTES and (_dependency_names(r) & AUTH_DEPENDENCIES)
    )
    assert not inzwischen_geschuetzt, (
        "Diese Routen haben inzwischen Auth und gehoeren nicht mehr in die "
        "Ausnahmeliste — bitte dort streichen:\n  " + "\n  ".join(inzwischen_geschuetzt)
    )


@pytest.mark.parametrize("pfad", ["/debug/tokens", "/license-health", "/v1/debug/request"])
def test_die_drei_gemeldeten_routen_bleiben_geschuetzt(pfad):
    """Namentlicher Schutz fuer die drei Routen des Vorfalls vom 02.09.2026.

    Der Test oben wuerde einen Rueckfall auch fangen — aber nur, solange niemand
    sie 'kurz' in PUBLIC_ROUTES eintraegt. Diese drei duerfen dort nie stehen.
    """
    assert pfad not in PUBLIC_ROUTES, (
        f"{pfad} steht in PUBLIC_ROUTES. Diese Route gab OAuth-Token-Vorschauen bzw. "
        "Request-Spiegel ins Internet und ist nie oeffentlich."
    )
    treffer = [r for r in _api_routes() if r.path == pfad]
    assert treffer, f"Route {pfad} nicht gefunden — wurde sie umbenannt?"
    for route in treffer:
        assert "require_service_token" in _dependency_names(route), (
            f"{pfad} hat require_service_token verloren."
        )
