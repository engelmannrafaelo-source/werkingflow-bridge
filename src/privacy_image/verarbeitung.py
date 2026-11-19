"""Bildverarbeitung der Bild-PII-Maske: feste Reihenfolge, nur im Speicher (Bildvertrag v4 A-BV-1/A-BV-2, Schnittstelle v3).

Reihenfolge (Schnittstelle v3 §1, A-SS-1b), keine andere zulaessig:
  1 Magic Bytes, sha256 der Eingangsbytes (final: == eingang_sha256)
  2 Frame 0 dekodieren (25-MP-Grenze VOR dem Laden)
  3 EXIF-Orientierung physisch anwenden
  4 auf Modellmasse skalieren (lange Kante min(Original, 1568), round() je Achse)
  5 Alpha auf deckend Weiss flachrechnen, RGB
  6 OCR + Erkennung -> Maske je Treffer (Wortboxen + kleiner Rand; unsicher groesser; unklar ganze Zeile);
    schwaerzen Automaske UNION Handmasken, deckend (0,0,0)
  7 neu kodieren ohne Metadaten (PNG, sonst JPEG q85/75/65/55 bis <= 400 000 B64, sonst 413)
  8 Metadaten-Scan der Ausgabebytes (Fund -> 500)
  9 sha256 der Ausgabebytes
Das Token (Schritt 10) stellt die Bridge aus (src.privacy_image.token), nicht diese Funktion.

Speicher: keine Datei, kein tempfile. OCR und Erkennung werden als Funktionen hereingereicht (``Ocr``/``Erkennung``),
damit dieser Kern ohne Modell/Netz pruefbar ist. Fehlertexte enthalten nie Eingabedaten (BildFehler).
"""
from __future__ import annotations

import base64
import hashlib
import io
import struct
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Optional, Sequence, Tuple

from src.privacy_image.fehler import BildFehler
from src.privacy_image.formular import Kasten

MODELL_KANTE = 1568
MIN_KANTE = 64
MAX_PIXEL = 25_000_000
MAX_B64 = 400_000
JPEG_QUALITAETEN = (85, 75, 65, 55)
PNG_SIG = b"\x89PNG\r\n\x1a\n"
JPEG_SIG = b"\xff\xd8\xff"

#: Erlaubte PNG-Chunks bzw. JPEG-Marker der AUSGABE (v4 A-BV-1 Punkt 4).
PNG_ERLAUBT = frozenset({b"IHDR", b"PLTE", b"IDAT", b"IEND", b"tRNS", b"gAMA", b"sRGB"})
JPEG_ERLAUBT = frozenset({0xD8, 0xE0, 0xDB, 0xC0, 0xC1, 0xC2, 0xC4, 0xDA, 0xD9, 0xDD})


@dataclass
class OcrZeile:
    """Eine OCR-Textzeile im Raum des uebergebenen Bildes (Pixel), Text nur im Speicher."""
    x0: int
    y0: int
    x1: int
    y1: int
    text: str
    #: Wortboxen der OCR (x0, y0, x1, y1, start, ende) mit Offsets im Zeilentext; None = keine belegte Zuordnung.
    woerter: Optional[Tuple[Tuple[int, int, int, int, int, int], ...]] = None


#: OCR: RGB-PIL-Bild -> Zeilen. Muss im Speicher arbeiten (keine Dateien).
Ocr = Callable[[object], Sequence[OcrZeile]]
#: Erkennung: Text -> [(start, ende, typ)] (Offsets im uebergebenen Text).
Erkennung = Callable[[str], Awaitable[Sequence[Tuple[int, int, str]]]]

GRUENDE = frozenset({"PERSON", "EMAIL_ADDRESS", "PHONE_NUMBER", "LOCATION", "ORGANIZATION", "IBAN_CODE", "URL"})


