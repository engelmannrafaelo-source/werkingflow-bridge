"""Nachbearbeitung von ``/v1/privacy/smart-anonymize`` im Bridge-Worker.

Warum im Worker und nicht im Privacy-Dienst: der Dienst (Presidio + Flair auf
gpu-privacy-1) wird von Dev- UND Prod-Bridge geteilt. Was hier im Worker laeuft,
wird je Bridge ausgerollt und per ``BRIDGE_PSEUDONYM_POSTPROCESS`` geschaltet —
Dev kann es tragen, ohne dass sich Prod bewegt. Alle Clients (Check/Report ueber
das TS-Paket, Energy direkt aus Python, das Recherche-Tor) bekommen dasselbe.

Ablauf je Aufruf (deterministisch, kein Modell, kein Netz):
  1. Detektor-Fundstellen aus der Dienst-Antwort zurueckrechnen (spans.py)
  2. Unplausibles und Freigelistetes verwerfen (rules.py)
  3. Firmen- und Adressbereiche vervollstaendigen
  4. Regel-Erkenner fuer Kennungen + Geburtsdatum (recognizers.py)
  5. Bekannte Entitaeten des Akts nachziehen (optional ``known_entities``)
  6. Ueberlappungen aufloesen, gleiche Werte im Dokument nachziehen
  7. Ein Platzhalter je Wert; Schreibvarianten (nur Nachname, Firmenkern) auf die Vollform
  8. Text + Mapping neu ausgeben, Invarianten pruefen (fail loud)
"""

from __future__ import annotations

import logging
import os
import re
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional, Tuple

from .recognizers import adresse_erweitern, erkenne
from .rules import Freiliste, in_url, lade_freiliste, plausibilitaet, verwerfen
from .spans import PRIO_BEKANNT, PRIO_DETEKTOR, Span, ausgeben, spans_aus_antwort

logger = logging.getLogger(__name__)

VERSION = "2026-10-06.1"

# Hartes PII: der Originalwert darf nie im Ausgabetext stehen bleiben (Lecktest).
HARTE_TYPEN = frozenset({
    "PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "IBAN_CODE", "CREDIT_CARD", "IP_ADDRESS",
    "US_SSN", "CRYPTO", "MEDICAL_LICENSE", "GEBURTSDATUM", "SVNR",
})
# Namens-Typen, bei denen eine Fundstelle in einer URL (z. B. ".../smart") nicht zaehlt.
_URL_INNEN_TYPEN = frozenset({"ORGANIZATION", "LOCATION", "NRP"})
# Reihenfolge bei gleicher Haeufigkeit, wenn derselbe Wert mit mehreren Typen gemeldet wird.
_TYP_RANG = {"PERSON": 0, "ORGANIZATION": 1, "LOCATION": 2}

_TITEL = {
    "di", "dipl.-ing.", "dipl.ing.", "dipl.", "ing.", "mag.", "mag.ª", "mag.a", "dr.", "prof.", "msc", "bsc",
    "ba", "ma", "herr", "frau", "hr.", "fr.", "univ.-prof.", "dkfm.", "bmstr.", "baumeister",
}
_RECHTSFORM = {
    "gmbh", "gesmbh", "ges.m.b.h.", "m.b.h.", "mbh", "ag", "kg", "og", "oeg", "keg", "eu", "e.u.", "gbr", "ohg",
    "ug", "se", "ltd", "ltd.", "inc", "inc.", "llc", "co", "co.", "&", "+", "und", "zt", "zt-gmbh",
    "ziviltechniker", "ziviltechnikergesellschaft", "ingenieurbüro", "ingenieurbuero", "ib", "technisches",
    "büro", "buero", "tb", "hausverwaltung", "immobilienverwaltung", "gruppe", "holding", "gmbh&co",
}
_ROLLEN_PRAEFIX = re.compile(r"^(?:AN|AG|BH|Fa\.|Firma|Auftragnehmer(?:in)?|Auftraggeber(?:in)?|Bauherr(?:in)?)\s+")
_GROSSWORT_DAVOR = re.compile(r"([A-ZÄÖÜ][\wäöüß\-]{2,})[ \t]$")
_BEKANNT_FORMAT = re.compile(r"^[A-Z][A-Z0-9]*_(.+)_\d+$")
_KANON_FORMAT = re.compile(r"^([A-Z]+)_P[0-9a-z]+_E[0-9a-z]+_[0-9A-Z]{2}$")


