"""Anlagenkennungen im belegten Anlagenkontext (src/privacy_postprocess/anlagenkontext.py).

Verbindliche Abnahmefaelle: Root, e2e-staging-7K3Q/PRIVACY-CONTEXT-ACCEPTANCE-CASES.json (8 Faelle, synthetisch),
plus Gegenproben (SEC-Auflage: nur synthetische, realistisch formatierte Namen/Firmen/Orte/Kontakte).
Die Detektor-Treffer sind die gemessenen Flair-Typen aus E2E 7K3Q (KV-01..FI-01 ORG, B-02 LOC).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.privacy_postprocess import postprocess_smart_anonymize  # noqa: E402
from src.privacy_postprocess import anlagenkontext as ak  # noqa: E402
from tests.unit.test_privacy_postprocess import dienst  # noqa: E402

KENNUNGEN_7K3Q = [("KV-01", "ORGANIZATION"), ("UK-01", "ORGANIZATION"), ("P-03", "ORGANIZATION"),
                  ("F-01", "ORGANIZATION"), ("FI-01", "ORGANIZATION"), ("B-02", "LOCATION")]


def lauf(text, treffer, anlagenkontext=True):
    return postprocess_smart_anonymize(text, dienst(text, list(treffer), "SN"), prefix="SN",
                                       anlagenkontext=anlagenkontext)


def klar(r, wert):
    """Wert steht an mindestens einer Stelle im Klartext (nicht als Platzhalter)."""
    return wert in r["smart_anonymized_text"]


def maskiert(r, wert):
    return wert not in r["smart_anonymized_text"] and wert in r["mapping"].values()


def basis(text, treffer):
    """Stand OHNE die neue Regel (Referenz fuer vorbestehendes Verhalten)."""
    return postprocess_smart_anonymize(text, dienst(text, list(treffer), "SN"), prefix="SN")


def nicht_mehr_klartext_als_basis(text, treffer, r):
    """Die Regel darf gegenueber der Basis NUR die herabgestuften Kennungen freigeben: jeder Wert, der in der
    Basis maskiert ist, ist mit Regel ebenfalls maskiert, ausser er ist als herabgestuft ausgewiesen."""
    b = basis(text, treffer)
    frei = {h["original"] for h in r["postprocessing"].get("anlagenkontext", {}).get("herabgestuft", [])}
    for w in b["mapping"].values():
        if w not in frei:
            assert w not in r["smart_anonymized_text"], f"{w!r} durch die Regel freigegeben"


# ── Root-Abnahmefaelle (verbindlich) ─────────────────────────────────────────

def test_A1_six_real_false_positives():
    t = ("Der Kaltwasserverteiler KV-01 versorgt den Umluftkühler UK-01 mit Umwälzpumpe P-03. Der Durchflussmesser "
         "F-01 misst den Brunnenkreis. Der Filter FI-01 liegt vor dem Schluckbrunnen B-02.")
    r = lauf(t, KENNUNGEN_7K3Q)
    for w, _ in KENNUNGEN_7K3Q:
        assert klar(r, w), w
    ak_info = r["postprocessing"]["anlagenkontext"]
    assert ak_info["aktiv"] is True and len(ak_info["herabgestuft"]) == 6 and ak_info["wieder_maskiert"] == 0
    assert {h["bauteilwort"] for h in ak_info["herabgestuft"]} == {
        "Kaltwasserverteiler", "Umluftkühler", "Umwälzpumpe", "Durchflussmesser", "Filter", "Schluckbrunnen"}
    assert r["postprocessing"]["dropped"][ak.GRUND] == 6


def test_A2_person_and_phone_next_to_equipment():
    # Telefon in Inlandsschreibweise: das Test-Hilfsmittel ``dienst`` ersetzt per \\b und erfasst ein fuehrendes "+"
    # nicht (Artefakt der Attrappe, nicht des Dienstes).
    t = "Herr Bernd Quastenberg prüft den Kaltwasserverteiler KV-01. Telefon 030 5550 1234."
    tr = [("Bernd Quastenberg", "PERSON"), ("KV-01", "ORGANIZATION"), ("030 5550 1234", "PHONE_NUMBER")]
    r = lauf(t, tr)
    assert klar(r, "KV-01")
    assert maskiert(r, "Bernd Quastenberg") and maskiert(r, "030 5550 1234")
    nicht_mehr_klartext_als_basis(t, tr, r)


def test_A3_company_same_shape():
    t = "Die Firma KV-01 liefert einen Kaltwasserverteiler."
    r = lauf(t, [("KV-01", "ORGANIZATION")])
    assert maskiert(r, "KV-01")
    assert r["postprocessing"]["anlagenkontext"]["herabgestuft"] == []


def test_A4_global_placeholder_collision_per_span():
    t = "Die Firma KV-01 betreut das Projekt. Der Kaltwasserverteiler KV-01 versorgt den Umluftkühler UK-01."
    r = lauf(t, [("KV-01", "ORGANIZATION"), ("UK-01", "ORGANIZATION")])
    # Firmenstelle -> der Wert ist im Dokument mehrdeutig -> JEDE Stelle bleibt maskiert (keine Teilfreigabe)
    assert "Firma KV-01" not in r["smart_anonymized_text"]
    assert maskiert(r, "KV-01") and "Kaltwasserverteiler KV-01" not in r["smart_anonymized_text"]
    assert klar(r, "UK-01")
    a = r["postprocessing"]["anlagenkontext"]
    assert {"wert": "KV-01", "stellen": 2, "grund": "mehrdeutig-marker:Firma"} in a["nicht_herabgestuft"]
    assert a["wieder_maskiert"] == 0


def test_A5_contact_context():
    t = "Kontaktperson B-02, zuständig für den Schluckbrunnen."
    for typ in ("LOCATION", "ORGANIZATION"):
        r = lauf(t, [("B-02", typ)])
        assert maskiert(r, "B-02"), typ
        assert r["postprocessing"]["anlagenkontext"]["herabgestuft"] == []
    # PERSON: die neue Regel fasst harte Typen nie an -> Ergebnis gleich der Basis. (Vorbestehend, NICHT Teil
    # dieses Fixes: die Plausibilitaet verwirft PERSON mit Ziffer, "person-with-digit"; als Befund gemeldet.)
    r = lauf(t, [("B-02", "PERSON")])
    assert r["smart_anonymized_text"] == basis(t, [("B-02", "PERSON")])["smart_anonymized_text"]
    assert r["postprocessing"]["anlagenkontext"]["herabgestuft"] == []


def test_A6_hard_pii_overlap():
    t = "Der Filter FI-01 gehört Bernd Quastenberg; Kontakt fi-01@example.org."
    r = lauf(t, [("FI-01", "ORGANIZATION"), ("Bernd Quastenberg", "PERSON"), ("fi-01@example.org", "EMAIL_ADDRESS")])
    assert klar(r, "FI-01")
    assert maskiert(r, "fi-01@example.org") and maskiert(r, "Bernd Quastenberg")
    assert "example.org" not in r["smart_anonymized_text"]


@pytest.mark.parametrize("anlagenkontext", [False])
def test_A7_wrong_app_or_missing_context_unchanged(anlagenkontext):
    t = "Der Kaltwasserverteiler KV-01 versorgt den Umluftkühler UK-01."
    tr = [("KV-01", "ORGANIZATION"), ("UK-01", "ORGANIZATION")]
    neu = lauf(t, tr, anlagenkontext=anlagenkontext)
    alt = postprocess_smart_anonymize(t, dienst(t, tr, "SN"), prefix="SN")
    assert neu == alt  # byte-gleich zum Stand ohne Regel
    assert "anlagenkontext" not in neu["postprocessing"]
    assert maskiert(neu, "KV-01") and maskiert(neu, "UK-01")


def test_A8_flag_disabled(monkeypatch):
    monkeypatch.delenv(ak.FLAG_ENV, raising=False)
    assert ak.flag_an() is False
    monkeypatch.setenv(ak.FLAG_ENV, "false")
    assert ak.flag_an() is False
    monkeypatch.setenv(ak.FLAG_ENV, "true")
    assert ak.flag_an() is True
    t = "Der Kaltwasserteiler KV-01 ist eingebaut."
    r = lauf(t, [("KV-01", "ORGANIZATION")], anlagenkontext=False)
    assert maskiert(r, "KV-01")


# ── Gegenproben (synthetisch): echte Namen/Firmen/Orte NEBEN Kennungen bleiben maskiert ──

GEGEN = [
    # (Text, Treffer, Wert der maskiert bleiben muss, Kennung die frei sein darf oder None)
    ("Die Huberwerk GmbH liefert die Umwälzpumpe P-03.", [("Huberwerk GmbH", "ORGANIZATION"), ("P-03", "ORGANIZATION")],
     "Huberwerk GmbH", "P-03"),
    ("Der Brunnen in Kleinmariazell versorgt den Kaltwasserverteiler KV-01.",
     [("Kleinmariazell", "LOCATION"), ("KV-01", "ORGANIZATION")], "Kleinmariazell", "KV-01"),
    ("Ansprechpartnerin Jana Wiesinger, Lindengasse 14, 3100 St. Pölten, j.wiesinger@example.net, betreut den Filter FI-01.",
     [("Jana Wiesinger", "PERSON"), ("St. Pölten", "LOCATION"), ("j.wiesinger@example.net", "EMAIL_ADDRESS"),
      ("FI-01", "ORGANIZATION")], "Jana Wiesinger", "FI-01"),
    ("Bauherr KV-01 beauftragt die Anlage.", [("KV-01", "ORGANIZATION")], "KV-01", None),
    ("Standort B-02 liegt in Kleinmariazell.", [("B-02", "LOCATION"), ("Kleinmariazell", "LOCATION")], "B-02", None),
    ("Lieferant AB-12 und der Wärmetauscher WT-01 sind bestellt.", [("AB-12", "ORGANIZATION"), ("WT-01", "ORGANIZATION")],
     "AB-12", "WT-01"),
    ("Auftragnehmerfirma KV-01 montiert.", [("KV-01", "ORGANIZATION")], "KV-01", None),
    ("Herr KV-01 prüft.", [("KV-01", "ORGANIZATION")], "KV-01", None),
    ("Die Wiesinger Haustechnik KG in Gmünd wartet den Umluftkühler UK-01.",
     [("Wiesinger Haustechnik KG", "ORGANIZATION"), ("Gmünd", "LOCATION"), ("UK-01", "ORGANIZATION")],
     "Wiesinger Haustechnik KG", "UK-01"),
]


@pytest.mark.parametrize("text,treffer,maske,frei", GEGEN)
def test_gegenprobe(text, treffer, maske, frei):
    r = lauf(text, treffer)
    assert maskiert(r, maske), (maske, r["smart_anonymized_text"])
    for w, _ in treffer:
        if w != frei:
            assert w not in r["smart_anonymized_text"], w
    if frei:
        assert klar(r, frei)
    nicht_mehr_klartext_als_basis(text, treffer, r)


@pytest.mark.parametrize("text,wert,typ", [
    ("Gemischt: Herr Bernd Quastenberg (KV-01) und die Firma UK-01.", "UK-01", "ORGANIZATION"),
    ("KV-01 versorgt den Umluftkühler.", "KV-01", "ORGANIZATION"),              # kein Wort davor
    ("Der Verteiler-KV-01 ist neu.", "KV-01", "ORGANIZATION"),                  # angeklebt
    ("Siehe https://example.org/KV-01 für den Verteiler.", "KV-01", "ORGANIZATION"),  # URL-Teil
    ("Der Kaltwasserverteiler KV-01a ist neu.", "KV-01a", "ORGANIZATION"),      # keine Kennungsform
    ("Der Kaltwasserverteiler Kv-01 ist neu.", "Kv-01", "ORGANIZATION"),        # keine Kennungsform
    ("Der Kaltwasserverteiler KVXYZ-01 ist neu.", "KVXYZ-01", "ORGANIZATION"),  # Praefix zu lang
    ("Der KALTWASSERVERTEILER KV-01 ist neu.", "KV-01", "ORGANIZATION"),        # kein Normal-Substantiv
    ("Der Kaltwasserverteiler\nKV-01 ist neu.", "KV-01", "ORGANIZATION"),       # andere Zeile
    ("Der Kaltwasserverteiler KV-01 ist neu.", "KV-01", "PERSON"),              # harter Typ
    ("Der Kaltwasserverteiler KV-01 ist neu.", "KV-01", "NRP"),                 # anderer weicher Typ
])
def test_keine_herabstufung(text, wert, typ):
    r = lauf(text, [(wert, typ)])
    assert r["postprocessing"]["anlagenkontext"]["herabgestuft"] == []
    # Ergebnis gleich der Basis (vorbestehende Regeln wie URL-Teil oder person-with-digit bleiben wie sie sind)
    assert r["smart_anonymized_text"] == basis(text, [(wert, typ)])["smart_anonymized_text"]


def test_bauteilwort_selbst_fundstelle():
    t = "Der Quastenberg KV-01 ist neu."  # Wort davor ist selbst als Person erkannt
    r = lauf(t, [("Quastenberg", "PERSON"), ("KV-01", "ORGANIZATION")])
    assert maskiert(r, "KV-01") and maskiert(r, "Quastenberg")


def test_ausgabe_sichtbar_und_begruendet():
    t = "Der Schluckbrunnen B-02 ist gebohrt."
    r = lauf(t, [("B-02", "LOCATION")])
    h = r["postprocessing"]["anlagenkontext"]["herabgestuft"]
    assert h == [{"original": "B-02", "type": "LOCATION", "start": t.index("B-02"), "confidence": 0.9,
                  "bauteilwort": "Schluckbrunnen", "grund": ak.GRUND}]


# ── Serverseitige Verifikation (kontext_verifiziert) ────────────────────────

import asyncio  # noqa: E402
import types  # noqa: E402

import src.routing.app_provider_policy as _app  # noqa: E402
from src.jobs import store_client as _store  # noqa: E402
from src.jobs.selbstaufruf import JOB_LAUF_HEADER  # noqa: E402

SCHEMA = "werking-schema-generator"
UID = "11111111-2222-3333-4444-555555555555"


def _req(peer="127.0.0.1", job=None):
    return types.SimpleNamespace(headers={JOB_LAUF_HEADER: job} if job else {},
                                 client=types.SimpleNamespace(host=peer), state=types.SimpleNamespace())


def _job(**kw):
    attr = {"app_id": SCHEMA, "user_id": UID, ak.JOB_SCHEMA_VERIFIZIERT_KEY: True}
    attr.update(kw.pop("attr", {}))
    return {"status": kw.get("status", "running"), "attribution": attr}


@pytest.fixture
def umgebung(monkeypatch):
    monkeypatch.setenv(ak.FLAG_ENV, "true")
    zustand = {"app": (SCHEMA, "header"), "job": _job(), "fehler": None}
    monkeypatch.setattr(_app, "_resolve_app_id", lambda r: zustand["app"])

    async def get_job(jid):
        if zustand["fehler"]:
            raise zustand["fehler"]
        return zustand["job"]
    monkeypatch.setattr(_store, "get_job", get_job)
    return zustand


def _v(req, hint=ak.ANLAGENKONTEXT, uid=UID):
    return asyncio.run(ak.kontext_verifiziert(req, hint, aufrufer_user_id=uid))


def test_V1_principal_schema_direkt(umgebung):
    umgebung["app"] = (SCHEMA, "principal")
    assert _v(_req(peer="10.0.0.9")) is True


def test_V2_nur_kopf_x_app_id_reicht_nicht(umgebung):
    assert _v(_req(peer="10.0.0.9")) is False  # header-Quelle, kein Job


def test_V3_falsche_app_principal(umgebung):
    umgebung["app"] = ("werking-report", "principal")
    assert _v(_req(peer="10.0.0.9")) is False


def test_V4_job_selbstaufruf_verifiziert(umgebung):
    assert _v(_req(job="job_dev_x")) is True


@pytest.mark.parametrize("aenderung", [
    {"peer": "10.0.0.9"},                                         # Job-Kopf nicht vom Loopback
    {"job_attr": {ak.JOB_SCHEMA_VERIFIZIERT_KEY: None}},          # Merkmal fehlt (Kopf-Schema-Job)
    {"job_attr": {ak.JOB_SCHEMA_VERIFIZIERT_KEY: "true"}},        # nur echtes True
    {"job_attr": {"app_id": "werking-report"}},                   # falsche App
    {"job_attr": {"user_id": "99999999-2222-3333-4444-555555555555"}},  # anderer Nutzer
    {"status": "done"},                                           # Job laeuft nicht
    {"uid": None},                                                # Aufrufer ohne Nutzer
    {"fehler": RuntimeError("store down")},                       # Speicher nicht lesbar
    {"hint": None}, {"hint": "Baugutachten"}, {"hint": "TECHNICAL_SYSTEM_DESCRIPTION"},
    {"flag": "false"}, {"flag": None},
])
def test_V5_fail_closed(umgebung, monkeypatch, aenderung):
    if "job_attr" in aenderung:
        umgebung["job"] = _job(attr=aenderung["job_attr"])
    if "status" in aenderung:
        umgebung["job"] = _job(status=aenderung["status"])
    if "fehler" in aenderung:
        umgebung["fehler"] = aenderung["fehler"]
    if "flag" in aenderung:
        if aenderung["flag"] is None:
            monkeypatch.delenv(ak.FLAG_ENV)
        else:
            monkeypatch.setenv(ak.FLAG_ENV, aenderung["flag"])
    req = _req(peer=aenderung.get("peer", "127.0.0.1"), job="job_dev_x")
    assert _v(req, hint=aenderung.get("hint", ak.ANLAGENKONTEXT), uid=aenderung.get("uid", UID)) is False


def test_V6_flag_aus_auch_mit_principal(umgebung, monkeypatch):
    umgebung["app"] = (SCHEMA, "principal")
    monkeypatch.delenv(ak.FLAG_ENV)
    assert _v(_req()) is False


# ── Bedingungen einzeln: entscheide() (Dokumentweite Definition/Referenz, Runde 1 nach Replay 7K3Q) ──

T_OK = "Der Kaltwasserverteiler KV-01 ist neu."
S_OK = (T_OK.index("KV-01"), T_OK.index("KV-01") + 5)


def _ent(text, kandidaten, hart=(), alle=None):
    return ak.entscheide(text, list(kandidaten), list(hart), list(alle if alle is not None else
                                                                  [(k[0], k[1]) for k in kandidaten]))


def test_entscheide_positiv_referenz():
    frei, ab = _ent(T_OK, [(*S_OK, "ORGANIZATION", "detektor")])
    assert frei == {S_OK: "Kaltwasserverteiler"} and ab == []


@pytest.mark.parametrize("quelle", ["regel:strasse", "bekannt", "nachzug", ""])
def test_entscheide_nur_detektor(quelle):
    frei, ab = _ent(T_OK, [(*S_OK, "ORGANIZATION", quelle)])
    assert frei == {} and ab[0]["grund"] == "harter-oder-anderer-typ"


def test_entscheide_harte_ueberlappung_und_anhaengen():
    frei, ab = _ent(T_OK, [(*S_OK, "ORGANIZATION", "detektor")], hart=[(S_OK[0] - 3, S_OK[1])])
    assert frei == {} and ab[0]["grund"] == "an-harter-pii"
    t = "Herr Bernd Quastenberg (KV-01) prüft den Kaltwasserverteiler KV-01."
    p = t.index("Bernd"); k1 = t.index("KV-01"); k2 = t.rindex("KV-01")
    frei, ab = _ent(t, [(k1, k1 + 5, "ORGANIZATION", "detektor"), (k2, k2 + 5, "ORGANIZATION", "detektor")],
                    hart=[(p, p + len("Bernd Quastenberg"))])
    assert frei == {} and ab[0]["grund"] == "an-harter-pii"


@pytest.mark.parametrize("text", [
    "Der Kaltwasserverteiler KV-01/2 ist neu.",
    "Der Kaltwasserverteiler KV-01@example.org ist neu.",
    "Der Kaltwasserverteiler KV-01.example.org ist neu.",
    "Der Kaltwasserverteiler KV-01-Neu ist neu.",
    "Der Kaltwasserverteiler KV-01:8080 ist neu.",
    "Der Kaltwasserverteiler KV-01_alt ist neu.",
    "Der Kaltwasserverteiler #KV-01 ist neu.",
])
def test_entscheide_einbettung(text):
    s = text.index("KV-01")
    frei, ab = _ent(text, [(s, s + 5, "ORGANIZATION", "detektor")])
    assert frei == {}


@pytest.mark.parametrize("text", ["Der Schluckbrunnen B-02.", "Der Schluckbrunnen B-02, gebohrt.",
                                  "Der Schluckbrunnen B-02: gebohrt.", "(Der Schluckbrunnen B-02)"])
def test_entscheide_satzzeichen_erlaubt(text):
    s = text.index("B-02")
    frei, _ = _ent(text, [(s, s + 4, "LOCATION", "detektor")])
    assert frei == {(s, s + 4): "Schluckbrunnen"}


def test_entscheide_tabellendefinition_und_referenzen():
    t = ("KV-01;Kaltwasserverteiler;Verteilung;DN 100;\n"
         "Der Pufferspeicher liegt zwischen KV-01 und WT-01.\n| KV-01 | Verteiler |")
    ks = [(m, m + 5, "ORGANIZATION", "detektor") for m in [i for i in range(len(t)) if t.startswith("KV-01", i)]]
    frei, ab = _ent(t, ks)
    assert len(frei) == 3 and set(frei.values()) == {"Kaltwasserverteiler"} and ab == []


def test_entscheide_nur_referenzen_ohne_definition():
    t = "Der Pufferspeicher liegt zwischen KV-01 und WT-01. Siehe auch KV-01."
    ks = [(i, i + 5, "ORGANIZATION", "detektor") for i in range(len(t)) if t.startswith("KV-01", i)]
    frei, ab = _ent(t, ks)
    assert frei == {} and ab[0]["grund"] == "keine-definition-im-dokument"


def test_entscheide_tabellenzelle_marker():
    t = "KV-01;Firma;Wartung;\nDer Kaltwasserverteiler KV-01 ist neu."
    ks = [(i, i + 5, "ORGANIZATION", "detektor") for i in range(len(t)) if t.startswith("KV-01", i)]
    frei, ab = _ent(t, ks)
    assert frei == {} and ab[0]["grund"] == "mehrdeutig-marker:Firma"


def test_entscheide_gemischter_typ_einer_stelle():
    t = "Der Kaltwasserverteiler KV-01 ist neu. Herr KV-01 prüft."
    k1 = t.index("KV-01"); k2 = t.rindex("KV-01")
    frei, ab = _ent(t, [(k1, k1 + 5, "ORGANIZATION", "detektor"), (k2, k2 + 5, "PERSON", "detektor")])
    assert frei == {}


def test_entscheide_bauteilwort_ist_weiche_fundstelle():
    t = "Der Brunnen Kleinmariazell KV-01 ist neu."
    k = t.index("KV-01"); o = t.index("Kleinmariazell")
    frei, ab = _ent(t, [(k, k + 5, "ORGANIZATION", "detektor"), (o, o + 14, "LOCATION", "detektor")])
    assert frei == {} and ab[0]["grund"] == "keine-definition-im-dokument"


def test_entscheide_harte_pii_direkt_danach():
    t = "Der Kaltwasserverteiler KV-01 (Bernd Quastenberg) ist neu."
    k = t.index("KV-01"); p = t.index("Bernd")
    frei, ab = _ent(t, [(k, k + 5, "ORGANIZATION", "detektor")], hart=[(p, p + 17)])
    assert frei == {} and ab[0]["grund"] == "an-harter-pii"


def test_entscheide_nur_kennungswerte_im_bericht():
    t = "Herr Bernd Quastenberg prüft den Kaltwasserverteiler KV-01."
    p = t.index("Bernd"); k = t.index("KV-01")
    frei, ab = _ent(t, [(p, p + 17, "PERSON", "detektor"), (k, k + 5, "ORGANIZATION", "detektor")], hart=[(p, p + 17)])
    assert ab == [] and list(frei.values()) == ["Kaltwasserverteiler"]
