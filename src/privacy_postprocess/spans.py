"""Fundstellen (Spans) der Pseudonymisierung: aus der Antwort des Privacy-Dienstes
zurueckgewinnen und am Ende wieder als Text + Mapping ausgeben.

Der Privacy-Dienst (gpu-privacy-1, von Dev- UND Prod-Bridge geteilt) liefert keine
Positionen, nur den ersetzten Text und ``Platzhalter -> Original``. Weil der
Worker den Originaltext kennt, laesst sich jede Ersetzung eindeutig zurueckrechnen:
der ersetzte Text ist eine Folge aus woertlichem Originaltext und Platzhaltern, und
jeder Platzhalter steht fuer genau seinen Originalwert. Passt eine Stelle nicht,
ist die Antwort in sich widerspruechlich — dann wird laut abgebrochen, nie geraten.

Ein Platzhalterwort kann aber schon im ORIGINAL stehen: Energy pseudonymisiert
Kontextdokumente vor Phase 1, und ein daraus erzeugter Rechercheauftrag traegt diese
Woerter (``ANON_LOCATION_007`` …) woertlich. Vergibt der Dienst dieselbe Nummer neu,
sieht das Literal im ersetzten Text aus wie der neue Platzhalter. Deshalb wird jede
Fundstelle gegen das Original entschieden: steht dort der Mapping-Wert, ist es eine
Ersetzung des Dienstes; steht dort das Platzhalterwort selbst, ist es Text und bleibt
Text; steht dort etwas Drittes, ist es ein Fehler (Befund 02.11.2026, N1 Phase 4).

Fehlertexte nennen Typ, Platzhalter, Offset und Laengen — nie den Originaltext.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional


class AlignmentError(ValueError):
    """Die Antwort des Privacy-Dienstes laesst sich nicht auf den Originaltext abbilden."""


# Prioritaet bei Ueberlappungen: bekannte Akt-Entitaet > Regel-Erkenner > Detektor.
PRIO_DETEKTOR = 1
PRIO_REGEL = 2
PRIO_BEKANNT = 3


@dataclass
class Span:
    start: int
    end: int
    type: str
    quelle: str  # "detektor", "regel:<name>", "bekannt", "nachzug"
    prio: int = PRIO_DETEKTOR
    confidence: float = 1.0
    # Wert, der ins Mapping kommt. Normalfall: der Text an der Fundstelle. Bei einer
    # zusammengefuehrten Schreibvariante (nur Nachname, Firmenkern) die Vollform.
    kanon: Optional[str] = None
    platzhalter: Optional[str] = None  # vorgegeben (bekannte Entitaet) oder vergeben
    meta: Dict[str, str] = field(default_factory=dict)

    def wert(self, text: str) -> str:
        return text[self.start:self.end]

    def ueberlappt(self, other: "Span") -> bool:
        return self.start < other.end and other.start < self.end


def platzhalter_typ(platzhalter: str, prefix: str) -> str:
    """``SN_PHONE_NUMBER_004`` -> ``PHONE_NUMBER`` (Praefix bekannt)."""
    p = prefix + "_"
    if not platzhalter.startswith(p):
        raise AlignmentError(f"Platzhalter {platzhalter!r} traegt nicht den Praefix {prefix!r}")
    m = re.fullmatch(r"(.+)_(\d+)", platzhalter[len(p):])
    if not m:
        raise AlignmentError(f"Platzhalter {platzhalter!r} folgt nicht {{PREFIX}}_{{TYPE}}_{{NNN}}")
    return m.group(1)


def _befund(grund: str, platzhalter: str, typ: str, offset: int, erwartet: int, gefunden: Optional[int]) -> str:
    """Benannter Befund ohne Klartext: was, welcher Platzhalter (Typ), wo, wie lang."""
    return (f"{grund}: Platzhalter {platzhalter} ({typ}) an Offset {offset}, erwartete Spanne "
            f"{erwartet} Zeichen, im Original bis zum naechsten woertlichen Anker "
            f"{'keine passende Spanne' if gefunden is None else f'{gefunden} Zeichen'}")


def _spanne_bis_anker(original: str, pos: int, anker: str) -> Optional[int]:
    """Laenge von ``pos`` bis zum naechsten Vorkommen des woertlichen Ankers (nur fuer den Befund)."""
    if not anker:
        return None
    q = original.find(anker, pos + 1)
    return None if q < 0 else q - pos


def spans_aus_antwort(
    original: str,
    anonymisiert: str,
    mapping: Dict[str, str],
    prefix: str,
    typ_je_platzhalter: Optional[Dict[str, str]] = None,
    confidence_je_platzhalter: Optional[Dict[str, float]] = None,
    protokoll: Optional[Dict[str, int]] = None,
) -> List[Span]:
    """Rechnet jede Ersetzung des Privacy-Dienstes auf eine Fundstelle im Original zurueck.

    ``protokoll`` (optional) erhaelt ``platzhalterwoerter_im_original``: Anzahl der
    Platzhalterwoerter, die schon im Original standen und deshalb Text bleiben.
    """
    if not mapping:
        if anonymisiert != original:
            raise AlignmentError("leeres Mapping, aber der Text wurde veraendert")
        return []
    typen = typ_je_platzhalter or {}
    conf = confidence_je_platzhalter or {}
    muster = re.compile("|".join(re.escape(k) for k in sorted(mapping, key=len, reverse=True)))
    treffer = list(muster.finditer(anonymisiert))
    spans: List[Span] = []
    literale = 0
    pos = 0
    zuletzt = 0
    for i, m in enumerate(treffer):
        ph = m.group(0)
        typ = typen.get(ph) or platzhalter_typ(ph, prefix)
        woertlich = anonymisiert[zuletzt:m.start()]
        if original[pos:pos + len(woertlich)] != woertlich:
            raise AlignmentError(_befund("woertlicher Abschnitt vor dem Platzhalter weicht vom Original ab",
                                         ph, typ, pos, len(woertlich), None))
        pos += len(woertlich)
        wert = mapping[ph]
        if original.startswith(wert, pos):
            # Ersetzung des Dienstes: an dieser Stelle stand der Mapping-Wert.
            spans.append(Span(pos, pos + len(wert), typ, "detektor", PRIO_DETEKTOR, float(conf.get(ph, 1.0))))
            pos += len(wert)
        elif original.startswith(ph, pos):
            # Das Platzhalterwort stand schon woertlich im Original (fruehere Pseudonymisierung):
            # keine Ersetzung des Dienstes, es bleibt Text. Kein Span, kein Mapping-Eintrag.
            literale += 1
            pos += len(ph)
        else:
            naechster = treffer[i + 1].start() if i + 1 < len(treffer) else len(anonymisiert)
            raise AlignmentError(_befund("Platzhalter steht nicht fuer den Originaltext", ph, typ, pos, len(wert),
                                         _spanne_bis_anker(original, pos, anonymisiert[m.end():naechster])))
        zuletzt = m.end()
    if original[pos:] != anonymisiert[zuletzt:]:
        raise AlignmentError(f"Textende weicht vom Original ab (Offset {pos}, Rest Original {len(original) - pos} "
                             f"Zeichen, Rest Antwort {len(anonymisiert) - zuletzt} Zeichen)")
    if protokoll is not None:
        protokoll["platzhalterwoerter_im_original"] = literale
    return spans


def ausgeben(original: str, spans: Iterable[Span]) -> str:
    """Setzt die (nicht ueberlappenden, mit Platzhalter versehenen) Spans in den Text."""
    teile: List[str] = []
    pos = 0
    for s in sorted(spans, key=lambda s: s.start):
        if s.start < pos:
            raise ValueError(f"Spans ueberlappen bei Offset {s.start}")
        if not s.platzhalter:
            raise ValueError(f"Span bei Offset {s.start} ohne Platzhalter")
        teile.append(original[pos:s.start])
        teile.append(s.platzhalter)
        pos = s.end
    teile.append(original[pos:])
    return "".join(teile)
