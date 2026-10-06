"""Regel-Erkenner fuer Kennungen, die der Detektor (Presidio + Flair) nicht kennt.

Jeder Erkenner ist ein Muster MIT Kontext, wo eine kontextfreie Form zu viele
Fachzahlen traefe. Maskiert wird nur der identifizierende Wert; das Schluesselwort
("geb.", "SV-Nr.", "FN", "EZ") bleibt lesbar, damit das Modell weiss, WAS dort stand.

Neue Entitaetstypen (bewusst ohne Unterstrich und kurz, damit auch aeltere Clients
daraus gueltige kanonische Tokens <= 32 Zeichen bauen koennen):
GEBURTSDATUM, SVNR, UIDNR, FIRMENBUCH, GRUNDBUCH, ZAEHLPUNKT, AKTENZEICHEN,
KENNZEICHEN, STEUERNR. Telefon mit Schraegstrich -> PHONE_NUMBER, IBAN ohne
gueltige Pruefziffer -> IBAN_CODE, Strasse + Hausnummer -> LOCATION.
"""

from __future__ import annotations

import re
from typing import Callable, Dict, List, Pattern, Tuple

from .spans import PRIO_REGEL, Span

# ── Bausteine ────────────────────────────────────────────────────────────────

_MONATE = (
    "Jänner|Januar|Jan\\.|Februar|Feber|Feb\\.|März|Maerz|April|Apr\\.|Mai|Juni|Juli|August|Aug\\.|"
    "September|Sept?\\.|Oktober|Okt\\.|November|Nov\\.|Dezember|Dez\\."
)
DATUM = (
    r"(?:\d{1,2}\.\s?\d{1,2}\.\s?(?:19|20)?\d{2}(?!\d)"
    r"|\d{1,2}\.\s?(?:" + _MONATE + r")\s+(?:19|20)\d{2}"
    r"|(?:19|20)\d{2}-\d{2}-\d{2})"
)
_GROSS_WORT = r"[A-ZÄÖÜ][a-zäöüß]+(?:-[A-ZÄÖÜ][a-zäöüß]+)?"

# Strassen-Endungen (auch als eigenes Wort: "Wiener Straße")
_STRASSEN_ENDUNG = (
    r"(?:straße|strasse|str\.|gasse|weg|platz|allee|ring|kai|zeile|lände|lande|promenade|ufer|steig|"
    r"graben|markt|damm|gürtel|guertel|hof|siedlung|berg|feld|au|anger|steg)"
)
# Fuer den kontextfreien Strassen-Erkenner nur eindeutige Endungen ("Sondenfeld 2" ist keine Adresse).
_STRASSEN_ENDUNG_STRENG = r"(?:straße|strasse|gasse|weg|platz|allee|kai|zeile|lände|promenade|ufer|gürtel|guertel)"
STRASSE_WERT = re.compile(r"(?:^|.*[a-zäöüß\s])" + _STRASSEN_ENDUNG + r"$", re.IGNORECASE)

ADRESS_REST = re.compile(
    r"""
    [ \t]*\d{1,4}[a-zA-Z]?(?![\d.,]\d)                       # Hausnummer
    (?:[ \t]*[-–][ \t]*\d{1,4}[a-zA-Z]?)?                    # Bereich 12-16
    (?:[ \t]*/[ \t]*\d{1,4}[a-zA-Z]?){0,3}                   # /Stiege/Tuer
    (?:[ \t]*,?[ \t]*(?:Stiege|Stg\.)[ \t]*\d{1,3}[a-zA-Z]?)?
    (?:[ \t]*,?[ \t]*(?:Top|Tür|Tuer|Whg\.)[ \t]*\d{1,4}[a-zA-Z]?)?
    (?:[ \t]*,?\s{0,2}(?:[AD]-)?\d{4,5}[ \t]+[A-ZÄÖÜ][\wäöüß\-]*(?:[ \t]+(?:an[ \t]+der|am|im|in|bei)[ \t]+[A-ZÄÖÜ][\wäöüß\-]*)?)?
    """,
    re.VERBOSE,
)


