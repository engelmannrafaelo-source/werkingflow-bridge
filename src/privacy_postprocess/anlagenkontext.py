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
  4. Bauteilbezug syntaktisch gebunden: das Wort UNMITTELBAR davor (gleiche Zeile, nur Leerraum
     dazwischen) ist ein Substantiv in Normalschreibung, das selbst keine Fundstelle ist und kein
     Rollen-, Personen- oder Organisationsmarker (Firma, Herr, Kontaktperson, ...);
  5. keine Ueberlappung mit einer harten Fundstelle.
Sonst bleibt die Maskierung (fail-closed). Die Entscheidung gilt je Fundstelle: wird derselbe Wert
an anderer Stelle maskiert (z. B. "Die Firma KV-01"), zieht ``_gleiche_werte_nachziehen`` ihn auch
hier wieder nach — nie eine globale Wiederherstellung.
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


def pruefe(text: str, start: int, ende: int, typ: str, quelle: str,
           harte_bereiche: List[Tuple[int, int]], alle_bereiche: List[Tuple[int, int]]) -> Optional[str]:
    """Bauteilwort, wenn diese Fundstelle herabgestuft werden darf; sonst None (= maskieren)."""
    if quelle != "detektor" or typ not in WEICHE_TYPEN:
        return None
    if not kennungsform(text, start, ende):
        return None
    if _ueberlappt(start, ende, harte_bereiche):
        return None
    wort = bauteilwort(text, start)
    if wort is None or not _SUBSTANTIV.fullmatch(wort) or ist_marker(wort):
        return None
    w_ende = len(text[:start].rstrip(" \t"))
    if _ueberlappt(w_ende - len(wort), w_ende, alle_bereiche):
        return None  # das Bauteilwort ist selbst eine Fundstelle (z. B. als Person erkannt)
    return wort


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