@dataclass
class Ergebnis:
    modus: str
    eingang_sha256: str
    mime: str
    bytes_: bytes
    breite: int
    hoehe: int
    sha256: str
    zeilen: int
    masken: List[Tuple[int, int, int, int, str]] = field(default_factory=list)
    handmasken_angewendet: int = 0

    @property
    def leer(self) -> bool:
        return self.zeilen == 0

    @property
    def b64(self) -> str:
        return base64.b64encode(self.bytes_).decode("ascii")


def magic(roh: bytes) -> str:
    if roh.startswith(PNG_SIG):
        return "image/png"
    if roh.startswith(JPEG_SIG):
        return "image/jpeg"
    raise BildFehler(415, "image_type_unsupported")


def modell_masse(breite: int, hoehe: int) -> Tuple[int, int]:
    lang = max(breite, hoehe)
    if lang <= MODELL_KANTE:
        return breite, hoehe
    f = MODELL_KANTE / lang
    return max(1, round(breite * f)), max(1, round(hoehe * f))


def _dekodieren(roh: bytes):
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        im = Image.open(io.BytesIO(roh))
        w, h = im.size
        if w * h > MAX_PIXEL:
            raise BildFehler(413, "image_too_large")
        im.seek(0)                                  # Schritt 2: nur Frame 0
        im.load()
        frame0 = im.copy()                          # loest vom Mehrbild-Container
        exif = im.getexif()
    except BildFehler:
        raise
    except (UnidentifiedImageError, OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        raise BildFehler(422, "image_unreadable")
    # Schritt 3: Orientierung physisch (exif_transpose liest Tag 0x0112 des Originals)
    if exif.get(0x0112) not in (None, 1):
        frame0.info["exif"] = exif.tobytes()
        try:
            frame0 = ImageOps.exif_transpose(frame0)
        except Exception:
            raise BildFehler(422, "image_unreadable")
    if min(frame0.size) < MIN_KANTE:
        raise BildFehler(422, "image_unreadable")
    return frame0


def _flach_rgb(im):
    """Schritt 5: Alpha deckend auf Weiss; Ergebnis ist ein FRISCHES RGB-Bild ohne info (keine Metadaten-Erbschaft)."""
    from PIL import Image

    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        rgba = im.convert("RGBA")
        grund = Image.new("RGB", rgba.size, (255, 255, 255))
        grund.paste(rgba, mask=rgba.getchannel("A"))
        quelle = grund
    else:
        quelle = im.convert("RGB")
    sauber = Image.new("RGB", quelle.size)
    sauber.paste(quelle)
    return sauber


def _zeilen_maske(z: OcrZeile, b: int, h: int):
    """Fail-closed (v1 §2.4): ganze OCR-Zeile + Rand max(25 px, 0,6 x Zeilenhoehe)."""
    rand = max(25, int(0.6 * (z.y1 - z.y0) + 0.999))
    return max(0, z.x0 - rand), max(0, z.y0 - rand), min(b, z.x1 + rand), min(h, z.y1 + rand)


def _wort_box(z: OcrZeile, ls: int, le: int):
    """Box fuer die Zeichen [ls, le) der Zeile -> (x0, y0, x1, y1, sicher) oder None (Zuordnung unklar).

    sicher: alle Nicht-Leerzeichen des Spans liegen in OCR-Wortboxen. Sonst Interpolation ueber die Zeichenbreite,
    nur fuer liegende Zeilen (unsicher -> groesserer Rand beim Aufrufer)."""
    if z.woerter:
        teil = [w for w in z.woerter if w[4] < le and ls < w[5]]
        gedeckt = set()
        for w in teil:
            gedeckt.update(range(max(ls, w[4]), min(le, w[5])))
        if teil and all(i in gedeckt for i in range(ls, le) if not z.text[i].isspace()):
            return (min(w[0] for w in teil), min(w[1] for w in teil),
                    max(w[2] for w in teil), max(w[3] for w in teil), True)
    n = len(z.text)
    breite, hoehe = z.x1 - z.x0, z.y1 - z.y0
    if n == 0 or breite < hoehe:
        return None
    zb = breite / n
    return int(z.x0 + ls * zb), z.y0, int(z.x0 + le * zb + 0.999), z.y1, False


def _treffer_masken(zeilen: Sequence[OcrZeile], treffer: Sequence[Tuple[int, int, str]], b: int, h: int):
    """Maske je Treffer und Zeile (Auftrag wortgenau, Kanzler 19.11.2026): belegte Wortboxen + Rand
    max(4 px, 0,25 x Zeilenhoehe); interpoliert max(10 px, 0,5 x Zeilenhoehe), waagrecht zusaetzlich >= 2 Zeichen;
    Zuordnung unklar -> ganze Zeile wie v1 (fail-closed). Ausgabeformat unveraendert (x, y, w, h, grund)."""
    out = []
    pos = 0
    bereiche = []
    for z in zeilen:
        bereiche.append((pos, pos + len(z.text)))
        pos += len(z.text) + 1
    for (a, e), z in zip(bereiche, zeilen):
        zh = max(1, z.y1 - z.y0)
        for s, en, typ in treffer:
            if not (s < e and a < en):
                continue
            grund = typ if typ in GRUENDE else "SONSTIGE"
            box = _wort_box(z, max(s, a) - a, min(en, e) - a)
            if box is None:
                x0, y0, x1, y1 = _zeilen_maske(z, b, h)
            else:
                bx0, by0, bx1, by1, sicher = box
                if sicher:
                    rx = ry = max(4, int(0.25 * zh + 0.999))
                else:
                    ry = max(10, int(0.5 * zh + 0.999))
                    rx = max(ry, int(2 * (z.x1 - z.x0) / max(1, len(z.text)) + 0.999))
                x0, y0 = max(0, bx0 - rx), max(0, by0 - ry)
                x1, y1 = min(b, bx1 + rx), min(h, by1 + ry)
            if x1 > x0 and y1 > y0:
                out.append((x0, y0, x1 - x0, y1 - y0, grund))
    return out


def _anlagenkennungen_frei(text: str, treffer: Sequence[Tuple[int, int, str]]):
    """Technische Anlagenkennungen frei wie im Textweg: dieselbe Regel R3 (anlagenkontext.entscheide), keine zweite
    Liste; nur bei BRIDGE_PRIVACY_ANLAGENKENNUNG=true am Dienst. Ungemeldete unklare Stellen kommen zusaetzlich dazu."""
    from src.privacy_postprocess import anlagenkontext as ak
    from src.privacy_postprocess.pipeline import HARTE_TYPEN

    if not ak.flag_an() or not treffer:
        return list(treffer)
    hart = [(s, e) for s, e, t in treffer if t in HARTE_TYPEN]
    alle = [(s, e) for s, e, _ in treffer]
    frei, _abgelehnt, zusatz = ak.entscheide(text, [(s, e, t, "detektor") for s, e, t in treffer], hart, alle)
    return [x for x in treffer if (x[0], x[1]) not in frei] + [(s, e, t) for s, e, t in zusatz]


def scan_png(daten: bytes) -> None:
    if not daten.startswith(PNG_SIG):
        raise BildFehler(500, "image_reencode_failed")
    i = len(PNG_SIG)
    while i < len(daten):
        if i + 8 > len(daten):
            raise BildFehler(500, "image_reencode_failed")
        laenge, typ = struct.unpack(">I4s", daten[i:i + 8])
        if typ not in PNG_ERLAUBT:
            raise BildFehler(500, "image_reencode_failed")
        i += 12 + laenge
        if typ == b"IEND":
            if i != len(daten):
                raise BildFehler(500, "image_reencode_failed")
            return
    raise BildFehler(500, "image_reencode_failed")


def scan_jpeg(daten: bytes) -> None:
    if not daten.startswith(b"\xff\xd8"):
        raise BildFehler(500, "image_reencode_failed")
    i = 2
    while i < len(daten):
        if daten[i] != 0xFF:
            raise BildFehler(500, "image_reencode_failed")
        marker = daten[i + 1]
        if marker not in JPEG_ERLAUBT:
            raise BildFehler(500, "image_reencode_failed")
        if marker == 0xD9:
            if i + 2 != len(daten):
                raise BildFehler(500, "image_reencode_failed")
            return
        laenge = struct.unpack(">H", daten[i + 2:i + 4])[0]
        if marker == 0xE0 and daten[i + 4:i + 9] != b"JFIF\x00":
            raise BildFehler(500, "image_reencode_failed")
        if marker == 0xDA:
            # Entropie-Daten bis zum naechsten Nicht-RST-Marker ueberspringen
            j = i + 2 + laenge
            while j < len(daten) - 1:
                if daten[j] == 0xFF and daten[j + 1] not in (0x00,) and not 0xD0 <= daten[j + 1] <= 0xD7:
                    break
                j += 1
            i = j
            continue
        i += 2 + laenge
    raise BildFehler(500, "image_reencode_failed")


def kodieren(im) -> Tuple[str, bytes]:
    """Schritt 7 + 8: PNG, sonst JPEG-Kaskade; ohne Metadaten; Scan; > 400 000 B64 -> 413."""
    buf = io.BytesIO()
    im.save(buf, format="PNG", optimize=True)
    daten = buf.getvalue()
    if len(base64.b64encode(daten)) <= MAX_B64:
        scan_png(daten)
        return "image/png", daten
    for q in JPEG_QUALITAETEN:
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=q, optimize=True)
        daten = buf.getvalue()
        if len(base64.b64encode(daten)) <= MAX_B64:
            scan_jpeg(daten)
            return "image/jpeg", daten
    raise BildFehler(413, "image_too_large")


