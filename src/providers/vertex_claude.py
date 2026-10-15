"""Claude ueber Google Vertex AI (EU-Endpunkt) — nur ausdruecklich angefordert.

Rafael 2026-10-15: ein zusaetzlicher Weg zu Claude, ohne dass sich Standard,
Routing oder ein bestehender Provider aendern. Erreichbar ausschliesslich ueber
den Tier ``claude-vertex-eu`` UND das Master-Flag ``BRIDGE_VERTEX_CLAUDE_ENABLED``
(Default: aus). Das Flag wird pro Aufruf gelesen — Abschalten wirkt sofort.

Endpunkt (gemessen 2026-10-15)::

    https://aiplatform.<region>.rep.googleapis.com/v1/projects/<PROJEKT>/
        locations/<region>/publishers/anthropic/models/<modell>:rawPredict

Body ist die Anthropic Messages API mit ``anthropic_version`` im Body statt im
Header und ohne ``model`` (das Modell steht in der URL).

Fail loud: ein Fehler von Google (auch 429 RESOURCE_EXHAUSTED, solange das
Kontingent auf 0 steht) wird als ``VertexClaudeError`` mit dem ORIGINAL-Status
weitergereicht. Kein Retry, kein Rueckfall auf einen anderen Anbieter — wer
diesen Weg anfordert, soll genau ihn bekommen oder eine Absage lesen.

Konfiguration (alles aus der Umgebung, nichts hartcodiert):
  GCP_VERTEX_SA_JSON       Dienstkonto-JSON als Text (Secret, Infisical)
  GCP_VERTEX_SA_JSON_FILE  Alternative: Pfad zu einer Datei mit dem JSON
  GCP_VERTEX_PROJECT       optional; Default: ``project_id`` aus der SA-JSON
  GCP_VERTEX_LOCATION      optional; Default ``eu``
  GCP_VERTEX_HOST          optional; Default ``aiplatform.<location>.rep.googleapis.com``

Der Schluessel taucht in keiner Fehlermeldung und keinem Log auf.
"""

import asyncio
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import httpx
import jwt

from src.models import ChatCompletionRequest
from src.providers.openai_compatible import ProviderError
from src.routing.vision_router import prepare_messages_for_vision

logger = logging.getLogger(__name__)

ENABLE_FLAG_ENV = "BRIDGE_VERTEX_CLAUDE_ENABLED"
SA_JSON_ENV = "GCP_VERTEX_SA_JSON"
SA_JSON_FILE_ENV = "GCP_VERTEX_SA_JSON_FILE"

ANTHROPIC_VERSION = "vertex-2023-10-16"
OAUTH_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
DEFAULT_LOCATION = "eu"
DEFAULT_TIMEOUT_S = 600.0
TOKEN_TIMEOUT_S = 30.0

#: Modelle, die der Request ueber ``model`` waehlen darf (gemessen verfuegbar).
#: Alles andere bekommt das Modell des Tiers — wie beim Direktweg: der Tier
#: waehlt den ANBIETER, das Modell nur innerhalb dieser Liste.
ALLOWED_MODELS = frozenset({"claude-sonnet-5-5", "claude-haiku-5-5"})

#: Token wird erneuert, sobald weniger als so viele Sekunden Restlaufzeit bleiben.
_TOKEN_REFRESH_MARGIN_S = 120


class VertexClaudeRefused(RuntimeError):
    """Weg angefragt, aber hier nicht eingeschaltet oder nicht konfiguriert."""


class VertexClaudeError(ProviderError):
    """Google hat den Aufruf abgelehnt oder ist gescheitert. ``status_code`` ist
    der ORIGINAL-Status von Google (429 bleibt 429)."""


def vertex_claude_enabled() -> bool:
    """Master-Flag, default aus. Pro Aufruf gelesen."""
    return (os.getenv(ENABLE_FLAG_ENV) or "").strip().lower() in ("1", "true", "yes", "on")


def vertex_claude_configured() -> bool:
    """True, wenn eine SA-JSON-Quelle gesetzt ist (Inhalt wird nicht geprueft)."""
    return bool(os.getenv(SA_JSON_ENV) or os.getenv(SA_JSON_FILE_ENV))


