"""Anlagenkennungen im belegten Anlagenkontext (src/privacy_postprocess/anlagenkontext.py).

Verbindliche Abnahmefaelle: Root, e2e-staging-7K3Q/PRIVACY-CONTEXT-ACCEPTANCE-CASES.json (8 Faelle, synthetisch),
plus Gegenproben (SEC-Auflage: nur synthetische, realistisch formatierte Namen/Firmen/Orte/Kontakte).
Die Detektor-Treffer sind die gemessenen Flair-Typen aus E2E 7K3Q (KV-01..FI-01 ORG, B-02 LOC).
"""

from __future__ import annotations

import re
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
    # Root-Fall woertlich. R3 (Rafael/SEC 17.11.): Person im selben Satz sperrt die Kennung NICHT, die Person wird
    # getrennt maskiert -> Root-Erwartung "KV-01 retained, person and telephone masked" erfuellt.
    t = "Herr Bernd Quastenberg prüft den Kaltwasserverteiler KV-01. Telefon 030 5550 1234."
    tr = [("Bernd Quastenberg", "PERSON"), ("KV-01", "ORGANIZATION"), ("030 5550 1234", "PHONE_NUMBER")]
    r = lauf(t, tr)
    assert klar(r, "KV-01") and maskiert(r, "Bernd Quastenberg") and maskiert(r, "030 5550 1234")
    assert r["postprocessing"]["anlagenkontext"]["nicht_herabgestuft"] == []
    nicht_mehr_klartext_als_basis(t, tr, r)
    # Bauteilsatz getrennt von Person und Telefon: KV-01 frei, PII maskiert
    t3 = "Herr Bernd Quastenberg prüft heute.\nDer Kaltwasserverteiler KV-01 ist eingebaut.\nTelefon 030 5550 1234."
    r3 = lauf(t3, tr)
    assert klar(r3, "KV-01") and maskiert(r3, "Bernd Quastenberg") and maskiert(r3, "030 5550 1234")
    nicht_mehr_klartext_als_basis(t3, tr, r3)


def test_A3_company_same_shape():
    t = "Die Firma KV-01 liefert einen Kaltwasserverteiler."
    r = lauf(t, [("KV-01", "ORGANIZATION")])
    assert maskiert(r, "KV-01")
    assert r["postprocessing"]["anlagenkontext"]["herabgestuft"] == []


def test_A4_global_placeholder_collision_per_span():
    t = "Die Firma KV-01 betreut das Projekt. Der Kaltwasserverteiler KV-01 versorgt den Umluftkühler UK-01."
    r = lauf(t, [("KV-01", "ORGANIZATION"), ("UK-01", "ORGANIZATION")])
    # R3 (SEC: same-value technisch/Person getrennt JE STELLE): Firmenstelle maskiert, die Stelle mit eigenem
    # Bauteilbegriff frei; keine globale Wiederherstellung (Firmenstelle bleibt Platzhalter), Mischfall exakt
    # ueber technical_retentions belegt.
    out = r["smart_anonymized_text"]
    assert "Firma KV-01" not in out and "Kaltwasserverteiler KV-01" in out and klar(r, "UK-01")
    assert "KV-01" in r["mapping"].values()
    a = r["postprocessing"]["anlagenkontext"]
    assert {x["grund"] for x in a["nicht_herabgestuft"] if x["wert"] == "KV-01"} == {"marker:Firma"}
    assert a["wieder_maskiert"] == 0
    tr_ = a["technical_retentions"]
    assert [(s["value"], out[s["start"]:s["end"]]) for s in tr_["spans"]] == [("KV-01", "KV-01"), ("UK-01", "UK-01")]
    assert out.count("KV-01") == 1


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
      ("FI-01", "ORGANIZATION")], "Jana Wiesinger", "FI-01"),  # R3: Kontakt im Satz sperrt nicht, Person maskiert
    ("Bauherr KV-01 beauftragt die Anlage.", [("KV-01", "ORGANIZATION")], "KV-01", None),
    ("Standort B-02 liegt in Kleinmariazell.", [("B-02", "LOCATION"), ("Kleinmariazell", "LOCATION")], "B-02", None),
    ("Lieferant AB-12 und der Wärmetauscher WT-01 sind bestellt.", [("AB-12", "ORGANIZATION"), ("WT-01", "ORGANIZATION")],
     "AB-12", "WT-01"),  # R3: AB-12 (Lieferant) maskiert, WT-01 mit eigenem Bauteilbegriff frei
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
    ("Der KaltWasserVerteiler KV-01 ist neu.", "KV-01", "ORGANIZATION"),  # gemischte Schreibung (R3b: Versalien/klein zulaessig)
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
                  "bauteilwort": "Schluckbrunnen", "beleg": "definition", "grund": ak.GRUND}]


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
    return _ent3(text, kandidaten, hart, alle)[:2]


