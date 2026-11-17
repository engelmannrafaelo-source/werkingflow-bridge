"""Anlagenkennungen im belegten Anlagenkontext (Schema-Generator, c9a50d3b, 17.11.2026).

Anlass: E2E 7K3Q — Flair-NER (geteilter Privacy-Dienst, kein Kontextkanal) wertet technische
Anlagenkennungen wie "Kaltwasserverteiler KV-01" oder "Schluckbrunnen B-02" als ORGANIZATION
bzw. LOCATION (Score bis 0,999). Eine Schwelle loest das nicht, und der Dienst ist mit Prod geteilt.

Regel (Kanzler/SEC 17.11.: eng gebundene Kontextregel, KEINE Wert- oder Musterliste, kein Modell):
eine Detektor-Fundstelle wird an GENAU DIESER Stelle nicht maskiert, wenn ALLE Bedingungen gelten —
  1. der Aufruf ist serverseitig als Schema-App verifiziert UND deklariert den Anlagenkontext
     (``context_hint == ANLAGENKONTEXT``) UND ``BRIDGE_PRIVACY_ANLAGENKENNUNG=true`` (Standard aus);
     das prueft der Aufrufer (main.py) und reicht nur dann ``anlagenkontext=True`` herein;
  2. Quelle Detektor, Typ ORGANIZATION oder LOCATION (harte Typen, Regel-Erkenner, bekannte
     Entitaeten werden nie angefasst);
  3. Kennungsform: 1–4 Grossbuchstaben, Bindestrich, 1–4 Ziffern, frei stehend (nicht Teil von
     Mail, URL, Pfad oder einem laengeren Wort);
  4. Bauteilbezug: das Dokument DEFINIERT die Kennung als Bauteil — Substantiv unmittelbar davor
     ("Kaltwasserverteiler KV-01") oder als naechste Tabellenzelle ("KV-01;Kaltwasserverteiler;...") —
     und KEINE Stelle desselben Werts traegt einen Rollen-/Organisations-/Ortsmarker ("Firma KV-01",
     "Kontaktperson B-02"); dann gelten auch die Verweise im selben Dokument ("zwischen KV-01 und WT-01")
     als Bauteil (Replay 7K3Q, Runde 1: technische Texte definieren einmal und verweisen dann);
  5. keine Stelle des Werts ueberlappt harte PII oder haengt unmittelbar an ihr ("Quastenberg (KV-01)").
Sonst bleibt JEDE Stelle des Werts maskiert (fail-closed, mehrdeutig = maskiert). Entschieden wird je
Wert innerhalb DIESES Dokuments, angewandt je Fundstelle — keine Wertliste, keine dokumentuebergreifende
Wiederherstellung.
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
# Zeichen, die eine Kennung in eine groessere Einheit einbetten (Mail, URL, Pfad, Wortteil)
_EINBETTUNG = set("@._/\\-:#&+=")
# Substantiv in Normalschreibung: Grossbuchstabe, dann nur Kleinbuchstaben (Umlaute, ß, Binnenbindestrich)
_SUBSTANTIV = re.compile(r"[A-ZÄÖÜ][a-zäöüß]{3,}(?:-[A-ZÄÖÜa-zäöüß][a-zäöüß]+)*")

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
# Trenner, mit dem die Schema-App mehrere Texte in EINEM Aufruf verbindet (homepage-schema pseudonymize.js).
# Geltungsbereich der Entscheidung ist der einzelne Text dazwischen (Review R1 D-PRV-1).
_TEXT_TRENNER = re.compile(r"WERKING_DOCUMENT_[0-9a-fA-F-]{36}")
_SATZENDE = re.compile(r"[.!?;]\s|\n")


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
        if _SUBSTANTIV.fullmatch(wort) and not _ueberlappt(ws[0], ws[1], alle_bereiche):
            return "definition", wort
    if _ist_zelle(text, start, ende):
        zs = _zellen_nachbar_stelle(text, ende)
        z = text[zs[0]:zs[1]] if zs else None
        if z and ist_marker(z):
            return "marker", z
        # Nachbarzelle nur als Definition, wenn sie selbst keine Fundstelle ist (Review R1 P-PRV-2:
        # "LF-02;Huberwerk;..." -> LF-02 ist firmennah, bleibt maskiert)
        if z and _SUBSTANTIV.fullmatch(z) and not _ueberlappt(zs[0], zs[1], alle_bereiche):
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


def _satz(text: str, start: int, ende: int, t0: int, t1: int) -> Tuple[int, int]:
    s = t0
    for m in _SATZENDE.finditer(text, t0, start):
        s = m.end()
    m = _SATZENDE.search(text, ende, t1)
    return s, (m.start() + 1 if m else t1)


_GROSSWORT = re.compile(r"[A-ZÄÖÜ][\wäöüß-]*")


def _marker_im_satz(text: str, start: int, ende: int, t0: int, t1: int) -> Optional[str]:
    """Rollen-/Organisations-/Orts-/Adressmarker IRGENDWO im selben Satz (Review R1 D-PRV-1)."""
    s, e = _satz(text, start, ende, t0, t1)
    for m in _GROSSWORT.finditer(text, s, e):
        if m.start() <= start < m.end():
            continue
        if ist_marker(m.group(0)):
            return m.group(0)
    return None


# PII-Spuren im Satz/in der Tabellenzeile OHNE Detektortreffer (SEC R2): Mail-Zeichen, Telefon-artige
# Ziffernfolge (>= 7 Ziffern, Leerzeichen/Schraegstrich/Bindestrich erlaubt), Adresse "Wort + Hausnummer" mit PLZ.
_PII_SPUR = re.compile(r"@|(?<![\w.])\+?\d(?:[ /-]?\d){6,}(?![\w])|\b\d{4,5}\s+[A-ZÄÖÜ][a-zäöüß]+")


def _pii_im_satz(text: str, start: int, ende: int, t0: int, t1: int) -> Optional[str]:
    s, e = _satz(text, start, ende, t0, t1)
    m = _PII_SPUR.search(text[s:start] + " " + text[ende:e])
    return m.group(0) if m else None


# Geschlossene grammatische Wortklasse (Praepositionen), KEINE Werteliste: "auf KV-01", "zwischen KV-01 und WT-01",
# "fuer UK-01". Sie belegt eine technische Referenz nur zusammen mit einer Definition desselben Werts im Aufruf.
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
    """Entscheidung JE STELLE (SEC-Designentscheid R2, Praezisierung mit Source-Owner).

    Fuer einen Wert in Kennungsform, dessen Detektor-Stellen alle ORG/LOC sind, gilt fuer JEDES textuelle
    Vorkommen im Aufruf (auch vom Dienst nicht gemeldete und eingebettete):
      frei  NUR wenn die Stelle LOKAL technisch belegt ist (Bauteil-Definition an der Stelle, oder Praeposition/
            Aufzaehlung als Referenz) UND sauber ist (Kennungsform, nicht an harter PII, kein Rollen-/Firmen-/Orts-/
            Adress-/Kontaktmarker an der Stelle oder im Satz bzw. in der Tabellenzeile inkl. Nachbarzellen, keine
            PII-Spur im Satz) UND der Wert IRGENDWO im Aufruf als Bauteil definiert ist (dateiuebergreifender HINWEIS,
            der allein nie freigibt);
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
            t0, t1 = texte[nr - 1]
            grund = None
            if not kennungsform(text, st, en):
                grund = "eingebettet"
            elif _haengt_an_harter_pii(st, en, text, harte_bereiche):
                grund = "an-harter-pii"
            else:
                art, wort = _lokaler_beleg(text, st, en, alle_bereiche)
                mk = _marker_im_satz(text, st, en, t0, t1)
                if art == "marker":
                    grund = "marker:" + wort
                elif mk:
                    grund = "marker-im-satz:" + mk
                elif _pii_im_satz(text, st, en, t0, t1):
                    grund = "pii-spur-im-satz"
                elif art == "keiner":
                    grund = "kein-lokaler-technischer-beleg"
                else:
                    definiert = definiert or art == "definition"
                    urteile.append((st, en, nr, True, wort))
                    continue
            urteile.append((st, en, nr, False, grund))
        # Im selben TEXT mehrdeutig (Root A4: "Die Firma KV-01 ... Kaltwasserverteiler KV-01"): eine Marker-,
        # PII- oder Einbettungs-Verwendung sperrt alle Stellen des Werts in DIESEM Text; andere Texte entscheiden
        # je Stelle (SEC: "Ansprechpartner B-02" anderswo bleibt maskiert, technische Stellen bleiben frei).
        mehrdeutig = {nr for _, _, nr, ok, info in urteile
                      if not ok and info != "kein-lokaler-technischer-beleg"}
        urteile = [(st, en, nr, False, "mehrdeutig-im-text") if ok and nr in mehrdeutig else (st, en, nr, ok, info)
                   for st, en, nr, ok, info in urteile]
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
