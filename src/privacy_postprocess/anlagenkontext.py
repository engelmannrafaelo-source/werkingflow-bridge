"""Anlagenkennungen im belegten Anlagenkontext (Schema-Generator, c9a50d3b, 17.11.2026).

Anlass: E2E 7K3Q — Flair-NER (geteilter Privacy-Dienst, kein Kontextkanal) wertet technische
Anlagenkennungen wie "Kaltwasserverteiler KV-01" oder "Schluckbrunnen B-02" als ORGANIZATION
bzw. LOCATION (Score bis 0,999). Eine Schwelle loest das nicht, und der Dienst ist mit Prod geteilt.

Regel R3 (Rafael/SEC 17.11.2026 ~20:30Z: "Technische Anlagenkennungen muessen nicht pseudonymisiert werden";
positive Bindung je Stelle, keine offene Substantiv-Sperrliste, kein Modell). Eine Detektor-Fundstelle bleibt an
GENAU DIESER Stelle Klartext, wenn ALLES gilt:
  1. Aufruf serverseitig als Schema-App verifiziert, ``context_hint == ANLAGENKONTEXT`` und
     ``BRIDGE_PRIVACY_ANLAGENKENNUNG=true`` (Standard aus) - geprueft in main.py (kontext_verifiziert);
  2. Quelle Detektor, Typ ORGANIZATION/LOCATION an allen Stellen des Werts (harte Typen nie);
  3. Kennungsform ``[A-Z]{1,4}-[0-9]{1,4}``, nicht eingebettet (Mail, URL, Pfad, Wortteil);
  4. POSITIVER technischer Beleg an der Stelle: Bauteilbegriff aus der Fachliste (``ist_bauteil``) direkt davor
     bzw. am Kopf der Aufzaehlung oder als Nachbarzelle ("Durchflussmesser F-01", "F-01;Durchflussmesser;...") -
     oder eine Praeposition als Querverweis ("zwischen KV-01 und WT-01"), wenn der Wert im Aufruf irgendwo als
     Bauteil definiert ist (Hinweis, gibt allein nie frei);
  5. keine harte PII ueberlappend oder direkt angrenzend; kein Rollen-/Organisations-/Ortsmarker gebunden an DIESE
     Stelle (davor: "Firma KV-01", danach: "B-02, Ansprechpartner ..."). Personen/Kontakte anderswo im Satz oder in
     anderen Tabellenzellen sperren NICHT - sie werden selbst maskiert.
Sonst bleibt die Stelle maskiert; ungemeldete unklare Stellen werden zusaetzlich maskiert. Gleicher Wert technisch
und als Firma/Person: je Stelle getrennt; Querverweise (Praeposition) eines solchen Werts im selben Text bleiben
maskiert. Freie Stellen weist pipeline.py als ``technical_retentions`` (exakte Ausgabe-Spans) aus.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

ANLAGENKONTEXT = "technical_system_description"
FLAG_ENV = "BRIDGE_PRIVACY_ANLAGENKENNUNG"
# Serverseitig bei POST /v1/jobs gesetzt (src/jobs/routes.py), nie aus dem Rumpf uebernommen.
JOB_SCHEMA_VERIFIZIERT_KEY = "schema_app_principal_verifiziert"
GRUND = "anlagenkennung-im-anlagenkontext"
WEICHE_TYPEN = frozenset({"ORGANIZATION", "LOCATION"})

_KENNUNG = re.compile(r"[A-Z]{1,4}-\d{1,4}")
# Bauteilwort in Normalschreibung: Grossbuchstabe, dann nur Kleinbuchstaben (Umlaute, ß, Binnenbindestrich)
_SUBSTANTIV = re.compile(r"[A-ZÄÖÜ][a-zäöüß]{3,}(?:-[A-ZÄÖÜa-zäöüß][a-zäöüß]+)*")
# Zeichen, die eine Kennung in eine groessere Einheit einbetten (Mail, URL, Pfad, Wortteil)
_EINBETTUNG = set("@._/\\-:#&+=")

# Rollen-, Personen- und Organisationsmarker. KEINE Liste von Werten (keine Kennung, kein Name,
# kein Ort steht hier), sondern Grundwoerter, die eine nachfolgende Bezeichnung als Rolle,
# Organisation oder Ort ausweisen. Geprueft wird das Grundwort (deutsches Kompositum: letzter Teil,
# casefold, endswith): "Kontaktperson", "Auftragnehmerfirma", "Standort" sind Marker,
# "Kaltwasserverteiler", "Transportpumpe" nicht. Ein Treffer verhindert die Herabstufung.
_MARKER_GRUNDWORTE = (
    "firma", "unternehmen", "gesellschaft", "betrieb", "konzern", "hersteller", "lieferant",
    "auftragnehmer", "auftraggeber", "bauherr", "betreiber", "eigentümer", "eigentuemer", "inhaber",
    "verwaltung", "behörde", "behoerde", "amt", "gemeinde", "stadt", "ort", "standort", "adresse",
    "straße", "strasse", "gasse", "platz", "weg",
    "person", "kontakt", "ansprech", "herr", "frau", "mitarbeiter", "kunde", "mieter", "nutzer",
    "leiter", "planer", "prüfer", "pruefer", "gutachter", "techniker", "monteur", "ingenieur",
    "büro", "buero", "kanzlei", "team", "abteilung", "partner",
    # adressaehnliche Kennungen (Review R1 P-PRV-3): "Wohnung W-12", "Haus B-02", "Stiege A-2", "Postfach PF-12"
    # bleiben maskiert. Kosten: echte Bauteile wie "Pumpenhaus PH-01" bleiben fail-closed maskiert.
    "wohnung", "haus", "stiege", "stock", "etage", "geschoss", "top", "tür", "tuer", "zimmer", "postfach",
    "hausnummer", "objekt", "liegenschaft", "parzelle", "grundstück", "grundstueck",
)
# R3 (Rafael/SEC 17.11.2026 ~20:30Z): POSITIVE technische Fachliste. Eine Bauteil-Definition liegt NUR vor, wenn das
# gebundene Wort (direkt davor bzw. Nachbarzelle) in dieser Liste steht - nicht "jedes grossgeschriebene Substantiv"
# (Review R2 P-PRV-4). Unbekannte Woerter belegen nichts (fail-closed). Marker haben Vorrang (ist_marker).
# R3b (Review R3 P-PRV-6 / SEC H-R3-1): KEIN Wortende-Treffer fuer mehrdeutige Grundwoerter ("Freundeskreis",
# "Landkreis", "Geschaeftsleitung", "Handelsregister", "Kapitalanlage", "Vertriebskanal" ...). Drei Klassen:
#   1. _BAUTEIL_KOPF: Grundwoerter, die als Kompositum-Kopf nur Technik bilden -> beliebiges Kompositum
#      ("Umwaelzpumpe", "Umluftkuehler"); keines davon bildet ein Personen-/Gruppen-/Haushalts-/Ortswort;
#   2. _BAUTEIL_WORT: ganze Woerter (inkl. definierter Komposita) - mehrdeutige Grundwoerter NUR hier
#      ("Heizkreis", "Rohrleitung", "Lueftungsanlage", "Kaltwasserverteiler"); "Leitung", "Register", "Anlage",
#      "Kanal", "Verteiler", "Tauscher", "Schalter" allein sind bewusst NICHT enthalten;
#   3. alles andere: kein Bauteil.
# Bewusst gar nicht aufgenommen (Familienname/Personenwort): brenner, hahn, schieber, mischer, sammler, halter.
# Schreibweise: casefold, ae/oe/ue/ss-normalisiert, Bindestriche entfernt (bzw. letzter Bindestrich-Teil).
_BAUTEIL_KOPF = (
    "pumpe", "ventil", "speicher", "behaelter", "gefaess", "sonde", "kollektor", "absorber", "filter", "abscheider",
    "fuehler", "sensor", "kuehler", "erhitzer", "verdichter", "kompressor", "verdampfer", "verfluessiger",
    "kondensator", "luefter", "ventilator", "geblaese", "heizkoerper", "heizstab", "wechselrichter", "umrichter",
    "transformator", "schaltschrank", "aggregat", "druckhaltung", "durchflussmesser", "mengenmesser", "druckmesser",
    "boiler",
)
_BAUTEIL_WORT = frozenset("""
kreis kreislauf brunnen maschine geraet modul steuerung weiche rohr vorlauf ruecklauf zaehler regler motor puffer
kessel klappe messstelle messpunkt antrieb trafo verbraucher erzeuger waermeerzeuger kaelteerzeuger
heizkreis heizungskreis kuehlkreis kaeltekreis kaeltemittelkreis solekreis brunnenkreis ladekreis entladekreis
verbraucherkreis erzeugerkreis primaerkreis sekundaerkreis wasserkreis kaltwasserkreis warmwasserkreis
kuehlwasserkreis heizwasserkreis glykolkreis solarkreis pufferkreis speicherladekreis lueftungskreis regelkreis
stromkreis serverraumkreis umluftkreis
heizkreislauf kuehlkreislauf kaeltekreislauf brunnenkreislauf wasserkreislauf solekreislauf
rohrleitung vorlaufleitung ruecklaufleitung steigleitung hauptleitung zuleitung verbindungsleitung anschlussleitung
kaeltemittelleitung kondensatleitung gasleitung wasserleitung trinkwasserleitung heizungsleitung fernwaermeleitung
steuerleitung druckleitung saugleitung soleleitung brunnenleitung stromleitung datenleitung abwasserleitung
entleerungsleitung bypassleitung
kuehlregister heizregister erhitzerregister waermeregister nacherhitzerregister vorerhitzerregister
lueftungsanlage klimaanlage heizungsanlage heizanlage kaelteanlage kuehlanlage photovoltaikanlage
solaranlage solarthermieanlage druckerhoehungsanlage enthaertungsanlage wasseraufbereitungsanlage
brunnenanlage geothermieanlage sprinkleranlage brandmeldeanlage waermepumpenanlage entrauchungsanlage hebeanlage
abwasserhebeanlage druckhalteanlage neutralisationsanlage filteranlage notstromanlage
kabelkanal lueftungskanal luftkanal abluftkanal zuluftkanal fortluftkanal aussenluftkanal abgaskanal
installationskanal
heizstrang heizungsstrang steigstrang rohrstrang kabelstrang verteilstrang lueftungsstrang wasserstrang
kaltwasserverteiler warmwasserverteiler heizungsverteiler heizkreisverteiler kuehlwasserverteiler
kaeltewasserverteiler fussbodenheizungsverteiler hauptverteiler unterverteiler stromverteiler
niederspannungsverteiler soleverteiler brunnenverteiler
niederspannungsverteilung hauptverteilung unterverteilung waermeverteilung kaelteverteilung luftverteilung
stromverteilung
waermetauscher plattenwaermetauscher plattentauscher rohrbuendelwaermetauscher kreuzstromwaermetauscher
gegenstromwaermetauscher waermeuebertrager plattenwaermeuebertrager
schluckbrunnen foerderbrunnen saugbrunnen entnahmebrunnen rueckgabebrunnen infiltrationsbrunnen
grundwasserbrunnen versickerungsbrunnen beobachtungsbrunnen
leistungsschalter lasttrennschalter hauptschalter notausschalter druckschalter stroemungsschalter
schwimmerschalter temperaturschalter endschalter
heizkessel brennwertkessel gaskessel oelkessel pelletkessel dampfkessel niedertemperaturkessel spitzenlastkessel
brandschutzklappe rueckschlagklappe luftklappe regelklappe absperrklappe jalousieklappe aussenluftklappe
fortluftklappe mischluftklappe
rueckflussverhinderer
kaeltemaschine absorptionskaeltemaschine kompressionskaeltemaschine
waermemengenzaehler waermezaehler kaeltemengenzaehler kaeltezaehler stromzaehler wasserzaehler gaszaehler
volumenzaehler betriebsstundenzaehler
volumenstromregler temperaturregler druckregler differenzdruckregler raumregler heizungsregler durchflussregler
stellmotor pumpenmotor antriebsmotor elektromotor
solarmodul photovoltaikmodul
heizungssteuerung anlagensteuerung pumpensteuerung lueftungssteuerung
lueftungsgeraet klimageraet kaeltegeraet rltgeraet zentralgeraet messgeraet steuergeraet regelgeraet heizgeraet
heizungsrohr kupferrohr stahlrohr kunststoffrohr vorlaufrohr ruecklaufrohr abgasrohr sondenrohr schutzrohr
heizungsvorlauf heizungsruecklauf brunnenvorlauf brunnenruecklauf
""".split())
_FLEXION = ("", "e", "n", "en", "s", "es", "er", "ern")


def _norm(w: str) -> str:
    return (w.casefold().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss"))


def ist_bauteil(wort: str) -> bool:
    """Positive Fachliste (R3b): Kopf-Grundwort beliebig komponiert, sonst nur ganzes/definiertes Wort; Marker gehen vor."""
    if not wort or not _SUBSTANTIV.fullmatch(wort) or ist_marker(wort):
        return False
    n = _norm(wort)
    for c in {n.replace("-", ""), n.rsplit("-", 1)[-1]}:
        for e in _FLEXION:
            if e and not c.endswith(e):
                continue
            basis = c[: len(c) - len(e)]
            if basis in _BAUTEIL_WORT or any(basis.endswith(k) for k in _BAUTEIL_KOPF):
                return True
    return False


# Trenner, mit dem die Schema-App mehrere Texte in EINEM Aufruf verbindet (homepage-schema pseudonymize.js).
# Geltungsbereich der Entscheidung ist der einzelne Text dazwischen (Review R1 D-PRV-1).
_TEXT_TRENNER = re.compile(r"WERKING_DOCUMENT_[0-9a-fA-F-]{36}")


def kennungsform(text: str, start: int, ende: int) -> bool:
    if not _KENNUNG.fullmatch(text[start:ende]):
        return False
    vor = text[start - 1] if start > 0 else " "
    nach = text[ende] if ende < len(text) else " "
    if vor.isalnum() or vor in _EINBETTUNG or nach.isalnum():
        return False
    if nach in _EINBETTUNG:
        # Satzzeichen am Satz-/Aufzaehlungsende ("... Schluckbrunnen B-02.") ist keine Einbettung;
        # "B-02.example.org", "B-02/3", "B-02-Neu" dagegen schon.
        danach = text[ende + 1] if ende + 1 < len(text) else " "
        if not (nach in ".:" and (danach.isspace() or danach in ")\"'“")):
            return False
    return True


_AUFZAEHLUNG = re.compile(r"(?:,\s*|\s+(?:und|oder|sowie)\s+)$")


def _vorwort_stelle(text: str, start: int, tiefe: int = 0) -> Optional[Tuple[int, int]]:
    """Stelle (von, bis) des Worts, an das die Kennung bei ``start`` syntaktisch gebunden ist:
      - unmittelbar davor, nur Leerzeichen/Tab dazwischen ("Kaltwasserverteiler KV-01"), auch am Anfang einer
        Tabellenzelle ("…;Kreis UK-01", "…,Ruecklauf UK-01");
      - davor eine oeffnende Klammer ("Umwaelzpumpe (P-02"), gleiche Zeile;
      - Aufzaehlung: steht davor ", " / " und " / " oder " / " sowie " und davor wieder eine freie Kennung,
        gilt deren Bindung ("Verbraucher RLT-01, UK-01"; hoechstens 6 Glieder).
    Sonst None."""
    if tiefe > 6:
        return None
    zs = text.rfind("\n", 0, start) + 1
    vor = text[zs:start]
    m = _AUFZAEHLUNG.search(vor)
    if m and tiefe < 6:
        k_ende = zs + m.start()
        km = re.search(r"[A-Z]{1,4}-\d{1,4}$", text[zs:k_ende])
        if km:
            k_start = zs + km.start()
            if kennungsform(text, k_start, k_ende) or text[k_ende:k_ende + 1] == ",":
                return _vorwort_stelle(text, k_start, tiefe + 1)
    i = start
    while i > zs and text[i - 1] in " \t":
        i -= 1
    if i > zs and text[i - 1] == "(":
        i -= 1
        while i > zs and text[i - 1] in " \t":
            i -= 1
    if i == start:
        return None  # nichts dazwischen = angeklebt
    j = i
    while j > zs and (text[j - 1].isalpha() or text[j - 1] == "-"):
        j -= 1
    if j == i or (j > zs and text[j - 1] not in " \t|(\"'„;,"):
        return None
    return j, i


def bauteilwort(text: str, start: int) -> Optional[str]:
    """Das Wort, an das die Kennung gebunden ist (siehe _vorwort_stelle), sonst None."""
    st = _vorwort_stelle(text, start)
    return text[st[0]:st[1]] if st else None


def ist_marker(wort: str) -> bool:
    w = wort.casefold().rsplit("-", 1)[-1]
    # Grundwort inkl. Flexions-/Femininendung ("Lieferanten", "Planerin", "Mitarbeiterinnen", "Firmen")
    return (any(w.endswith(t + e) for t in _MARKER_GRUNDWORTE for e in ("", "e", "n", "en", "s", "es", "in", "innen"))
            or any(w.endswith(t[:-1] + "en") for t in _MARKER_GRUNDWORTE if t.endswith("a"))
            or w.startswith(("kontakt", "ansprech")))


def _ueberlappt(a: int, b: int, bereiche: Iterable[Tuple[int, int]]) -> bool:
    return any(a < e and s < b for s, e in bereiche)


def _ist_zelle(text: str, start: int, ende: int) -> bool:
    """Steht die Kennung als eigene Tabellenzelle (CSV ; , Tab, Markdown |)?"""
    zs = text.rfind("\n", 0, start) + 1
    vor = text[zs:start].rstrip(" \t")
    if vor and vor[-1] not in ";,|\t":
        return False
    nach = text[ende:ende + 2].lstrip(" ")
    return bool(nach) and nach[0] in ";,|\t"


def _zellen_nachbar_stelle(text: str, ende: int) -> Optional[Tuple[int, int]]:
    i = ende
    while i < len(text) and text[i] == " ":
        i += 1
    if i >= len(text) or text[i] not in ";,|\t":
        return None
    i += 1
    while i < len(text) and text[i] in " \t":
        i += 1
    j = i
    while j < len(text) and (text[j].isalpha() or text[j] == "-"):
        j += 1
    return (i, j) if j > i else None


def _bindung(text: str, start: int, ende: int, alle_bereiche: List[Tuple[int, int]]) -> Tuple[str, Optional[str]]:
    """Art der syntaktischen Bindung dieser Kennungsstelle:
    ("marker", wort)     Rollen-/Organisations-/Ortsmarker unmittelbar davor ("Firma KV-01", "Kontaktperson B-02");
    ("definition", wort) Bauteil-Substantiv unmittelbar davor ("Kaltwasserverteiler KV-01") oder als naechste
                         Tabellenzelle ("KV-01;Kaltwasserverteiler;...") — das Dokument erklaert die Kennung;
    ("referenz", None)   sonst (Verweis auf eine anderswo definierte Kennung: "zwischen KV-01 und", "auf KV-01").
    Ein Wort, das selbst Fundstelle ist, zaehlt nicht als Bauteilwort."""
    ws = _vorwort_stelle(text, start)
    if ws:
        wort = text[ws[0]:ws[1]]
        if ist_marker(wort):
            return "marker", wort
        if ist_bauteil(wort) and not _ueberlappt(ws[0], ws[1], alle_bereiche):
            return "definition", wort
    if _ist_zelle(text, start, ende):
        zs = _zellen_nachbar_stelle(text, ende)
        z = text[zs[0]:zs[1]] if zs else None
        if z and ist_marker(z):
            return "marker", z
        # Nachbarzelle nur als Definition, wenn sie selbst keine Fundstelle ist (Review R1 P-PRV-2:
        # "LF-02;Huberwerk;..." -> LF-02 ist firmennah, bleibt maskiert)
        if z and ist_bauteil(z) and not _ueberlappt(zs[0], zs[1], alle_bereiche):
            return "definition", z
    return "referenz", None


def _haengt_an_harter_pii(start: int, ende: int, text: str, harte_bereiche: List[Tuple[int, int]]) -> bool:
    """Harte Fundstelle ueberlappt oder grenzt an (nur Leerraum/Klammer/Komma dazwischen): 'Quastenberg (KV-01)'."""
    if _ueberlappt(start, ende, harte_bereiche):
        return True
    for s, e in harte_bereiche:
        if e <= start and text[e:start].strip(" \t(),;:") == "":
            return True
        if ende <= s and text[ende:s].strip(" \t(),;:") == "":
            return True
    return False


def _alle_vorkommen(text: str, wert: str) -> set:
    return {(m.start(), m.end()) for m in re.finditer(re.escape(wert), text)}


def _texte(text: str) -> List[Tuple[int, int]]:
    """Grenzen der einzelnen Texte eines Aufrufs (zwischen den Trennern der Schema-App)."""
    grenzen, pos = [], 0
    for m in _TEXT_TRENNER.finditer(text):
        grenzen.append((pos, m.start()))
        pos = m.end()
    grenzen.append((pos, len(text)))
    return grenzen


# R3: Rollenbindung DERSELBEN Stelle (SEC: "mehrdeutige Rollenbindung derselben Stelle sperrt") - praedikativ oder
# als Apposition direkt NACH der Kennung, innerhalb derselben Zelle/Zeile: "B-02 ist unser Ansprechpartner",
# "B-02, Bewohnerin", "B-02 (Kontakt)", "B-02: Herr ...". Ein Rollenwort anderswo im Satz oder in einer anderen
# Tabellenzelle sperrt NICHT (Rafael 17.11.: "Die Pumpe P-03 hat Ole Fennimann eingebaut" -> P-03 frei, Name maskiert).
_ROLLE_NACH = re.compile(
    r"[ \t]*(?:,|;|\||\(|:|=|–|ist|war|sind|waren|als|heisst|heißt)[ \t]*"
    r"(?:(?:der|die|das|den|dem|unser|unsere|unserem|unseren|unserer|ein|eine|einen|einem|ihr|ihre|sein|seine)[ \t]+)*"
    r"([A-ZÄÖÜ][\wäöüß-]*)")


def _rolle_an_stelle(text: str, ende: int) -> Optional[str]:
    """Rollenbindung DERSELBEN Stelle nach der Kennung (R3b, Review R3 P-PRV-7: POSITIV gefasst): Apposition oder
    Praedikat ("B-02, Bewohnerin ...", "B-02 ist Patient", "B-02 (Familie ...)") mit einem Substantiv, das kein
    Bauteil der Fachliste ist, sperrt die Stelle; Marker sperren immer. Ausnahme: Zelltrenner (; | Tab, CSV-Komma
    ohne Leerzeichen) - Tabellenzellen sind getrennte Daten (Rafael 17.11.); dort sperren nur Marker."""
    ze = text.find("\n", ende)
    rest = text[ende: ze if ze != -1 else len(text)]
    m = _ROLLE_NACH.match(rest)
    if not m:
        return None
    w = m.group(1)
    if ist_marker(w):
        return w
    trenner = rest[: m.start(1)].strip()
    zelle = trenner in (";", "|") or rest[:1] == "\t" or (trenner == "," and rest[:1] == "," and rest[1:2] not in (" ", ""))
    if zelle:
        return None
    if re.fullmatch(r"[A-ZÄÖÜ][a-zäöüß][\wäöüß-]*", w) and not ist_bauteil(w):
        return w
    return None


_PRAEPOSITIONEN = frozenset({
    "an", "am", "auf", "aus", "bei", "beim", "bis", "durch", "fuer", "für", "gegen", "hinter", "im", "in", "ins",
    "mit", "nach", "neben", "ueber", "über", "um", "unter", "vom", "von", "vor", "zu", "zum", "zur", "zwischen",
    "hinter", "oberhalb", "unterhalb", "parallel",
})


def _lokaler_beleg(text: str, start: int, ende: int, alle_bereiche: List[Tuple[int, int]]) -> Tuple[str, Optional[str]]:
    """Lokaler technischer Beleg EINER Stelle: ("marker", w) | ("definition", w) | ("referenz", w) | ("keiner", None).
    referenz = Praeposition unmittelbar davor bzw. am Kopf der Aufzaehlung ("P-02 fuer RLT-01, P-03 fuer UK-01")."""
    art, wort = _bindung(text, start, ende, alle_bereiche)
    if art in ("marker", "definition"):
        return art, wort
    ws = _vorwort_stelle(text, start)
    if ws and text[ws[0]:ws[1]].casefold() in _PRAEPOSITIONEN:
        return "referenz", text[ws[0]:ws[1]]
    return "keiner", None


def entscheide(text: str, kandidaten: List[Tuple[int, int, str, str]],
               harte_bereiche: List[Tuple[int, int]], alle_bereiche: List[Tuple[int, int]]) -> Tuple[dict, list, list]:
    """Entscheidung JE STELLE (R3, siehe Modulkopf).

    Fuer einen Wert in Kennungsform, dessen Detektor-Stellen alle ORG/LOC sind, gilt fuer JEDES textuelle
    Vorkommen im Aufruf (auch vom Dienst nicht gemeldete und eingebettete):
      frei  NUR wenn die Stelle LOKAL positiv technisch belegt ist (Bauteilbegriff der Fachliste an der Stelle,
            oder Praeposition/Aufzaehlung als Referenz) UND sauber ist (Kennungsform, nicht an harter PII, kein
            Marker davor, keine Rollenbindung danach) UND der Wert IRGENDWO im Aufruf als Bauteil definiert ist
            (Hinweis, der allein nie freigibt); Referenzen eines Werts, der im selben Text auch als Marker/Rolle/
            PII/eingebettet vorkommt, bleiben maskiert;
      sonst maskiert — eine ungemeldete unklare Stelle wird ZUSAETZLICH maskiert (zusatz).
    Eine maskierte Stelle exponiert nie eine andere und eine freie Stelle wird nicht wertglobal zurueckmaskiert:
    der Aufrufer schuetzt die freien Stellen vor dem Nachzug (geschuetzt) und maskiert die zusatz-Stellen.
    Rueckgabe: ({(start, ende): beleg} freie Detektor-Stellen, [abgelehnte Stellen mit Grund], [(start, ende, typ)] zusatz)."""
    texte = _texte(text)
    werte: dict = {}
    for st, en, typ, quelle in kandidaten:
        werte.setdefault(text[st:en], []).append((st, en, typ, quelle))
    frei: dict = {}
    abgelehnt: list = []
    zusatz: list = []
    for wert, stellen in werte.items():
        if not _KENNUNG.fullmatch(wert):
            continue
        if any(q != "detektor" or t not in WEICHE_TYPEN for _, _, t, q in stellen):
            abgelehnt.append({"wert": wert, "text": 0, "pos": None, "grund": "harter-oder-anderer-typ"})
            continue
        typ0 = stellen[0][2]
        gemeldet = {(st, en) for st, en, _, _ in stellen}
        urteile = []  # (st, en, nr, frei?, grund/beleg)
        definiert = False
        for st, en in sorted(_alle_vorkommen(text, wert)):
            nr = next(i for i, (t0, t1) in enumerate(texte, start=1) if t0 <= st <= t1)
            grund = None
            if not kennungsform(text, st, en):
                grund = "eingebettet"
            elif _haengt_an_harter_pii(st, en, text, harte_bereiche):
                grund = "an-harter-pii"
            else:
                art, wort = _lokaler_beleg(text, st, en, alle_bereiche)
                rolle = _rolle_an_stelle(text, en)
                if art == "marker":
                    grund = "marker:" + wort
                elif rolle:
                    grund = "rolle-an-stelle:" + rolle
                elif art == "keiner":
                    grund = "kein-lokaler-technischer-beleg"
                else:
                    definiert = definiert or art == "definition"
                    urteile.append((st, en, nr, True, (art, wort)))
                    continue
            urteile.append((st, en, nr, False, grund))
        # R3 (SEC: same-value technisch/Person GETRENNT je Stelle): eine Marker-/Rollen-/PII-/Einbettungs-Verwendung
        # sperrt im selben TEXT nur die REFERENZ-Stellen des Werts (Praeposition/Aufzaehlung ohne eigenen
        # Bauteilbegriff); eine Stelle mit eigenem Bauteilbegriff ("Kaltwasserverteiler KV-01") bleibt frei.
        konflikt = {nr for _, _, nr, ok, info in urteile
                    if not ok and info != "kein-lokaler-technischer-beleg"}
        urteile = [(st, en, nr, False, "referenz-bei-konflikt-im-text")
                   if ok and nr in konflikt and info[0] == "referenz" else (st, en, nr, ok, info)
                   for st, en, nr, ok, info in urteile]
        urteile = [(st, en, nr, ok, info[1] if ok else info) for st, en, nr, ok, info in urteile]
        for st, en, nr, ok, info in urteile:
            if ok and not definiert:
                ok, info = False, "keine-definition-im-aufruf"
            if ok and (st, en) in gemeldet:
                frei[(st, en)] = info
            elif not ok:
                abgelehnt.append({"wert": wert, "text": nr, "pos": st, "grund": info})
                if (st, en) not in gemeldet:
                    zusatz.append((st, en, typ0))
        if any((st, en) not in {(u[0], u[1]) for u in urteile} for st, en in gemeldet):
            # fail-closed: eine gemeldete Stelle, die die Vorkommenssuche nicht sieht -> nichts freigeben
            for k in [k for k in frei if text[k[0]:k[1]] == wert]:
                del frei[k]
            abgelehnt.append({"wert": wert, "text": 0, "pos": None, "grund": "stelle-ohne-pruefung"})
    return frei, abgelehnt, zusatz


def flag_an() -> bool:
    return os.environ.get(FLAG_ENV, "").strip().lower() == "true"


async def kontext_verifiziert(request: Any, context_hint: Optional[str], *, aufrufer_user_id: Optional[str]) -> bool:
    """True NUR, wenn ALLES gilt (sonst bleibt die Maskierung unveraendert):
      - Flag ``BRIDGE_PRIVACY_ANLAGENKENNUNG=true`` (Standard aus);
      - ``context_hint`` exakt ``technical_system_description``;
      - die Schema-App ist SERVERSEITIG verifiziert: direkt ueber einen nicht-legacy Principal mit genau dieser
        einen App (``_resolve_app_id`` -> source "principal"), ODER beim Job-Selbstaufruf (nur Loopback-Peer,
        Job-Kopf) ueber das bei POST /v1/jobs serverseitig gespeicherte Merkmal eines LAUFENDEN Jobs mit
        app_id Schema und demselben Nutzer. Kopf X-App-ID oder Rumpf allein genuegen nie.
    Fehler beim Lesen des Job-Speichers -> False."""
    if not flag_an() or context_hint != ANLAGENKONTEXT:
        return False
    from src.routing.app_provider_policy import _resolve_app_id
    from src.routing.schema_bedrock_guard import SCHEMA_APP_ID
    if _resolve_app_id(request) == (SCHEMA_APP_ID, "principal"):
        return True
    from src.jobs.selbstaufruf import JOB_LAUF_HEADER
    job_id = (request.headers.get(JOB_LAUF_HEADER) or "").strip()
    peer = getattr(getattr(request, "client", None), "host", None)
    if not job_id or peer not in ("127.0.0.1", "::1"):
        return False
    from src.jobs import store_client
    try:
        job = await store_client.get_job(job_id)
    except Exception as e:  # noqa: BLE001 — nicht lesbar: Maskierung bleibt
        logger.error("privacy anlagenkontext: job store not readable (%s) — unchanged masking", type(e).__name__)
        return False
    if not isinstance(job, dict) or job.get("status") != "running":
        return False
    attr = job.get("attribution") or {}
    return (attr.get(JOB_SCHEMA_VERIFIZIERT_KEY) is True and attr.get("app_id") == SCHEMA_APP_ID
            and aufrufer_user_id is not None and attr.get("user_id") == aufrufer_user_id)