def _ent3(text, kandidaten, hart=(), alle=None):
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
    # R3 je Stelle: die an der Person haengende Stelle maskiert, die Bauteilstelle frei
    assert frei == {(k2, k2 + 5): "Kaltwasserverteiler"} and [x["grund"] for x in ab] == ["an-harter-pii"]


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
         "Der Pufferspeicher liegt zwischen KV-01 und WT-01.\n| KV-01 | Heizkreisverteiler |")
    ks = [(m, m + 5, "ORGANIZATION", "detektor") for m in [i for i in range(len(t)) if t.startswith("KV-01", i)]]
    frei, ab = _ent(t, ks)
    # Definition (Tabellenzelle), Referenz (Praeposition "zwischen"), Markdown-Zelle "| KV-01 | Heizkreisverteiler |"
    # (R3b: "Verteiler" allein ist mehrdeutig - E-Mail-Verteiler = Personengruppe - und belegt nichts)
    assert len(frei) == 3 and set(frei.values()) == {"Kaltwasserverteiler", "zwischen", "Heizkreisverteiler"} and ab == []


def test_entscheide_nur_referenzen_ohne_definition():
    t = "Der Pufferspeicher liegt zwischen KV-01 und WT-01. Siehe auch KV-01."
    ks = [(i, i + 5, "ORGANIZATION", "detektor") for i in range(len(t)) if t.startswith("KV-01", i)]
    frei, ab = _ent(t, ks)
    assert frei == {} and {x["grund"] for x in ab} == {"keine-definition-im-aufruf", "kein-lokaler-technischer-beleg"}


def test_entscheide_tabellenzelle_marker():
    t = "KV-01;Firma;Wartung;\nDer Kaltwasserverteiler KV-01 ist neu."
    ks = [(i, i + 5, "ORGANIZATION", "detektor") for i in range(len(t)) if t.startswith("KV-01", i)]
    frei, ab = _ent(t, ks)
    # R3: Firmenzeile maskiert, Bauteilstelle im selben Text frei (je Stelle)
    assert list(frei.values()) == ["Kaltwasserverteiler"] and [x["grund"] for x in ab] == ["marker:Firma"]


def test_entscheide_gemischter_typ_einer_stelle():
    t = "Der Kaltwasserverteiler KV-01 ist neu. Herr KV-01 prüft."
    k1 = t.index("KV-01"); k2 = t.rindex("KV-01")
    frei, ab = _ent(t, [(k1, k1 + 5, "ORGANIZATION", "detektor"), (k2, k2 + 5, "PERSON", "detektor")])
    assert frei == {}


def test_entscheide_bauteilwort_ist_weiche_fundstelle():
    t = "Der Brunnen Kleinmariazell KV-01 ist neu."
    k = t.index("KV-01"); o = t.index("Kleinmariazell")
    frei, ab = _ent(t, [(k, k + 5, "ORGANIZATION", "detektor"), (o, o + 14, "LOCATION", "detektor")])
    assert frei == {} and ab[0]["grund"] == "kein-lokaler-technischer-beleg"


def test_entscheide_harte_pii_direkt_danach():
    t = "Der Kaltwasserverteiler KV-01 (Bernd Quastenberg) ist neu."
    k = t.index("KV-01"); p = t.index("Bernd")
    frei, ab = _ent(t, [(k, k + 5, "ORGANIZATION", "detektor")], hart=[(p, p + 17)])
    assert frei == {} and ab[0]["grund"] == "an-harter-pii"


def test_entscheide_nur_kennungswerte_im_bericht():
    t = "Herr Bernd Quastenberg prüft heute.\nDer Kaltwasserverteiler KV-01 ist neu."
    p = t.index("Bernd"); k = t.index("KV-01")
    frei, ab = _ent(t, [(p, p + 17, "PERSON", "detektor"), (k, k + 5, "ORGANIZATION", "detektor")], hart=[(p, p + 17)])
    assert ab == [] and list(frei.values()) == ["Kaltwasserverteiler"]


# ── Adversarialfaelle R2 (Review e8511fe6 P-PRV-1/2/3, SEC-Designentscheid R2) ──────────

SEP = "\n\nWERKING_DOCUMENT_00000000-0000-4000-8000-000000000001\n\n"


def test_R2_P1_ungemeldete_firmenstelle_bleibt_maskiert_pipeline():
    # Dienst meldet nur die Bauteilstelle; die Firmenstelle (gleicher Text) darf nicht Klartext werden
    t = "Der Kaltwasserverteiler KV-01 ist neu.\nRechnung an die Firma KV-01 bitte."
    k = t.index("KV-01")
    antwort = dienst(t, [("KV-01", "ORGANIZATION")], "SN")
    r = postprocess_smart_anonymize(t, antwort, prefix="SN", anlagenkontext=True)
    assert "Firma KV-01" not in r["smart_anonymized_text"]
    # R3: die ungemeldete Firmenstelle wird zusaetzlich maskiert, die Bauteilstelle bleibt frei
    assert r["smart_anonymized_text"].count("KV-01") == 1 and "Kaltwasserverteiler KV-01" in r["smart_anonymized_text"]


