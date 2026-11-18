"""Strenge Pruefung der Felder von /v1/privacy/image-mask und /v1/privacy/image-receipt (Schnittstelle Bild v2).

Regeln (A-SS-1, A-SS-2):
  * Multipart-Teile genau aus FELDER_MASKE, keine doppelten;
  * raum / handmasken sind UTF-8-Textteile mit JSON-String; nur JSON-integer (kein bool, kein float);
  * handmasken <= 256 Kaesten, Teil <= 65 536 Bytes; ausserhalb -> 422 image_mask_out_of_bounds, nie klemmen;
  * eingang_sha256 Pflicht bei final, verboten bei vorschlag; handmasken bei vorschlag verboten.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Tuple

from src.privacy_image.fehler import BildFehler

FELDER_MASKE = frozenset({"bild", "modus", "sprache", "eingang_sha256", "raum", "handmasken"})
MAX_BILD_BYTES = 8 * 1024 * 1024
MAX_HANDMASKEN = 256
MAX_HANDMASKEN_BYTES = 65536
MAX_KANTE_RAUM = 10000
_HEX64 = re.compile(r"[0-9a-f]{64}")

Kasten = Tuple[int, int, int, int]  # x, y, w, h


@dataclass
class MaskenAnfrage:
    modus: str
    bild: bytes
    sprache: str = "de"
    eingang_sha256: Optional[str] = None
    raum: Optional[Tuple[int, int]] = None
    handmasken: List[Kasten] = field(default_factory=list)


def _int(v) -> bool:
    return type(v) is int  # bool ist Unterklasse von int und hier verboten


def _json_text(roh: bytes, *, grenze: Optional[int] = None):
    if grenze is not None and len(roh) > grenze:
        raise BildFehler(413, "image_handmask_too_large")
    try:
        text = roh.decode("utf-8")
    except UnicodeDecodeError:
        raise BildFehler(422, "image_handmask_invalid")
    if text.startswith("﻿"):
        raise BildFehler(422, "image_handmask_invalid")
    try:
        return json.loads(text, parse_constant=_kein_nan)
    except ValueError:
        raise BildFehler(422, "image_handmask_invalid")


def _kein_nan(_):
    raise ValueError("NaN/Infinity")


def raum_lesen(roh: bytes) -> Tuple[int, int]:
    obj = _json_text(roh)
    if not isinstance(obj, dict) or set(obj) != {"breite", "hoehe"}:
        raise BildFehler(422, "image_handmask_invalid")
    b, h = obj["breite"], obj["hoehe"]
    if not (_int(b) and _int(h) and 1 <= b <= MAX_KANTE_RAUM and 1 <= h <= MAX_KANTE_RAUM):
        raise BildFehler(422, "image_handmask_invalid")
    return b, h


def handmasken_lesen(roh: bytes) -> List[Kasten]:
    obj = _json_text(roh, grenze=MAX_HANDMASKEN_BYTES)
    if not isinstance(obj, list) or len(obj) > MAX_HANDMASKEN:
        raise BildFehler(422, "image_handmask_invalid")
    out: List[Kasten] = []
    for k in obj:
        if not isinstance(k, dict) or set(k) != {"x", "y", "w", "h"}:
            raise BildFehler(422, "image_handmask_invalid")
        x, y, w, h = k["x"], k["y"], k["w"], k["h"]
        if not all(_int(v) for v in (x, y, w, h)) or x < 0 or y < 0 or w < 1 or h < 1:
            raise BildFehler(422, "image_handmask_invalid")
        out.append((x, y, w, h))
    return out


def handmasken_im_raum(masken: List[Kasten], raum: Tuple[int, int]) -> None:
    b, h = raum
    for x, y, w, hh in masken:
        if x + w > b or y + hh > h:
            raise BildFehler(422, "image_mask_out_of_bounds")


def maske_anfrage(teile: Iterable[Tuple[str, bytes]]) -> MaskenAnfrage:
    """``teile``: (Name, Bytes) je Multipart-Teil in Eingangsreihenfolge."""
    gesehen = {}
    for name, wert in teile:
        if name not in FELDER_MASKE:
            raise BildFehler(422, "image_field_unknown")
        if name in gesehen:
            raise BildFehler(422, "image_field_duplicate")
        gesehen[name] = wert
    modus = gesehen.get("modus", b"").decode("utf-8", "replace").strip()
    if modus not in ("vorschlag", "final"):
        raise BildFehler(400, "image_mode_invalid")
    bild = gesehen.get("bild")
    if not bild:
        raise BildFehler(422, "image_unreadable")
    if len(bild) > MAX_BILD_BYTES:
        raise BildFehler(413, "image_too_large")
    sprache = gesehen.get("sprache", b"de").decode("utf-8", "replace").strip() or "de"
    if sprache not in ("de", "en"):
        raise BildFehler(422, "image_field_unknown")
    a = MaskenAnfrage(modus=modus, bild=bild, sprache=sprache)
    if modus == "vorschlag":
        if "handmasken" in gesehen or "eingang_sha256" in gesehen:
            raise BildFehler(422, "image_handmask_not_allowed")
        if "raum" in gesehen:
            a.raum = raum_lesen(gesehen["raum"])
        return a
    e = gesehen.get("eingang_sha256")
    if e is None or not _HEX64.fullmatch(e.decode("ascii", "replace").strip()):
        raise BildFehler(422, "image_preview_mismatch")
    a.eingang_sha256 = e.decode("ascii").strip()
    if "handmasken" in gesehen:
        a.handmasken = handmasken_lesen(gesehen["handmasken"])
    if "raum" in gesehen:
        a.raum = raum_lesen(gesehen["raum"])
    if a.handmasken and a.raum is None:
        raise BildFehler(422, "image_handmask_invalid")
    if a.raum is not None:
        handmasken_im_raum(a.handmasken, a.raum)
    return a


FELDER_RECEIPT = frozenset({"final_token", "sha256", "mime", "bestaetigung"})
FELDER_BESTAETIGUNG = frozenset({"version", "genau_dieses_bild", "leer_zweitbestaetigung", "zeit"})


@dataclass
class ReceiptAnfrage:
    final_token: str
    sha256: str
    mime: str
    genau_dieses_bild: bool
    leer_zweitbestaetigung: bool
    zeit: str


def receipt_anfrage(obj) -> ReceiptAnfrage:
    if not isinstance(obj, dict):
        raise BildFehler(422, "image_field_unknown")
    if set(obj) - FELDER_RECEIPT:
        raise BildFehler(422, "image_field_unknown")
    if set(obj) != FELDER_RECEIPT:
        raise BildFehler(403, "image_receipt_confirmation_missing")
    b = obj["bestaetigung"]
    if not isinstance(b, dict) or set(b) - FELDER_BESTAETIGUNG:
        raise BildFehler(422, "image_field_unknown")
    if set(b) != FELDER_BESTAETIGUNG or b["version"] != "bild-v1" or b["genau_dieses_bild"] is not True \
            or type(b["leer_zweitbestaetigung"]) is not bool or not isinstance(b["zeit"], str):
        raise BildFehler(403, "image_receipt_confirmation_missing")
    tok, sha, mime = obj["final_token"], obj["sha256"], obj["mime"]
    if not isinstance(tok, str) or not tok or len(tok) > 2048:
        raise BildFehler(403, "image_final_token_invalid")
    if not isinstance(sha, str) or not _HEX64.fullmatch(sha) or mime not in ("image/png", "image/jpeg"):
        raise BildFehler(403, "image_receipt_sha_mismatch")
    return ReceiptAnfrage(tok, sha, mime, True, b["leer_zweitbestaetigung"], b["zeit"])
