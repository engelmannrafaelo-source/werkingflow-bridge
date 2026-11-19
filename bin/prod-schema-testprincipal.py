#!/usr/bin/env python3
"""prod-schema-testprincipal.py — GENAU EIN privater Schema-Test-Principal mit Test-User und Pin auf der PROD-Bridge.

Auftrag: Kanzler an 51c8185c (18.11.2026), Plan fac5edd1a245, Plan-Review e8511fe6 GO c022ea60fd72 (Auflagen P-1..P-4).
Ausfuehrung NUR durch SEC im Fenster nach G1v10 (Release 8bcf742), Guard-/LB-Readback (A1/A2/A2b) und Katalog-Reload.

Feste Allowlist (kein Schalter, kein Codepfad fuer andere Objekte):
  Tenant  id/Name-Kern werking-schema-rafael-prod-test, account_type test, billing_type managed
  User    werking-schema-rafael-prod-test@test.werkingflow.com, Rolle user, OHNE Passwort (kein Login)
  Grant   werking-schema-generator / schema-generator (einziger Grant)
  Pin     provider_config {"provider": "anthropic_direct"}
  Princ.  werking-schema-rafael-prod-test, allowed_apps ["werking-schema-generator"] (→ Praefix wsg_),
          allowed_paths = ALLOWED_PATHS (P-1, nie leer, nie "*")
NICHT im Werkzeug (P-4): SCHEMA_NONEU_TEST_USER_IDS/_PRINCIPALS und Worker-Recreate (eigener Root/SEC-Schritt),
SCHEMA_ERZEUGEN_AKTIV (Prod-GO), Vercel-Abbildung (SEC), N-C (Modellaufruf, nur auf Wort).

Befehle:
  vorab     nur lesend: Health, Plan im 079-Zustand, Allowlist-Objekte frei, Infisical-Namen frei
  anlegen   --beleg <neu>: vorab, dann Tenant, User, Grant, Budget-Readback, Pin, Principal, Infisical-Ablage, Readbacks
  pruefen   --beleg: Identitaet, Grant, Pin, Principal (nur lesend)
  schloss   --beleg: N-B (Nicht-EU-Schloesser zu, ohne Spuren, P-3) und N-D (Zufallsschluessel 401); >= 65 s nach Pin
  abbauen   --beleg --nachweis <neu>: Principal aus (+ >= 25 s, alter Schluessel 401 an allen Workern, P-2),
            Pin loesen, Abo kuendigen, User loeschen (409 → anonymize), Tenant nur wenn leer

Geheimnisse: Operator-Token NUR ueber FD 3. Der Principal-Schluessel existiert nur im Prozess: einmal aus der
Anlage-Antwort, sofort per --schreiber (stdin) nach Infisical; spaeter nur per --leser (stdout) in den Prozess.
Ausgabe, Beleg, Nachweis: nur Name, Laenge, Fingerabdruck-8 (sha256[:8]), Status, Kurz-IDs.

Schreiber/Leser (SEC bindet den regulaeren Infisical-Weg an, NICHT get-token.py): JSON-argv mit den Platzhaltern
{ws} {env} {pfad} {name}. Schreiber: Wert auf stdin, rc 0 = geschrieben. Leser: rc 0 + Wert auf stdout = vorhanden,
rc 4 = nicht vorhanden, sonst Fehler. Spurenleser (P-3): JSON-argv mit {uid}, gibt {"ledger":n,"usage":n,
"reservierungen":n} fuer den Test-User aus (SQL in SPUREN_SQL).

Exit: 0 = alles PASS; 1 = Fehler/abgebrochen; 2 = Aufruf-/Vorbedingung (kein Write); 6 = nach Principal-Anlage
gescheitert, Principal deaktiviert; 7 = Principal NICHT nachweislich deaktiviert (SOFORT an SEC/Kanzler).
"""
import argparse
import datetime
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid

NAME = "werking-schema-rafael-prod-test"
TENANT_ID = NAME
TENANT_NAME = "WerkING Schema PRIVATER Rafael-Test (Prod-Bridge)"
EMAIL = NAME + "@test.werkingflow.com"
APP, PLAN = "werking-schema-generator", "schema-generator"
PIN = {"provider": "anthropic_direct"}
MODELL = "claude-sonnet-5-5"
# P-1 (Kanzler 19.11. nach Code-Analyse c041 4df72753cdbe): Homepage 552766d5 ruft direkt nur POST /v1/jobs,
# GET /v1/jobs/<id>, POST /v1/privacy/image-mask|image-receipt. Chat und smart-anonymize laufen nur im Job-Selbstaufruf
# (Worker-Schluessel), daher NICHT freigegeben. "/v1/jobs/*" = per _segment_match genau EIN Segment (Poll).
# Hinweis: wsg_-Schluessel duerfen am 8bcf742 nur /v1/jobs* (auth.py:560-565); Bildrouten erst mit Folge-Fix (c041).
ALLOWED_PATHS = ("/v1/jobs", "/v1/jobs/*", "/v1/privacy/image-mask", "/v1/privacy/image-receipt")
POLL_S, POLL_MAX = 2, 60
# Rafaels persoenliche Prod-IDs (BEFUND-RAFAEL-PROD-USERID): nie anfassen, nie gleich der Test-UUID.
RAFAEL_IDS = frozenset({"c51392fb-0956-4a00-a052-d395ef411e1a", "dbde0f89-bad5-4d3b-b3a5-b21427e8dd03",
                        "be44fb61-5607-4a35-8726-70297a8f07d4", "53c3d016-0c2e-4c84-b49a-092204de44ad"})