async def verarbeiten(
    *,
    modus: str,
    roh: bytes,
    ocr: Ocr,
    erkennung: Erkennung,
    eingang_sha256: Optional[str] = None,
    raum: Optional[Tuple[int, int]] = None,
    handmasken: Sequence[Kasten] = (),
) -> Ergebnis:
    from PIL import Image, ImageDraw

    if modus not in ("vorschlag", "final"):
        raise BildFehler(422, "image_mode_invalid")
    magic(roh)                                                   # 1
    sha_ein = hashlib.sha256(roh).hexdigest()
    if modus == "final" and eingang_sha256 != sha_ein:
        raise BildFehler(422, "image_preview_mismatch")
    if modus == "vorschlag" and handmasken:
        raise BildFehler(422, "image_handmask_not_allowed")
    im = _dekodieren(roh)                                        # 2 + 3
    b, h = modell_masse(*im.size)                                # 4
    if (b, h) != im.size:
        im = im.resize((b, h), Image.LANCZOS)
    if raum is not None and tuple(raum) != (b, h):
        raise BildFehler(422, "image_mask_space_mismatch")       # K-2: werfen, nie umrechnen
    for x, y, w, hh in handmasken:
        if x < 0 or y < 0 or w < 1 or hh < 1 or x + w > b or y + hh > h:
            raise BildFehler(422, "image_mask_out_of_bounds")
    rgb = _flach_rgb(im)                                         # 5
    zeilen = list(ocr(rgb))                                      # 6
    text = "\n".join(z.text for z in zeilen)
    treffer = list(await erkennung(text)) if zeilen else []
    treffer = _anlagenkennungen_frei(text, treffer)
    auto = _treffer_masken(zeilen, treffer, b, h)
    zeichnen = ImageDraw.Draw(rgb)
    for x, y, w, hh, _ in auto:
        zeichnen.rectangle([x, y, x + w - 1, y + hh - 1], fill=(0, 0, 0))
    for x, y, w, hh in handmasken:                               # nur additiv (A-SS-1a)
        zeichnen.rectangle([x, y, x + w - 1, y + hh - 1], fill=(0, 0, 0))
    mime, daten = kodieren(rgb)                                  # 7 + 8
    return Ergebnis(                                             # 9
        modus=modus, eingang_sha256=sha_ein, mime=mime, bytes_=daten, breite=b, hoehe=h,
        sha256=hashlib.sha256(daten).hexdigest(), zeilen=len(zeilen), masken=auto,
        handmasken_angewendet=len(handmasken) if modus == "final" else 0,
    )