class PostprocessError(ValueError):
    """Ergebnis waere widerspruechlich oder leckt — nie still ausliefern."""


def ist_aktiv() -> bool:
    return os.environ.get("BRIDGE_PSEUDONYM_POSTPROCESS", "").strip().lower() == "true"


# ── Hilfen ───────────────────────────────────────────────────────────────────

def _wortgrenzen_muster(wert: str) -> "re.Pattern[str]":
    return re.compile(r"(?<![\w])" + re.escape(wert) + r"(?![\w])")


def _tokens(wert: str) -> List[str]:
    return [t for t in re.split(r"[\s,;:()]+", wert) if t]


def _personen_kern(wert: str) -> List[str]:
    return [t for t in _tokens(wert) if t.casefold() not in _TITEL]


def _firmen_kern(wert: str) -> frozenset:
    return frozenset(t.casefold().strip(".-") for t in _tokens(wert) if t.casefold() not in _RECHTSFORM and t.strip(".-&+"))


def _bekannt_typ(platzhalter: str) -> str:
    m = _BEKANNT_FORMAT.match(platzhalter)
    if m:
        # {PREFIX}_{TYPE}_{NNN}; TYPE kann selbst Unterstriche tragen (PHONE_NUMBER)
        return m.group(1)
    m = _KANON_FORMAT.match(platzhalter)
    if m:
        return {"ORG": "ORGANIZATION", "LOC": "LOCATION", "PHONE": "PHONE_NUMBER", "EMAIL": "EMAIL_ADDRESS",
                "IBAN": "IBAN_CODE"}.get(m.group(1), m.group(1))
    raise PostprocessError(f"known_entities: Platzhalter {platzhalter!r} hat kein bekanntes Format")


# ── Schritte ─────────────────────────────────────────────────────────────────

def _firma_vervollstaendigen(text: str, s: Span, freiliste: Freiliste) -> Span:
    if s.type != "ORGANIZATION":
        return s
    wert = s.wert(text)
    m = _ROLLEN_PRAEFIX.match(wert)
    if m and _firmen_kern(wert[m.end():]):
        s = Span(s.start + m.end(), s.end, s.type, s.quelle, s.prio, s.confidence, meta={**s.meta, "rolle": "ab"})
        wert = s.wert(text)
    if not _firmen_kern(wert):
        # Nur Rechtsform erkannt ("ZT GmbH"): das Namenswort davor gehoert dazu
        # ("Halbwidl ZT GmbH" — sonst bleibt der Name Klartext, Audit L1).
        davor = _GROSSWORT_DAVOR.search(text, max(0, s.start - 60), s.start)
        if davor and davor.end() == s.start:
            wort = davor.group(1)
            if wort not in freiliste.woerter and wort.casefold() not in _RECHTSFORM:
                s = Span(davor.start(1), s.end, s.type, s.quelle, s.prio, s.confidence, meta={**s.meta, "erweitert": "links"})
    return s


def _bekannte_nachziehen(text: str, bekannt: Dict[str, str]) -> List[Span]:
    spans: List[Span] = []
    for ph, wert in sorted(bekannt.items(), key=lambda kv: len(kv[1]), reverse=True):
        if not isinstance(wert, str) or len(wert.strip()) < 3:
            continue
        if not re.search(r"[^\W\d_]", wert) and sum(c.isdigit() for c in wert) < 7:
            continue  # kurze reine Zahlen (EZ 2213) nie blind nachziehen
        typ = _bekannt_typ(ph)
        for m in _wortgrenzen_muster(wert).finditer(text):
            spans.append(Span(m.start(), m.end(), typ, "bekannt", PRIO_BEKANNT, 1.0, kanon=wert, platzhalter=ph))
    return spans


def _aufloesen(spans: List[Span], text: str) -> List[Span]:
    """Ueberlappende Fundstellen zu einer verschmelzen: Umfang = Vereinigung, Typ/Platzhalter
    vom staerksten Mitglied (Prioritaet, dann Laenge). Nie bleibt ein Teil Klartext."""
    if not spans:
        return []
    geordnet = sorted(spans, key=lambda s: (s.start, -s.end))
    gruppen: List[List[Span]] = [[geordnet[0]]]
    ende = geordnet[0].end
    for s in geordnet[1:]:
        if s.start < ende:
            gruppen[-1].append(s)
            ende = max(ende, s.end)
        else:
            gruppen.append([s])
            ende = s.end
    out: List[Span] = []
    for g in gruppen:
        start, stop = min(s.start for s in g), max(s.end for s in g)
        rep = max(g, key=lambda s: (s.prio, s.end - s.start, s.confidence))
        gleich = (rep.start, rep.end) == (start, stop)
        out.append(Span(
            start, stop, rep.type, rep.quelle, rep.prio, rep.confidence,
            kanon=rep.kanon if gleich else None,
            platzhalter=rep.platzhalter if gleich else None,
            meta={**rep.meta, **({"verschmolzen": str(len(g))} if len(g) > 1 else {})},
        ))
    return out


