"""Adapter des Privacy-Dienstes fuer die Bild-PII-Maske: OCR (RapidOCR) und Erkennung (Presidio + Flair), nur im Speicher.

OCR-Speicherweg (Review-Hinweis v4 R2: pytesseract schreibt Tempdateien): RapidOCR 3.x (poetry.lock: 3.9.0, onnxruntime)
nimmt ein numpy-Array direkt (``RapidOCR()(ndarray)``) und schreibt dabei keine Bild- oder Ergebnisdateien; die Modelle
liegen im Paket bzw. werden einmalig beim ersten Start geladen (keine Nutzerdaten). Kein pytesseract, kein Docling-convert().
Erkennung: ``PresidioAnonymizer.anonymize_async`` liefert ``DetectedEntity`` mit start/end (src/privacy/anonymizer.py:111-118).
"""
from __future__ import annotations

import threading
from typing import List, Optional, Sequence, Tuple

from src.privacy_image.verarbeitung import OcrZeile

_engine = None
_lock = threading.Lock()


def _rapidocr():
    global _engine
    with _lock:
        if _engine is None:
            from rapidocr import RapidOCR  # optionales Extra "pdf" des Privacy-Images
            _engine = RapidOCR()
        return _engine


def _woerter(zeile: OcrZeile, worte) -> Optional[Tuple[Tuple[int, int, int, int, int, int], ...]]:
    """Wortboxen einer Zeile mit Offsets im Zeilentext - nur bei belegter Zuordnung, sonst None (Aufrufer: unsicher).

    Belegt heisst: jedes Wort ist in Reihenfolge im Zeilentext auffindbar UND seine Box liegt in der Zeilenbox
    (Toleranz 1/2 Zeilenhoehe). Sonst koennten Wortlisten verschiedener Zeilen vertauscht sein (RapidOCR filtert
    word_results getrennt von boxes)."""
    if not worte:
        return None
    tol = max(2.0, 0.5 * (zeile.y1 - zeile.y0))
    aus = []
    cursor = 0
    for wort in worte:
        try:
            txt, _score, box = wort
            xs = [float(p[0]) for p in box]
            ys = [float(p[1]) for p in box]
        except (TypeError, ValueError, IndexError):
            return None
        txt = str(txt)
        if not txt.strip():
            continue
        start = zeile.text.find(txt, cursor)
        if start < 0:
            return None
        x0, y0, x1, y1 = int(min(xs)), int(min(ys)), int(max(xs) + 0.999), int(max(ys) + 0.999)
        if x0 < zeile.x0 - tol or y0 < zeile.y0 - tol or x1 > zeile.x1 + tol or y1 > zeile.y1 + tol or x1 <= x0 or y1 <= y0:
            return None
        aus.append((x0, y0, x1, y1, start, start + len(txt)))
        cursor = start + len(txt)
    return tuple(aus) or None


def ocr_rapid(bild) -> Sequence[OcrZeile]:
    """RGB-PIL-Bild -> Zeilen (achsparalleles Rechteck um das OCR-Viereck), Reihenfolge wie RapidOCR.

    Wortboxen (``return_word_box``, RapidOCR 3.9.0) werden je Zeile nur uebernommen, wenn die Liste genau so lang ist
    wie die Zeilenliste und die Zuordnung je Zeile belegt ist (``_woerter``); sonst bleibt ``woerter`` None."""
    import numpy as np

    arr = np.asarray(bild)
    out = _rapidocr()(arr, return_word_box=True)
    boxes = getattr(out, "boxes", None)
    txts = getattr(out, "txts", None)
    if boxes is None or txts is None:
        return []
    wortlisten = getattr(out, "word_results", None)
    if not wortlisten or len(wortlisten) != len(txts):
        wortlisten = [None] * len(txts)
    zeilen: List[OcrZeile] = []
    for box, txt, worte in zip(boxes, txts, wortlisten):
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        z = OcrZeile(int(min(xs)), int(min(ys)), int(max(xs) + 0.999), int(max(ys) + 0.999), str(txt))
        z.woerter = _woerter(z, worte)
        zeilen.append(z)
    return zeilen


def erkennung_presidio(sprache: str = "de"):
    async def erkennen(text: str) -> Sequence[Tuple[int, int, str]]:
        from src.privacy.anonymizer import PresidioAnonymizer

        if not text.strip():
            return []
        res = await PresidioAnonymizer(language=sprache).anonymize_async(text, sprache)
        return [(e.start, e.end, e.entity_type) for e in res.detected_entities]
    return erkennen
