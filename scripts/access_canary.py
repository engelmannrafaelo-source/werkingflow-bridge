#!/usr/bin/env python3
"""Zugangs-Kanarienvogel: kommt nach einem Bridge-Deploy noch jemand in die Apps?

WARUM ES DAS GIBT
-----------------
`bridge_smoke.py` prueft rund dreissig /v1/*-Endpunkte der Bridge, aber
ausdruecklich NICHT das Anmelden (EXCLUDED["/login"]: "would need a throwaway
credential"). Nach einem Deploy war damit ungeprueft, ob ein zahlender Kunde
noch in seine App kommt — genau der Fall vom 13.08.2026, als mehrere Kunden mit
versendeter Zugangsmail nie hineinkamen, waehrend jede Pruefung gruen aussah.
Der Grund: die Pruefungen lagen eine Schicht zu frueh (Identitaet statt
Berechtigung). Ein Bridge-Login mit 200 ist bei jedem kaputten Zugang ebenfalls
gruen; erst die App-Seite hinter dem Abo-Tor beweist etwas.

WAS ER TUT
----------
Je App: echter Login ueber das App-Frontend, dann eine Seite, die nur mit
gueltiger Berechtigung 200 liefert. Zur Kontrolle wird dieselbe Seite OHNE
Cookie geholt — antwortet sie auch dann 200, ist die Seite gar nicht geschuetzt
und der Kanarienvogel waere ein Placebo. Das faellt hier auf, statt still zu
bestehen.

WAS ER NICHT TUT
----------------
Er blockiert nicht und rollt nicht zurueck (Entscheidung Rafael, 08.09.2026):
ein Kanarienvogel kann aus Gruenden rot werden, die mit dem Deploy nichts zu
tun haben (abgelaufenes Abo, gedriftetes Passwort). Er MELDET. Der Deploy wertet
die Marker-Zeile CANARY_FAIL: aus.

KONTEN
------
Eigene Konten auf @example.com (RFC 2606, nehmen nie Post an), angelegt am
08.09.2026 auf der PROD-Bridge. NIEMALS ein Kundenkonto benutzen: die Apps
lassen pro Konto nur EINE aktive Sitzung zu — ein Login hier wuerde den Kunden
aus seinem Fenster werfen (packages/auth/src/single-active-session.ts).
Passwort: CANARY_PASSWORD (Infisical dev-server/dev), Klartext-Spiegel im
prod-known-creds.json des Partner-Hosts.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from typing import Optional

import httpx

# app -> (Basis-URL, Login-Pfad, geschuetzte Seite, Konto)
# Die geschuetzte Seite ist je App verschieden und wurde am 08.09.2026 einzeln
# gemessen — noise hat kein /dashboard (404), tools erzwingt Schraegstriche
# (ohne den antwortet auch der Login-Endpunkt 308).
APPS = {
    "werking-report": ("https://report.werking.tools", "/api/auth/login", "/dashboard",
                       "kanarienvogel-report@example.com"),
    "werking-energy": ("https://energy.werking.tools", "/api/auth/login", "/dashboard",
                       "kanarienvogel-energy@example.com"),
    "werking-noise":  ("https://noise.werking.tools",  "/api/auth/login", "/projekte",
                       "kanarienvogel-noise@example.com"),
    "werking-tools":  ("https://werking.tools",        "/api/auth/login/", "/konto/",
                       "kanarienvogel-tools@example.com"),
}

TIMEOUT = 25.0


def canary_password() -> str:
    """Env zuerst, dann Infisical — dieselbe Reihenfolge wie bridge_smoke.py."""
    pw = os.environ.get("CANARY_PASSWORD")
    if pw:
        return pw
    ws = os.environ.get("INFISICAL_WS_DEV_SERVER")
    if not ws:
        raise SystemExit("CANARY_PASSWORD fehlt und INFISICAL_WS_DEV_SERVER ist nicht gesetzt")
    out = subprocess.run(
        ["bash", "-lc", f'source /root/.infisical/infisical-api.sh && '
                        f'infisical_get_secret "{ws}" dev CANARY_PASSWORD'],
        capture_output=True, text=True, timeout=60,
    )
    pw = (out.stdout or "").strip()
    if not pw:
        raise SystemExit("CANARY_PASSWORD weder im Env noch in Infisical dev-server/dev")
    return pw


def check(app: str, pw: str) -> tuple[bool, str]:
    base, login_path, guarded, email = APPS[app]
    with httpx.Client(timeout=TIMEOUT, follow_redirects=False) as c:
        try:
            r = c.post(f"{base}{login_path}", json={"email": email, "password": pw})
        except httpx.HTTPError as e:
            return False, f"Anmeldung nicht erreichbar: {e}"
        if r.status_code != 200:
            return False, f"Anmeldung fehlgeschlagen (HTTP {r.status_code})"

        try:
            g = c.get(f"{base}{guarded}")
        except httpx.HTTPError as e:
            return False, f"{guarded} nicht erreichbar: {e}"
        if g.status_code != 200:
            # 307 auf sortiment?reason=no-license bzw. /login?expired=1 heisst:
            # angemeldet, aber keine Berechtigung. Das ist der Fall vom 13.08.
            return False, f"Zugang ZU: {guarded} antwortet {g.status_code} (Anmeldung war ok)"

    # Gegenprobe ohne Cookie — sonst beweist die 200 oben nichts.
    with httpx.Client(timeout=TIMEOUT, follow_redirects=False) as anon:
        try:
            a = anon.get(f"{base}{guarded}")
        except httpx.HTTPError:
            a = None
    if a is not None and a.status_code == 200:
        return False, f"Sonde wertlos: {guarded} liefert auch OHNE Anmeldung 200"
    return True, f"Zugang steht ({guarded} 200, ohne Anmeldung {a.status_code if a else 'n/a'})"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apps", default=",".join(APPS), help="Kommaliste, Standard: alle")
    args = ap.parse_args()

    pw = canary_password()
    failed: list[str] = []
    for app in [a.strip() for a in args.apps.split(",") if a.strip()]:
        if app not in APPS:
            print(f"  {app}: unbekannt — uebersprungen")
            continue
        ok, msg = check(app, pw)
        print(f"  {app}: {'OK' if ok else 'FEHLER'} — {msg}")
        if not ok:
            failed.append(f"{app} ({msg})")

    if failed:
        # Marker-Zeile fuer bridge-deploy.sh: warnen, nicht zurueckrollen.
        print("CANARY_FAIL: " + "; ".join(failed))
    else:
        print("CANARY_OK: alle geprueften Apps lassen ihren Nutzer hinein")
    return 0


if __name__ == "__main__":
    sys.exit(main())