def _rx(p: str, flags: int = 0) -> Pattern[str]:
    return re.compile(p, flags)


# (name, typ, regex mit Gruppe v, optionaler Pruefer)
_ERKENNER: List[Tuple[str, str, Pattern[str], Callable[[str], bool] | None]] = []


def _erkenner(name: str, typ: str, regex: str, flags: int = 0, pruefer: Callable[[str], bool] | None = None) -> None:
    _ERKENNER.append((name, typ, _rx(regex, flags), pruefer))


# ── Pruefer ──────────────────────────────────────────────────────────────────

def svnr_pruefziffer_ok(wert: str) -> bool:
    """Oesterreichische Versicherungsnummer: 3 Laufziffern, Pruefziffer, TTMMJJ."""
    z = re.sub(r"\D", "", wert)
    if len(z) != 10:
        return False
    gewichte = (3, 7, 9, 0, 5, 8, 4, 2, 1, 6)
    summe = sum(int(c) * g for c, g in zip(z, gewichte)) % 11
    if summe == 10 or summe != int(z[3]):
        return False
    tag, monat = int(z[4:6]), int(z[6:8])
    return 1 <= tag <= 31 and 1 <= monat <= 12


# ── Erkenner ─────────────────────────────────────────────────────────────────

# 1. Geburtsdatum: nur mit Kontext. Ohne Kontext ist ein Datum Fachinhalt.
_erkenner(
    "geburtsdatum", "GEBURTSDATUM",
    r"(?:\bgeb(?:oren)?\.?(?:[ \t]+am)?|\bGeburtsdatum|\bGeb\.?-?Datum|\bGeburtstag|\bGeb\.[ \t]?dat\.?|\bdate[ \t]+of[ \t]+birth|\bDOB)"
    r"[ \t]*:?[ \t]*(?P<v>" + DATUM + r")",
    re.IGNORECASE,
)

# 2. SV-Nummer: mit Kontext jede 10-stellige Form; ohne Kontext nur mit gueltiger Pruefziffer.
_SV_KONTEXT = (
    r"(?:\bSV-?[ \t]?Nr\.?|\bSVNR\b|\bSV-?Nummer|\bSozialversicherungs-?(?:nummer|nr\.?)|\bVersicherungsnummer|\bVSNR\b|\bVers\.-?Nr\.?)"
)
_erkenner("svnr", "SVNR", _SV_KONTEXT + r"[ \t]*:?[ \t]*(?P<v>\d{4}[ \t]?\d{6}|\d{4}[ \t]?\d{2}[ \t]?\d{2}[ \t]?\d{2})(?!\d)", re.IGNORECASE)
_erkenner("svnr_pruefziffer", "SVNR", r"(?<![\d/.,])(?P<v>\d{4}[ \t]\d{6})(?![\d/.,]\d)", 0, svnr_pruefziffer_ok)

# 3. UID (ATU + 8 Ziffern, auch gruppiert) und auslaendische USt-IdNr mit Kontext.
_erkenner("uid_at", "UIDNR", r"(?<![\w])(?P<v>ATU[ \t]?\d{2}[ \t]?\d{3}[ \t]?\d{3})(?!\d)")
_erkenner(
    "uid_kontext", "UIDNR",
    r"(?:\bUID(?:-Nr\.?)?|\bUSt-?Id(?:Nr\.?|-Nr\.?)?|\bUmsatzsteuer-?Identifikationsnummer|\bVAT(?:[ \t]?(?:No\.?|ID))?)[ \t]*:?[ \t]*"
    r"(?P<v>(?:DE|CH|IT|CZ|SK|HU|SI|LU|NL|FR|BE)[ \t]?[0-9A-Z][0-9A-Z \t.]{6,14}[0-9A-Z])",
)

# 4. Firmenbuchnummer "FN 512334 t" — nur Nummer + Pruefbuchstabe maskieren.
_erkenner("firmenbuch", "FIRMENBUCH", r"(?:\bFN|\bFirmenbuch(?:nummer|-?Nr\.?)?:?(?:[ \t]*FN)?)[ \t]*(?P<v>\d{1,6}[ \t]?[a-z])(?![\w])")

