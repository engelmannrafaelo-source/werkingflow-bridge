"""Bild-Policy: Bildinhalt einer Gemini-only-App geht NUR an Gemini — am Server, einmal.

WARUM HIER (Kanzler Dev, 18.10.2026, nach drei Pruefrunden an Energy)
--------------------------------------------------------------------
Rafael, 07.10.2026: Energy-Bildarbeit nur ueber Gemini Flash. Eine Pruefung im
CLIENT (Wache im Python-Client, im TS-Client) sieht nur die Sendewege, die sie
kennt; curl, urllib, ein Socket, Node-fetch, ein neuer SDK-Client gehen daran
vorbei (Codex-Urteil R3: sieben Transportproben, alle kamen mit PNG-Base64 und
ohne Gemini-Felder an). Die Bridge prueft die durch sie laufenden Anbieter-Ausgaenge unabhaengig
von der Client-Bibliothek. Der externe Sandbox-Passthrough hat einen eigenen
Riegel mit wortgleicher Erkennung und Vertragstest; CLI-Werkzeugwege werden
vor dem Start auf einen ausdruecklich textuellen Vertrag beschraenkt.
Ein Quelltext-Inventar ist dabei nur eine Lint-Hilfe, kein Vollstaendigkeitsbeweis.

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

#: UMGEKEHRT (R6, Kanzler Dev 18.10.2026): Bildinhalt an einen Nicht-Gemini-Anbieter ist NUR fuer diese
#: ausdruecklich erlaubten Apps zulaessig (Positivliste). Fehlende oder unbekannte App-Identitaet wird
#: wie Energy behandelt: nur Gemini, sonst 403. Die Sicherheit haengt damit nicht mehr daran, dass jeder
#: Nebenweg (PDF-Proxy, Privacy-Dienst, Research, Erkunder, Jobs) die Identitaet korrekt weiterreicht.
#: Eine festgeschriebene Architekturentscheidung = KNOWN_APP_IDS (main.py) ohne werking-energy.
#: Namentlich zugefuegt (Inventar 18.10.2026, kein KNOWN_APP_IDS-Eintrag): ``partner-platform`` (CUI-Server,
#: journey-evaluator/bridgeChat sendet Claude-Vision-Bilder mit X-App-ID partner-platform) und ``dev-tooling``
#: (Orchestrator-Werkzeuge, z. B. der Prepaid-Probe-Aufruf von vision-prepaid-budget-guard, der ein 1x1-PNG an Haiku sendet).
IMAGE_ANTHROPIC_ALLOWED_EXTRA_APPS = frozenset({"partner-platform", "dev-tooling"})
IMAGE_ANTHROPIC_ALLOWED_APPS = frozenset({
    "engelmann", "werking-report", "werking-safety", "werking-noise", "cui", "platform", "acro-community",
}) | IMAGE_ANTHROPIC_ALLOWED_EXTRA_APPS
#: Diese Apps sind nie erlaubt, auch nicht per Umgebungsvariable (Rafael 07.10.2026).
IMAGE_GEMINI_ONLY_APPS = frozenset({"werking-energy"})

GEMINI_VISION_TIER = "gemini-vision"
CHAT_PATH = "/v1/chat/completions"
JOBS_PATH = "/v1/jobs"

_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH"})
_IMAGE_BLOCK_TYPES = frozenset({"image", "image_url", "input_image", "input_image_url"})
_MIME_KEYS = frozenset(
    {"media_type", "mime_type", "mimetype", "content_type", "contenttype"}
)
_DATA_KEYS = frozenset(
    {"data", "bytes", "base64", "b64_json", "content", "url", "source", "file_data"}
)
_IMAGE_EXTENSIONS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".tif",
    ".tiff",
    ".heic",
    ".avif",
    ".svg",
)
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
    """Vergleicht dekodierte Bytes ab start mit Bild-Signaturen."""
    window = text[start : start + 400]
    m = _B64_CHARS.match(window)
    if m is None:
        return False
    cleaned = re.sub(r"\s+", "", m.group(0)).replace("-", "+").replace("_", "/")
    cleaned = cleaned[: len(cleaned) - len(cleaned) % 4]
    if len(cleaned) < _MIN_B64_RUN:
        return False
    try:
        raw = base64.b64decode(cleaned[:64], validate=False)
    except (
        binascii.Error,
        ValueError,
    ):  # silent-ok: kein gueltiges Base64 = kein Bild-Treffer
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
    # Native PDF/document blocks may be rasterized by Anthropic. Without decoding
    # the document we cannot prove text-only content; URL/base64 PDFs are refused.
    if typ == "document" and isinstance(src, dict):
        if src.get("type") == "url" or (
            src.get("type") == "base64" and src.get("media_type") != "text/plain"
        ):
            found.append(f"{path}: visuell auswertbarer Dokumentblock")
    if isinstance(src, dict) and any(
        _mime_is_image(src.get(k)) for k in ("media_type", "mime_type", "mimeType")
    ):
        found.append(f"{path}: source mit Bild-MIME")
    mimes = [
        v
        for k, v in node.items()
        if isinstance(k, str) and k.lower().replace("-", "_") in _MIME_KEYS
    ]
    if any(_mime_is_image(v) for v in mimes) and any(k in _DATA_KEYS for k in node):
        found.append(f"{path}: Bild-MIME neben Datenfeld")
    return found


def find_images(
    obj: Any, path: str = "body", *, structural_only: bool = False
) -> list[str]:
    """Alle Bildfunde in einem JSON-Objekt (Pfad + Art). Wirft bei uebertiefer Struktur.

    structural_only: nur Anbieter-Bildbloecke, keine Textstrings mit Base64
    oder Data-URL (siehe Modul-Doku, Abschnitt AUSGANG)."""
    funde: list[str] = []
    stack: list[tuple[Any, str, int]] = [(obj, path, 0)]
    nodes = 0
    while stack:
        node, p, depth = stack.pop()
        if not isinstance(
            node, (dict, list, str, int, float, bool, type(None))
        ) and hasattr(node, "model_dump"):
            node = node.model_dump()  # Pydantic: serialisierten Body pruefen
        nodes += 1
        if depth > _MAX_DEPTH or nodes > _MAX_NODES:
            raise ValueError(
                "Koerper zu tief oder zu gross verschachtelt, um ihn zu pruefen"
            )
        if isinstance(node, dict):
            funde.extend(_dict_findings(node, p))
            stack.extend((v, f"{p}.{k}", depth + 1) for k, v in node.items())
        elif isinstance(node, list):
            stack.extend((v, f"{p}[{i}]", depth + 1) for i, v in enumerate(node))
        elif isinstance(node, str) and not structural_only:
            hit = scan_string(node)
            if hit:
                funde.append(f"{p}: {hit}")
    return funde



def nontext_content(obj: Any, path: str = "payload") -> list[str]:
    """Positive content contract at a non-Gemini sink, independent of MIME.

    Envelopes are traversed only at their content slots, not at tool schemas.
    Unknown content blocks/fields fail closed. Tool inputs are JSON arguments,
    not model attachments; the independent payload scan still checks them.
    """
    findings: list[str] = []
    visited = 0

    def content(
        value: Any, where: str, depth: int = 0, *, text_result: bool = False
    ) -> None:
        nonlocal visited
        visited += 1
        if depth > _MAX_DEPTH or visited > _MAX_NODES:
            raise ValueError("Inhalt zu tief oder zu gross")
        if hasattr(value, "model_dump"):
            value = value.model_dump(exclude_none=True)
        if isinstance(value, str):
            return
        if isinstance(value, list):
            for i, item in enumerate(value):
                content(item, f"{where}[{i}]", depth + 1, text_result=text_result)
            return
        if not isinstance(value, dict):
            findings.append(f"{where}: kein belegter Textinhalt")
            return
        typ = value.get("type")
        if text_result and typ not in {"text", "input_text", "output_text"}:
            findings.append(f"{where}: Werkzeugergebnis muss ausschließlich Text sein")
            return
        fields = {
            "text": {"type", "text", "cache_control", "citations"},
            "input_text": {"type", "text"},
            "output_text": {"type", "text", "annotations"},
            "tool_use": {"type", "id", "name", "input", "cache_control"},
            "tool_result": {
                "type",
                "tool_use_id",
                "content",
                "is_error",
                "cache_control",
            },
            "document": {
                "type",
                "source",
                "title",
                "context",
                "citations",
                "cache_control",
            },
        }
        if not isinstance(typ, str) or typ not in fields or set(value) - fields[typ]:
            findings.append(f"{where}: Inhaltsform nicht auf Text-Positivliste")
            return
        if typ in {"text", "input_text", "output_text"}:
            if not isinstance(value.get("text"), str):
                findings.append(f"{where}: Textblock ohne Text")
        elif typ == "tool_result":
            content(
                value.get("content", ""), where + ".content", depth + 1,
                text_result=True,
            )
        elif typ == "tool_use":
            if not isinstance(value.get("input"), dict):
                findings.append(f"{where}: Werkzeugargumente sind kein JSON-Objekt")
        elif typ == "document":
            source = value.get("source")
            if not (
                isinstance(source, dict)
                and source.get("type") == "text"
                and isinstance(source.get("data"), str)
                and source.get("media_type", "text/plain") == "text/plain"
                and not set(source) - {"type", "data", "media_type"}
            ):
                findings.append(f"{where}: Dokumentquelle ist kein belegter Klartext")

    def envelope(value: Any, where: str, depth: int = 0) -> None:
        if depth > _MAX_DEPTH:
            raise ValueError("Nachrichten zu tief verschachtelt")
        if hasattr(value, "model_dump"):
            value = value.model_dump(exclude_none=True)
        if isinstance(value, list):
            for i, item in enumerate(value):
                envelope(item, f"{where}[{i}]", depth + 1)
        elif isinstance(value, dict):
            if "type" in value:
                content(value, where)
            elif "role" in value:
                # Null assistant content is normal for OpenAI tool calls.
                if value.get("content") is not None:
                    content(value["content"], where + ".content")
            else:
                for key in ("messages", "input", "system", "content", "prompt"):
                    if key in value:
                        envelope(value[key], f"{where}.{key}", depth + 1)
        elif isinstance(value, str):
            return
        elif value is not None:
            findings.append(f"{where}: unbekannte Nachrichtenform")

    envelope(obj, path)
    return findings


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
    teile = list(_multipart_parts(body, boundary))
    if body.strip() and not teile:
        raise ValueError("Multipart ohne erkennbare Teile bei nichtleerem Koerper")
    for i, (headers, payload) in enumerate(teile):
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
        elif payload.startswith(b"%PDF-"):
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
    if body.startswith(b"%PDF-"):
        return []  # ein echtes PDF; ein behaupteter PDF-/Audio-/Video-Typ ist kein Freibrief fuer beliebige Bytes
    return _json_or_text(body, "body")


# ------------------------------------------------------------------------ Policy

def allowed_apps() -> frozenset[str]:
    """Apps, die Bildinhalt auch an Nicht-Gemini-Anbieter schicken duerfen.

    ``BRIDGE_IMAGE_ANTHROPIC_ALLOWED_APPS`` (kommagetrennt) ersetzt die Liste; ``werking-energy`` bleibt
    in jedem Fall ausgeschlossen. Leer = niemand ausser Gemini."""
    raw = os.getenv("BRIDGE_IMAGE_ANTHROPIC_ALLOWED_APPS")
    apps = IMAGE_ANTHROPIC_ALLOWED_APPS if raw is None else frozenset(a.strip() for a in raw.split(",") if a.strip())
    return apps - IMAGE_GEMINI_ONLY_APPS


def is_restricted(app_id: Optional[str]) -> bool:
    """Gilt fuer diese App 'Bild nur an Gemini'? Fehlend/leer/unbekannt = ja."""
    return not app_id or app_id not in allowed_apps()


def max_body_bytes() -> int:
    raw = os.getenv("BRIDGE_IMAGE_POLICY_MAX_BODY")
    if not raw:
        return 64 * 1024 * 1024
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"BRIDGE_IMAGE_POLICY_MAX_BODY={raw!r} ist keine Zahl") from exc


def is_gemini_bound(chat_body: Any) -> bool:
    """Am Eingang gilt NUR die ausdrueckliche Bindung ``provider_tier=gemini-vision``.

    Ein Deployment-Standard bindet nicht (Codex R4): ob ein Bild wirklich an Gemini geht, entscheidet
    erst die Weiche NACH allen Pins; das prueft ``enforce_image_egress`` am Ausgang."""
    if not isinstance(chat_body, dict):
        return False
    return chat_body.get("provider_tier") == GEMINI_VISION_TIER and chat_body.get("backend") in (None, "", "gemini_api")


def _refusal(funde: list[str], where: str) -> Violation:
    shown = "; ".join(funde[:4]) + (f" (+{len(funde) - 4} weitere)" if len(funde) > 4 else "")
    return Violation(
        403,
        "image_requires_gemini",
        f"Bildinhalt ({shown}) ist fuer diese App nur ueber den Gemini-Bildweg zulaessig. {where} "
        "Nicht stillschweigend umgeleitet: /v1/chat/completions mit provider_tier='gemini-vision' "
        "senden, oder das Bild weglassen.",
    )


def describe_images_refusal(app_id: Optional[str], form_value: Any) -> Optional[Violation]:
    """Dokumentdienst (describe_images): Bildbeschreibung laeuft ueber den Anthropic-Vision-Provider und hat
    keinen Gemini-Weg. Fuer eingeschraenkte Apps laut abweisen statt still ohne Beschreibung zu antworten."""
    wanted = str(form_value or "").strip().lower() in ("1", "true", "yes", "on")
    if wanted and is_restricted(app_id):
        return Violation(
            403, "image_requires_gemini",
            "describe_images=true schickt Bilder an den Anthropic-Vision-Provider; fuer diese App (Energy bzw. ohne "
            "erlaubte Identitaet) sind Bilder nur an Gemini zulaessig, und der Dokumentdienst hat keinen Gemini-Weg. "
            "Nicht stillschweigend ohne Beschreibung beantwortet.",
        )
    return None


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
    if not is_restricted(app_id) or method.upper() not in _WRITE_METHODS:
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


# ----------------------------------------------------------------------------- AUSGANG
#
# Die TRAGENDE Pruefung (Kanzler Dev, 18.10.2026, R5): nicht der rohe Eingang, den wir anders
# parsen koennten als FastAPI, sondern das, was wir selbst zum Anbieter schicken.
#
# 1. ``enforce_image_egress`` sitzt in ``chat_completions`` EINMAL nach allen Pins und Tier-
#    Entscheidungen, vor dem ersten Backend-Zweig. Es benutzt dieselbe Weiche wie der Router
#    (``has_vision_content`` + ``resolve_vision_target``): ein Bild einer Gemini-only-App ist nur
#    zulaessig, wenn der Router es selbst an Gemini schickt. Bedrock-Pin, Anthropic-Direct,
#    OpenAI-kompatibel, Claude-Vision-Schluessel: alles andere endet laut.
# 2. ``guard_provider_payload`` ist die Letztpruefung im Serialisierer jedes Nicht-Gemini-Anbieters
#    (Bedrock, Anthropic-Vision, OpenAI-kompatibel) auf dem fertigen Anbieter-Body, auch fuer
#    Wege, die nicht durch ``chat_completions`` laufen.
#
# Base64/Data-URL, die im fertigen Anbieter-Body nur als TEXT steht, ist kein Bildtransport: bei
# Anthropic, Bedrock, OpenAI-kompatiblen Anbietern und Gemini wird ein Bild ausschliesslich ueber
# einen typisierten Block (image / image_url / inline_data) als Pixel gelesen; ein Textstring wird
# tokenisiert wie jeder Text. Das Modell sieht kein Bild (eine PNG-Nutzlast ist komprimiert und
# fuer ein Sprachmodell nicht entzifferbar) — es kostet Token. Der Eingang weist es trotzdem ab
# (Datenminimierung, kein Roh-Bild an Anthropic), der Ausgang zaehlt es nicht als Bild.

import contextvars  # noqa: E402

#: Die vom Eingang aufgeloeste App des laufenden Requests: None = nicht gesetzt (kein Request-Kontext),
#: "" = Eingang lief, aber ohne Identitaet. Beides gilt als eingeschraenkt.
CURRENT_APP: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar("bild_policy_app", default=None)


class ImageEgressRefused(Exception):
    """Ein Nicht-Gemini-Anbieter soll Bildinhalt einer Gemini-only-App bekommen."""

    def __init__(self, message: str, status: int = 403, code: str = "image_requires_gemini") -> None:
        super().__init__(message)
        self.message, self.status, self.code = message, status, code


def app_is_gemini_only(*app_ids: Optional[str]) -> bool:
    """Eingeschraenkt (nur Gemini)? Die vom Eingang aufgeloeste App gewinnt; ohne Kontext die uebergebene."""
    current = CURRENT_APP.get()
    effective = current if current is not None else next((a for a in app_ids if a), None)
    return is_restricted(effective)


def structural_images(obj: Any, path: str = "payload") -> list[str]:
    return nontext_content(obj, path) + find_images(obj, path, structural_only=True)


def guard_provider_payload(provider: str, payload: Any) -> None:
    """Letztpruefung im Serialisierer eines Nicht-Gemini-Anbieters (fertiger Body)."""
    if not app_is_gemini_only():
        return
    funde = structural_images(payload, f"{provider}-payload")
    if funde:
        raise ImageEgressRefused(
            f"Bildinhalt ({'; '.join(funde[:3])}) darf fuer diese App nicht an {provider} gehen — "
            "nur an Gemini (provider_tier='gemini-vision'). Aufruf nicht gesendet."
        )


def image_route(backend_config: Any) -> tuple[bool, bool]:
    """(backend ist Gemini, Vision-Weiche schickt ein Bild an Gemini) fuer das FINALE backend_config.

    Dieselbe Funktion (``resolve_vision_target``) entscheidet im Router; ein Bedrock-Pin bleibt
    Anthropic, ein fremder provider_tier ebenso — auch bei gesetztem Deployment-Standard."""
    from src.models import BackendType
    from src.routing.vision_router import VISION_TARGET_GEMINI, resolve_vision_target

    backend = getattr(backend_config, "backend", None)
    return (
        backend == BackendType.GEMINI_API,
        backend != BackendType.BEDROCK and resolve_vision_target(backend_config) == VISION_TARGET_GEMINI,
    )


def enforce_image_egress(*, app_id: Optional[str], messages: Any, has_vision: bool, backend_is_gemini: bool,
                         router_sends_to_gemini: bool) -> None:
    """Die zentrale Pruefung vor dem Backend-Dispatch. Wirft :class:`ImageEgressRefused`.

    ``has_vision``: Ergebnis von ``has_vision_content`` (die Weiche, die das Bild erst erkennt);
    ``router_sends_to_gemini``: ``resolve_vision_target(...) == gemini`` UND kein Bedrock-Pin.
    """
    if not app_is_gemini_only(app_id):
        return
    funde = structural_images(messages, "messages")
    if not funde:
        return
    if backend_is_gemini:
        return
    if has_vision and router_sends_to_gemini:
        return
    if not has_vision:
        raise ImageEgressRefused(
            f"Bildinhalt ({'; '.join(funde[:3])}) liegt in einer Form vor, die die Vision-Weiche nicht "
            "an Gemini routet (z. B. in assistant-/tool-Nachrichten). Er wird nicht an ein Claude-Modell "
            "geschickt.", 400, "image_not_routable",
        )
    raise ImageEgressRefused(
        f"Bildinhalt ({'; '.join(funde[:3])}) wuerde nach den Provider-Entscheidungen (Pin/Tier/Standard) "
        "nicht an Gemini gehen. Nicht stillschweigend umgeleitet: provider_tier='gemini-vision' senden, "
        "ohne Operator-Pin.",
    )


def guard_prompt_text(provider: str, text: Any) -> None:
    """Ausgang fuer Wege, die einen TEXT-Prompt an einen Agenten mit Werkzeugen geben (Claude-CLI,
    Erkunder, Research). Dort kann ein Agent Base64 dekodieren und als Bild lesen — deshalb gilt hier
    auch Bildnutzlast im Text (anders als im typisierten Chat-Body, siehe Abschnitt AUSGANG)."""
    if not app_is_gemini_only():
        return
    funde = find_images(text if not isinstance(text, (bytes, bytearray)) else bytes(text).decode("utf-8", "ignore"), f"{provider}-prompt")
    if funde:
        raise ImageEgressRefused(
            f"Bildinhalt ({'; '.join(funde[:3])}) darf fuer diese App nicht an {provider} gehen — nur an Gemini. "
            "Aufruf nicht gesendet."
        )


# Read is checked before execution, built-in results after execution.
# Fetch, notebooks, MCP and delegation do not have this contract.
TEXT_ONLY_AGENT_TOOLS = frozenset(
    {"Grep", "Glob", "Write", "Edit", "Read", "Bash", "LS"}
)


def guard_agent_tools(
    provider: str,
    allowed: Any,
    denied: Any = None,
    *,
    mcp_servers: Any = None,
    resume: Any = None,
) -> Optional[list[str]]:
    """Return the exact CLI tool set, or refuse a capability we cannot constrain.

    None means an unrestricted app. An empty list means NO tools (CLI --tools "").
    allowed_tools alone is a permission list, not removal of the other built-ins.
    """
    if not app_is_gemini_only():
        return None
    wanted = list(allowed or [])
    # No explicit allow list normally means all CLI defaults. Only the existing
    # no-tools contract (Read AND Bash denied) is safe to interpret as no tools.
    implicit_defaults = not wanted and not {"Read", "Bash"}.issubset(set(denied or []))
    if (
        implicit_defaults
        or set(wanted) - TEXT_ONLY_AGENT_TOOLS
        or mcp_servers
        or resume
    ):
        raise ImageEgressRefused(
            f"{provider}: Werkzeugzugriff kann Bilder an Claude liefern. "
            "Für diese App sind nur ausdrücklich gewählte Textwerkzeuge "
            "(Grep, Glob, Write, Edit, Read, Bash, LS) mit Inhaltsprüfung erlaubt. "
            "MCP und Sitzungsfortsetzung nicht ausgeführt.",
            code="image_tool_access_refused",
        )
    return wanted


def constrain_agent_options(options: Any) -> None:
    """Final tool restriction immediately before SDK dispatch."""
    exact = guard_agent_tools(
        "claude-cli",
        options.allowed_tools,
        options.disallowed_tools,
        mcp_servers=options.mcp_servers,
        resume=getattr(options, "resume", None)
        or getattr(options, "continue_conversation", False),
    )
    if exact is None:
        return
    install_agent_content_hooks(options)
    options.extra_args = {
        **(getattr(options, "extra_args", None) or {}),
        "tools": ",".join(exact),
        "strict-mcp-config": None,
        "setting-sources": "",
        "disable-slash-commands": None,
    }


def check_read_text(tool_input: Any, cwd: str) -> None:
    """Read has native image/PDF modes: prove the complete file is text first."""
    from pathlib import Path

    from src.erkunder.dateien import read_bytes

    raw = tool_input.get("file_path") if isinstance(tool_input, dict) else None
    try:
        if not isinstance(raw, str):
            raise ValueError("Dateipfad fehlt")
        path = Path(raw)
        path = path if path.is_absolute() else Path(cwd) / path
        data = read_bytes(path)
        # PDF can be entirely ASCII; UTF-8 alone is not proof of plain text.
        if b"%PDF-" in data[:1024] or _is_raw_image(data):
            raise ValueError("Bild-/PDF-Signatur")
        text = data.decode("utf-8")
        if any(ord(c) < 32 and c not in "\n\r\t\f" for c in text):
            raise ValueError("Binaere Steuerzeichen")
        if scan_string(text):
            raise ValueError("Bildnutzlast im Text")
    except (OSError, UnicodeError, ValueError) as exc:
        raise ImageEgressRefused(
            "Read abgewiesen: Datei ist nicht als reiner UTF-8-Text belegt "
            "(Inhaltsprüfung einschließlich Bild-/PDF-Signaturen).",
            code="image_tool_access_refused",
        ) from exc


def check_tool_response(name: str, response: Any) -> None:
    """Normalize documented CLI text wrappers; everything else is content."""
    content = response
    if isinstance(response, dict) and name == "Bash":
        allowed = {
            "stdout",
            "stderr",
            "interrupted",
            "isImage",
            "backgroundTaskId",
            "returnCode",
            "exitCode",
        }
        if set(response) <= allowed and not response.get("isImage", False):
            if all(isinstance(response.get(k, ""), str) for k in ("stdout", "stderr")):
                content = response.get("stdout", "") + "\n" + response.get("stderr", "")
    elif (
        isinstance(response, dict) and name == "Read" and response.get("type") == "text"
    ):
        file = response.get("file")
        if (
            set(response) <= {"type", "file"}
            and isinstance(file, dict)
            and set(file)
            <= {"filePath", "content", "numLines", "startLine", "totalLines"}
            and isinstance(file.get("content"), str)
        ):
            content = file["content"]
    elif name in {"Grep", "Glob", "Write", "Edit", "LS"} and isinstance(response, dict):
        # These CLI built-ins serialize their structured results as text.
        # Typed content, if present, must still pass the block contract.
        if "type" not in response and "content" not in response:
            content = json.dumps(response)
        elif "type" not in response and isinstance(response.get("content"), str):
            content = response["content"]
    # Wrap to force content validation even for an unknown dict without a type.
    findings = nontext_content({"type": "tool_result", "content": content})
    findings += find_images(content)
    if findings:
        raise ImageEgressRefused(
            "Werkzeugantwort abgewiesen: Inhalt nicht auf Text-Positivliste; "
            "nur an Gemini.",
            code="image_tool_access_refused",
        )


def install_agent_content_hooks(options: Any) -> None:
    from claude_code_sdk import HookMatcher

    failures: list[ImageEgressRefused] = []
    options._image_policy_failures = failures

    async def before(event: dict, tool_use_id: Any, context: Any) -> dict:
        try:
            if event.get("tool_name") == "Read":
                check_read_text(event.get("tool_input"), str(options.cwd))
        except ImageEgressRefused as exc:
            failures.append(exc)
            return {
                "continue": False,
                "stopReason": exc.message,
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": exc.message,
                },
            }
        return {}

    async def after(event: dict, tool_use_id: Any, context: Any) -> dict:
        try:
            check_tool_response(event.get("tool_name", ""), event.get("tool_response"))
        except ImageEgressRefused as exc:
            failures.append(exc)
            return {
                "continue": False,
                "stopReason": exc.message,
                "decision": "block",
                "reason": exc.message,
            }
        return {}

    existing = dict(getattr(options, "hooks", None) or {})
    # Preserve path-boundary hooks; the policy applies to every selected tool.
    existing["PreToolUse"] = [
        *existing.get("PreToolUse", []),
        HookMatcher(matcher="Read", hooks=[before]),
    ]
    existing["PostToolUse"] = [
        *existing.get("PostToolUse", []),
        HookMatcher(hooks=[after]),
    ]
    options.hooks = existing
