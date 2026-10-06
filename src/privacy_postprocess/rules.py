"""Verwerf-Regeln fuer Detektor-Fundstellen: Plausibilitaet + Fach-Freiliste.

Die Plausibilitaetsregeln sind die Python-Fassung von
``packages/document-pipeline/src/entity-plausibility.ts`` (TS-Commit 4dc2d5b52),
erweitert um die Faelle aus dem Audit vom 06.10.2026 (Datumsbereiche, Zeitstempel
ohne Minuten, Tausenderzahlen, Zahlenbereiche, Abschnittsnummern als IP,
Fundstellen innerhalb einer URL, gesperrte Buchstaben aus PDF-Kopfzeilen).

Richtung: "verwerfen" heisst Klartext. Deshalb gilt keine Regel fuer E-Mail, IBAN,
Kreditkarte oder die Kennungs-Erkenner, und keine Regel gibt ein Datum frei, das
im Geburtsdatums-Kontext steht (das erledigt der Regel-Erkenner GEBURTSDATUM, der
hoehere Prioritaet hat als jede Detektor-Fundstelle).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import FrozenSet, List, Optional, Pattern, Tuple
from urllib.parse import urlparse

from .spans import Span

TELEFON_TYPEN = frozenset({"PHONE_NUMBER", "PHONE"})
NAMENS_TYPEN = frozenset({"PERSON", "ORGANIZATION", "LOCATION", "ORG", "LOC"})
# Typen, die die Freiliste beruehren darf. Nie: E-Mail, Telefon, IBAN, Kennungen.
FREILISTEN_TYPEN = frozenset({"PERSON", "ORGANIZATION", "LOCATION", "URL", "ORG", "LOC", "NRP"})

_DATUM_VOLL = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2})")
_ZEIT = r"(?:\d{1,2}(?:[:.]?\d{2})?(?:\s*Uhr)?)"
_DATUM_MIT_ZEIT = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4}|\d{2})(?:,?\s+" + _ZEIT + r")?$")
_DATUM_PRAEFIX = re.compile(r"^(\d{1,2})\.(\d{1,2})\.((?:19|20)\d{2})(?!\d)")  # "03.08.2026 3.982.387"
_DATUM_BEREICH = re.compile(
    r"^\d{1,2}\.\d{1,2}\.(?:\d{4}|\d{2})?\s*(?:-|–|—|bis)\s*\d{1,2}\.\d{1,2}\.(?:\d{4}|\d{2})$"
)
_TAUSENDER = re.compile(r"^[-−]?\d{1,3}(?:\.\d{3})+(?:,\d+)?$")
_ZAHLEN_BEREICH = re.compile(r"^[-−]?[\d.,]+\s*(?:-|–|—|bis)\s*[-−]?[\d.,]+$")
_GESETZBLATT = re.compile(r"^\d{1,4}/(?:19|20)\d{2}$")  # BGBl. II Nr. 164/2020
_IP_ABSCHNITT = re.compile(r"^\d(?:\.\d){2,4}$")  # 7.2.6.3 = Abschnittsnummer
_KANONISCHES_TOKEN = re.compile(r"[A-Z]+_P[0-9a-z]+_E[0-9a-z]+_[0-9A-Z]{2}")
_URL_IM_TEXT = re.compile(r"(?:https?://|www\.)[^\s<>\]\)\"'|]+", re.IGNORECASE)


def _ziffern(s: str) -> int:
    return sum(c.isdigit() for c in s)


def _gueltiges_datum(m: "re.Match[str]") -> bool:
    return 1 <= int(m.group(1)) <= 31 and 1 <= int(m.group(2)) <= 12


def plausibilitaet(wert: str, typ: str) -> Optional[str]:
    """Grund, warum ``wert`` unter keinen Umstaenden PII dieses Typs ist — sonst None."""
    w = wert.strip()
    if not w:
        return "leer"
    if _KANONISCHES_TOKEN.search(w):
        return "nested-token"
    if typ == "PERSON" and re.search(r"\d", w):
        return "person-with-digit"
    if typ in TELEFON_TYPEN:
        m = _DATUM_MIT_ZEIT.match(w)
        if m and _gueltiges_datum(m):
            return "phone-is-date"
        if _DATUM_BEREICH.match(w):
            return "phone-is-date-range"
        m = _DATUM_PRAEFIX.match(w)
        if m and _gueltiges_datum(m):
            return "phone-is-date"
        if _TAUSENDER.match(w):
            return "phone-is-number"
        if _ZAHLEN_BEREICH.match(w) and not w.startswith(("0", "+")):
            return "phone-is-range"
        if _GESETZBLATT.match(w):
            return "phone-is-citation"
        if _ziffern(w) < 7:
            return "phone-too-few-digits"
        # Eine Telefonnummer in AT/DE-Schreibweise beginnt mit 0, + oder (.
        # "1685 021157" (SV-Nr), "045612/2026-3" (GZ-Teil), "360.000" sind keine.
        if not w.startswith(("0", "+", "(")):
            return "phone-without-prefix"
    if typ == "IP_ADDRESS" and _IP_ABSCHNITT.match(w):
        return "ip-is-section-number"
    if typ in NAMENS_TYPEN:
        if not re.search(r"[^\W\d_]", w):
            return "no-letters"
        if len(w) <= 2:
            return "too-short"
        teile = w.split()
        if len(teile) >= 3 and sum(len(t) <= 2 for t in teile) / len(teile) >= 0.6:
            return "letter-spacing-artifact"  # "PO WER ED B Y", "ER S T EL LT"
    elif len(w) <= 2:
        return "too-short"
    return None


def in_url(text: str, span: Span) -> bool:
    """Liegt die Fundstelle INNERHALB einer URL, die selbst nicht die Fundstelle ist?"""
    zeile_start = text.rfind("\n", 0, span.start) + 1
    zeile_ende = text.find("\n", span.end)
    zeile_ende = len(text) if zeile_ende < 0 else zeile_ende
    for m in _URL_IM_TEXT.finditer(text, zeile_start, zeile_ende):
        if m.start() <= span.start and span.end <= m.end() and (m.start(), m.end()) != (span.start, span.end):
            return True
    return False


# ── Fach-Freiliste ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Freiliste:
    woerter: FrozenSet[str]
    woerter_gross: FrozenSet[str]  # Grossschreibung der Woerter (Tabellenkopf „SONDENFELD")
    phrasen: FrozenSet[str]  # casefold
    muster: Tuple[Pattern[str], ...]
    url_domains: Tuple[str, ...]
    quelle: str

    def ist_frei(self, wert: str, typ: str) -> Optional[str]:
        if typ not in FREILISTEN_TYPEN:
            return None
        w = " ".join(wert.split())
        if typ == "URL":
            return "freiliste-url" if self._url_frei(w) else None
        if w.casefold() in self.phrasen or self._wort_frei(w):
            return "freiliste-begriff"
        if any(m.fullmatch(w) for m in self.muster):
            return "freiliste-muster"
        # Alle Bestandteile frei (Kuerzel, Normteile, Zahlen): "DBA SH1", "ÖNORM EN", "VDI 4650"
        teile = [t for t in re.split(r"[\s,;:()/]+", w) if t]
        if typ != "PERSON" and teile and all(self._teil_frei(t) for t in teile):
            return "freiliste-bestandteile"
        return None

    def _wort_frei(self, t: str) -> bool:
        # Exakt, oder als reine Grossschreibung eines gelisteten Worts (Ueberschrift).
        return t in self.woerter or (t.isupper() and t in self.woerter_gross)

    def _teil_frei(self, t: str) -> bool:
        if self._wort_frei(t) or t.casefold() in self.phrasen:
            return True
        if not re.search(r"[^\W\d_]", t):  # Zahl, Strich, Satzzeichen
            return True
        return any(m.fullmatch(t) for m in self.muster)

    def _url_frei(self, url: str) -> bool:
        u = url.strip().rstrip(".,;")
        if not re.match(r"^[a-z][a-z0-9+.-]*://", u, re.IGNORECASE):
            u = "http://" + u
        host = (urlparse(u).hostname or "").lower()
        return bool(host) and any(host == d or host.endswith("." + d) for d in self.url_domains)


_STANDARD_PFAD = Path(__file__).with_name("fach_allowlist.json")


@lru_cache(maxsize=4)
def lade_freiliste(pfad: Optional[str] = None) -> Freiliste:
    """Laedt die Freiliste. Fehlt die Datei oder ist sie kaputt, wird laut abgebrochen."""
    p = Path(pfad or os.environ.get("BRIDGE_PSEUDONYM_ALLOWLIST_PATH") or _STANDARD_PFAD)
    daten = json.loads(p.read_text(encoding="utf-8"))
    for schluessel in ("woerter", "phrasen", "muster", "url_domains"):
        if not isinstance(daten.get(schluessel), list):
            raise ValueError(f"Freiliste {p}: Schluessel {schluessel!r} fehlt oder ist keine Liste")
    return Freiliste(
        woerter=frozenset(daten["woerter"]),
        woerter_gross=frozenset(w.upper() for w in daten["woerter"]),
        phrasen=frozenset(x.casefold() for x in daten["phrasen"]),
        muster=tuple(re.compile(m) for m in daten["muster"]),
        url_domains=tuple(d.lower() for d in daten["url_domains"]),
        quelle=str(p),
    )


def verwerfen(text: str, span: Span, freiliste: Freiliste) -> Optional[str]:
    """Grund, eine DETEKTOR-Fundstelle zu verwerfen (Klartext) — sonst None."""
    wert = span.wert(text)
    grund = plausibilitaet(wert, span.type)
    if grund:
        return grund
    if span.type != "URL" and in_url(text, span):
        return "inside-url"
    if span.type in NAMENS_TYPEN and re.match(r"-\s+(?:und|oder|bzw\.|sowie)\s", text[span.end:span.end + 12]):
        return "compound-fragment"  # "Bau- und Anlagenbehoerde": "Bau" ist kein Name
    return freiliste.ist_frei(wert, span.type)


__all__: List[str] = ["plausibilitaet", "in_url", "Freiliste", "lade_freiliste", "verwerfen"]