# Env-Namen der Homepage-Prod. FESTLEGUNG Kanzler 19.11.2026 00:01Z: Prod-Linie = Homepage-Paket 552766d5, Familie
# SCHEMA_BRIDGE_* (Belege 48cf002d kz31.json, STAGING-BINDUNG-SCHEMA-c9a50d3b). SCHEMA_CHECK_*/SCHEMA_ERZEUGEN_* und
# SCHEMA_ENABLED NICHT anfassen (die Check-Familie ist deshalb hier nicht zugelassen). Pflichtsatz: --namensfamilie mini
# plus genau die fuenf --name-<rolle>; das Werkzeug prueft Satz und Namen exakt, nichts verdrahtet ohne Parameter.
FAMILIEN = {
    "mini": {"schluessel": "SCHEMA_BRIDGE_API_KEY", "url": "SCHEMA_BRIDGE_URL", "user": "SCHEMA_BRIDGE_USER_ID",
             "tenant": "SCHEMA_BRIDGE_TENANT_ID", "app": "SCHEMA_BRIDGE_APP_ID"},
}
ROLLEN = ("schluessel", "url", "user", "tenant", "app")
PROD = "https://prod-bridge.werking.tools"
PROD_WORKER = ("http://100.93.143.105:8001", "http://100.93.143.105:8002",
               "http://100.93.143.105:8003", "http://100.93.143.105:8004")
LOOPBACK = re.compile(r"^http://127\.0\.0\.1:[0-9]{1,5}$")
PRINCIPAL_CACHE_S, PIN_CACHE_S = 25, 65   # P-2: Principal-Cache 20 s, Pin-Cache 60 s, je + 5 s
SEITE, SEITEN_MAX = 1000, 20
TIMEOUT = 30
ABLAGE_TIMEOUT = 60
NONEU_CODES = ("schema_noneu_test_user_not_allowed", "schema_noneu_test_principal_not_allowed")
# Fuer SECs Spurenleser (nur lesend, $1 = Test-UUID). Tabellen am Commit: usage_events (Bridge-Ledger, 016),
# ai_jobs (attribution JSONB), budget_reservations (080). "usage" = Jobs des Test-Users.
SPUREN_SQL = ("select (select count(*) from usage_events where user_id=$1::uuid) ledger, "
              "(select count(*) from ai_jobs where attribution->>'user_id'=$1) usage, "
              "(select count(*) from budget_reservations where user_id=$1::uuid) reservierungen")


def pin_von(j):
    """provider_config wie geliefert: GET /v1/users/{id} gibt am 8bcf742 einen JSON-STRING (admin_routes.py:470 json.dumps,
    Lesen ohne Dekodierung; live 19.11. 24/24 str). Nicht-JSON bleibt als Text und vergleicht damit ungleich."""
    pc = (j or {}).get("provider_config") if isinstance(j, dict) else None
    if isinstance(pc, str):
        try:
            return json.loads(pc)
        except ValueError:
            return pc
    return pc


def zeile(t):
    print(t, flush=True)


class Abbruch(Exception):
    pass


class Vorbedingung(Exception):
    pass


def fp8(wert):
    return hashlib.sha256(wert.encode()).hexdigest()[:8]


