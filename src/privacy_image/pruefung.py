"""Bild-Pruefstelle fuer die Schema-App (A-W3-1, Schnittstelle Bild v3 §3). Gilt NUR fuer X-App-ID werking-schema-generator;
der Textweg ohne Bild bleibt unveraendert.

(a) Allowlist, fail-closed: Inhalt ist entweder ein String oder eine Liste aus
      {"type":"text","text":str}  und hoechstens EINEM  {"type":"image_url","image_url":{"url":"data:image/png|jpeg;base64,…"}}
    in einer Nachricht mit role "user". Jede andere Form (type image / input_image / file / image_file, http(s)-URL,
    Bild in system/assistant, unbekannter type, Nicht-Dict-Teil, Bild ausserhalb messages) -> 422 image_content_not_allowed.
    Eine nicht erkannte Bildform laeuft NIE als Textweg durch.
(b) Bindung: Bild <=> payload.metadata.image_receipt_id (sonst 422 image_receipt_binding_invalid); Bytes muessen strikt
    Base64 sein und zur MIME passen (Magic Bytes).
(c) gleiche Funktion fuer Jobannahme (routes) und Chat-Handler (direkt, Stream, Job-Selbstaufruf).
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import re
from dataclasses import dataclass
from typing import Any, Optional

from src.privacy_image.fehler import BildFehler

SCHEMA_APP_ID = "werking-schema-generator"
_DATA_URL = re.compile(r"data:(image/png|image/jpeg);base64,([A-Za-z0-9+/]+={0,2})")
_TEXT_TYPEN = frozenset({"text"})
#: Strukturelle Bildspuren im JSON (Schluessel/Typwerte; Nutzertext ist im Dump escaped und trifft sie nicht).
_SPUR = re.compile(r'"(?:image_url|input_image|image_file|input_file|inline_data)"'
                   r'|"type": "(?:image|file|document|input_image|input_file|image_file|image_url)"'
                   r'|"(?:media_type|mime_type|mimetype|content_type)": "image/')


@dataclass
class Bild:
    mime: str
    bytes_: bytes
    sha256: str
    receipt_id: str


def _bild_teil(teil: dict, rolle: Any) -> tuple:
    if set(teil) != {"type", "image_url"} or not isinstance(teil["image_url"], dict) or set(teil["image_url"]) - {"url", "detail"}:
        raise BildFehler(422, "image_content_not_allowed")
    if rolle != "user":
        raise BildFehler(422, "image_content_not_allowed")
    url = teil["image_url"].get("url")
    m = _DATA_URL.fullmatch(url) if isinstance(url, str) else None
    if m is None:
        raise BildFehler(422, "image_content_not_allowed")   # http(s)-URL, anderer MIME, kein strenges Base64
    mime, b64 = m.group(1), m.group(2)
    try:
        roh = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        raise BildFehler(422, "image_content_not_allowed")
    if base64.b64encode(roh).decode("ascii") != b64:
        raise BildFehler(422, "image_content_not_allowed")   # nur kanonisches Base64 (kein Ueberpadding, keine Restbits)
    passt = roh.startswith(b"\x89PNG\r\n\x1a\n") if mime == "image/png" else roh.startswith(b"\xff\xd8\xff")
    if not passt:
        raise BildFehler(422, "image_content_not_allowed")
    return mime, roh


def bild_aus_chat(body: Any) -> Optional[Bild]:
    """Chat-Request (dict, wie /v1/chat/completions bzw. payload eines kind=chat-Jobs) -> None (reiner Text) oder Bild.
    Wirft BildFehler bei jeder nicht zugelassenen Form."""
    if not isinstance(body, dict):
        raise BildFehler(422, "image_content_not_allowed")
    nachrichten = body.get("messages")
    if not isinstance(nachrichten, list):
        nachrichten = []
    funde = []
    for n in nachrichten:
        if not isinstance(n, dict):
            raise BildFehler(422, "image_content_not_allowed")
        inhalt = n.get("content")
        if inhalt is None or isinstance(inhalt, str):
            continue
        if not isinstance(inhalt, list):
            raise BildFehler(422, "image_content_not_allowed")
        for teil in inhalt:
            if not isinstance(teil, dict):
                raise BildFehler(422, "image_content_not_allowed")
            typ = teil.get("type")
            if typ in _TEXT_TYPEN:
                if set(teil) - {"type", "text", "cache_control"} or not isinstance(teil.get("text"), str):
                    raise BildFehler(422, "image_content_not_allowed")
                continue
            if typ == "image_url":
                funde.append(_bild_teil(teil, n.get("role")))
                continue
            raise BildFehler(422, "image_content_not_allowed")   # image, input_image, file, image_file, unbekannt
    # Bild ausserhalb messages (andere Felder, metadata) -> 422; Inhalt von messages ist oben abschliessend geprueft.
    keine_bildspur({k: v for k, v in body.items() if k != "messages"})
    meta = body.get("metadata")
    rid = meta.get("image_receipt_id") if isinstance(meta, dict) else None
    if len(funde) > 1:
        raise BildFehler(422, "image_count_exceeded")
    if not funde:
        if rid is not None:
            raise BildFehler(422, "image_receipt_binding_invalid")
        return None
    if not isinstance(rid, str) or not rid:
        raise BildFehler(422, "image_receipt_binding_invalid")
    mime, roh = funde[0]
    return Bild(mime=mime, bytes_=roh, sha256=hashlib.sha256(roh).hexdigest(), receipt_id=rid)


def keine_bildspur(payload: Any) -> None:
    """Fuer andere kinds der Schema-App: jede Bildspur im Payload -> 422 (kein Bild ausserhalb kind=chat)."""
    import json

    text = json.dumps(payload, default=str).lower()
    if "data:image/" in text or _SPUR.search(text):
        raise BildFehler(422, "image_content_not_allowed")
