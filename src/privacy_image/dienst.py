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


def ocr_rapid(bild) -> Sequence[OcrZeile]:
    """RGB-PIL-Bild -> Zeilen (achsparalleles Rechteck um das OCR-Viereck), Reihenfolge wie RapidOCR."""
    import numpy as np

    arr = np.asarray(bild)
    out = _rapidocr()(arr)
    boxes = getattr(out, "boxes", None)
    txts = getattr(out, "txts", None)
    if boxes is None or txts is None:
        return []
    zeilen: List[OcrZeile] = []
    for box, txt in zip(boxes, txts):
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        zeilen.append(OcrZeile(int(min(xs)), int(min(ys)), int(max(xs) + 0.999), int(max(ys) + 0.999), str(txt)))
    return zeilen


def erkennung_presidio(sprache: str = "de"):
    async def erkennen(text: str) -> Sequence[Tuple[int, int, str]]:
        from src.privacy.anonymizer import PresidioAnonymizer

        if not text.strip():
            return []
        res = await PresidioAnonymizer(language=sprache).anonymize_async(text, sprache)
        return [(e.start, e.end, e.entity_type) for e in res.detected_entities]
    return erkennen