def test_R2_P1_ungemeldete_firmenstelle_anderer_text_zusatzmaske():
    t = "Der Kaltwasserverteiler KV-01 ist neu." + SEP + "Rechnung an die Firma KV-01 bitte."
    k = t.index("KV-01")
    frei, ab, zusatz = _ent3(t, [(k, k + 5, "ORGANIZATION", "detektor")])
    k2 = t.rindex("KV-01")
    assert frei == {(k, k + 5): "Kaltwasserverteiler"}
    assert zusatz == [(k2, k2 + 5, "ORGANIZATION")]  # ungemeldete Firmenstelle wird zusaetzlich maskiert
    # Pipeline: Firmenstelle maskiert, Bauteilstelle frei (je Stelle, keine wertglobale Angleichung)
    antwort = {"status": "success", "anonymization_performed": True, "raw_anonymized_text": t.replace("KV-01", "SN_ORGANIZATION_001", 1),
               "raw_entity_count": 1, "smart_anonymized_text": t.replace("KV-01", "SN_ORGANIZATION_001", 1),
               "smart_entity_count": 1, "restored_entities": [], "mapping": {"SN_ORGANIZATION_001": "KV-01"},
               "detected_entities": [{"placeholder": "SN_ORGANIZATION_001", "type": "ORGANIZATION", "original": "KV-01",
                                      "confidence": 0.9, "decision": "KEEP", "reason": ""}]}
    r = postprocess_smart_anonymize(t, antwort, prefix="SN", anlagenkontext=True)
    assert "Firma KV-01" not in r["smart_anonymized_text"]
    assert "Kaltwasserverteiler KV-01" in r["smart_anonymized_text"]


@pytest.mark.parametrize("zweite", [
    "Kontakt KV-01: 0664 1234567.", "Mail an kv-01@example.org oder KV-01 direkt.",
    "KV-01, Lindengasse 14, 3100 Musterdorf.", "Die Hausverwaltung KV-01 ruft an.",
    "Ansprechpartner ist KV-01, bitte anrufen.",
])
def test_R2_P1_kontakt_adresse_firma_ohne_detektortreffer(zweite):
    for verbindung in ("\n", SEP):
        t = "Der Kaltwasserverteiler KV-01 ist neu." + verbindung + zweite
        k = t.index("KV-01")
        frei, ab, zusatz = _ent3(t, [(k, k + 5, "ORGANIZATION", "detektor")])
        k2 = t.rindex("KV-01")
        assert (k2, k2 + 5) not in frei
        # R3: in beiden Faellen wird die Kontakt-/Firmenstelle zusaetzlich maskiert, die Bauteilstelle bleibt frei
        assert (k2, k2 + 5, "ORGANIZATION") in zusatz, zweite
        assert frei == {(k, k + 5): "Kaltwasserverteiler"}, zweite


def test_R2_P1_eingebettetes_vorkommen():
    t = "Der Kaltwasserverteiler KV-01 ist neu.\nSiehe https://firma.example/KV-01 und KV-01_alt."
    k = t.index("KV-01")
    frei, ab = _ent(t, [(k, k + 5, "ORGANIZATION", "detektor")])
    assert (k, k + 5) in frei and "eingebettet" in {x["grund"] for x in ab}
    assert all(x["pos"] != k for x in ab)


def test_R2_P2_nachbarzelle_ist_fundstelle():
    t = "LF-02;Huberwerk;Lieferung\nWT-01;Waermetauscher;"
    l = t.index("LF-02"); h = t.index("Huberwerk"); w = t.index("WT-01")
    frei, ab = _ent(t, [(l, l + 5, "ORGANIZATION", "detektor"), (h, h + 9, "ORGANIZATION", "detektor"),
                        (w, w + 5, "ORGANIZATION", "detektor")])
    assert (l, l + 5) not in frei and frei.get((w, w + 5)) == "Waermetauscher"


@pytest.mark.parametrize("satz", ["Die Wohnung W-12 ist vermietet.", "Das Haus B-02 steht leer.",
                                  "Die Stiege A-2 ist gesperrt.", "Das Zimmer R-12 ist belegt.",
                                  "Postfach PF-12 wird geleert.", "Top T-4 liegt im Stock S-2.",
                                  "Die Etage E-3 ist frei.", "Die Liegenschaft L-17 wird verkauft."])
def test_R2_P3_adressaehnliche_kennungen(satz):
    for w in re.findall(r"[A-Z]{1,2}-\d+", satz):
        s0 = satz.index(w)
        frei, ab = _ent(satz, [(s0, s0 + len(w), "LOCATION", "detektor")])
        assert frei == {}, (satz, w)


def test_R2_B02_ueber_texte_je_stelle():
    # SEC-Beispiel: technische Stelle frei, "Ansprechpartner B-02" in anderem Text maskiert
    t = "Der Schluckbrunnen B-02 ist dicht." + SEP + "Ansprechpartner ist B-02, bitte anrufen."
    ks = [(m.start(), m.end(), "LOCATION", "detektor") for m in re.finditer("B-02", t)]
    frei, ab = _ent(t, ks)
    assert list(frei) == [(ks[0][0], ks[0][1])]
    assert ab == [{"wert": "B-02", "text": 2, "pos": ks[1][0], "grund": "kein-lokaler-technischer-beleg"}]
    r = postprocess_smart_anonymize(t, dienst(t, [("B-02", "LOCATION")], "SN"), prefix="SN", anlagenkontext=True)
    assert "Schluckbrunnen B-02" in r["smart_anonymized_text"] and "ist B-02" not in r["smart_anonymized_text"]