def jetzt():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def schreibe_neu(pfad, daten):
    fd = os.open(pfad, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(daten, f, indent=1, sort_keys=True)


def lies_fd3():
    try:
        with os.fdopen(3, "r", closefd=True) as f:
            tok = f.read().strip()
    except OSError:
        raise SystemExit("ABBRUCH Operator-Token fehlt auf FD 3 — kein Request gesendet")
    if len(tok) < 16 or any(c.isspace() for c in tok):
        raise SystemExit("ABBRUCH Operator-Token auf FD 3 ungueltig — kein Request gesendet")
    return tok


def ziele(bridge, worker):
    """Nur Prod (mit genau den vier Prod-Workern) oder vollstaendig Loopback (Proben)."""
    if bridge == PROD:
        if tuple(worker) != PROD_WORKER:
            raise SystemExit("ABBRUCH --worker muss bei Prod genau die vier Prod-Worker nennen")
        return False
    if LOOPBACK.match(bridge) and worker and all(LOOPBACK.match(w) for w in worker):
        return True
    raise SystemExit("ABBRUCH --bridge/--worker nicht Prod-Bridge bzw. Loopback — kein Request gesendet")


def wartezeiten(lokal, cache_s, pin_s):
    """P-2: unter den Cache-Fristen nur gegen Loopback (Proben)."""
    if not lokal and (cache_s < PRINCIPAL_CACHE_S or pin_s < PIN_CACHE_S):
        raise SystemExit("ABBRUCH Wartezeiten unter den Cache-Fristen sind nur gegen Loopback erlaubt")


class Bridge:
    def __init__(self, url, op):
        self.url, self.op, self.schreibe = url, op, 0

    def req(self, methode, pfad, body=None, basis=None, kopf=None, operator=True):
        if methode != "GET":
            self.schreibe += 1
        daten = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request((basis or self.url) + pfad, data=daten, method=methode)
        if operator:
            r.add_header("X-Bridge-Service-Token", self.op)
        for k, v in (kopf or {}).items():
            r.add_header(k, v)
        if daten is not None:
            r.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(r, timeout=TIMEOUT) as a:
                roh, st = a.read(), a.status
        except urllib.error.HTTPError as e:
            roh, st = e.read(), e.code
        except (urllib.error.URLError, OSError) as e:
            raise Abbruch(f"{methode} {pfad}: Verbindung ({type(e).__name__})")
        try:
            j = json.loads(roh) if roh else None
        except ValueError:
            j = None
        return st, j, roh.decode(errors="replace")

    # Existenzbeweise wie probe-testmandanten R3 (exakter Lookup, belegte Pagination).
    def mandant_existiert(self, tid):
        st, j, _ = self.req("GET", f"/v1/tenants/{tid}/billing-address")
        if st == 200 and isinstance(j, dict):
            return True
        if st == 404 and isinstance(j, dict) and j.get("detail") == f"Tenant '{tid}' not found":
            return False
        raise Abbruch(f"GET /v1/tenants/{tid}/billing-address: {st} — Existenz unbewiesen")

    def typ_test(self, tid):
        st, j, _ = self.req("GET", f"/v1/tenants?account_type=test&limit={SEITE}")
        if st != 200 or not isinstance(j, list):
            raise Abbruch(f"GET /v1/tenants?account_type=test: {st}")
        if any(t.get("id") == tid and t.get("account_type") == "test" for t in j):
            return True
        if len(j) >= SEITE:
            raise Abbruch("GET /v1/tenants?account_type=test: Obergrenze erreicht — Typ unbewiesen")
        return False

    def durchgang(self):
        aus = []
        for s in range(SEITEN_MAX):
            st, j, _ = self.req("GET", f"/v1/users?limit={SEITE}&offset={s * SEITE}")
            if st != 200 or not isinstance(j, list) or len(j) > SEITE:
                raise Abbruch(f"GET /v1/users: {st} — Bestand unbewiesen")
            aus.extend(j)
            if len(j) < SEITE:
                ids = [str(u.get("id")) for u in aus]
                if len(set(ids)) != len(ids):
                    raise Abbruch("GET /v1/users: Seiten ueberlappen (doppelte id) — Bestand unbewiesen")
                return aus
        raise Abbruch(f"GET /v1/users: mehr als {SEITE * SEITEN_MAX} Eintraege — Bestand unbewiesen")

    def nutzer(self):
        eins, zwei = self.durchgang(), self.durchgang()
        if sorted(str(u.get("id")) for u in eins) != sorted(str(u.get("id")) for u in zwei):
            raise Abbruch("GET /v1/users: Bestand aenderte sich zwischen zwei Durchgaengen — Bestand unbewiesen")
        return eins

    def principals(self):
        st, j, _ = self.req("GET", "/v1/admin/service-principals")
        if st != 200 or not isinstance(j, dict) or not isinstance(j.get("principals"), list):
            raise Abbruch(f"GET /v1/admin/service-principals: {st}")
        return [p for p in j["principals"] if p.get("name") == NAME]

    def deaktivieren(self, grund):
        """set-active false mit Readback; ohne Beleg rc 7."""
        try:  # A-W-3 (R2): auch ein Verbindungsfehler hier endet in rc 7 mit SOFORT-Zeile
            st, j, _ = self.req("POST", "/v1/admin/service-principals/set-active", {"name": NAME, "active": False})
        except Abbruch:
            st = None
        try:
            ist = [p.get("active") for p in self.principals()]
        except Abbruch:
            ist = None
        if not (st == 200 and ist == [False]):
            zeile(f"FEHLER Principal {NAME} NICHT nachweislich deaktiviert ({grund}; set-active {st}, Readback {ist}) "
                  "— SOFORT an SEC/Kanzler")
            raise SystemExit(7)
        zeile(f"INFO Principal deaktiviert (Readback active=false), Grund: {grund}; wirksam in den Workern nach <= 20 s")


class Ablage:
    """Infisical-Ablage ueber SECs regulaeren Weg (argv-Vertrag, Wert nur ueber Pipes)."""

    def __init__(self, a):
        fam = FAMILIEN.get(a.namensfamilie)
        if fam is None:
            raise SystemExit("ABBRUCH --namensfamilie muss mini sein (Festlegung Kanzler 00:01Z; SCHEMA_CHECK_* nicht anfassen)")
        gegeben = {r: getattr(a, f"name_{r}") for r in ROLLEN if getattr(a, f"name_{r}") is not None}
        if set(gegeben) != set(fam):
            raise SystemExit(f"ABBRUCH Familie {a.namensfamilie} verlangt genau --name-{', --name-'.join(fam)} "
                             f"(gegeben: {sorted(gegeben)})")
        for r, soll in fam.items():
            if gegeben[r] != soll:
                raise SystemExit(f"ABBRUCH --name-{r} muss in Familie {a.namensfamilie} {soll} sein")
        self.familie, self.namen = a.namensfamilie, dict(fam)
        if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", a.infisical_ws or ""):
            raise SystemExit("ABBRUCH --infisical-ws fehlt oder ist keine Workspace-UUID")
        if a.infisical_env != "prod":
            raise SystemExit("ABBRUCH --infisical-env muss prod sein")
        if not re.fullmatch(r"/[A-Za-z0-9/_-]*", a.infisical_pfad or ""):
            raise SystemExit("ABBRUCH --infisical-pfad fehlt oder ist ungueltig")
        self.p = {"ws": a.infisical_ws, "env": a.infisical_env, "pfad": a.infisical_pfad}
        self.schreiber, self.leser = self._argv(a.schreiber, "schreiber"), self._argv(a.leser, "leser")

    @staticmethod
    def _argv(roh, was):
        try:
            v = json.loads(roh or "")
        except ValueError:
            v = None
        if not (isinstance(v, list) and v and all(isinstance(x, str) for x in v) and any("{name}" in x for x in v)):
            raise SystemExit(f"ABBRUCH --{was} muss ein JSON-argv mit Platzhalter {{name}} sein")
        return v

    def _fuell(self, argv, name):
        return [x.format(name=name, **self.p) for x in argv]

    def lies(self, name):
        """(vorhanden, wert). Wert bleibt im Prozess."""
        try:
            r = subprocess.run(self._fuell(self.leser, name), capture_output=True, text=True, timeout=ABLAGE_TIMEOUT)
        except (subprocess.SubprocessError, OSError) as e:
            raise Abbruch(f"Leser {name}: {type(e).__name__}")
        if r.returncode == 4:
            return False, None
        if r.returncode != 0:
            raise Abbruch(f"Leser {name}: rc {r.returncode}")
        return True, r.stdout.rstrip("\n")

    def schreib(self, name, wert):
        try:
            r = subprocess.run(self._fuell(self.schreiber, name), input=wert, capture_output=True, text=True,
                               timeout=ABLAGE_TIMEOUT)
        except (subprocess.SubprocessError, OSError) as e:
            raise Abbruch(f"Schreiber {name}: {type(e).__name__}")
        if wert in (r.stdout + r.stderr):
            raise Abbruch(f"Schreiber {name}: Ausgabe enthielt den Wert — abgebrochen")
        if r.returncode != 0:
            raise Abbruch(f"Schreiber {name}: rc {r.returncode}")


def vorab(b, abl):
    """Nur lesend. Liefert die Liste der Hinderungsgruende (leer = frei)."""
    st, j, _ = b.req("GET", "/health", operator=False)
    if st != 200 or not isinstance(j, dict) or j.get("status") != "healthy":
        raise Vorbedingung(f"GET /health: {st}")
    st, j, _ = b.req("GET", "/v1/billing/plans", operator=False)
    pl = [p for p in ((j or {}).get("plans") or []) if p.get("id") == PLAN] if isinstance(j, dict) else []
    if st != 200 or len(pl) != 1 or float(pl[0].get("apiBudgetEur") or 0) != 100.0 or pl[0].get("budgetPlanId") is not None:
        raise Vorbedingung(f"Plan {PLAN} nicht im 079-Zustand (G1v10/R-A/Katalog-Reload?) st={st}")
    hindernis = []
    if b.mandant_existiert(TENANT_ID):
        hindernis.append("Tenant existiert")
    if any(str(u.get("email", "")).lower() == EMAIL for u in b.nutzer()):
        hindernis.append("User existiert")
    if b.principals():
        hindernis.append("Principal existiert")
    for name in abl.namen.values():
        da, _ = abl.lies(name)
        if da:
            hindernis.append(f"Infisical-Name {name} existiert (kein Ueberschreiben)")
    return hindernis


def anlegen(b, abl, a):
    if os.path.exists(a.beleg):
        raise SystemExit("ABBRUCH --beleg existiert schon — kein Ueberschreiben")
    h = vorab(b, abl)
    if h:
        raise Vorbedingung("VORHANDEN: " + "; ".join(h) + " — kein Write")
    zeile("PASS vorab: Plan 079, Tenant/User/Principal/Infisical-Namen frei")
    beleg = {"bridge_url": b.url, "erstellt_am": jetzt(), "tenant_id": TENANT_ID, "email": EMAIL, "principal": NAME,
             "allowed_paths": list(ALLOWED_PATHS), "infisical": dict(abl.p), "familie": abl.familie, "namen": {}}
    rc = 0
    try:
        st, _, _ = b.req("POST", "/v1/tenants", {"id": TENANT_ID, "name": TENANT_NAME, "account_type": "test",
                                                 "billing_type": "managed"})
        if st != 201:
            raise Abbruch(f"POST /v1/tenants: {st}")
        beleg["tenant_angelegt"] = True
        st, j, _ = b.req("POST", "/v1/users", {"email": EMAIL, "name": "Schema Rafael-Test (Prod)", "tenant_id": TENANT_ID,
                                               "role": "user"})
        if st != 201 or not isinstance(j, dict) or not j.get("id"):
            raise Abbruch(f"POST /v1/users: {st}")
        uid = str(uuid.UUID(str(j["id"])))
        beleg["user_id"] = uid
        if uid in RAFAEL_IDS:
            raise Abbruch("Test-UUID gleich einer persoenlichen Rafael-ID")
        st, j, _ = b.req("POST", f"/v1/admin/users/{uid}/subscriptions", {"app_id": APP, "plan_id": PLAN})
        sub = (j or {}).get("subscription") if isinstance(j, dict) else None
        if st != 200 or not isinstance(sub, dict) or not sub.get("id"):
            raise Abbruch(f"POST subscriptions: {st}")
        beleg["sub_id"] = str(sub["id"])
        for est, soll in ((0.5, True), (99.0, True), (101.0, False)):
            st, j, _ = b.req("POST", "/v1/budget/check", {"userId": uid, "planId": PLAN, "estimatedCostEur": est})
            if not (st == 200 and isinstance(j, dict) and bool(j.get("allowed")) == soll
                    and float(j.get("topUpRemainingEur") or 0) == 0.0):
                raise Abbruch(f"budget/check {est}: {st} — Topf nicht im Sollzustand (Katalog-Reload?)")
        zeile("PASS Grant schema-generator, Topf 100 hart (0,5/99 erlaubt, 101 abgelehnt, kein Top-up)")
        st, _, _ = b.req("PATCH", f"/v1/users/{uid}", {"provider_config": PIN})
        if st != 200:
            raise Abbruch(f"PATCH Pin: {st}")
        beleg["pin_gesetzt_epoch"] = int(time.time())
        st, ru, _ = b.req("GET", f"/v1/users/{uid}")
        if st != 200 or pin_von(ru) != PIN:
            raise Abbruch("Pin-Readback abweichend")
        zeile("PASS Pin anthropic_direct (Readback); wirksam in den Workern nach <= 60 s")
        principal_anlegen(b, abl, beleg, uid)
    except Vorbedingung:
        raise
    except Abbruch as e:
        zeile(f"FAIL anlegen: {e}")
        rc = nach_principal_fehler(b, beleg)
    except Exception as e:  # W-1 (R1): auch unerwartete Fehler nach dem POST duerfen keinen aktiven Principal hinterlassen
        zeile(f"FAIL anlegen: unerwartet {type(e).__name__}")
        rc = nach_principal_fehler(b, beleg)
    finally:
        schreibe_neu(a.beleg, beleg)
        zeile(f"INFO Anlagebeleg geschrieben (Schreibaufrufe={b.schreibe})")
    return rc or pruefe(b, abl, beleg)


def nach_principal_fehler(b, beleg):
    """W-2 (R1): Wurde der Principal-POST versucht, entscheidet die Bridge-Liste, nicht die Antwort (Verbindungsfehler
    nach serverseitiger Anlage). Vorhanden → deaktivieren, rc 6; Liste unlesbar → rc 7 (unbelegt)."""
    if not beleg.get("principal_post_versucht"):
        return 1
    try:
        vorhanden = bool(b.principals())
    except Exception as e:
        zeile(f"FEHLER Principal-Liste nach Fehler unlesbar ({type(e).__name__}) — Zustand unbelegt, SOFORT an SEC/Kanzler")
        return 7
    if not vorhanden:
        return 1
    beleg["principal_angelegt"] = True
    b.deaktivieren("Fehler nach Principal-POST")
    zeile("INFO evtl. teilweise geschriebene Infisical-Namen entfernt SEC (H-2, nicht im Werkzeug)")
    return 6


def principal_anlegen(b, abl, beleg, uid):
    beleg["principal_post_versucht"] = True
    st, p, _ = b.req("POST", "/v1/admin/service-principals",
                     {"name": NAME, "allowed_apps": [APP], "allowed_paths": list(ALLOWED_PATHS)})
    key = p.pop("token", None) if isinstance(p, dict) else None
    if st != 201 or not isinstance(key, str):
        if st == 201:
            beleg["principal_angelegt"] = True
        raise Abbruch(f"POST service-principals: {st}")
    beleg["principal_angelegt"] = True
    if not key.startswith("wsg_"):
        raise Abbruch("Schluessel ohne Praefix wsg_")
    beleg["schluessel"] = {"len": len(key), "fp8": fp8(key)}
    quelle = {"schluessel": key, "url": b.url, "user": uid, "tenant": TENANT_ID, "app": APP}
    werte = {name: quelle[r] for r, name in abl.namen.items()}
    for name, wert in werte.items():
        abl.schreib(name, wert)
    for name, wert in werte.items():
        da, ist = abl.lies(name)
        if not da or len(ist) != len(wert) or fp8(ist) != fp8(wert):
            raise Abbruch(f"Infisical-Readback {name} abweichend")
        beleg["namen"][name] = {"len": len(wert), "fp8": fp8(wert)}
        zeile(f"PASS Infisical {name} len={len(wert)} fp8={fp8(wert)}")
    del key, werte


def principal_ok(b):
    pr = b.principals()
    return (len(pr) == 1 and pr[0].get("allowed_apps") == [APP] and pr[0].get("allowed_paths") == list(ALLOWED_PATHS)
            and pr[0].get("active") is True and str(pr[0].get("token_prefix") or "").startswith("wsg_")), pr


def identitaet(b, beleg):
    uid = beleg.get("user_id")
    if not uid or uid in RAFAEL_IDS:
        return False, None
    st, j, _ = b.req("GET", f"/v1/users/{uid}")
    if st != 200 or not isinstance(j, dict):
        return False, j
    return (str(j.get("email", "")).lower() == EMAIL and j.get("tenant_id") == TENANT_ID), j


def pruefe(b, abl, beleg):
    ok_id, j = identitaet(b, beleg)
    liz = (j or {}).get("app_licenses") or []
    lizenz = len(liz) == 1 and liz[0].get("app_id") == APP and liz[0].get("plan_id") == PLAN
    pin = pin_von(j) == PIN
    typ = b.typ_test(TENANT_ID)
    mitglieder = sorted(str(u.get("id")) for u in b.nutzer() if u.get("tenant_id") == TENANT_ID)
    allein = mitglieder == [beleg.get("user_id")]
    pr_ok, _ = principal_ok(b)
    namen = True
    for name, soll in (beleg.get("namen") or {}).items():
        da, ist = abl.lies(name)
        namen = namen and da and len(ist) == soll["len"] and fp8(ist) == soll["fp8"]
    namen = (namen and beleg.get("familie") == abl.familie
             and sorted(beleg.get("namen") or {}) == sorted(abl.namen.values()))
    alles = ok_id and lizenz and pin and typ and allein and pr_ok and namen
    zeile(f"{'PASS' if alles else 'FAIL'} pruefen: identitaet={ok_id} lizenz_nur_schema={lizenz} pin={pin} typ_test={typ} "
          f"einziges_mitglied={allein} principal={pr_ok} infisical_familie_{abl.familie}={namen}")
    return 0 if alles else 1


def probe_koepfe(key, uid):
    """Einreichung und Poll tragen DIESELBEN Koepfe: der Job-Owner ist Digest(Principal, App, User)
    (jobs/routes.py _idempotency_owner/_visible_to_caller); ohne X-User-ID im Poll antwortet die Bridge 404."""
    return {"Authorization": "Bearer " + key, "X-App-ID": APP, "X-User-ID": uid,
            "X-Workflow-ID": NAME, "X-Agent-ID": "schloss-probe"}


def job_probe(b, key, uid):
    """Ein synthetischer Minimal-Chat-Job ueber den LB (einziger Chatweg des Principals): POST /v1/jobs mit
    Idempotency-Key (LB-Pflicht fuer Schema). Liefert (status_einreichung, antwort, roh)."""
    body = {"kind": "chat", "payload": {"model": MODELL, "max_tokens": 1,
                                        "messages": [{"role": "user", "content": "schloss-probe"}]}}
    return b.req("POST", "/v1/jobs", body, operator=False,
                 kopf={**probe_koepfe(key, uid), "Idempotency-Key": "schloss-probe-" + secrets.token_hex(16)})


def job_ende(b, key, uid, job_id):
    """Pollt GET /v1/jobs/<id> bis zum Endzustand; (status, error-JSON als Text)."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", str(job_id or "")):
        raise Abbruch("Job-ID der Einreichung ungueltig")
    for _ in range(POLL_MAX):
        st, j, _ = b.req("GET", f"/v1/jobs/{job_id}", operator=False,
                         kopf=probe_koepfe(key, uid))
        if st != 200 or not isinstance(j, dict):
            raise Abbruch(f"GET /v1/jobs/<id>: {st}")
        if j.get("status") in ("done", "error", "cancelled"):
            return j.get("status"), json.dumps(j.get("error"))
        time.sleep(POLL_S)
    raise Abbruch("Probejob nicht in Endzustand — Ergebnis unbelegt")


def spuren(a, uid):
    try:
        argv = json.loads(a.spuren_leser or "")
    except ValueError:
        argv = None
    if not (isinstance(argv, list) and argv and all(isinstance(x, str) for x in argv) and any("{uid}" in x for x in argv)):
        raise SystemExit("ABBRUCH --spuren-leser (JSON-argv mit {uid}) fehlt — N-B nur mit Spurenzaehlung (P-3)")
    r = subprocess.run([x.replace("{uid}", uid) for x in argv], capture_output=True, text=True, timeout=60)
    try:
        j = json.loads(r.stdout)
        z = {k: int(j[k]) for k in ("ledger", "usage", "reservierungen")}
    except (ValueError, KeyError, TypeError):
        raise Abbruch(f"Spurenleser: rc {r.returncode}, Ausgabe unlesbar")
    if r.returncode != 0:
        raise Abbruch(f"Spurenleser: rc {r.returncode}")
    return z


def schloss(b, abl, a, beleg):
    uid = beleg.get("user_id")
    seit = int(time.time()) - int(beleg.get("pin_gesetzt_epoch") or 0)
    if seit < a.pin_wartezeit:
        raise Vorbedingung(f"Pin erst {seit} s alt; N-B fruehestens nach {a.pin_wartezeit} s (Worker-Cache 60 s)")
    da, key = abl.lies(abl.namen["schluessel"])
    if not da or fp8(key) != (beleg.get("schluessel") or {}).get("fp8"):
        raise Abbruch("Schluessel in Infisical fehlt oder Fingerabdruck != Anlagebeleg")
    vor = spuren(a, uid)
    st, j, _ = job_probe(b, key, uid)
    if st not in (200, 201) or not isinstance(j, dict):
        raise Abbruch(f"POST /v1/jobs (Probejob): {st}")
    ende, fehler = job_ende(b, key, uid, j.get("job_id"))
    nach = spuren(a, uid)
    # Die Schloesser greifen im Job-Selbstaufruf vor jeder Reservierung/jedem Dispatch (main.py ~3940 vor 4097).
    zu = ende == "error" and any(c in fehler for c in NONEU_CODES)
    # P-3 im Jobweg: Ledger und Reservierungen +0; ai_jobs genau +1 (der Probejob selbst).
    still = (nach["ledger"] == vor["ledger"] and nach["reservierungen"] == vor["reservierungen"]
             and nach["usage"] == vor["usage"] + 1)
    zeile(f"{'PASS' if zu else 'FAIL'} N-B Schloesser zu: job={ende} noneu_code={any(c in fehler for c in NONEU_CODES)}")
    zeile(f"{'PASS' if still else 'FAIL'} N-B ohne Spuren (ledger/reservierungen +0, ai_jobs +1): vorher={vor} nachher={nach}")
    zufall = "wsg_" + secrets.token_hex(32)
    st2, _, _ = job_probe(b, zufall, uid)
    zeile(f"{'PASS' if st2 == 401 else 'FAIL'} N-D Zufallsschluessel wsg_: status={st2}")
    del key, zufall
    return 0 if (zu and still and st2 == 401) else 1


def lade_beleg(pfad, url):
    with open(pfad) as f:
        beleg = json.load(f)
    if beleg.get("bridge_url") != url:
        raise Abbruch("Beleg gehoert zu einer anderen Bridge")
    if beleg.get("tenant_id") != TENANT_ID or beleg.get("email") != EMAIL or beleg.get("principal") != NAME:
        raise Abbruch("Beleg ausserhalb der Allowlist")
    if beleg.get("user_id") is not None:
        beleg["user_id"] = str(uuid.UUID(str(beleg["user_id"])))
        if beleg["user_id"] in RAFAEL_IDS:
            raise Abbruch("Beleg nennt eine persoenliche Rafael-ID")
    return beleg


def alter_schluessel_tot(b, abl, a, beleg, worker):
    """P-2: nach >= 25 s an JEDEM Worker direkt; Poll einer nie vergebenen Job-ID (kein Dispatch, keine Kosten)."""
    time.sleep(a.cache_wartezeit)
    da, key = abl.lies(abl.namen["schluessel"])
    if not da or fp8(key) != (beleg.get("schluessel") or {}).get("fp8"):
        return None
    st = {}
    for w in worker:
        s, _, _ = b.req("GET", "/v1/jobs/job_probe_" + "0" * 32, basis=w, operator=False,
                        kopf={"Authorization": "Bearer " + key, "X-App-ID": APP})
        st[w.rsplit(":", 1)[1]] = s
    del key
    return st


def abbauen(b, abl, a, beleg, worker):
    if os.path.exists(a.nachweis):
        raise SystemExit("ABBRUCH --nachweis existiert schon — kein Ueberschreiben")
    n = {"bridge_url": b.url, "erstellt_am": jetzt(), "beleg_erstellt_am": beleg.get("erstellt_am"),
         "nicht_im_werkzeug": ["SCHEMA_NONEU_* leeren + Worker-Recreate (Root/SEC, P-4)",
                               "Infisical-Namen/Vercel-Abbildung entfernen (SEC)"]}
    try:
        abbau_schritte(b, abl, a, beleg, worker, n)
    except Abbruch as e:
        n["abbruch"] = str(e)
        zeile(f"FAIL abbauen: {e}")
    finally:
        gut = (n.get("principal_aus") in (True, "nicht_vorhanden")
               and n.get("alter_schluessel_401_alle") in (True, "kein_schluessel_abgelegt", "nicht_vorhanden") and "abbruch" not in n
               and n.get("pin_geloest") is True and n.get("abo_gekuendigt") is True
               and n.get("user") in ("geloescht", "anonymisiert"))
        n["rc"] = 0 if gut else 1
        schreibe_neu(a.nachweis, n)
        zeile(f"INFO Abbau-Nachweis geschrieben (Schreibaufrufe={b.schreibe})")
    return n["rc"]


def abbau_schritte(b, abl, a, beleg, worker, n):
    uid = beleg.get("user_id")
    # 1. Principal zuerst (Sofort-Rueckweg: zu nach <= 20 s). Ein vorhandener Principal mit diesem Namen ist
    #    Allowlist-Objekt; ohne Anlagebeleg wird er trotzdem nur deaktiviert (nie geloescht, keine Route).
    #    W-2 (R1): unabhaengig von principal_angelegt im Beleg (Anlage evtl. ohne Antwort).
    if b.principals():
        b.deaktivieren("abbauen")
        n["principal_aus"] = True
        if (beleg.get("schluessel") or {}).get("fp8"):
            st = alter_schluessel_tot(b, abl, a, beleg, worker)
            n["alter_schluessel_status"] = st
            n["alter_schluessel_401_alle"] = bool(st) and len(st) == len(worker) and all(v == 401 for v in st.values())
            zeile(f"{'PASS' if n['alter_schluessel_401_alle'] else 'FAIL'} alter Schluessel nach {a.cache_wartezeit} s: {st}")
        else:
            n["alter_schluessel_401_alle"] = "kein_schluessel_abgelegt"
            zeile("INFO Principal deaktiviert; Schluessel nie abgelegt (niemandem bekannt), kein 401-Readback moeglich")
    else:
        n["principal_aus"] = n["alter_schluessel_401_alle"] = "nicht_vorhanden"
        zeile("INFO kein Principal mit dem Allowlist-Namen vorhanden")
    if not beleg.get("tenant_angelegt") or not uid:
        raise Abbruch("Anlage von Tenant/User nicht belegt — kein weiterer Write")
    ok_id, _ = identitaet(b, beleg)
    typ = b.typ_test(TENANT_ID)
    mitglieder = sorted(str(u.get("id")) for u in b.nutzer() if u.get("tenant_id") == TENANT_ID)
    if not (ok_id and typ and mitglieder == [uid]):
        raise Abbruch(f"Beweis fehlt (identitaet={ok_id} typ_test={typ} einziges_mitglied={mitglieder == [uid]}) — KEIN Write")
    # 3. Pin loesen
    st, _, _ = b.req("PATCH", f"/v1/users/{uid}", {"provider_config": {}})
    st2, ru, _ = b.req("GET", f"/v1/users/{uid}")
    n["pin_geloest"] = st == 200 and st2 == 200 and not pin_von(ru)
    # 4. Abo kuendigen
    if beleg.get("sub_id"):
        st, _, _ = b.req("POST", f"/v1/billing/{uid}/subscriptions/{beleg['sub_id']}/cancel")
        st2, j, _ = b.req("POST", "/v1/budget/check", {"userId": uid, "planId": PLAN, "estimatedCostEur": 0.5})
        n["abo_gekuendigt"] = st == 204 and st2 == 200 and (j or {}).get("allowed") is False
    else:
        n["abo_gekuendigt"] = False
    # 5. User: DELETE; 409 (Usage/Ledger) → anonymize nur diese uid, Stub bleibt im Test-Tenant (B-L-2)
    st, _, _ = b.req("DELETE", f"/v1/users/{uid}")
    if st == 204:
        st2, _, _ = b.req("GET", f"/v1/users/{uid}")
        n["user"] = "geloescht" if st2 == 404 else f"delete 204, readback {st2}"
    elif st == 409:
        st2, _, _ = b.req("POST", f"/v1/users/{uid}/anonymize")
        n["user"] = "anonymisiert" if st2 == 200 else f"anonymize {st2}"
    else:
        n["user"] = f"delete {st}"
    # 6. Tenant nur wenn leer (nach DELETE raeumt die Bridge ihn i. d. R. mit)
    if n["user"] == "geloescht" and b.mandant_existiert(TENANT_ID):
        b.req("DELETE", f"/v1/tenants/{TENANT_ID}")
    n["tenant_weg"] = not b.mandant_existiert(TENANT_ID)
    zeile(f"PASS/INFO abbauen: pin_geloest={n['pin_geloest']} abo_gekuendigt={n['abo_gekuendigt']} user={n['user']} "
          f"tenant_weg={n['tenant_weg']}")


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("befehl", choices=("vorab", "anlegen", "pruefen", "schloss", "abbauen"))
    p.add_argument("--bridge", required=True)
    p.add_argument("--worker", action="append", default=[])
    p.add_argument("--beleg")
    p.add_argument("--nachweis")
    p.add_argument("--namensfamilie", required=True)
    for r in ROLLEN:
        p.add_argument(f"--name-{r}")
    p.add_argument("--infisical-ws", required=True)
    p.add_argument("--infisical-env", required=True)
    p.add_argument("--infisical-pfad", required=True)
    p.add_argument("--schreiber", required=True)
    p.add_argument("--leser", required=True)
    p.add_argument("--spuren-leser")
    p.add_argument("--cache-wartezeit", type=int, default=PRINCIPAL_CACHE_S)
    p.add_argument("--pin-wartezeit", type=int, default=PIN_CACHE_S)
    a = p.parse_args(argv)
    wartezeiten(ziele(a.bridge, a.worker), a.cache_wartezeit, a.pin_wartezeit)
    if a.befehl != "vorab" and not a.beleg:
        raise SystemExit("ABBRUCH --beleg fehlt")
    if a.befehl == "abbauen" and not a.nachweis:
        raise SystemExit("ABBRUCH abbauen braucht --nachweis")
    abl = Ablage(a)
    b = Bridge(a.bridge, lies_fd3())
    try:
        if a.befehl == "vorab":
            h = vorab(b, abl)
            zeile(("PASS vorab: alles frei" if not h else "INFO vorab: " + "; ".join(h)))
            rc = 0 if not h else 2
        elif a.befehl == "anlegen":
            rc = anlegen(b, abl, a)
        else:
            beleg = lade_beleg(a.beleg, b.url)
            if beleg.get("familie") != abl.familie:
                raise Abbruch(f"Beleg-Namensfamilie {beleg.get('familie')} != --namensfamilie {abl.familie}")
            if a.befehl == "pruefen":
                rc = pruefe(b, abl, beleg)
            elif a.befehl == "schloss":
                rc = schloss(b, abl, a, beleg)
            else:
                rc = abbauen(b, abl, a, beleg, a.worker)
    except Vorbedingung as e:
        zeile(f"ABBRUCH {a.befehl}: {e}")
        rc = 2
    except Abbruch as e:
        zeile(f"FAIL {a.befehl}: {e}")
        rc = 1
    zeile(f"ERGEBNIS {a.befehl} RC={rc} Schreibaufrufe={b.schreibe}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
