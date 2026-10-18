"""Bild-Policy: Bildinhalt einer Gemini-only-App geht NUR an Gemini — am Server, einmal.

WARUM HIER (Kanzler Dev, 18.10.2026, nach drei Pruefrunden an Energy)
--------------------------------------------------------------------
Rafael, 07.10.2026: Energy-Bildarbeit nur ueber Gemini Flash. Eine Pruefung im
CLIENT (Wache im Python-Client, im TS-Client) sieht nur die Sendewege, die sie
kennt; curl, urllib, ein Socket, Node-fetch, ein neuer SDK-Client gehen daran
vorbei (Codex-Urteil R3: sieben Transportproben, alle kamen mit PNG-Base64 und
ohne Gemini-Felder an). Die Bridge ist der einzige verpflichtende Ausgang zu einem
Modell: wer ein Bild an Anthropic bringen will, muss durch diese Anwendung. Die
Erkennung sitzt deshalb hier, einmal, unabhaengig davon, mit welcher Bibliothek
der Client sendet.

WAS SIE TUT
-----------
``evaluate`` bekommt Methode, Pfad, Content-Type, Koerper und die Kopfzeilen, ordnet
den Aufrufer einer App zu und sucht Bildinhalt im ROHEN Koerper (JSON, Multipart,
Roh-Bild). Enthaelt der Koerper eines Aufrufers aus ``IMAGE_GEMINI_ONLY_APPS`` ein
Bild, gilt:

* ``/v1/chat/completions`` (und ``/v1/jobs`` mit ``kind=chat``): erlaubt nur, wenn
  der Aufruf an den Gemini-Bildweg gebunden ist — ``provider_tier == "gemini-vision"``
  oder (kein Tier, kein Backend, Deployment-Standard ``BRIDGE_VISION_DEFAULT_PROVIDER
  =gemini``). Danach prueft die vorhandene Umgebungssperre (``gemini_vision_gate``).
* JEDER andere Pfad (Recherche, Dokument-Agent, Konvertierung, Upload ...): abgewiesen.
  Diese Wege haben keine Gemini-Weiche; ein Bild dort wuerde von einem Claude-Modell
  gesehen.

Abgewiesen wird mit 403 und ``code=image_requires_gemini``; die Meldung nennt den
Fund (Pfad im Koerper) und was zu tun ist. Kein stilles Umrouten.

WAS ERKANNT WIRD (und was bewusst nicht)
-----------------------------------------
Erkannt: Bildbloecke (``type`` = image / image_url / input_image / input_image_url,
auch ohne Nutzlast), ``image_url`` mit URL, Gemini ``inline_data``/``inlineData`` mit
Bild-MIME, Anthropic-``source`` mit Bild-MIME, MIME-Angabe ``image/*`` neben einem
Datenfeld, ``data:image/<typ>;base64,<Nutzlast>`` in JEDEM String, Base64 mit
PNG-/JPEG-/GIF-/WEBP-Signatur in jedem String (Zeilenumbrueche und URL-sichere
Alphabete eingeschlossen), Multipart-Teile mit Bild-MIME, Bild-Dateiendung oder
Bild-Magic-Bytes, ein Roh-Koerper mit Bild-Content-Type oder -Magic-Bytes.

NICHT erkannt, absichtlich: ein Wort ``data:image/`` ohne Nutzlast im Fliesstext, ein
JSON-Schema mit einer Eigenschaft namens ``image_url`` (kein Block), der Text einer
Bild-URL im Prompt. Ob ein Modell eine genannte URL SELBST abruft, entscheidet nicht
dieser Pruefer, sondern der ausfuehrende Dienst: Bridge-Worker laufen fuer Chat ohne
Browse-/Fetch-Werkzeuge (``enable_tools`` ist ohne Header abgeschaltet).

GRENZEN
-------
* Die App-Zuordnung ist der Principal, wenn das Token genau EINE App benennt, sonst
  die selbst erklaerte ``X-App-ID``/``X-Client-ID`` (wie ``app_provider_policy``).
  Ein Aufrufer, der sich als andere App ausgibt, entgeht der Policy; schliessen laesst
  sich das nur mit app-gebundenen Tokens (``BRIDGE_PRINCIPALS_ENABLED``).
* Ein Koerper ueber ``BRIDGE_IMAGE_POLICY_MAX_BODY`` Bytes wird fuer diese Apps mit
  413 abgewiesen — nicht pruefbar heisst nicht erlaubt.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

#: Apps, deren Bildinhalt nur an Gemini gehen darf. Eine festgeschriebene
#: Architekturentscheidung (Rafael 07.10.2026), kein Ops-Schalter.
IMAGE_GEMINI_ONLY_APPS = frozenset({"werking-energy"})

GEMINI_VISION_TIER = "gemini-vision"
CHAT_PATH = "/v1/chat/completions"
JOBS_PATH = "/v1/jobs"

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH"})
_IMAGE_BLOCK_TYPES = frozenset({"image", "image_url", "input_image", "input_image_url"})
_MIME_KEYS = frozenset({"media_type", "mime_type", "mimetype", "content_type", "contenttype"})
_DATA_KEYS = frozenset({"data", "bytes", "base64", "b64_json", "content", "url", "source", "file_data"})
_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".avif", ".svg")
_MAX_DEPTH = 64
_MAX_NODES = 2_000_000

_DATA_URL = re.compile(
    r"data:image/[A-Za-z0-9.+-]+(?:;[^;,\s\"']+)*"
    r"(?:;base64,[A-Za-z0-9+/_\-=\s]{16,}|,\S.{7,})",
    re.IGNORECASE,
)
_B64_MARK = re.compile(r"(?:iVBORw0KGgo|/9j/|_9j_|R0lGOD[dl]h|UklGR)")
_B64_CHARS = re.compile(r"[A-Za-z0-9+/_\-=\s]+")
_MIN_B64_RUN = 48

_RAW_MAGIC = (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a")


@dataclass(frozen=True)
class Violation:
    status: int
    code: str
    message: str


# --------------------------------------------------------------------- Erkennung

def _is_raw_image(head: bytes) -> bool:
    if head.startswith(_RAW_MAGIC):
        return True
    return head[:4] == b"RIFF" and head[8:12] == b"WEBP"


def _b64_image_at(text: str, start: int) -> bool:
    """Dekodiert die ersten Bytes ab ``start`` und vergleicht mit den Bild-Signaturen."""
    window = text[start:start + 400]
    m = _B64_CHARS.match(window)
    if m is None:
        return False
    cleaned = re.sub(r"\s+", "", m.group(0)).replace("-", "+").replace("_", "/")
    cleaned = cleaned[: len(cleaned) - len(cleaned) % 4]
    if len(cleaned) < _MIN_B64_RUN:
        return False
    try:
        raw = base64.b64decode(cleaned[:64], validate=False)
    except (binascii.Error, ValueError):  # silent-ok: kein gueltiges Base64 = kein Bild-Treffer
        return False
    return _is_raw_image(raw)


def scan_string(text: str) -> Optional[str]:
    """Fund-Beschreibung, wenn der String Bildnutzlast traegt; sonst None."""
    if len(text) < 24:
        return None
    if "data:image/" in text.lower() and _DATA_URL.search(text):
        return "data:image-URL mit Nutzlast"
    if len(text) >= _MIN_B64_RUN:
        for m in _B64_MARK.finditer(text):
            if _b64_image_at(text, m.start()):
                return "Base64 mit Bild-Signatur"
    return None


def _mime_is_image(value: Any) -> bool:
    return isinstance(value, str) and value.strip().lower().startswith("image/")


def _dict_findings(node: dict[str, Any], path: str) -> list[str]:
    found: list[str] = []
    typ = node.get("type")
    if isinstance(typ, str) and typ.strip().lower() in _IMAGE_BLOCK_TYPES:
        found.append(f"{path}: Bildblock type={typ!r}")
    iu = node.get("image_url")
    if isinstance(iu, str) or (isinstance(iu, dict) and isinstance(iu.get("url"), str)):
        found.append(f"{path}: image_url mit URL")
    for key in ("inline_data", "inlineData"):
        inl = node.get(key)
        if isinstance(inl, dict):
            mime = inl.get("mime_type", inl.get("mimeType"))
            if mime is None or _mime_is_image(mime):
                found.append(f"{path}: {key}")
    src = node.get("source")
    if isinstance(src, dict) and any(_mime_is_image(src.get(k)) for k in ("media_type", "mime_type", "mimeType")):
        found.append(f"{path}: source mit Bild-MIME")
    mimes = [v for k, v in node.items() if isinstance(k, str) and k.lower().replace("-", "_") in _MIME_KEYS]
    if any(_mime_is_image(v) for v in mimes) and any(k in _DATA_KEYS for k in node):
        found.append(f"{path}: Bild-MIME neben Datenfeld")
    return found


def find_images(obj: Any, path: str = "body") -> list[str]:
    """Alle Bildfunde in einem JSON-Objekt (Pfad + Art). Wirft bei uebertiefer Struktur."""
    funde: list[str] = []
    stack: list[tuple[Any, str, int]] = [(obj, path, 0)]
    nodes = 0
    while stack:
        node, p, depth = stack.pop()
        nodes += 1
        if depth > _MAX_DEPTH or nodes > _MAX_NODES:
            raise ValueError("Koerper zu tief oder zu gross verschachtelt, um ihn zu pruefen")
        if isinstance(node, dict):
            funde.extend(_dict_findings(node, p))
            stack.extend((v, f"{p}.{k}", depth + 1) for k, v in node.items())
        elif isinstance(node, list):
            stack.extend((v, f"{p}[{i}]", depth + 1) for i, v in enumerate(node))
        elif isinstance(node, str):
            hit = scan_string(node)
            if hit:
                funde.append(f"{p}: {hit}")
    return funde


def _scan_text_bytes(data: bytes, path: str) -> list[str]:
    hit = scan_string(data.decode("utf-8", errors="ignore"))
    return [f"{path}: {hit}"] if hit else []


def _json_or_text(data: bytes, path: str) -> list[str]:
    try:
        parsed = json.loads(data)
    except (ValueError, UnicodeDecodeError):  # silent-ok: kein JSON -> Rohtext wird durchsucht
        return _scan_text_bytes(data, path)
    return find_images(parsed, path)


def _multipart_parts(body: bytes, boundary: bytes) -> Iterable[tuple[dict[str, str], bytes]]:
    delimiter = b"--" + boundary
    for chunk in body.split(delimiter)[1:]:
        if chunk.startswith(b"--"):
            break
        chunk = chunk.lstrip(b"\r\n")
        head, sep, payload = chunk.partition(b"\r\n\r\n")
        if not sep:
            head, sep, payload = chunk.partition(b"\n\n")
        headers: dict[str, str] = {}
        for line in head.decode("latin-1", errors="ignore").splitlines():
            name, colon, value = line.partition(":")
            if colon:
                headers[name.strip().lower()] = value.strip()
        yield headers, payload.rstrip(b"\r\n")


def _multipart_findings(body: bytes, content_type: str) -> list[str]:
    m = re.search(r"boundary=(?:\"([^\"]+)\"|([^;\s]+))", content_type, re.IGNORECASE)
    if m is None:
        raise ValueError("Multipart ohne boundary, nicht pruefbar")
    boundary = (m.group(1) or m.group(2)).encode("latin-1")
    funde: list[str] = []
    for i, (headers, payload) in enumerate(_multipart_parts(body, boundary)):
        p = f"body.part[{i}]"
        disposition = headers.get("content-disposition", "")
        fname = re.search(r"filename\*?=(?:\"([^\"]*)\"|([^;]+))", disposition, re.IGNORECASE)
        name = ((fname.group(1) or fname.group(2)) if fname else "").strip().lower()
        ptype = headers.get("content-type", "").lower()
        if ptype.startswith("image/"):
            funde.append(f"{p}: Teil mit Content-Type {ptype}")
        elif name.endswith(_IMAGE_EXTENSIONS):
            funde.append(f"{p}: Datei mit Bild-Endung ({name})")
        elif _is_raw_image(payload[:16]):
            funde.append(f"{p}: Bild-Magic-Bytes")
        elif ptype.startswith(("application/pdf", "audio/", "video/")):
            continue
        else:
            funde.extend(_json_or_text(payload, p))
    return funde


def find_images_in_body(body: bytes, content_type: str) -> list[str]:
    """Bildfunde im rohen HTTP-Koerper, nach Content-Type."""
    if not body:
        return []
    ctype = (content_type or "").split(";")[0].strip().lower()
    if ctype.startswith("image/"):
        return [f"body: Roh-Koerper mit Content-Type {ctype}"]
    if _is_raw_image(body[:16]):
        return ["body: Bild-Magic-Bytes"]
    if ctype.startswith("multipart/"):
        return _multipart_findings(body, content_type)
    if ctype in ("application/pdf",) or ctype.startswith(("audio/", "video/")):
        return []
    return _json_or_text(body, "body")


# ------------------------------------------------------------------------ Policy

def policy_apps() -> frozenset[str]:
    """Apps unter der Policy. ``BRIDGE_IMAGE_POLICY_APPS`` (kommagetrennt) ersetzt die Liste;
    leer gesetzt schaltet die Policy ab (Notausgang, laut protokolliert)."""
    raw = os.getenv("BRIDGE_IMAGE_POLICY_APPS")
    if raw is None:
        return IMAGE_GEMINI_ONLY_APPS
    apps = frozenset(a.strip() for a in raw.split(",") if a.strip())
    if not apps:
        logger.warning("BRIDGE_IMAGE_POLICY_APPS ist leer: Bild-Policy ist ABGESCHALTET")
    return apps


def max_body_bytes() -> int:
    raw = os.getenv("BRIDGE_IMAGE_POLICY_MAX_BODY")
    if not raw:
        return 64 * 1024 * 1024
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"BRIDGE_IMAGE_POLICY_MAX_BODY={raw!r} ist keine Zahl") from exc


def is_gemini_bound(chat_body: Any) -> bool:
    """Ist dieser Chat-Aufruf an den Gemini-Bildweg gebunden (wie ``resolve_vision_target``)?"""
    if not isinstance(chat_body, dict):
        return False
    tier = chat_body.get("provider_tier")
    backend = chat_body.get("backend")
    if tier == GEMINI_VISION_TIER:
        return backend in (None, "", "gemini_api")
    if tier or backend:
        return False
    from src.routing.vision_router import VISION_TARGET_GEMINI, default_vision_target

    return default_vision_target() == VISION_TARGET_GEMINI


def _refusal(funde: list[str], where: str) -> Violation:
    shown = "; ".join(funde[:4]) + (f" (+{len(funde) - 4} weitere)" if len(funde) > 4 else "")
    return Violation(
        403,
        "image_requires_gemini",
        f"Bildinhalt ({shown}) ist fuer diese App nur ueber den Gemini-Bildweg zulaessig. {where} "
        "Nicht stillschweigend umgeleitet: /v1/chat/completions mit provider_tier='gemini-vision' "
        "senden, oder das Bild weglassen.",
    )


def too_large(size: int) -> Violation:
    return Violation(
        413,
        "image_policy_body_too_large",
        f"Der Koerper ({size} Bytes) ist groesser als BRIDGE_IMAGE_POLICY_MAX_BODY; "
        "er kann nicht auf Bildinhalt geprueft werden und wird deshalb nicht angenommen.",
    )


def evaluate(
    *,
    app_id: Optional[str],
    method: str,
    path: str,
    content_type: str,
    body: bytes,
) -> Optional[Violation]:
    """Die Entscheidung. ``None`` = durchlassen. Wirft nie; Unpruefbares ist eine Absage."""
    if not app_id or app_id not in policy_apps() or method.upper() not in _WRITE_METHODS:
        return None
    if len(body) > max_body_bytes():
        return too_large(len(body))
    try:
        funde = find_images_in_body(body, content_type)
    except ValueError as exc:
        return Violation(400, "image_policy_unverifiable", f"Der Koerper ist nicht pruefbar: {exc}")
    if not funde:
        return None

    clean_path = path.rstrip("/") or "/"
    if clean_path == CHAT_PATH:
        chat_body = _parse(body)
        if is_gemini_bound(chat_body):
            return None
        return _refusal(funde, "Dieser Chat-Aufruf ist nicht an Gemini gebunden.")
    if clean_path == JOBS_PATH:
        job = _parse(body)
        if isinstance(job, dict) and job.get("kind") == "chat" and is_gemini_bound(job.get("payload")):
            return None
        return _refusal(funde, "Dieser Job ist kein an Gemini gebundener Chat-Job.")
    return _refusal(funde, f"Der Pfad {clean_path} hat keinen Gemini-Bildweg.")


def _parse(body: bytes) -> Any:
    try:
        return json.loads(body)
    except (ValueError, UnicodeDecodeError):  # silent-ok: kein JSON -> nicht an Gemini gebunden
        return None
