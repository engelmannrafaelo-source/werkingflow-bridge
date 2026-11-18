"""final_token der Bild-PII-Maske (Bildvertrag v3 §1/§3.2, SEC 2; Schnittstelle v3).

HMAC-SHA256 ueber die kanonische JSON-Fassung (sortierte Schluessel) ALLER Felder:
  version, sha256, mime, leer, principal, tenant, breite, hoehe, ausgestellt, ablauf, automasken, handmasken, jti
Pruefung: hmac.compare_digest, Feldmenge exakt, version exakt, Ablauf. Ein korrekt signiertes Token alter Version wird
abgewiesen. Einmaligkeit (A-SS-3) haelt der Receipt-Speicher ueber token_hash (sha256 des Tokentexts, eindeutig).

Schluessel: Umgebungsvariable BRIDGE_PRIVACY_IMAGE_TOKEN_SECRET (>= 32 Bytes). Fehlt er -> 503 image_mask_unavailable
(fail-closed, kein Ersatzschluessel).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Any, Dict, Optional

from src.privacy_image.fehler import BildFehler

VERSION = "bild-ft-v1"
GUELTIG_S = 15 * 60
SECRET_ENV = "BRIDGE_PRIVACY_IMAGE_TOKEN_SECRET"
FELDER = frozenset({"version", "sha256", "mime", "leer", "principal", "tenant", "breite", "hoehe",
                    "ausgestellt", "ablauf", "automasken", "handmasken", "jti"})


def _schluessel(schluessel: Optional[bytes]) -> bytes:
    if schluessel is None:
        roh = os.getenv(SECRET_ENV) or ""
        schluessel = roh.encode("utf-8")
    if len(schluessel) < 32:
        raise BildFehler(503, "image_mask_unavailable")
    return schluessel


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("ascii").rstrip("=")


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def ausstellen(*, sha256: str, mime: str, leer: bool, principal: str, tenant: str, breite: int, hoehe: int,
               automasken: int, handmasken: int, jetzt: Optional[int] = None,
               schluessel: Optional[bytes] = None) -> Dict[str, Any]:
    k = _schluessel(schluessel)
    t = int(time.time()) if jetzt is None else int(jetzt)
    felder = {"version": VERSION, "sha256": sha256, "mime": mime, "leer": bool(leer), "principal": principal,
              "tenant": tenant, "breite": int(breite), "hoehe": int(hoehe), "ausgestellt": t, "ablauf": t + GUELTIG_S,
              "automasken": int(automasken), "handmasken": int(handmasken), "jti": secrets.token_hex(16)}
    roh = json.dumps(felder, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(k, roh, hashlib.sha256).digest()
    return {"token": _b64(roh) + "." + _b64(sig), "ablauf": felder["ablauf"], "felder": felder}


def pruefen(token: str, *, jetzt: Optional[int] = None, schluessel: Optional[bytes] = None) -> Dict[str, Any]:
    k = _schluessel(schluessel)
    try:
        teil_roh, teil_sig = token.split(".")
        roh, sig = _unb64(teil_roh), _unb64(teil_sig)
    except (ValueError, AttributeError):
        raise BildFehler(403, "image_final_token_invalid")
    if not hmac.compare_digest(hmac.new(k, roh, hashlib.sha256).digest(), sig):
        raise BildFehler(403, "image_final_token_invalid")
    try:
        f = json.loads(roh)
    except ValueError:
        raise BildFehler(403, "image_final_token_invalid")
    if not isinstance(f, dict) or set(f) != FELDER or f["version"] != VERSION:
        raise BildFehler(403, "image_final_token_invalid")
    t = int(time.time()) if jetzt is None else int(jetzt)
    if t >= int(f["ablauf"]):
        raise BildFehler(403, "image_final_token_expired")
    return f


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
