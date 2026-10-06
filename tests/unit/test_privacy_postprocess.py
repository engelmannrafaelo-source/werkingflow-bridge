"""Tests der Pseudonymisierungs-Nachbearbeitung im Worker (src/privacy_postprocess).

Jeder Erkenner und jede Regel hat positive UND negative Faelle in deutsch/oesterreichischer
Schreibweise. Die Faelle stammen aus dem Audit vom 06.10.2026
(local-storage/pseudonymisierung-audit-20261006): Lecks L1-L18, Uebermaskierung Muehl/Weidenhof.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.privacy_postprocess import AlignmentError, PostprocessError, postprocess_smart_anonymize  # noqa: E402
from src.privacy_postprocess.recognizers import erkenne, svnr_pruefziffer_ok  # noqa: E402
from src.privacy_postprocess.rules import lade_freiliste, plausibilitaet  # noqa: E402
from src.privacy_postprocess.spans import spans_aus_antwort  # noqa: E402


# ── Hilfen ───────────────────────────────────────────────────────────────────

def dienst(text: str, treffer: List[Tuple[str, str]], prefix: str = "SN") -> Dict:
    """Simuliert die Antwort des Privacy-Dienstes: ersetzt jede (Wert, Typ)-Fundstelle
    (erstes Vorkommen, dann Backfill wie anonymizer.py) durch {prefix}_{TYP}_{NNN}."""
    mapping: Dict[str, str] = {}
    zaehler: Dict[str, int] = {}
    out = text
    for wert, typ in treffer:
        zaehler[typ] = zaehler.get(typ, 0) + 1
        ph = f"{prefix}_{typ}_{zaehler[typ]:03d}"
        mapping[ph] = wert
    for ph, wert in sorted(mapping.items(), key=lambda kv: len(kv[1]), reverse=True):
        out = re.sub(r"\b" + re.escape(wert) + r"\b", ph, out)
    mapping = {ph: w for ph, w in mapping.items() if ph in out}
    return {
        "status": "success", "anonymization_performed": True,
        "raw_anonymized_text": out, "raw_entity_count": len(mapping),
        "smart_anonymized_text": out, "smart_entity_count": len(mapping),
        "restored_entities": [], "mapping": mapping,
        "detected_entities": [
            {"placeholder": ph, "type": re.fullmatch(prefix + r"_(.+)_\d+", ph).group(1), "original": w,
             "confidence": 0.9, "decision": "KEEP", "reason": ""}
            for ph, w in mapping.items()
        ],
    }


def lauf(text: str, treffer: List[Tuple[str, str]] = (), prefix: str = "SN", **kw) -> Dict:
    return postprocess_smart_anonymize(text, dienst(text, list(treffer), prefix), prefix=prefix, **kw)


def regel(text: str) -> List[Tuple[str, str]]:
    return [(s.type, text[s.start:s.end]) for s in erkenne(text)]


def rueck(r: Dict) -> str:
    t = r["smart_anonymized_text"]
    for ph in sorted(r["mapping"], key=len, reverse=True):
        t = t.replace(ph, r["mapping"][ph])
    return t


# ── Rueckrechnung der Dienst-Antwort ─────────────────────────────────────────

class TestRueckrechnung:
    def test_spans_exakt(self):
        text = "Herr Klaus Reiter, Wien. Reiter grüßt Klaus Reiter."
        a = dienst(text, [("Klaus Reiter", "PERSON"), ("Wien", "LOCATION")])
        spans = spans_aus_antwort(text, a["smart_anonymized_text"], a["mapping"], "SN")
        assert [text[s.start:s.end] for s in spans] == ["Klaus Reiter", "Wien", "Klaus Reiter"]

    def test_widerspruch_bricht_laut_ab(self):
        with pytest.raises(AlignmentError):
            spans_aus_antwort("Hallo Anna", "Hallo SN_PERSON_001", {"SN_PERSON_001": "Berta"}, "SN")
        with pytest.raises(AlignmentError):
            spans_aus_antwort("Hallo Anna!", "Hallo SN_PERSON_001?", {"SN_PERSON_001": "Anna"}, "SN")

    def test_platzhalter_praefix_eindeutig(self):
        # SN_ORGANIZATION_100 ist Praefix von SN_ORGANIZATION_1000 — laengster gewinnt
        text = "A B"
        spans = spans_aus_antwort(text, "SN_ORGANIZATION_1000 SN_ORGANIZATION_100",
                                  {"SN_ORGANIZATION_100": "B", "SN_ORGANIZATION_1000": "A"}, "SN")
        assert [(s.start, s.end) for s in spans] == [(0, 1), (2, 3)]


# ── Regel-Erkenner ───────────────────────────────────────────────────────────

class TestGeburtsdatum:
    @pytest.mark.parametrize("text,wert", [
        ("Mag.ª Verena Hollaus, geb. 14.03.1989, SV-Nr.", "14.03.1989"),
        ("(geb. 27.08.1986, SV-Nr. 2519 270886)", "27.08.1986"),
        ("geboren am 2.11.1957 in Graz", "2.11.1957"),
        ("Geburtsdatum: 03.05.1978", "03.05.1978"),
        ("Geb.-Datum 03. 05. 1978", "03. 05. 1978"),
        ("geb. 14. März 1989", "14. März 1989"),
    ])
    def test_positiv(self, text, wert):
        assert ("GEBURTSDATUM", wert) in regel(text)

    @pytest.mark.parametrize("text", [
        "Ablesung bei Übergabe 30.06.2026",
        "Gebäude errichtet 12.03.1989",
        "Gebühr fällig 01.02.2026",
        "geb. Müller, verheiratet",
        "abgeb. 01.02.2026",
    ])
    def test_negativ(self, text):
        assert not [t for t in regel(text) if t[0] == "GEBURTSDATUM"]

    def test_filter_gibt_geburtsdatum_nicht_frei(self):
        # Der Dienst meldet das Geburtsdatum als Telefonnummer (Audit L3-L11)
        text = "Vermieter: Ing. Johann Prantner, geb. 02.11.1957, wohnhaft dort. Übergabe 30.06.2026."
        r = lauf(text, [("Johann Prantner", "PERSON"), ("02.11.1957", "PHONE_NUMBER"), ("30.06.2026", "PHONE_NUMBER")])
        assert "02.11.1957" not in r["smart_anonymized_text"]
        assert "SN_GEBURTSDATUM_001" in r["smart_anonymized_text"]
        assert "30.06.2026" in r["smart_anonymized_text"]  # gewoehnliches Datum bleibt Fachinhalt

    def test_geburtsdatum_ohne_kontext_an_anderer_stelle_mit(self):
        text = "Hollaus, geb. 14.03.1989. Tabelle: Hollaus | 14.03.1989 | Mieterin"
        r = lauf(text)
        assert "14.03.1989" not in r["smart_anonymized_text"]


class TestSvnr:
    @pytest.mark.parametrize("text,wert", [
        ("SV-Nr. 3742 140389", "3742 140389"),
        ("SV-Nr. 1685 021157, Rechbauerstraße", "1685 021157"),
        ("Sozialversicherungsnummer: 1234030578", "1234030578"),
        ("SVNR 1234 03 05 78", "1234 03 05 78"),
    ])
    def test_positiv(self, text, wert):
        assert ("SVNR", wert) in regel(text)

    def test_pruefziffer(self):
        assert svnr_pruefziffer_ok("1237 010180")
        assert not svnr_pruefziffer_ok("1685 021157")
        assert ("SVNR", "1237 010180") in regel("Versicherter 1237 010180 laut Bescheid")

    @pytest.mark.parametrize("text", ["Menge 1685 021157 Stück", "Zählerstand 4565 234958", "Konto 1234 567890"])
    def test_negativ_ohne_kontext_ohne_pruefziffer(self, text):
        assert not [t for t in regel(text) if t[0] == "SVNR"]

    def test_teilmaskierung_wird_vollstaendig(self):
        # Audit L2: der Dienst maskiert nur "1685"; der alte Filter machte es zu Klartext
        text = "Vermieter Prantner, SV-Nr. 1685 021157, Graz"
        r = lauf(text, [("Prantner", "PERSON"), ("1685", "PHONE_NUMBER")])
        assert "1685" not in r["smart_anonymized_text"] and "021157" not in r["smart_anonymized_text"]


class TestKennungen:
    @pytest.mark.parametrize("text,typ,wert", [
        ("office@ib-muster.at ATU 61 204 877", "UIDNR", "ATU 61 204 877"),
        ("UID ATU63218847 · FN", "UIDNR", "ATU63218847"),
        ("USt-IdNr. DE123456789", "UIDNR", "DE123456789"),
        ("Graz FN 512334 t · Verwaltungsobjekt", "FIRMENBUCH", "512334 t"),
        ("FN 298441k, Landesgericht Linz", "FIRMENBUCH", "298441k"),
        ("Liegenschaft EZ 2213, KG 63120 Andritz;", "GRUNDBUCH", "2213"),
        ("Liegenschaft EZ 2213, KG 63120 Andritz;", "GRUNDBUCH", "63120 Andritz"),
        ("Grundstück Nr. 412/3, EZ 1789", "GRUNDBUCH", "412/3"),
        ("Gst.-Nr. 1022/4 der KG", "GRUNDBUCH", "1022/4"),
        ("| AT00390000112044 | 44.812 kWh", "ZAEHLPUNKT", "AT00390000112044"),
        ("Zählpunkt AT0010000000000000001000004392417", "ZAEHLPUNKT", "AT0010000000000000001000004392417"),
        ("Wien Tel. 01 000 · GZ 2026-114 · Ausfertigung", "AKTENZEICHEN", "2026-114"),
        ("14.09.2021 · GZ: R-2021/338", "AKTENZEICHEN", "R-2021/338"),
        ("office@zthollaus.at GZ 26-041-SR · Linz", "AKTENZEICHEN", "26-041-SR"),
        ("Anlagenbehörde, GZ A17-BAB-045612/2026-3;", "AKTENZEICHEN", "A17-BAB-045612/2026-3"),
        ("Bezirksgericht Graz-Ost, 12 C 345/26k.", "AKTENZEICHEN", "12 C 345/26k"),
        ("Geschäftszahl: 4 Ob 123/25m", "AKTENZEICHEN", "4 Ob 123/25m"),
        ("Kennzeichen des Firmenfahrzeugs G-123 AB.", "KENNZEICHEN", "G-123 AB"),
        ("amtl. Kennzeichen W 12345 X", "KENNZEICHEN", "W 12345 X"),
        ("Steuernummer 68 123/4567.", "STEUERNR", "68 123/4567"),
        ("Konto AT21 2081 5000 0402 8817 Mietverhältnis", "IBAN_CODE", "AT21 2081 5000 0402 8817"),
    ])
    def test_positiv(self, text, typ, wert):
        assert (typ, wert) in regel(text)

    @pytest.mark.parametrize("text,typ", [
        ("Prüfung nach ÖNORM EN 12101-6 und EN 1434", "KENNZEICHEN"),
        ("Messwert 2213 kWh und 412/3 Teile", "GRUNDBUCH"),
        ("Die KG hat 3 Gesellschafter", "GRUNDBUCH"),
        ("IBAN AT61 1904 3002 3457 3201", "ZAEHLPUNKT"),
        ("BGBl. II Nr. 164/2020", "AKTENZEICHEN"),
        ("Brutto 1.234,56 EUR, Zeitraum 2026-114", "AKTENZEICHEN"),
        ("Taupunkt 9,8 °C, MP1–MP4", "AKTENZEICHEN"),
        ("Gruppe FN-A, Variante FN 3", "FIRMENBUCH"),
        ("Fahrzeug fährt 50 km/h", "KENNZEICHEN"),
    ])
    def test_negativ(self, text, typ):
        assert not [t for t in regel(text) if t[0] == typ], regel(text)


class TestTelefonMitSchraegstrich:
    @pytest.mark.parametrize("text,wert", [
        ("Tel. 0316/482917, mobil", "0316/482917"),
        ("mobil 0664/5512398,", "0664/5512398"),
        ("Fax: +43 316/48 29 18", "+43 316/48 29 18"),
        ("erreichbar unter 0732/774190 werktags", "0732/774190"),
    ])
    def test_positiv(self, text, wert):
        assert ("PHONE_NUMBER", wert) in regel(text)

    @pytest.mark.parametrize("text", ["GZ 164/2020", "Verhältnis 68/2410", "Abschnitt 2026/27", "Seite 012/2026"])
    def test_negativ(self, text):
        assert not [t for t in regel(text) if t[0] == "PHONE_NUMBER"]


class TestAdresse:
    def test_ganzer_bereich_aus_strassen_fundstelle(self):
        text = "Wohnung Almweg 17, Stiege 2, Top 11, 8045 Graz (66,4 m²)"
        r = lauf(text, [("Almweg", "LOCATION"), ("Graz", "LOCATION")])
        assert r["smart_anonymized_text"] == "Wohnung SN_LOCATION_001 (66,4 m²)"
        assert r["mapping"]["SN_LOCATION_001"] == "Almweg 17, Stiege 2, Top 11, 8045 Graz"

    @pytest.mark.parametrize("text,wert", [
        ("Mietobjekt: Almweg 17/2/11, 8045 Graz (EZ", "Almweg 17/2/11, 8045 Graz"),
        ("Beispielgasse 41/12, 1090 Wien Tel.", "Beispielgasse 41/12, 1090 Wien"),
        ("Familie Brandstetter Leondinger Straße 22 4060 Leonding\n", "Leondinger Straße 22 4060 Leonding"),
        ("Sonnenhofstraße 12-16, 1120 Wien\n\nGZ", "Sonnenhofstraße 12-16, 1120 Wien"),
    ])
    def test_erkenner_ohne_detektor(self, text, wert):
        r = lauf(text)
        assert wert in r["mapping"].values(), r

    def test_fachbegriff_mit_nummer_ist_keine_adresse(self):
        text = "Sondenfeld 2, 4 Sonden je 120 m. SONDENFELD 3, 8045 Graz"
        r = lauf(text)
        assert "Sondenfeld 2" in r["smart_anonymized_text"] and "SONDENFELD 3" in r["smart_anonymized_text"]

    def test_keine_adresse_ohne_hausnummer(self):
        text = "Ablagerung am Rautenweg (öffentliche Deponie)"
        r = lauf(text, [("Rautenweg", "LOCATION")])
        assert r["mapping"] == {"SN_LOCATION_001": "Rautenweg"}

    def test_stadt_allein_bleibt_wie_erkannt(self):
        r = lauf("Termin in Graz, danach Linz.", [("Graz", "LOCATION"), ("Linz", "LOCATION")])
        assert set(r["mapping"].values()) == {"Graz", "Linz"}


# ── Plausibilitaet (Port von entity-plausibility.ts + Audit) ─────────────────

class TestPlausibilitaet:
    @pytest.mark.parametrize("wert,typ,grund", [
        ("-", "ORGANIZATION", "no-letters"),
        ("11", "LOCATION", "no-letters"),
        ("EG", "ORGANIZATION", "too-short"),
        ("WP3", "PERSON", "person-with-digit"),
        ("1685", "PHONE_NUMBER", "phone-too-few-digits"),
        ("03.08.2026", "PHONE_NUMBER", "phone-is-date"),
        ("03.08.2026 0535", "PHONE_NUMBER", "phone-is-date"),
        ("03.08.2026 05", "PHONE_NUMBER", "phone-is-date"),
        ("22.07.2026 10", "PHONE_NUMBER", "phone-is-date"),
        ("03.08.2026 3.982.387", "PHONE_NUMBER", "phone-is-date"),
        ("02.06.2026 - 27.06.2026", "PHONE_NUMBER", "phone-is-date-range"),
        ("360.000", "PHONE_NUMBER", "phone-is-number"),
        ("397.514", "PHONE_NUMBER", "phone-is-number"),
        ("2.222–282.504", "PHONE_NUMBER", "phone-is-range"),
        ("164/2020", "PHONE_NUMBER", "phone-is-citation"),
        ("1685 021157", "PHONE_NUMBER", "phone-without-prefix"),
        ("7.2.6.3", "IP_ADDRESS", "ip-is-section-number"),
        ("PO WER ED B Y", "ORGANIZATION", "letter-spacing-artifact"),
        ("ORG_P1827_E71a187_BV", "ORGANIZATION", "nested-token"),
    ])
    def test_verwerfen(self, wert, typ, grund):
        assert plausibilitaet(wert, typ) == grund

    @pytest.mark.parametrize("wert,typ", [
        ("0316 482918", "PHONE_NUMBER"),
        ("+43 (0)676 2219034", "PHONE_NUMBER"),
        ("0664 2833917", "PHONE_NUMBER"),
        ("KONE", "ORGANIZATION"),
        ("BIG", "ORGANIZATION"),
        ("Klaus Reiter", "PERSON"),
        ("10.0.0.12", "IP_ADDRESS"),
        ("Graz", "LOCATION"),
    ])
    def test_behalten(self, wert, typ):
        assert plausibilitaet(wert, typ) is None

    def test_bindestrich_bleibt_bindestrich(self):
        text = "Sondenfeld-Regeneration und Normjahr-Wert, Taupunkt -3,5 °C"
        r = lauf(text, [("-", "ORGANIZATION")])
        assert r["smart_anonymized_text"] == text and r["mapping"] == {}

    def test_fundstelle_in_url_wird_verworfen(self):
        text = "Programm SMART. Quelle: https://www.wohnfonds.wien.at/smart; Ende"
        r = lauf(text, [("SMART", "ORGANIZATION"), ("smart", "ORGANIZATION")])
        assert "https://www.wohnfonds.wien.at/smart;" in r["smart_anonymized_text"]
        assert rueck(r) == text

    def test_gz_fragment_als_telefon_wird_ganze_geschaeftszahl(self):
        text = "Anlagenbehörde, GZ A17-BAB-045612/2026-3; weiter"
        r = lauf(text, [("045612/2026-3", "PHONE_NUMBER")])
        assert r["smart_anonymized_text"] == "Anlagenbehörde, GZ SN_AKTENZEICHEN_001; weiter"

    def test_wortteil_vor_und(self):
        text = "Magistrat Graz, Bau- und Anlagenbehörde, Abt. 17"
        r = lauf(text, [("Bau", "ORGANIZATION"), ("Anlagenbehörde", "ORGANIZATION")])
        assert "Bau- und Anlagenbehörde" in r["smart_anonymized_text"]


# ── Fach-Freiliste ───────────────────────────────────────────────────────────

class TestFreiliste:
    @pytest.mark.parametrize("wert,typ", [
        ("WP1", "ORGANIZATION"), ("COP", "ORGANIZATION"), ("JAZ", "ORGANIZATION"), ("Sondenfeld", "LOCATION"),
        ("Sondenfeld", "PERSON"), ("ÖNORM EN", "ORGANIZATION"), ("VDI", "ORGANIZATION"), ("VDI 4650", "ORGANIZATION"),
        ("DBA SH1", "ORGANIZATION"), ("BMA", "ORGANIZATION"), ("TRVB 112 S", "ORGANIZATION"),
        ("OIB-Richtlinie 2", "ORGANIZATION"), ("Wien Hohe Warte", "LOCATION"), ("WIEN HOHE WARTE", "LOCATION"),
        ("Statistik Austria", "ORGANIZATION"), ("GeoSphere Austria", "ORGANIZATION"),
        ("kw-en-1434-1-waermezaehler", "ORGANIZATION"), ("ABGB", "ORGANIZATION"), ("Anlagenbehörde", "ORGANIZATION"),
        ("https://www.statistik.at/statistiken/energie", "URL"), ("wohnfonds.wien.at", "URL"),
        ("Stiegenhaus", "LOCATION"), ("KENNGRÖSSE", "ORGANIZATION"), ("ZEITRAUM", "ORGANIZATION"),
        ("BEH008", "ORGANIZATION"), ("TU Braunschweig", "ORGANIZATION"), ("PVGIS Wien", "LOCATION"),
        ("oa-montero-2022-w4285014502", "ORGANIZATION"), ("SONDENFELD", "LOCATION"), ("SONDENFELD 2", "LOCATION"),
    ])
    def test_frei(self, wert, typ):
        assert lade_freiliste().ist_frei(wert, typ), (wert, typ)

    @pytest.mark.parametrize("wert,typ", [
        ("Hochbau Steiner GmbH", "ORGANIZATION"), ("Kamstrup", "ORGANIZATION"), ("Graz", "LOCATION"),
        ("Andreas Muster", "PERSON"), ("Velmaro Fenstersysteme GmbH", "ORGANIZATION"),
        ("https://www.ib-muster.at/team", "URL"), ("WP Huber", "PERSON"), ("COP", "EMAIL_ADDRESS"),
        ("Musterbau GmbH", "ORGANIZATION"), ("Podhagskygasse", "LOCATION"), ("Cop", "PERSON"),
    ])
    def test_nicht_frei(self, wert, typ):
        assert not lade_freiliste().ist_frei(wert, typ), (wert, typ)

    def test_konfigurierbar_per_env(self, tmp_path, monkeypatch):
        p = tmp_path / "liste.json"
        p.write_text('{"woerter": ["Kesselhaus"], "phrasen": [], "muster": [], "url_domains": []}', encoding="utf-8")
        lade_freiliste.cache_clear()
        monkeypatch.setenv("BRIDGE_PSEUDONYM_ALLOWLIST_PATH", str(p))
        try:
            fl = lade_freiliste()
            assert fl.ist_frei("Kesselhaus", "LOCATION") and not fl.ist_frei("COP", "ORGANIZATION")
        finally:
            monkeypatch.delenv("BRIDGE_PSEUDONYM_ALLOWLIST_PATH")
            lade_freiliste.cache_clear()

    def test_kaputte_liste_bricht_laut_ab(self, tmp_path):
        p = tmp_path / "kaputt.json"
        p.write_text('{"woerter": "COP"}', encoding="utf-8")
        with pytest.raises(ValueError):
            lade_freiliste(str(p))

    def test_anlagenbezeichnung_bleibt_lesbar(self):
        text = "|   | DBA SH1 (Nord) | DBA SH2 (Süd) | Auslösung | BMA, Handtaster Feuerwehr EG |"
        r = lauf(text, [("DBA SH1", "ORGANIZATION"), ("DBA SH2", "ORGANIZATION"), ("BMA", "ORGANIZATION"), ("EG", "ORGANIZATION")])
        assert r["smart_anonymized_text"] == text


# ── Vereinheitlichung ────────────────────────────────────────────────────────

class TestVereinheitlichung:
    def test_ein_platzhalter_je_wert(self):
        text = "Verena Hollaus übergibt. Unterschrift Verena Hollaus. Graz, Graz."
        a = dienst(text, [("Verena Hollaus", "PERSON"), ("Verena Hollaus", "PERSON"), ("Graz", "LOCATION"), ("Graz", "LOCATION")])
        r = postprocess_smart_anonymize(text, a, prefix="SN")
        assert r["mapping"] == {"SN_PERSON_001": "Verena Hollaus", "SN_LOCATION_001": "Graz"}
        assert r["smart_anonymized_text"].count("SN_PERSON_001") == 2

    def test_nachname_auf_vollform(self):
        text = "Prüfer DI Andreas Muster. Herr DI Muster bestätigt."
        r = lauf(text, [("Andreas Muster", "PERSON"), ("Muster", "PERSON")])
        assert r["mapping"] == {"SN_PERSON_001": "Andreas Muster"}
        assert r["smart_anonymized_text"] == "Prüfer DI SN_PERSON_001. Herr DI SN_PERSON_001 bestätigt."

    def test_nachname_mehrdeutig_wird_nicht_gefaltet(self):
        text = "Verena Hollaus und Daniel Hollaus; Hollaus unterschreibt."
        r = lauf(text, [("Verena Hollaus", "PERSON"), ("Daniel Hollaus", "PERSON"), ("Hollaus", "PERSON")])
        assert "Hollaus" in r["mapping"].values()

    def test_firmenkern(self):
        text = "AN Hochbau Steiner GmbH. Die Steiner GmbH rechnet. VELMARO und Velmaro Fenstersysteme GmbH."
        r = lauf(text, [("AN Hochbau Steiner GmbH", "ORGANIZATION"), ("Steiner GmbH", "ORGANIZATION"),
                        ("VELMARO", "ORGANIZATION"), ("Velmaro Fenstersysteme GmbH", "ORGANIZATION")])
        werte = sorted(r["mapping"].values())
        assert werte == ["Hochbau Steiner GmbH", "Velmaro Fenstersysteme GmbH"]
        assert r["smart_anonymized_text"].startswith("AN SN_ORGANIZATION_001.")

    def test_halbwidl_rechtsform_allein_zieht_namen_mit(self):
        # Audit L1: nur "ZT GmbH" erkannt, "Halbwidl" blieb Klartext
        text = "- Brandschutzkonzept BSK 17-042, Rev. C, Brandschutzplanung Halbwidl ZT GmbH"
        r = lauf(text, [("ZT GmbH", "ORGANIZATION")])
        assert "Halbwidl" not in r["smart_anonymized_text"]
        assert r["mapping"] == {"SN_ORGANIZATION_001": "Halbwidl ZT GmbH"}

    def test_bekannte_entitaeten_des_akts(self):
        # Dokument 2 eines Akts: der Detektor findet "Halbwidl ZT GmbH" und "Andritz" hier nicht
        text = "Verfasser Brandschutzplanung Halbwidl ZT GmbH, KG Andritz."
        bekannt = {"ENGDOCAAAA_ORGANIZATION_004": "Halbwidl ZT GmbH", "ENGDOCAAAA_LOCATION_002": "Andritz"}
        r = lauf(text, [], prefix="ENGDOCBBBB", known_entities=bekannt)
        assert r["smart_anonymized_text"] == "Verfasser Brandschutzplanung ENGDOCAAAA_ORGANIZATION_004, KG ENGDOCAAAA_LOCATION_002."
        assert r["mapping"] == bekannt

    def test_bekannte_variante_und_neue_nummern_kollidieren_nicht(self):
        text = "Herr Esterl prüft. Frau Lindtner."
        bekannt = {"SN_PERSON_001": "Rupert Esterl"}
        r = lauf(text, [("Esterl", "PERSON"), ("Lindtner", "PERSON")], known_entities=bekannt)
        assert r["mapping"]["SN_PERSON_001"] == "Rupert Esterl"
        assert r["mapping"]["SN_PERSON_002"] == "Lindtner"

    def test_gross_klein_bleibt_getrennt_damit_rueckweg_treu(self):
        text = "SONDERPLAN Huberhof und Sonderplan Huberhof"
        r = lauf(text, [("SONDERPLAN Huberhof", "ORGANIZATION"), ("Sonderplan Huberhof", "ORGANIZATION")])
        assert rueck(r) == text

    def test_rueckweg_exakt_ohne_varianten(self):
        text = ("Auftraggeberin Mag. Karin Feichtinger, geb. 03.05.1978, SV-Nr. 1234 030578, wohnhaft Hauptstraße 12/3/7, "
                "8010 Graz. Tel. 0316/482917. UID ATU12345678, FN 445566 w. Wärmepumpe WP1 mit COP 4,1 nach VDI 4650.")
        r = lauf(text, [("Karin Feichtinger", "PERSON"), ("03.05.1978", "PHONE_NUMBER"), ("1234 030578", "PHONE_NUMBER"),
                        ("Hauptstraße", "LOCATION"), ("Graz", "LOCATION"), ("WP1", "ORGANIZATION"), ("COP", "ORGANIZATION"),
                        ("VDI", "ORGANIZATION")])
        assert rueck(r) == text
        t = r["smart_anonymized_text"]
        for leck in ("Feichtinger", "03.05.1978", "030578", "Hauptstraße", "482917", "ATU12345678", "445566"):
            assert leck not in t
        for frei in ("WP1", "COP 4,1", "VDI 4650", "geb.", "SV-Nr.", "FN "):
            assert frei in t

    def test_deterministisch(self):
        text = "Herr Klaus Reiter, Almweg 17, 8045 Graz, geb. 01.02.1960"
        a = lauf(text, [("Klaus Reiter", "PERSON"), ("Almweg", "LOCATION")])
        b = lauf(text, [("Klaus Reiter", "PERSON"), ("Almweg", "LOCATION")])
        assert a == b


# ── Invarianten und Vertrag ──────────────────────────────────────────────────

class TestVertrag:
    def test_antwortform_bleibt(self):
        r = lauf("Herr Klaus Reiter", [("Klaus Reiter", "PERSON")])
        for k in ("status", "anonymization_performed", "smart_anonymized_text", "mapping", "detected_entities",
                  "raw_anonymized_text", "smart_entity_count", "postprocessing"):
            assert k in r
        assert r["status"] == "success" and r["anonymization_performed"] is True
        assert r["postprocessing"]["version"]
        assert "Klaus Reiter" not in str(r["postprocessing"])  # wertfrei

    def test_fehlerantwort_unveraendert(self):
        a = {"status": "error", "error": "x"}
        assert postprocess_smart_anonymize("t", a, prefix="SN") is a

    def test_leeres_mapping_aber_regel_treffer(self):
        r = postprocess_smart_anonymize(
            "UID ATU12345678", dienst("UID ATU12345678", []), prefix="SN")
        assert r["smart_anonymized_text"] == "UID SN_UIDNR_001"

    def test_neue_typen_sind_kurz_genug_fuer_kanonische_tokens(self):
        # TS canonical-token: {TYPE}_P{4}_E{6}_{2} <= 32 Zeichen, auch ohne Typ-Code-Tabelle
        for typ in ("GEBURTSDATUM", "SVNR", "UIDNR", "FIRMENBUCH", "GRUNDBUCH", "ZAEHLPUNKT", "AKTENZEICHEN",
                    "KENNZEICHEN", "STEUERNR"):
            assert re.fullmatch(r"[A-Z]+", typ) and len(typ) + 17 <= 32

    def test_unbekanntes_bekannt_format_bricht_laut_ab(self):
        with pytest.raises(PostprocessError):
            lauf("Anna", [], known_entities={"anna-1": "Anna"})