# 5. Grundbuch: EZ, KG (Nummer + Name), Grundstuecksnummer.
_erkenner("grundbuch_ez", "GRUNDBUCH", r"\b(?:EZ|Einlagezahl)\.?[ \t]*(?:Nr\.?[ \t]*)?(?P<v>\d{1,5})(?![\d/])")
_erkenner(
    "grundbuch_kg", "GRUNDBUCH",
    r"\b(?:KG|Katastralgemeinde)\.?[ \t]*(?:Nr\.?[ \t]*)?(?P<v>\d{5}(?:[ \t]+" + _GROSS_WORT + r")?)(?![\d])",
)
_erkenner(
    "grundbuch_gst", "GRUNDBUCH",
    r"(?:\bGrundstück(?:s)?(?:-?nummer|-?nr\.?)?|\bGst\.?(?:-?Nr\.?)?|\bParzelle(?:n(?:nummer|-?nr\.?))?)[ \t]*(?:Nr\.?[ \t]*)?:?[ \t]*"
    r"(?P<v>\d{1,5}(?:/\d{1,4})?)(?![\d])",
    re.IGNORECASE,
)

# 6. Zaehlpunkt: AT00 + Netzbetreiber + ... (IBAN-Pruefziffer "00" gibt es nicht).
_erkenner("zaehlpunkt", "ZAEHLPUNKT", r"(?<![\w])(?P<v>AT[ \t]?00(?:[ \t]?[0-9A-Z]){12,29})(?![0-9A-Z])")
_erkenner(
    "zaehlpunkt_kontext", "ZAEHLPUNKT",
    r"\bZählpunkt(?:nummer|-?Nr\.?|bezeichnung)?[ \t]*:?[ \t]*(?P<v>[A-Z]{2}(?:[ \t]?[0-9A-Z]){10,31})(?![0-9A-Z])",
    re.IGNORECASE,
)

# 7. Aktenzeichen / Geschaeftszahl (Behoerde, Gericht, Sachverstaendiger) + Gerichtsaktenzeichen.
_erkenner(
    "aktenzeichen", "AKTENZEICHEN",
    r"(?:\bGZ\b\.?|\bGz\.|\bGeschäftszahl|\bGeschaeftszahl|\bAktenzahl|\bAktenzeichen|\bAZ\b\.?|\bAz\.|\bZl\.|\bUnser[ \t]+Zeichen|\bIhr[ \t]+Zeichen)"
    r"[ \t]*:?[ \t]*(?P<v>[A-Za-z0-9]+(?:[-/.][A-Za-z0-9]+)+|\d{3,})(?![\w-])",
)
_erkenner(
    "gerichtsaktenzeichen", "AKTENZEICHEN",
    r"(?<![\w/.])(?P<v>\d{1,3}[ \t]?(?:Cg|Cga|C|Ob|Os|Nc|Hc|Fam|Ps|P|U|Hv|Bl|Ds|St|Msch|Rs|Ra|Bs|Nb|E|R|S)[ \t]?\d{1,6}/\d{2}[a-z]?)(?![\w/])",
)

# 8. Telefon mit Schraegstrich (der Detektor schliesst "/" absichtlich aus, wegen GZ).
_erkenner(
    "telefon_schraeg_kontext", "PHONE_NUMBER",
    r"(?:\bTel(?:efon)?\.?(?:-?Nr\.?)?|\bMobil(?:telefon|nummer)?|\bmobil|\bHandy|\bTelefax|\bFax|\bT\.|\bM\.)[ \t]*:?[ \t]*"
    r"(?P<v>(?:\+\d{2,3}[ \t]?(?:\(0\)[ \t]?)?|0)\d{1,4}[ \t]?/[ \t]?\d{2,}(?:[ \t\-]?\d{2,})*)(?![\d])",
    re.IGNORECASE,
)
_erkenner("telefon_schraeg", "PHONE_NUMBER", r"(?<![\w/.])(?P<v>0\d{2,4}[ \t]?/[ \t]?\d{5,}(?:[ \t\-]\d{2,})*)(?![\w/])")