def test_R2_querverweis_als_hinweis_nicht_als_freigabe():
    # Definition in Text 1 (Hinweis); Text 2 verweist mit Praeposition -> frei; Text 3 ohne lokalen Beleg -> maskiert
    t = ("Der Schluckbrunnen B-02 ist dicht." + SEP + "Die Leitung fuehrt zu B-02." + SEP + "B-02 meldet sich morgen.")
    ks = [(m.start(), m.end(), "LOCATION", "detektor") for m in re.finditer("B-02", t)]
    frei, ab = _ent(t, ks)
    assert set(frei) == {(ks[0][0], ks[0][1]), (ks[1][0], ks[1][1])}
    assert ab == [{"wert": "B-02", "text": 3, "pos": ks[2][0], "grund": "kein-lokaler-technischer-beleg"}]


def test_R2_referenz_ohne_definition_im_aufruf():
    t = "Die Leitung fuehrt zu B-02." + SEP + "Der Ruecklauf geht ueber B-02."
    ks = [(m.start(), m.end(), "LOCATION", "detektor") for m in re.finditer("B-02", t)]
    frei, ab = _ent(t, ks)
    assert frei == {} and {x["grund"] for x in ab} == {"keine-definition-im-aufruf"}


@pytest.mark.parametrize("satz,pii", [
    ("Der Kaltwasserverteiler KV-01 gehört der Hausverwaltung.", None),
    ("Der Kaltwasserverteiler KV-01 wird vom Monteur getauscht.", None),
    ("Der Kaltwasserverteiler KV-01 steht am Standort Nord.", None),
    ("Der Kaltwasserverteiler KV-01 ist defekt, Rückruf 0664 1234567.", ("0664 1234567", "PHONE_NUMBER")),
    ("Der Kaltwasserverteiler KV-01 an service@example.org melden.", ("service@example.org", "EMAIL_ADDRESS")),
])
def test_R3_rolle_oder_pii_anderswo_im_satz_sperrt_nicht(satz, pii):
    # R3 (Rafael 17.11.: "Die Pumpe P-03 hat Ole Fennimann eingebaut" -> P-03 frei, Name maskiert): Rollenwort oder
    # Kontakt anderswo im Satz sperrt die Kennung mit eigenem Bauteilbegriff nicht; der Kontakt bleibt maskiert.
    k = satz.index("KV-01")
    frei, _ = _ent(satz, [(k, k + 5, "ORGANIZATION", "detektor")])
    assert frei == {(k, k + 5): "Kaltwasserverteiler"}, satz
    r = lauf(satz, [("KV-01", "ORGANIZATION")] + ([pii] if pii else []))
    assert "Kaltwasserverteiler KV-01" in r["smart_anonymized_text"]
    if pii:  # vom Dienst gemeldeter Kontakt bleibt maskiert
        assert maskiert(r, pii[0]), pii


def test_R3_tabellenzeile_nachbarzellen_kontakt_getrennt():
    # Rafael: Zellen sind getrennte Daten. Bauteilbezug in der Nachbarzelle -> F-01 frei; Name maskiert.
    t = "F-01,Brunnenkreis,Durchfluss,Ansprechpartnerin: Frau Ilka Sonnwendt"
    frei, ab = _ent(t, [(0, 4, "ORGANIZATION", "detektor")])
    assert frei == {(0, 4): "Brunnenkreis"} and ab == []
    r = lauf(t, [("F-01", "ORGANIZATION"), ("Ilka Sonnwendt", "PERSON")])
    assert r["smart_anonymized_text"].startswith("F-01,Brunnenkreis") and maskiert(r, "Ilka Sonnwendt")


@pytest.mark.parametrize("satz,w", [("Erzeuger WT-01, KM-01 und Verbraucher RLT-01, UK-01.", "UK-01"),
                                    ("Die Umwaelzpumpe (P-02) laeuft.", "P-02"),
                                    ("P-03;Umwaelzpumpe;Kreis UK-01;3 m3/h;", "UK-01")])
def test_R2_aufzaehlung_klammer_zelle_definieren(satz, w):
    k = satz.index(w)
    frei, _ = _ent(satz, [(k, k + len(w), "ORGANIZATION", "detektor")])
    assert len(frei) == 1, satz


def test_R2_aufzaehlung_marker_vererbt():
    t = "Lieferanten AB-01, AB-02 und AB-03 liefern."
    ks = [(m.start(), m.end(), "ORGANIZATION", "detektor") for m in re.finditer(r"AB-0\d", t)]
    frei, _ = _ent(t, ks)
    assert frei == {}


def test_R2_keine_wertglobale_rueckmaskierung_und_keine_reexposition():
    # Wert in Text 1 frei, in Text 2 (Kontakt) maskiert: Ausgabe zeigt Text-1-Stelle, Text-2-Stelle maskiert,
    # Mapping enthaelt den Wert (Rueckweg intakt); harte PII bleibt maskiert.
    t = "Der Filter FI-01 ist neu." + SEP + "Kontakt FI-01: Jana Wiesinger, j.wiesinger@example.net."
    tr = [("FI-01", "ORGANIZATION"), ("Jana Wiesinger", "PERSON"), ("j.wiesinger@example.net", "EMAIL_ADDRESS")]
    r = lauf(t, tr)
    out = r["smart_anonymized_text"]
    assert "Filter FI-01" in out and "Kontakt FI-01" not in out
    assert "Jana Wiesinger" not in out and "j.wiesinger@example.net" not in out
    assert "FI-01" in r["mapping"].values()
    nicht_mehr_klartext_als_basis(t, tr, r)