def _gleiche_werte_nachziehen(text: str, spans: List[Span]) -> List[Span]:
    """Jeder maskierte Wert wird an allen weiteren Stellen des Dokuments maskiert."""
    belegt = sorted((s.start, s.end) for s in spans)
    neu: List[Span] = []
    werte: Dict[str, Span] = {}
    for s in spans:
        werte.setdefault(s.wert(text), s)

    def frei(a: int, b: int) -> bool:
        return not any(a < e and s < b for s, e in belegt)

    for wert, vorbild in sorted(werte.items(), key=lambda kv: len(kv[0]), reverse=True):
        if len(wert.strip()) < 3:
            continue
        if not re.search(r"[^\W\d_]", wert) and sum(c.isdigit() for c in wert) < 7:
            continue
        for m in _wortgrenzen_muster(wert).finditer(text):
            if not frei(m.start(), m.end()):
                continue
            kandidat = Span(m.start(), m.end(), vorbild.type, "nachzug", PRIO_DETEKTOR, vorbild.confidence,
                            kanon=vorbild.kanon, platzhalter=vorbild.platzhalter if vorbild.prio == PRIO_BEKANNT else None)
            if kandidat.type in _URL_INNEN_TYPEN and in_url(text, kandidat):
                continue
            neu.append(kandidat)
            belegt.append((m.start(), m.end()))
    return spans + neu


_ADRESS_KOPF = re.compile(r"^\s*(.+?)[ \t]+(\d{1,4}[a-zA-Z]?)(?![\d.,]\d)")


def adress_schluessel(wert: str) -> Optional[Tuple[str, str]]:
    """Strasse + erste Hausnummer, schreibweisen-unabhaengig ("Sonnenhofstraße 12-16" ==
    "SONNENHOFSTRASSE 12–16"). None, wenn der Wert keine Adresse mit Hausnummer ist."""
    m = _ADRESS_KOPF.match(wert)
    if not m or not re.search(r"[^\W\d_]", m.group(1)):
        return None
    strasse = " ".join(m.group(1).casefold().replace("str.", "straße".casefold()).split())
    return strasse, m.group(2).casefold()


def _varianten_falten(werte: Dict[str, str], bekannt_werte: Dict[str, str]) -> Dict[str, str]:
    """Wert -> Vollform fuer eindeutige Schreibvarianten (Typ je Wert in ``werte``).

    PERSON: ein einzelner Name ("Muster", "DI Muster") -> die eine Vollform, deren
    letzter Namensteil er ist ("Andreas Muster"). ORGANIZATION: Firmenkern ist echte
    Teilmenge genau einer anderen Firma ("Steiner GmbH" -> "Hochbau Steiner GmbH").
    Mehrdeutig -> nicht falten (dann lieber zwei Platzhalter als eine falsche Identitaet).
    """
    alle = {**{w: t for w, t in bekannt_werte.items()}, **werte}
    falten: Dict[str, str] = {}
    personen = [w for w, t in alle.items() if t == "PERSON" and len(_personen_kern(w)) >= 2]
    firmen = [w for w, t in alle.items() if t == "ORGANIZATION" and _firmen_kern(w)]
    # LOCATION: dieselbe Adresse (Strasse + Hausnummer) in mehreren Schreibweisen ->
    # die ausfuehrlichste Fassung (nie auf eine kuerzere falten: Tuer/PLZ gingen verloren).
    adressen: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    for w, t in alle.items():
        k = adress_schluessel(w) if t == "LOCATION" else None
        if k:
            adressen[k].append(w)
    for w, t in werte.items():
        if t == "LOCATION":
            k = adress_schluessel(w)
            if k and len(adressen[k]) > 1:
                # ausfuehrlichste Fassung; bei gleichem Umfang die nicht durchgehend grosse
                # (Briefkopf/Ueberschrift "SONNENHOFSTRASSE ... WIEN" ist nicht die Normalform)
                def umfang(x: str) -> int:
                    return len(" ".join(x.casefold().split()))
                voll = max(adressen[k], key=lambda x: (umfang(x), not x.isupper(), x))
                if voll != w and umfang(voll) >= umfang(w):
                    falten[w] = voll
            continue
        if t == "PERSON":
            kern = _personen_kern(w)
            if len(kern) != 1 or len(kern[0]) < 3:
                continue
            kandidaten = {p for p in personen if p != w and _personen_kern(p)[-1] == kern[0]}
            voll = {p for p in kandidaten if _personen_kern(p) != kern}
            if len({" ".join(_personen_kern(p)) for p in voll}) == 1:
                falten[w] = sorted(voll, key=len)[-1]
        elif t == "ORGANIZATION":
            kern = _firmen_kern(w)
            if not kern:
                continue
            obermengen = [f for f in firmen if f != w and kern < _firmen_kern(f)]
            if not obermengen:
                # gleicher Kern, andere Schreibweise nur Zusatz der Rechtsform ("VELMARO" vs "Velmaro GmbH")
                gleich = [f for f in firmen if f != w and _firmen_kern(f) == kern and len(f) > len(w)]
                if len({_firmen_kern(f) for f in gleich}) == 1 and gleich:
                    falten[w] = max(gleich, key=len)
                continue
            maximal = [f for f in obermengen if not any(_firmen_kern(f) < _firmen_kern(g) for g in obermengen)]
            if len({_firmen_kern(f) for f in maximal}) == 1:
                falten[w] = max(maximal, key=len)
    return falten


