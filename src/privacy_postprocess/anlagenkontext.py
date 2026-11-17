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
)


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


def bauteilwort(text: str, start: int) -> Optional[str]:
    """Das Wort unmittelbar vor der Fundstelle (nur Leerzeichen/Tab dazwischen, gleiche Zeile)."""
    i = start
    while i > 0 and text[i - 1] in " \t":
        i -= 1
    if i == start:
        return None  # nichts dazwischen = angeklebt
    j = i
    while j > 0 and (text[j - 1].isalpha() or text[j - 1] == "-"):
        j -= 1
    wort = text[j:i]
    if not wort or (j > 0 and text[j - 1] not in " \t\n|(\"'„"):
        return None
    return wort


def ist_marker(wort: str) -> bool:
    w = wort.casefold().rsplit("-", 1)[-1]
    return any(w.endswith(t) for t in _MARKER_GRUNDWORTE) or w.startswith(("kontakt", "ansprech"))


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


def _zellen_nachbar(text: str, ende: int) -> Optional[str]:
    """Erstes Wort der NAECHSTEN Zelle derselben Zeile (Tabellen-Definition 'KV-01;Kaltwasserverteiler;...')."""
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
    return text[i:j] or None


def _bindung(text: str, start: int, ende: int, alle_bereiche: List[Tuple[int, int]]) -> Tuple[str, Optional[str]]:
    """Art der syntaktischen Bindung dieser Kennungsstelle:
    ("marker", wort)     Rollen-/Organisations-/Ortsmarker unmittelbar davor ("Firma KV-01", "Kontaktperson B-02");
    ("definition", wort) Bauteil-Substantiv unmittelbar davor ("Kaltwasserverteiler KV-01") oder als naechste
                         Tabellenzelle ("KV-01;Kaltwasserverteiler;...") — das Dokument erklaert die Kennung;
    ("referenz", None)   sonst (Verweis auf eine anderswo definierte Kennung: "zwischen KV-01 und", "auf KV-01").
    Ein Wort, das selbst Fundstelle ist, zaehlt nicht als Bauteilwort."""
    wort = bauteilwort(text, start)
    if wort:
        if ist_marker(wort):
            return "marker", wort
        w_ende = len(text[:start].rstrip(" \t"))
        if _SUBSTANTIV.fullmatch(wort) and not _ueberlappt(w_ende - len(wort), w_ende, alle_bereiche):
            return "definition", wort
    if _ist_zelle(text, start, ende):
        z = _zellen_nachbar(text, ende)
        if z and ist_marker(z):
            return "marker", z
        if z and _SUBSTANTIV.fullmatch(z):
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


def entscheide(text: str, kandidaten: List[Tuple[int, int, str, str]],
               harte_bereiche: List[Tuple[int, int]], alle_bereiche: List[Tuple[int, int]]) -> Tuple[dict, list]:
    """Dokumentweite Entscheidung je WERT, angewandt je Fundstelle.

    kandidaten: (start, ende, typ, quelle) aller Detektor-Fundstellen.
    Ein Wert gilt als Anlagenkennung dieses Dokuments, wenn
      - JEDE seiner Fundstellen Detektor ORG/LOC in Kennungsform ist, nicht an harter PII haengt,
      - mindestens EINE Fundstelle eine Definition ist (das Dokument erklaert die Kennung als Bauteil),
      - KEINE Fundstelle einen Rollen-/Organisations-/Ortsmarker traegt (sonst mehrdeutig -> alles maskiert).
    Dann werden seine Fundstellen herabgestuft (Definition und Referenzen). Sonst bleibt jede maskiert.
    Rueckgabe: ({(start, ende): beleg}, [nicht herabgestufte Werte mit Grund]) — beides geht ins Ergebnis."""
    nach_wert: dict = {}
    for st, en, typ, quelle in kandidaten:
        nach_wert.setdefault(text[st:en], []).append((st, en, typ, quelle))
    frei: dict = {}
    abgelehnt: list = []
    for wert, stellen in nach_wert.items():
        if not _KENNUNG.fullmatch(wert):
            continue
        grund = None
        defs, marker = [], []
        for st, en, typ, quelle in stellen:
            if quelle != "detektor" or typ not in WEICHE_TYPEN:
                grund = grund or "harter-oder-anderer-typ"
                continue
            if not kennungsform(text, st, en):
                grund = grund or "eingebettet"
                continue
            if _haengt_an_harter_pii(st, en, text, harte_bereiche):
                grund = grund or "an-harter-pii"
                continue
            art, wort = _bindung(text, st, en, alle_bereiche)
            if art == "marker":
                marker.append(wort)
            elif art == "definition":
                defs.append(wort)
        if grund is None and marker:
            grund = "mehrdeutig-marker:" + marker[0]
        if grund is None and not defs:
            grund = "keine-definition-im-dokument"
        if grund:
            abgelehnt.append({"wert": wert, "stellen": len(stellen), "grund": grund})
            continue
        beleg = defs[0]
        for st, en, _, _ in stellen:
            frei[(st, en)] = beleg
    return frei, abgelehnt


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
