#!/usr/bin/env python3
"""probe-testmandanten.py — genau ZWEI synthetische Probe-Mandanten auf der Dev-Bridge anlegen, pruefen, abbauen.

Auftrag: SEC-JA L1+L2 (Fall 25318036, Kanzler 18.11.2026), Plan 880ce900e95e. Fuer die Report-Staging-Probe v2
(zwei getrennte synthetische Mandanten, Mandantenloeschung nur synthetisch). KEIN Ersatz fuer seed-bridge-users.sh,
das unveraendert bleibt.

Bewusst NICHT moeglich (kein Schalter, kein Codepfad):
  - andere Konten/Mandanten als die festen zwei Paare (ALLOWLIST);
  - Abo, Rechnungsadresse, Guthaben/Top-up, Projekt-Slots, interactive@/demo-report@;
  - PATCH oder Abgleich bestehender Konten: existiert etwas schon, bricht `anlegen` OHNE Write ab;
  - Abbau ohne Identitaets- und Mandantenbeweis gegen den Anlagebeleg;
  - jede andere Bridge als die Dev-Bridge (und die lokale Attrappe 127.0.0.1 fuer Proben).

Aufruf:
  probe-testmandanten.py anlegen --beleg <neue datei> [--pw-datei <0600-datei>]
  probe-testmandanten.py pruefen --beleg <anlagebeleg>
  probe-testmandanten.py abbauen --beleg <anlagebeleg> --nachweis <neue datei>

Prozessvariablen: BRIDGE_URL (Pflicht), BRIDGE_SERVICE_TOKEN (Pflicht), PROBE_PW_A/PROBE_PW_B (nur `anlegen`,
alternativ --pw-datei mit {"a": "...", "b": "..."}, Eigentuemer = Aufrufer, Modus 0600). Geheimnisse erscheinen nie
in Ausgabe, Beleg oder Nachweis.

Exit: 0 = alles PASS, 1 = Fehler/abgebrochen (Ausgabe nennt den Schritt), 2 = Aufruf-/Vorbedingungsfehler.
"""
import argparse
import datetime
import json
import os
import re
import stat
import sys
import urllib.error
import urllib.request
import uuid

ALLOWLIST = (
    {"kennung": "a", "tenant_id": "test-tenant-probe-a", "email": "test-tenant-probe-a@test.werkingflow.com"},
    {"kennung": "b", "tenant_id": "test-tenant-probe-b", "email": "test-tenant-probe-b@test.werkingflow.com"},
)
APP_ID, PLAN_ID = "werking-report", "trial"
# Dev-Bridge (49.12.72.66 = bridge.werking.tools) und die lokale Attrappe fuer Proben. Nichts sonst.
ERLAUBTE_BRIDGE = re.compile(r"^(http://49\.12\.72\.66:8000|https://bridge\.werking\.tools|http://127\.0\.0\.1:[0-9]{1,5})$")
SEITE, SEITEN_MAX = 1000, 20
TIMEOUT = 15


def zeile(t):
    print(t, flush=True)


class Abbruch(Exception):
    pass


def bridge_url():
    url = os.environ.get("BRIDGE_URL", "").rstrip("/")
    if not ERLAUBTE_BRIDGE.match(url):
        raise SystemExit("ABBRUCH BRIDGE_URL ist nicht die Dev-Bridge (http://49.12.72.66:8000 / https://bridge.werking.tools) "
                         "— kein Request gesendet")
    if not os.environ.get("BRIDGE_SERVICE_TOKEN"):
        raise SystemExit("ABBRUCH BRIDGE_SERVICE_TOKEN fehlt")
    return url


