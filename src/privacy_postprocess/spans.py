"""Fundstellen (Spans) der Pseudonymisierung: aus der Antwort des Privacy-Dienstes
zurueckgewinnen und am Ende wieder als Text + Mapping ausgeben.

Der Privacy-Dienst (gpu-privacy-1, von Dev- UND Prod-Bridge geteilt) liefert keine
Positionen, nur den ersetzten Text und ``Platzhalter -> Original``. Weil der
Worker den Originaltext kennt, laesst sich jede Ersetzung eindeutig zurueckrechnen:
der ersetzte Text ist eine Folge aus woertlichem Originaltext und Platzhaltern, und
jeder Platzhalter steht fuer genau seinen Originalwert. Passt eine Stelle nicht,
ist die Antwort in sich widerspruechlich — dann wird laut abgebrochen, nie geraten.
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


def spans_aus_antwort(
    original: str,
    anonymisiert: str,
    mapping: Dict[str, str],
    prefix: str,
    typ_je_platzhalter: Optional[Dict[str, str]] = None,
    confidence_je_platzhalter: Optional[Dict[str, float]] = None,
) -> List[Span]:
    """Rechnet jede Ersetzung des Privacy-Dienstes auf eine Fundstelle im Original zurueck."""
    if not mapping:
        if anonymisiert != original:
            raise AlignmentError("leeres Mapping, aber der Text wurde veraendert")
        return []
    typen = typ_je_platzhalter or {}
    conf = confidence_je_platzhalter or {}
    muster = re.compile("|".join(re.escape(k) for k in sorted(mapping, key=len, reverse=True)))
    spans: List[Span] = []
    pos = 0
    zuletzt = 0
    for m in muster.finditer(anonymisiert):
        woertlich = anonymisiert[zuletzt:m.start()]
        if original[pos:pos + len(woertlich)] != woertlich:
            raise AlignmentError(f"woertlicher Abschnitt vor {m.group(0)} weicht vom Original ab (Offset {pos})")
        pos += len(woertlich)
        wert = mapping[m.group(0)]
        if not original.startswith(wert, pos):
            raise AlignmentError(f"Platzhalter {m.group(0)} steht nicht fuer den Originaltext an Offset {pos}")
        typ = typen.get(m.group(0)) or platzhalter_typ(m.group(0), prefix)
        spans.append(Span(pos, pos + len(wert), typ, "detektor", PRIO_DETEKTOR, float(conf.get(m.group(0), 1.0))))
        pos += len(wert)
        zuletzt = m.end()
    if original[pos:] != anonymisiert[zuletzt:]:
        raise AlignmentError("Textende weicht vom Original ab")
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