def _platzhalter_vergeben(
    text: str, spans: List[Span], prefix: str, bekannt: Dict[str, str]
) -> Tuple[List[Span], Dict[str, str], Dict[str, int]]:
    # Wert -> Typ (Mehrheit, bei Gleichstand PERSON > ORG > LOC > Rest)
    typen: Dict[str, Counter] = defaultdict(Counter)
    for s in spans:
        if s.platzhalter is None:
            typen[s.kanon or s.wert(text)][s.type] += 1
    typ_je_wert = {
        w: sorted(c.items(), key=lambda kv: (-kv[1], _TYP_RANG.get(kv[0], 9), kv[0]))[0][0]
        for w, c in typen.items()
    }
    bekannt_werte: Dict[str, str] = {}
    ph_je_bekanntem_wert: Dict[str, str] = {}
    for ph, w in bekannt.items():
        if isinstance(w, str):
            bekannt_werte.setdefault(w, _bekannt_typ(ph))
            ph_je_bekanntem_wert.setdefault(w, ph)
    falten = _varianten_falten(typ_je_wert, bekannt_werte)

    belegt_nr: Dict[str, int] = defaultdict(int)
    for ph in bekannt:
        if ph.startswith(prefix + "_"):
            m = re.fullmatch(re.escape(prefix) + r"_(.+)_(\d+)", ph)
            if m:
                belegt_nr[m.group(1)] = max(belegt_nr[m.group(1)], int(m.group(2)))

    vergeben: Dict[str, str] = {}  # Vollform -> Platzhalter
    mapping: Dict[str, str] = {}
    stat = Counter()
    for s in sorted(spans, key=lambda s: s.start):
        if s.platzhalter is not None:
            mapping[s.platzhalter] = s.kanon if s.kanon is not None else s.wert(text)
            continue
        wert = s.kanon or s.wert(text)
        # Fundstelle mitten in einem Wort/Dateinamen ("Podhagskygasse 57_Angebot.pdf"): nie
        # auf eine Vollform falten, sonst aendert der Rueckweg den Dateinamen.
        angeklebt = (s.start > 0 and (text[s.start - 1].isalnum() or text[s.start - 1] == "_")) or (
            s.end < len(text) and (text[s.end].isalnum() or text[s.end] == "_"))
        voll = wert if angeklebt else falten.get(wert, wert)
        if voll != wert:
            stat["varianten_gefaltet"] += 1
        if voll in vergeben:
            ph = vergeben[voll]
        elif voll in ph_je_bekanntem_wert:
            ph = ph_je_bekanntem_wert[voll]
            vergeben[voll] = ph
        else:
            typ = typ_je_wert.get(voll) or bekannt_werte.get(voll) or s.type
            belegt_nr[typ] += 1
            ph = f"{prefix}_{typ}_{belegt_nr[typ]:03d}"
            vergeben[voll] = ph
        s.platzhalter = ph
        s.kanon = voll
        s.type = _bekannt_typ(ph) if voll in ph_je_bekanntem_wert else (typ_je_wert.get(voll) or s.type)
        mapping[ph] = voll
    return spans, mapping, dict(stat)