# 9. Kfz-Kennzeichen: nur mit Kontext (sonst traefe es Normbezeichnungen).
_erkenner(
    "kennzeichen", "KENNZEICHEN",
    r"(?:\bKennzeichen|\bKfz-?Kennz(?:eichen|\.)?|\bamtl\.[ \t]?Kennz(?:eichen|\.)?|\bKfz\b|\bKFZ\b|\bFahrzeug)[^\n.;]{0,40}?"
    r"(?<![\w-])(?P<v>[A-ZÄÖÜ]{1,3}(?:[ \t]?-[ \t]?|[ \t])(?:\d{1,5}[ \t]?[A-Z]{1,3}|[A-Z]{1,2}[ \t]?\d{1,4}[EH]?))(?![\w])",
)

# 10. Steuernummer: nur mit Kontext.
_erkenner(
    "steuernummer", "STEUERNR",
    r"(?:\bSteuernummer|\bSteuer-?Nr\.?|\bSt\.?-?Nr\.?|\bStNr\.?)[ \t]*:?[ \t]*(?P<v>\d{2}[ \t]?\d{3}/\d{4}|\d{2,3}/\d{3}/\d{4,5})(?![\d])",
)

# 11. IBAN-foermig mit Konto-Kontext (auch ohne gueltige Pruefziffer — Tippfehler sind
#     trotzdem Kontodaten; echte IBANs erkennt der Detektor selbst).
_erkenner(
    "iban_kontext", "IBAN_CODE",
    r"(?:\bIBAN|\bKonto(?:nummer)?|\bKto\.?(?:-?Nr\.?)?|\bBankverbindung)[^\n\d]{0,30}?"
    r"(?<![\w])(?P<v>[A-Z]{2}\d{2}(?:[ \t]?[0-9A-Z]{4}){2,7}(?:[ \t]?[0-9A-Z]{1,4})?)(?![0-9A-Za-z])",
)

# 12. Strasse + Hausnummer, falls der Detektor die Strasse nicht als Ort erkannt hat.
_erkenner(
    "strasse", "LOCATION",
    r"(?<![\w])(?P<v>(?:[A-ZÄÖÜ][a-zäöüß\-]*" + _STRASSEN_ENDUNG_STRENG + r"|[A-ZÄÖÜ][a-zäöüß]+[ \t](?:Straße|Strasse|Str\.|Gasse|Weg|Platz|Allee|Ring|Gürtel|Kai|Zeile|Lände)))"
    r"(?=[ \t]+\d{1,4}[a-zA-Z]?(?:[ \t]*[,/\-–]|[ \t]+\d{4}|\s*$|[ \t]*\n))",
)


def erkenne(text: str) -> List[Span]:
    """Alle Regel-Fundstellen im Text (koennen sich ueberlappen; aufgeloest wird spaeter)."""
    spans: List[Span] = []
    for name, typ, regex, pruefer in _ERKENNER:
        for m in regex.finditer(text):
            wert = m.group("v")
            if not wert or not wert.strip():
                continue
            if pruefer is not None and not pruefer(wert):
                continue
            s, e = m.start("v"), m.end("v")
            while e > s and text[e - 1] in " \t":
                e -= 1
            spans.append(Span(s, e, typ, f"regel:{name}", PRIO_REGEL, 0.9))
    return spans


def erkenner_namen() -> Dict[str, str]:
    return {name: typ for name, typ, _, _ in _ERKENNER}


def adresse_erweitern(text: str, span: Span) -> Span:
    """Strassenname -> ganzer Adressbereich (Nr/Stiege/Tuer, PLZ Ort)."""
    if span.type != "LOCATION" or not STRASSE_WERT.match(span.wert(text).strip()):
        return span
    m = ADRESS_REST.match(text, span.end)
    if not m or m.end() == span.end:
        return span
    ende = m.end()
    while ende > span.end and text[ende - 1] in " \t,":
        ende -= 1
    return Span(span.start, ende, span.type, span.quelle, span.prio, span.confidence, meta={**span.meta, "adresse": "1"})