def assert_vertex_claude_allowed() -> None:
    """Wirft ``VertexClaudeRefused``, wenn Flag aus oder Konfiguration fehlt."""
    if not vertex_claude_enabled():
        raise VertexClaudeRefused(
            f"Der Vertex-Claude-Weg ist auf dieser Bridge nicht eingeschaltet "
            f"({ENABLE_FLAG_ENV} ist nicht gesetzt)."
        )
    if not vertex_claude_configured():
        raise VertexClaudeRefused(
            f"Vertex-Claude ist eingeschaltet, aber weder {SA_JSON_ENV} noch "
            f"{SA_JSON_FILE_ENV} ist gesetzt. Kein Rueckfall auf einen anderen Anbieter."
        )


@dataclass(frozen=True)
class VertexSettings:
    project: str
    location: str
    host: str


def _load_service_account() -> Dict[str, Any]:
    """SA-JSON aus Env oder Datei lesen. Fehlertexte nennen nie den Inhalt."""
    raw = os.getenv(SA_JSON_ENV)
    if not raw:
        path = os.getenv(SA_JSON_FILE_ENV)
        if not path:
            raise VertexClaudeRefused(f"{SA_JSON_ENV} / {SA_JSON_FILE_ENV} nicht gesetzt")
        try:
            with open(path, "r", encoding="utf-8") as fh:
                raw = fh.read()
        except OSError as e:
            raise VertexClaudeRefused(
                f"{SA_JSON_FILE_ENV} nicht lesbar: {type(e).__name__}"
            ) from None
    try:
        sa = json.loads(raw)
    except ValueError:
        # bewusst ohne Ausschnitt des Inhalts: das waere ein Schluessel-Leck
        raise VertexClaudeRefused("Dienstkonto-JSON ist kein gueltiges JSON") from None
    missing = [k for k in ("client_email", "private_key", "token_uri") if not sa.get(k)]
    if missing:
        raise VertexClaudeRefused(
            f"Dienstkonto-JSON unvollstaendig, es fehlt: {', '.join(missing)}"
        )
    return sa


def resolve_settings(sa: Dict[str, Any]) -> VertexSettings:
    project = os.getenv("GCP_VERTEX_PROJECT") or sa.get("project_id")
    if not project:
        raise VertexClaudeRefused(
            "Kein Projekt: weder GCP_VERTEX_PROJECT noch project_id in der SA-JSON"
        )
    location = os.getenv("GCP_VERTEX_LOCATION") or DEFAULT_LOCATION
    host = os.getenv("GCP_VERTEX_HOST") or f"aiplatform.{location}.rep.googleapis.com"
    return VertexSettings(project=project, location=location, host=host)


class _TokenCache:
    """OAuth2-Bearer-Token per JWT-Bearer-Grant, bis kurz vor Ablauf gecacht."""

    def __init__(self) -> None:
        self._token: Optional[str] = None
        self._expires_at: float = 0.0
        self._lock: Optional[asyncio.Lock] = None
        self._lock_loop: Optional[asyncio.AbstractEventLoop] = None

    def reset(self) -> None:
        self._token = None
        self._expires_at = 0.0

    def _get_lock(self) -> asyncio.Lock:
        # Ein Lock gehoert an eine Event-Loop; bei Loop-Wechsel (Tests) neu anlegen.
        loop = asyncio.get_running_loop()
        if self._lock is None or self._lock_loop is not loop:
            self._lock = asyncio.Lock()
            self._lock_loop = loop
        return self._lock

    def _valid(self) -> bool:
        return bool(self._token) and time.time() < self._expires_at - _TOKEN_REFRESH_MARGIN_S

    async def get(self, sa: Dict[str, Any]) -> str:
        if self._valid():
            return self._token  # type: ignore[return-value]
        async with self._get_lock():
            if self._valid():
                return self._token  # type: ignore[return-value]
            now = int(time.time())
            assertion = jwt.encode(
                {
                    "iss": sa["client_email"],
                    "scope": OAUTH_SCOPE,
                    "aud": sa["token_uri"],
                    "iat": now,
                    "exp": now + 3600,
                },
                sa["private_key"],
                algorithm="RS256",
            )
            async with httpx.AsyncClient(timeout=TOKEN_TIMEOUT_S) as client:
                resp = await client.post(
                    sa["token_uri"],
                    data={
                        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                        "assertion": assertion,
                    },
                )
            if resp.status_code != 200:
                raise VertexClaudeError(
                    resp.status_code,
                    f"Google-Token-Abruf fehlgeschlagen: {_error_text(resp)}",
                )
            body = resp.json()
            token = body.get("access_token")
            if not token:
                raise VertexClaudeError(502, "Google-Token-Antwort ohne access_token")
            self._token = token
            self._expires_at = time.time() + float(body.get("expires_in", 3600))
            return token