# ── R3 (Rafael/SEC 17.11.2026 ~20:30Z): positive Bauteilbindung je Stelle, Zellen getrennt, Retentions-Vertrag ──

@pytest.mark.parametrize("wort", ["Familie", "Bewohner", "Bewohnerin", "Patient", "Patientin", "Kollege", "Klient",
                                  "Nachbar", "Vermieter", "Sachbearbeiter", "Endverbraucher", "Schüler", "Bewerber",
                                  "Besitzer", "Käufer", "Vertreter", "Absender", "Empfänger", "Haushalt", "Gast"])
def test_R3_P_PRV_4_personenwort_ist_kein_bauteil(wort):
    # Review R2 P-PRV-4: frueher galt jedes grossgeschriebene Nicht-Marker-Substantiv als Definition. Jetzt nur die
    # positive Fachliste -> Personen-/Haushaltswoerter belegen nichts (fail-closed), ohne Sperrliste.
    t = f"{wort} B-02 meldet sich.\nDer Schluckbrunnen B-02 ist dicht."
    ks = [(m.start(), m.end(), "LOCATION", "detektor") for m in re.finditer("B-02", t)]
    frei, ab = _ent(t, ks)
    assert (ks[0][0], ks[0][1]) not in frei and ab[0]["grund"] in ("kein-lokaler-technischer-beleg", "marker:" + wort), wort
    assert frei == {(ks[1][0], ks[1][1]): "Schluckbrunnen"}
    assert not ak.ist_bauteil(wort)


@pytest.mark.parametrize("t,frei_w,name", [
    ("Die Pumpe P-03 hat Ole Fennimann eingebaut.", "P-03", "Ole Fennimann"),
    ("F-01;Durchflussmesser;Brunnenkreis;;geprueft von Ole Fennimann", "F-01", "Ole Fennimann"),
    ("F-01;Durchflussmesser;Brunnenkreis;;geprueft von Herrn Ole Fennimann", "F-01", "Ole Fennimann"),
])
def test_R3_P_PRV_5_name_getrennt_maskiert(t, frei_w, name):
    # Review R2 P-PRV-5 / Rafael: Name im Satz bzw. in anderer Zelle wird maskiert, die Kennung bleibt erhalten.
    r = lauf(t, [(frei_w, "ORGANIZATION"), (name, "PERSON")])
    assert klar(r, frei_w) and maskiert(r, name)
    tr_ = r["postprocessing"]["anlagenkontext"]["technical_retentions"]
    assert [s["value"] for s in tr_["spans"]] == [frei_w]


@pytest.mark.parametrize("t", [
    "Ansprechpartner B-02 ruft an.", "Ansprechpartner ist B-02, bitte anrufen.", "Familie B-02 zieht ein.",
    "Bewohner B-02 meldet einen Schaden.", "Kontaktperson B-02, zuständig für den Schluckbrunnen.",
    "Der Schluckbrunnen B-02, Ansprechpartner Herr Wiesinger.", "Der Schluckbrunnen B-02 ist unser Ansprechpartner.",
    "Der Schluckbrunnen B-02: Frau Sonnwendt.", "Der Schluckbrunnen B-02 (Kontakt) ist dicht.",
])
def test_R3_B02_rollenfaelle_maskiert(t):
    k = t.index("B-02")
    frei, ab = _ent(t, [(k, k + 4, "LOCATION", "detektor")])
    assert frei == {}, (t, ab)
    r = lauf(t, [("B-02", "LOCATION")])
    assert maskiert(r, "B-02")
    assert r["postprocessing"]["anlagenkontext"]["technical_retentions"]["spans"] == []


def test_R3_referenz_bei_konflikt_im_text_maskiert():
    # Gleicher Text: Firmenstelle + Praepositions-Referenz -> Referenz maskiert, Stelle mit Bauteilbegriff frei
    t = "Die Firma KV-01 liefert. Der Kaltwasserverteiler KV-01 ist neu. Die Leitung fuehrt zu KV-01."
    ks = [(m.start(), m.end(), "ORGANIZATION", "detektor") for m in re.finditer("KV-01", t)]
    frei, ab = _ent(t, ks)
    assert frei == {(ks[1][0], ks[1][1]): "Kaltwasserverteiler"}
    assert sorted(x["grund"] for x in ab) == ["marker:Firma", "referenz-bei-konflikt-im-text"]


def test_R3_fachliste_ohne_marker_und_ohne_personennamen():
    for g in list(ak._BAUTEIL_KOPF) + sorted(ak._BAUTEIL_WORT):
        assert not ak.ist_marker(g), g
    for w in ("Brenner", "Hahn", "Schieber", "Mischer", "Sammler", "Halter", "Herr", "Firma", "Haus", "Pumpenhaus"):
        assert not ak.ist_bauteil(w), w
    for w in ("Kaltwasserverteiler", "Umluftkühler", "Serverraum-Umluftkuehler", "Umwälzpumpe", "Durchflussmesser",
              "Filter", "Schluckbrunnen", "Kreis", "Ruecklauf", "Brunnenkreis", "Verbraucher", "Erzeuger"):
        assert ak.ist_bauteil(w), w