def _invarianten(text_aus: str, mapping: Dict[str, str], spans: List[Span], original: str) -> None:
    fehlend = [ph for ph in mapping if ph not in text_aus]
    if fehlend:
        raise PostprocessError(f"{len(fehlend)} Platzhalter fehlen im Ausgabetext: {fehlend[:5]}")
    lecks = []
    for s in spans:
        if s.type not in HARTE_TYPEN:
            continue
        wert = s.wert(original)
        if len(wert) > 3 and _wortgrenzen_muster(wert).search(text_aus):
            lecks.append(f"{s.platzhalter}({s.type})")
    if lecks:
        raise PostprocessError(f"hartes PII bleibt im Klartext stehen: {sorted(set(lecks))[:5]}")


# ── Einstieg ─────────────────────────────────────────────────────────────────

def postprocess_smart_anonymize(
    original: str,
    antwort: Dict[str, Any],
    prefix: Optional[str] = None,
    known_entities: Optional[Dict[str, str]] = None,
    freiliste: Optional[Freiliste] = None,
) -> Dict[str, Any]:
    """Nimmt die Antwort des Privacy-Dienstes und gibt eine Antwort derselben Form zurueck."""
    if antwort.get("status") != "success":
        return antwort
    prefix = prefix or "ANON"
    bekannt = dict(known_entities or {})
    fl = freiliste or lade_freiliste()
    anon = antwort.get("smart_anonymized_text")
    if anon is None:
        raise PostprocessError("Antwort ohne smart_anonymized_text")
    mapping_alt: Dict[str, str] = antwort.get("mapping") or {}
    typen = {e.get("placeholder"): e.get("type") for e in (antwort.get("detected_entities") or []) if e.get("placeholder")}
    konf = {e.get("placeholder"): e.get("confidence") or 1.0 for e in (antwort.get("detected_entities") or []) if e.get("placeholder")}

    detektor = spans_aus_antwort(original, anon, mapping_alt, prefix, typen, konf)
    verworfen: Counter = Counter()
    behalten: List[Span] = []
    for s in detektor:
        if s.type not in HARTE_TYPEN:
            grund = verwerfen(original, s, fl)
        else:
            # Harte Typen: Plausibilitaet; Freiliste nur fuer PERSON (Fachbegriff als Person
            # erkannt, z. B. "Sondenfeld") und nie die URL-Regel.
            grund = plausibilitaet(s.wert(original), s.type) or (
                fl.ist_frei(s.wert(original), s.type) if s.type == "PERSON" else None
            )
        if grund:
            verworfen[grund] += 1
            continue
        s = _firma_vervollstaendigen(original, s, fl)
        s = adresse_erweitern(original, s)
        behalten.append(s)

    regel = [
        adresse_erweitern(original, s) for s in erkenne(original)
        # Strassen-Erkenner: der Strassenname selbst darf kein Fachbegriff sein
        if not (s.quelle == "regel:strasse" and fl.ist_frei(s.wert(original), "LOCATION"))
    ]
    bekannte = _bekannte_nachziehen(original, bekannt)
    spans = _aufloesen(behalten + regel + bekannte, original)
    spans = _aufloesen(_gleiche_werte_nachziehen(original, spans), original)
    spans, mapping, stat = _platzhalter_vergeben(original, spans, prefix, bekannt)
    text_aus = ausgeben(original, spans)
    _invarianten(text_aus, mapping, spans, original)

    quellen = Counter(s.quelle.split(":")[0] for s in spans)
    regel_typen = Counter(s.type for s in spans if s.quelle.startswith("regel"))
    detected = [
        {
            "placeholder": s.platzhalter,
            "type": s.type,
            "original": s.wert(original),
            "confidence": s.confidence,
            "decision": "KEEP",
            "reason": s.quelle,
        }
        for s in spans
    ]
    return {
        **antwort,
        "raw_anonymized_text": text_aus,
        "raw_entity_count": len(spans),
        "smart_anonymized_text": text_aus,
        "smart_entity_count": len(mapping),
        "mapping": mapping,
        "detected_entities": detected,
        "postprocessing": {
            "version": VERSION,
            "detector_spans": len(detektor),
            "dropped": dict(verworfen),
            "rule_spans_by_type": dict(regel_typen),
            "spans_by_source": dict(quellen),
            "known_entities_in": len(bekannt),
            "placeholders": len(mapping),
            **stat,
        },
    }
