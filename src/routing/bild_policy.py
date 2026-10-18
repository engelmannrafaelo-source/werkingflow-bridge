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


def find_images(obj: Any, path: str = "body", *, structural_only: bool = False) -> list[str]:
    """Alle Bildfunde in einem JSON-Objekt (Pfad + Art). Wirft bei uebertiefer Struktur.

    ``structural_only``: nur Bildbloecke im Schema eines Anbieters (image/image_url/inline_data/
    source mit Bild-MIME). Base64 oder eine data:-URL, die nur als TEXT dasteht, zaehlt dann nicht:
    kein Anbieter wertet einen Textstring als Bild (siehe Modul-Doku, Abschnitt AUSGANG)."""
    funde: list[str] = []
    stack: list[tuple[Any, str, int]] = [(obj, path, 0)]
    nodes = 0
    while stack:
        node, p, depth = stack.pop()
        if not isinstance(node, (dict, list, str, int, float, bool, type(None))) and hasattr(node, "model_dump"):
            node = node.model_dump()  # Pydantic-ContentPart & Co.: auf dem serialisierten Body pruefen
        nodes += 1
        if depth > _MAX_DEPTH or nodes > _MAX_NODES:
            raise ValueError("Koerper zu tief oder zu gross verschachtelt, um ihn zu pruefen")
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
    return find_images(obj, path, structural_only=True)


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