# Vertrag technical_retentions (Root-Vorschlag version 1, Praezisierungen c9 1-6)

def _hp_check(r):
    """Nachbildung der Homepage-Pruefung (contract.js) MIT Retentions-Ausnahme: jedes freie Vorkommen eines
    mapping-Werts muss genau ein Retentions-Span sein."""
    out = r["smart_anonymized_text"]
    ret = r["postprocessing"]["anlagenkontext"]["technical_retentions"]
    import hashlib
    assert ret["version"] == 1 and ret["index_unit"] == "unicode_codepoint"
    assert ret["output_sha256"] == hashlib.sha256(out.encode("utf-8")).hexdigest()
    belegt = set()
    for s in ret["spans"]:
        assert out[s["start"]:s["end"]] == s["value"] and s["entity_type"] in ("ORGANIZATION", "LOCATION")
        belegt.add((s["start"], s["end"]))
    st = [s["start"] for s in ret["spans"]]
    assert st == sorted(st)
    for w in r["mapping"].values():
        for m in re.finditer(r"(?<![^\W_])" + re.escape(w) + r"(?![^\W_])", out):
            assert (m.start(), m.end()) in belegt, w
    return ret


def test_R3_retentions_exakt_mit_mehrbyte_und_trennern():
    t = ("Ümlaut-Zeile: Der Kältemaschinen-Kaltwasserverteiler KV-01 versorgt 😀 den Umluftkühler UK-01." + SEP +
         "Die Firma KV-01 liefert; Herr Bernd Quastenberg prüft den Filter FI-01.")
    tr = [("KV-01", "ORGANIZATION"), ("UK-01", "ORGANIZATION"), ("FI-01", "ORGANIZATION"),
          ("Bernd Quastenberg", "PERSON")]
    r = lauf(t, tr)
    ret = _hp_check(r)
    assert [s["value"] for s in ret["spans"]] == ["KV-01", "UK-01", "FI-01"]
    assert "Firma KV-01" not in r["smart_anonymized_text"] and maskiert(r, "Bernd Quastenberg")


def test_R3_retentions_fehlen_ohne_kontext():
    r = lauf("Der Kaltwasserverteiler KV-01 ist neu.", [("KV-01", "ORGANIZATION")], anlagenkontext=False)
    assert "anlagenkontext" not in r["postprocessing"]


def test_R3_retentions_failclosed_bei_inkonsistenz(monkeypatch):
    from src.privacy_postprocess import pipeline
    t = "Der Kaltwasserverteiler KV-01 ist neu."
    tr = [("KV-01", "ORGANIZATION")]
    monkeypatch.setattr(pipeline, "_technische_retentionen", lambda *a, **k: (None, "span-versatz"))
    r = lauf(t, tr)
    assert maskiert(r, "KV-01")
    assert r["postprocessing"]["anlagenkontext"] == {"aktiv": False, "abbruch": "span-versatz"}
    assert r["smart_anonymized_text"] == basis(t, tr)["smart_anonymized_text"]


def test_R3_retentions_gegenprobe_freies_vorkommen_ohne_span():
    from src.privacy_postprocess.pipeline import _technische_retentionen
    from src.privacy_postprocess.spans import Span, PRIO_DETEKTOR
    orig = "Firma KV-01 und KV-01."
    s = Span(6, 11, "ORGANIZATION", "detektor", PRIO_DETEKTOR, 1.0)
    s.platzhalter = "SN_ORGANIZATION_001"
    out = "Firma SN_ORGANIZATION_001 und KV-01."
    ret, abbruch = _technische_retentionen(orig, out, [s], {"SN_ORGANIZATION_001": "KV-01"}, [])
    assert ret is None and abbruch == "freies-vorkommen-ohne-span"


# ── R3b (Review R3 e8511fe6: P-PRV-6, P-PRV-7; Adapter-Abgleich '_') ──────────────────────────────────────────

@pytest.mark.parametrize("wort,kennung", [
    ("Freundeskreis", "B-02"), ("Bekanntenkreis", "B-02"), ("Kundenkreis", "KK-01"), ("Familienkreis", "FK-01"),
    ("Hauskreis", "HK-01"), ("Landkreis", "LK-12"), ("Wahlkreis", "WK-12"), ("Geschäftsleitung", "GL-01"),
    ("Bauleitung", "BL-01"), ("Projektleitung", "PL-01"), ("Handelsregister", "HRB-1234"),
    ("Firmenbuchregister", "FN-1234"), ("Kapitalanlage", "KA-01"), ("Geldanlage", "GA-01"),
    ("Vertriebskanal", "VK-01"), ("Verteiler", "VT-01"), ("Mailverteiler", "MV-01"), ("Geldtauscher", "GT-01"),
    ("Leitung", "L-1"), ("Register", "R-1"), ("Anlage", "A-1"), ("Kanal", "K-1"), ("Bankschalter", "BS-01"),
    ("Talkessel", "TK-01"), ("Landvermesser", "LV-01"), ("Erbsenzähler", "EZ-01"), ("Kreisverband", "KV-01"),
])
def test_R3b_P_PRV_6_mehrdeutige_grundwoerter_geben_nicht_frei(wort, kennung):
    t = f"Der {wort} {kennung} ist eingetragen."
    k = t.index(kennung)
    frei, ab = _ent(t, [(k, k + len(kennung), "ORGANIZATION", "detektor")])
    assert frei == {}, (wort, ab)
    assert not ak.ist_bauteil(wort), wort