_token_cache = _TokenCache()


def _error_text(resp: httpx.Response) -> str:
    """Kurzer, schluesselfreier Fehlertext aus einer Google-Fehlerantwort."""
    try:
        err = resp.json().get("error", {})
        if isinstance(err, dict):
            text = f"{err.get('status', '')} {err.get('message', '')}".strip()
            return text[:500] or resp.text[:500]
        return str(err)[:500]
    except Exception:  # noqa: BLE001 — Fehlertext ist best effort
        return resp.text[:500]


def _split_system(
    messages: List[Dict[str, Any]],
) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    """Anthropic fuehrt ``system`` ausserhalb von ``messages``."""
    system_parts: List[str] = []
    rest: List[Dict[str, Any]] = []
    for m in messages:
        if m["role"] == "system":
            content = m["content"]
            if isinstance(content, list):
                content = "\n".join(
                    b.get("text", "")
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                )
            system_parts.append(str(content))
        else:
            rest.append(m)
    return ("\n\n".join(system_parts) or None), rest


def build_body(request: ChatCompletionRequest, max_tokens: int) -> Dict[str, Any]:
    messages = prepare_messages_for_vision(request.messages)
    system, rest = _split_system(messages)
    body: Dict[str, Any] = {
        "anthropic_version": ANTHROPIC_VERSION,
        "max_tokens": max_tokens,
        "messages": rest,
    }
    if system:
        body["system"] = system
    if request.temperature is not None:
        body["temperature"] = request.temperature
    if request.thinking is not None:
        body["thinking"] = request.thinking
    if request.output_config is not None:
        body["output_config"] = request.output_config
    return body


def pick_model(request_model: Optional[str], tier_model: str) -> str:
    return request_model if request_model in ALLOWED_MODELS else tier_model


async def call_vertex_claude(
    request: ChatCompletionRequest,
    tier_model: str,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> Dict[str, Any]:
    """Nicht-streamender Aufruf (``:rawPredict``); Rueckgabe im OpenAI-Format
    wie ``call_anthropic_direct``.

    Raises:
        VertexClaudeRefused: Flag aus / Konfiguration fehlt.
        VertexClaudeError: Google antwortete mit einem Fehler (Original-Status).
    """
    assert_vertex_claude_allowed()
    sa = _load_service_account()
    settings = resolve_settings(sa)
    model = pick_model(request.model, tier_model)
    body = build_body(request, request.max_tokens or 4096)
    token = await _token_cache.get(sa)

    url = (
        f"https://{settings.host}/v1/projects/{settings.project}/locations/"
        f"{settings.location}/publishers/anthropic/models/{model}:rawPredict"
    )
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            url,
            json=body,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
    if resp.status_code != 200:
        text = _error_text(resp)
        logger.error(
            "Vertex-Claude %s (model=%s, location=%s): %s",
            resp.status_code, model, settings.location, text,
        )
        raise VertexClaudeError(resp.status_code, text)

    data = resp.json()
    content = "".join(
        b.get("text", "")
        for b in data.get("content", [])
        if isinstance(b, dict) and b.get("type") == "text"
    )
    usage = data.get("usage") or {}
    in_tok = usage.get("input_tokens", 0) or 0
    out_tok = usage.get("output_tokens", 0) or 0
    stop = data.get("stop_reason")
    return {
        "id": f"chatcmpl-vertex-{uuid.uuid4().hex[:16]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": data.get("model") or model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "length" if stop == "max_tokens" else "stop",
            }
        ],
        "usage": {
            "prompt_tokens": in_tok,
            "completion_tokens": out_tok,
            "total_tokens": in_tok + out_tok,
            "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0) or 0,
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0) or 0,
        },
    }