class Bridge:
    def __init__(self, url):
        self.url = url
        self.schreibe = 0

    def req(self, methode, pfad, body=None):
        if methode != "GET":
            self.schreibe += 1
        daten = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.url + pfad, data=daten, method=methode)
        r.add_header("X-Bridge-Service-Token", os.environ["BRIDGE_SERVICE_TOKEN"])
        if daten is not None:
            r.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(r, timeout=TIMEOUT) as a:
                roh = a.read()
                st = a.status
        except urllib.error.HTTPError as e:
            roh, st = e.read(), e.code
        except (urllib.error.URLError, OSError) as e:
            raise Abbruch(f"{methode} {pfad}: Verbindung ({type(e).__name__})")
        try:
            j = json.loads(roh) if roh else None
        except ValueError:
            j = None
        return st, j

    def alle(self, pfad):
        """Volle Liste ueber Seiten; fail-closed, wenn die Obergrenze erreicht wird (dann ist 'nicht da' unbewiesen)."""
        aus = []
        for s in range(SEITEN_MAX):
            trenn = "&" if "?" in pfad else "?"
            st, j = self.req("GET", f"{pfad}{trenn}limit={SEITE}&offset={s * SEITE}")
            if st != 200 or not isinstance(j, list):
                raise Abbruch(f"GET {pfad}: {st}")
            aus.extend(j)
            if len(j) < SEITE:
                return aus
        raise Abbruch(f"GET {pfad}: mehr als {SEITE * SEITEN_MAX} Eintraege — Bestand unbewiesen")

    def mandanten(self, art):
        st, j = self.req("GET", f"/v1/tenants?account_type={art}&limit={SEITE}")
        if st != 200 or not isinstance(j, list):
            raise Abbruch(f"GET /v1/tenants?account_type={art}: {st}")
        if len(j) >= SEITE:
            raise Abbruch(f"GET /v1/tenants?account_type={art}: Obergrenze erreicht — Bestand unbewiesen")
        return {t.get("id"): t for t in j}


def paar(kennung):
    return next(p for p in ALLOWLIST if p["kennung"] == kennung)


def passwoerter(pw_datei):
    if pw_datei:
        st = os.stat(pw_datei)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or stat.S_IMODE(st.st_mode) != 0o600:
            raise SystemExit("ABBRUCH --pw-datei muss eine Datei des Aufrufers mit Modus 0600 sein")
        with open(pw_datei) as f:
            d = json.load(f)
        pw = {"a": d.get("a"), "b": d.get("b")}
    else:
        pw = {"a": os.environ.get("PROBE_PW_A"), "b": os.environ.get("PROBE_PW_B")}
    for k, v in pw.items():
        if not isinstance(v, str) or len(v) < 16:
            raise SystemExit(f"ABBRUCH Passwort {k} fehlt oder ist kuerzer als 16 Zeichen")
    return pw