@pytest.mark.parametrize("wort", [
    "Brunnenkreis", "Heizkreis", "Kältekreis", "Solekreis", "Serverraumkreis", "Kreis", "Rücklauf",
    "Rohrleitung", "Rücklaufleitung", "Steuerleitung", "Steigleitung", "Lüftungsanlage", "Kabelkanal",
    "Lüftungskanal", "Kühlregister", "Kaltwasserverteiler", "Wärmetauscher", "Schluckbrunnen", "Kältemaschine",
    "Rückflussverhinderer", "Umwälzpumpe", "Umwaelzpumpen", "Serverraum-Umluftkuehler", "Durchflussmesser",
    "Temperaturfühler", "Pufferspeicher", "Dreiwegeventil", "Filter", "Heizkreise", "Rohrleitungen",
])
def test_R3b_positive_komposita_und_koepfe(wort):
    assert ak.ist_bauteil(wort), wort


@pytest.mark.parametrize("t", [
    "Der Brunnen B-02, Bewohnerin des Hauses, meldet.", "Der Brunnen B-02, Familie Sonnwendt, meldet.",
    "Der Brunnen B-02 ist Patient der Praxis.", "Der Schluckbrunnen B-02 (Kollegin) prüft.",
    "Der Schluckbrunnen B-02 – Nachbar – meldet.", "Der Schluckbrunnen B-02 ist unsere Mieterin.",
    "Der Schluckbrunnen B-02: Bernd prüft.", "Der Schluckbrunnen B-02 als Vertreter der Eigentümer.",
])
def test_R3b_P_PRV_7_rolle_nach_kennung_positiv(t):
    k = t.index("B-02")
    frei, ab = _ent(t, [(k, k + 4, "LOCATION", "detektor")])
    assert frei == {}, t
    assert ab[0]["grund"].startswith("rolle-an-stelle:") or ab[0]["grund"].startswith("marker"), ab


@pytest.mark.parametrize("t", [
    "Der Pufferspeicher PS-01 (Wärmespeicher) liegt im Rücklauf.", "Der Schluckbrunnen B-02 ist dicht.",
    "Der Kaltwasserverteiler KV-01, Heizkreisverteiler, ist neu.", "KV-01;Kaltwasserverteiler;Verteilung;DN 100;",
    "T-03,Ruecklauf UK-01,Temperatur,", "| KV-01 | Kaltwasserverteiler | Familie Sonnwendt |",
    "Der Schluckbrunnen B-02 (2.000 l) ist dicht.", "Der Schluckbrunnen B-02 (DN 100) ist dicht.",
])
def test_R3b_P_PRV_7_bauteil_apposition_und_zellen_bleiben_frei(t):
    m = re.search(r"[A-Z]{1,4}-\d{1,4}(?= ist dicht| \(|,|;| \|)", t)
    k0, k1 = (m.start(), m.end()) if m and "UK-01" not in t else (t.index("UK-01"), t.index("UK-01") + 5)
    frei, ab = _ent(t, [(k0, k1, "ORGANIZATION", "detektor")])
    assert (k0, k1) in frei, (t, ab)


@pytest.mark.parametrize("t", ["Der Filter FI-01_alt ist neu.", "Der Filter _FI-01 ist neu.",
                               "Der Filter FI-01_ ist neu.", "Datei Filter_FI-01.pdf"])
def test_R3b_unterstrich_ist_keine_wortgrenze(t):
    # Adapter-Abgleich 451d80c (e8511fe6): '_' neben der Kennung -> maskiert (Backend == Adapter)
    k = t.index("FI-01")
    frei, _ = _ent(t, [(k, k + 5, "ORGANIZATION", "detektor")])
    assert frei == {}, t
    r = lauf(t + "\nDer Filter FI-01 ist neu.", [("FI-01", "ORGANIZATION")])
    out = r["smart_anonymized_text"]
    for s in r["postprocessing"]["anlagenkontext"]["technical_retentions"]["spans"]:
        assert out[s["start"] - 1:s["start"]] != "_" and out[s["end"]:s["end"] + 1] != "_"
    assert "FI-01_" not in out and "_FI-01" not in out


@pytest.mark.parametrize("wort", ["PUMPE", "Pumpe", "pumpe", "WÄRMETAUSCHER", "WAERMETAUSCHER", "Wärmetauscher",
                                  "wärmetauscher", "KALTWASSERVERTEILER", "SCHLUCKBRUNNEN", "HEIZKREIS",
                                  "ROHRLEITUNG", "UMWÄLZPUMPEN"])