def schreibe_neu(pfad, daten):
    fd = os.open(pfad, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(daten, f, indent=1, sort_keys=True)


def jetzt():
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def anlegen(b, a):
    pw = passwoerter(a.pw_datei)
    if os.path.exists(a.beleg):
        raise SystemExit("ABBRUCH --beleg existiert schon — kein Ueberschreiben")
    # 1. Vorpruefung NUR lesend: weder Konto noch Mandant darf existieren.
    emails = {u.get("email", "").lower() for u in b.alle("/v1/users")}
    vorhanden = set()
    for art in ("test", "customer", "internal"):
        vorhanden |= set(b.mandanten(art))
    for p in ALLOWLIST:
        if p["email"] in emails or p["tenant_id"] in vorhanden:
            raise Abbruch(f"VORHANDEN {p['kennung']}: Konto oder Mandant existiert bereits — kein Write, kein PATCH")
    zeile("PASS Vorpruefung: beide Paare frei")
    beleg = {"bridge_url": b.url, "erstellt_am": jetzt(), "paare": []}
    try:
        for p in ALLOWLIST:
            e = {"kennung": p["kennung"], "tenant_id": p["tenant_id"], "email": p["email"], "user_id": None, "lizenz": False}
            beleg["paare"].append(e)
            st, _ = b.req("POST", "/v1/tenants", {"id": p["tenant_id"], "name": f"Probe-Mandant {p['kennung'].upper()} (synthetisch)",
                                                   "account_type": "test"})
            if st != 201:
                raise Abbruch(f"POST /v1/tenants {p['kennung']}: {st}")
            e["tenant_angelegt"] = True
            st, j = b.req("POST", "/v1/users", {"email": p["email"], "name": f"Probe {p['kennung'].upper()}",
                                                 "tenant_id": p["tenant_id"], "password": pw[p["kennung"]], "role": "user"})
            if st != 201 or not isinstance(j, dict) or not j.get("id"):
                raise Abbruch(f"POST /v1/users {p['kennung']}: {st}")
            e["user_id"] = str(uuid.UUID(str(j["id"])))
            st, _ = b.req("POST", f"/v1/users/{e['user_id']}/app-licenses",
                          {"appId": APP_ID, "planId": PLAN_ID, "startDate": datetime.date.today().isoformat(), "endDate": None, "seats": 1})
            if st not in (200, 201):
                raise Abbruch(f"POST app-licenses {p['kennung']}: {st}")
            e["lizenz"] = True
            zeile(f"PASS angelegt {p['kennung']}: tenant={p['tenant_id']} user_id={e['user_id']}")
    finally:
        schreibe_neu(a.beleg, beleg)
        zeile(f"INFO Anlagebeleg geschrieben (Schreibaufrufe={b.schreibe})")
    return pruefe(b, beleg)


def lade_beleg(pfad, url):
    with open(pfad) as f:
        beleg = json.load(f)
    if beleg.get("bridge_url") != url:
        raise Abbruch("Beleg gehoert zu einer anderen Bridge")
    paare = beleg.get("paare")
    if not isinstance(paare, list) or not paare or len(paare) > 2:
        raise Abbruch("Beleg ohne gueltige Paarliste")
    for e in paare:
        soll = next((p for p in ALLOWLIST if p["kennung"] == e.get("kennung")), None)
        if not soll or e.get("tenant_id") != soll["tenant_id"] or e.get("email") != soll["email"]:
            raise Abbruch(f"Beleg-Eintrag ausserhalb der Allowlist: {e.get('kennung')}")
        if e.get("user_id") is not None:
            e["user_id"] = str(uuid.UUID(str(e["user_id"])))
    return beleg


def nutzer_beweis(b, e):
    """Identitaetsbeweis: uid lebt, E-Mail und Mandant gleich dem Beleg."""
    st, j = b.req("GET", f"/v1/users/{e['user_id']}")
    if st != 200 or not isinstance(j, dict):
        return False, j, st
    return (str(j.get("email", "")).lower() == e["email"] and j.get("tenant_id") == e["tenant_id"]), j, st


def mitglieder(b, tid):
    # Ueber ALLE Nutzer, nicht nur account_type=test: der Mitgliederbeweis bleibt unabhaengig vom Typ-Beweis.
    return sorted(str(u.get("id")) for u in b.alle("/v1/users") if u.get("tenant_id") == tid)


def pruefe(b, beleg):
    rot = 0
    test = b.mandanten("test")
    for e in beleg["paare"]:
        if not e.get("user_id"):
            zeile(f"FAIL pruefen {e['kennung']}: kein Nutzer im Beleg"); rot += 1; continue
        ok, j, st = nutzer_beweis(b, e)
        liz = (j or {}).get("app_licenses") or []
        lizenz_ok = len(liz) == 1 and liz[0].get("app_id") == APP_ID and liz[0].get("plan_id") == PLAN_ID and liz[0].get("seats") == 1
        typ_ok = e["tenant_id"] in test
        m = mitglieder(b, e["tenant_id"])
        alles = ok and lizenz_ok and typ_ok and m == [e["user_id"]]
        rot += not alles
        zeile(f"{'PASS' if alles else 'FAIL'} pruefen {e['kennung']}: identitaet={ok} lizenz_minimal={lizenz_ok} "
              f"mandant_typ_test={typ_ok} einziges_mitglied={m == [e['user_id']]} (GET user {st})")
    return 0 if rot == 0 else 1


def abbauen(b, a):
    beleg = lade_beleg(a.beleg, b.url)
    if os.path.exists(a.nachweis):
        raise SystemExit("ABBRUCH --nachweis existiert schon — kein Ueberschreiben")
    test = b.mandanten("test")
    nachweis = {"bridge_url": b.url, "erstellt_am": jetzt(), "beleg_erstellt_am": beleg.get("erstellt_am"), "paare": []}
    rot = 0
    for e in beleg["paare"]:
        n = {"kennung": e["kennung"], "tenant_id": e["tenant_id"], "user_id": e.get("user_id")}
        nachweis["paare"].append(n)
        tid = e["tenant_id"]
        n["tenant_typ_test"] = tid in test
        m = mitglieder(b, tid)
        if e.get("user_id"):
            ok, _, st = nutzer_beweis(b, e)
            n["identitaet_ok"] = ok
            n["tenant_ein_mitglied"] = m == [e["user_id"]]
            if not (ok and n["tenant_typ_test"] and n["tenant_ein_mitglied"]):
                rot += 1
                zeile(f"FAIL abbauen {e['kennung']}: Beweis fehlt (identitaet={ok} typ_test={n['tenant_typ_test']} "
                      f"einziges_mitglied={n['tenant_ein_mitglied']}) — KEIN DELETE")
                continue
            st, _ = b.req("DELETE", f"/v1/users/{e['user_id']}")
            n["delete_user_status"] = st
            st2, _ = b.req("GET", f"/v1/users/{e['user_id']}")
            n["readback_user_404"] = st2 == 404
        else:
            # Teilstand: nur der Mandant wurde angelegt. Beweis: Typ test und KEIN Mitglied.
            n["identitaet_ok"] = None
            n["tenant_ein_mitglied"] = None
            if not (n["tenant_typ_test"] and m == []):
                rot += 1
                zeile(f"FAIL abbauen {e['kennung']}: Mandant ohne Nutzer, aber typ_test={n['tenant_typ_test']} mitglieder={m} — KEIN DELETE")
                continue
            n["readback_user_404"] = None
        # Die Bridge raeumt einen leeren, datenlosen Mandanten beim Nutzer-DELETE mit (drop_tenant_if_orphaned).
        if tid in b.mandanten("test"):
            st, _ = b.req("DELETE", f"/v1/tenants/{tid}")
            n["delete_tenant_status"] = st
        else:
            n["delete_tenant_status"] = "durch Nutzer-DELETE mitgeloescht"
        weg = all(tid not in b.mandanten(art) for art in ("test", "customer", "internal"))
        n["readback_tenant_weg"] = weg
        gut = weg and n.get("readback_user_404") is not False and n.get("delete_user_status", 204) == 204
        rot += not gut
        zeile(f"{'PASS' if gut else 'FAIL'} abbauen {e['kennung']}: user_delete={n.get('delete_user_status')} "
              f"user_404={n['readback_user_404']} tenant={n['delete_tenant_status']} tenant_weg={weg}")
    nachweis["rc"] = 0 if rot == 0 else 1
    schreibe_neu(a.nachweis, nachweis)
    zeile(f"INFO Abbau-Nachweis geschrieben (Schreibaufrufe={b.schreibe})")
    return nachweis["rc"]


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("befehl", choices=("anlegen", "pruefen", "abbauen"))
    p.add_argument("--beleg", required=True)
    p.add_argument("--pw-datei")
    p.add_argument("--nachweis")
    a = p.parse_args(argv)
    if a.befehl == "abbauen" and not a.nachweis:
        raise SystemExit("ABBRUCH abbauen braucht --nachweis")
    b = Bridge(bridge_url())
    try:
        if a.befehl == "anlegen":
            rc = anlegen(b, a)
        elif a.befehl == "pruefen":
            rc = pruefe(b, lade_beleg(a.beleg, b.url))
        else:
            rc = abbauen(b, a)
    except Abbruch as e:
        zeile(f"FAIL {a.befehl}: {e}")
        rc = 1
    zeile(f"ERGEBNIS {a.befehl} RC={rc} Schreibaufrufe={b.schreibe}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