def test_R3b_schreibweise_versal_klein(wort):
    # Root R3b-Check: Tabellenueberschriften in Versalien; Vergleich normalisiert gegen DIESELBE positive Liste
    assert ak.ist_bauteil(wort), wort
    # R3c: Zeilen ohne Grossschreibung sind nach der Kennung fail-closed (P-PRV-8); Probe deshalb mit Praeposition
    t = f"{wort} P-01 im Heizraum."
    frei, _ = _ent(t, [(t.index("P-01"), t.index("P-01") + 4, "ORGANIZATION", "detektor")])
    assert list(frei.values()) == [wort]


@pytest.mark.parametrize("wort", ["LANDKREIS", "landkreis", "FREUNDESKREIS", "GESCHÄFTSLEITUNG", "HANDELSREGISTER",
                                  "LEITUNG", "VERTEILER", "FIRMA", "FAMILIE", "BEWOHNER", "PuMpE", "Pumpe-Haus"])
def test_R3b_versal_keine_breitere_freigabe(wort):
    assert not ak.ist_bauteil(wort), wort
    t = f"{wort} LK-12 ist eingetragen."
    frei, _ = _ent(t, [(t.index("LK-12"), t.index("LK-12") + 5, "LOCATION", "detektor")])
    assert frei == {}, wort


@pytest.mark.parametrize("t", ["Der Brunnen B-02, BEWOHNERIN des Hauses, meldet.", "Der Brunnen B-02 ist PATIENT.",
                               "Der Brunnen B-02 (FAMILIE Sonnwendt) meldet."])
def test_R3b_rolle_danach_in_versalien(t):
    k = t.index("B-02")
    frei, _ = _ent(t, [(k, k + 4, "LOCATION", "detektor")])
    assert frei == {}, t



# ── R3c (Review FINAL e8511fe6: P-PRV-8 - Rolle nach beliebigem Trenner, jede einheitliche Schreibung) ─────────

@pytest.mark.parametrize("t", [
    "Brunnen B-02 - Herr Sonnwendt", "Brunnen B-02 / Familie Sonnwendt", "Brunnen B-02 - Mieterin",
    "Brunnen B-02 Mieterin", "Brunnen B-02 — Mieterin", "Brunnen B-02 [Mieterin]", "brunnen B-02, bewohnerin",
    "Brunnen B-02 – Bewohnerin des Hauses", "Brunnen B-02 :: Patient", "Brunnen B-02 ~ Kollegin",
    "Brunnen B-02 » Nachbarin", "Brunnen B-02 {Mieterin}", "Brunnen B-02 \"Mieterin\"", "Brunnen B-02 ist die Mieterin",
    "BRUNNEN B-02 - MIETERIN", "BRUNNEN B-02 BEWOHNERIN", "brunnen B-02 mieterin", "brunnen B-02 - herr sonnwendt",
    "brunnen B-02 ist bewohnerin", "Brunnen B-02 - Dr. Sonnwendt",
])
def test_R3c_P_PRV_8_rolle_nach_beliebigem_trenner(t):
    k = t.index("B-02")
    frei, ab = _ent(t, [(k, k + 4, "LOCATION", "detektor")])
    assert frei == {}, (t, ab)


@pytest.mark.parametrize("t", [
    "Der Schluckbrunnen B-02 ist dicht.", "Die Pumpe P-03 hat Ole Fennimann eingebaut.",
    "Der Schluckbrunnen B-02 - Heizkreis Nord.", "Der Schluckbrunnen B-02 [Schluckbrunnen] ist dicht.",
    "Der Schluckbrunnen B-02 / B-03 ist dicht.", "Der Schluckbrunnen B-02 (2.000 l) ist dicht.",
    "Der Schluckbrunnen B-02. Danach folgt Herr Sonnwendt.", "B-02;Schluckbrunnen;Familie Sonnwendt",
    "| B-02 | Schluckbrunnen | Familie Sonnwendt |", "Schluckbrunnen B-02\tFamilie Sonnwendt",
    "SCHLUCKBRUNNEN B-02 DN 100", "schluckbrunnen B-02 im heizraum", "Der Schluckbrunnen B-02 und der Filter FI-01.",
    "Der Schluckbrunnen B-02 gehört Bewohnerin Sonnwendt.",  # H-F-1 Verbbindung: frei (SEC-Entscheidpunkt)
])
def test_R3c_technische_fortsetzung_bleibt_frei(t):
    m = re.search(r"[A-Z]{1,4}-\d{1,4}", t)
    k0, k1 = m.span()
    frei, ab = _ent(t, [(k0, k1, "LOCATION", "detektor")])
    assert (k0, k1) in frei, (t, ab)


def test_R3c_genitiv_nach_artikel_maskiert():
    # H-F-1 Genitiv: durch das Ueberspringen des Artikels jetzt maskiert (strenger als 2ddac66, fail-closed)
    t = "Der Schluckbrunnen B-02 der Familie Sonnwendt ist dicht."
    k = t.index("B-02")
    frei, ab = _ent(t, [(k, k + 4, "LOCATION", "detektor")])
    assert frei == {} and ab[0]["grund"] == "rolle-an-stelle:Familie"
