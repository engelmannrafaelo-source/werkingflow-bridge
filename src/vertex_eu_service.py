"""Claude über Google Vertex AI, EU-Multiregion (backend="vertex_eu").

Vorgebaut 21.10.2026 (Auftrag Rafael, Bühne b-hey-chat-kannst-du-mich-in-
meinem-gemin--20261021). Gleiche Rolle wie src/bedrock_service.py: ein
direkter Anthropic-Messages-Aufruf ohne Claude-Code-SDK, nur mit Google als
Verarbeiter und EU-Multiregion ``locations/eu``.

Gemessen 21.10.2026: der Endpunkt antwortet 429, weil die Quote
``eu_multi_region_online_prediction_requests_per_base_model`` auf 0 steht.
Darum ist der Weg standardmaessig AUS (VERTEX_EU_ENABLED) und jede
Quotenablehnung wird laut und mit Namen gemeldet — eine Quote 0 ist keine
Ueberlastung, die ein Retry heilt.

Konfiguration (nur aus der Umgebung, nichts hier eingetragen):

* ``VERTEX_EU_ENABLED``          ``1``/``true`` schaltet den Weg frei. Sonst ist
  ein Pin auf vertex_eu unbedienbar (503, kein Rueckfall).
* ``GCP_VERTEX_SA_JSON``         Service-Account-Schluessel als JSON-Text oder
  als Pfad zu einer JSON-Datei. ``project_id`` kommt daraus. Wird nie geloggt.
* ``VERTEX_EU_LOCATION``         Vorgabe ``eu``.
* ``VERTEX_EU_ENDPOINT``         Vorgabe ``https://aiplatform.eu.rep.googleapis.com``.
* ``VERTEX_EU_ALLOW_NON_PROD``   ``1`` erlaubt Aufrufe aus staging/local (siehe
  src/routing/user_provider_override.assert_vertex_eu_allowed).

Der OAuth2-Token entsteht ohne google-auth: signierte JWT-Assertion (RS256,
pyjwt + cryptography sind ohnehin Abhaengigkeiten) gegen ``token_uri`` des
Service-Accounts, gecacht bis kurz vor Ablauf.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx
from fastapi import HTTPException

from config.logging_config import get_logger
from src.bedrock_service import convert_messages_to_anthropic
from src.error_contract import UNCLASSIFIED_RETRYABLE, fields
from src.model_registry import model_supports_temperature, resolve_model
from src.model_request import adapt_model_request
from src.models import ChatCompletionRequest
from src.stream_start import error_event

logger = get_logger(__name__)

VERTEX_ANTHROPIC_VERSION = "vertex-2023-10-16"
DEFAULT_LOCATION = "eu"
DEFAULT_ENDPOINT = "https://aiplatform.eu.rep.googleapis.com"
OAUTH_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
DEFAULT_MAX_TOKENS = 4096

#: Vertex-Modellnamen je Bridge-Modell. Bewusst nur, was am 21.10.2026 gegen
#: den Endpunkt geprueft wurde (Antwort 429 Quote, also Modell bekannt). Ein
#: fehlender Eintrag ist ein lauter 400, kein geratener Name.
VERTEX_MODEL_IDS: Dict[str, str] = {
    "claude-sonnet-5-5": "claude-sonnet-5-5",
    "claude-haiku-5-5": "claude-haiku-5-5",
}

# connect kurz, read lang: eine grosse Generierung braucht Minuten (wie Bedrock).
_HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=600.0, write=60.0, pool=10.0)

_TRUTHY = {"1", "true", "yes", "on"}

_STOP_REASON_MAP = {
    "end_turn": "stop",
    "max_tokens": "length",
    "stop_sequence": "stop",
    "refusal": "content_filter",
    "tool_use": "tool_calls",
    "pause_turn": "pause_turn",
}

_TRANSIENT_STREAM_ERROR_TYPES = frozenset({"overloaded_error", "rate_limit_error", "api_error"})


class VertexEUNotConfigured(RuntimeError):
    """Schalter aus oder Zugangsdaten fehlen/kaputt — Konfiguration, kein Aussetzer."""


class VertexEUStreamAborted(RuntimeError):
    def __init__(self, message: str, *, retryable: bool = UNCLASSIFIED_RETRYABLE):
        super().__init__(message)
        self.retryable = retryable


class VertexEUError(RuntimeError):
    """Abgelehnter Vertex-Aufruf mit Status und Urteil (fuer Strom und Ledger)."""

    def __init__(self, status: int, code: str, message: str, retryable: bool):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retryable = retryable

    def to_http(self) -> HTTPException:
        return HTTPException(status_code=self.status, detail={
            "message": str(self),
            "type": "vertex_eu_error",
            "code": self.code,
            **fields(self.retryable),
        })


def is_enabled() -> bool:
    return (os.getenv("VERTEX_EU_ENABLED") or "").strip().lower() in _TRUTHY


def non_prod_allowed() -> bool:
    return (os.getenv("VERTEX_EU_ALLOW_NON_PROD") or "").strip().lower() in _TRUTHY


def _load_service_account() -> Dict[str, Any]:
    raw = (os.getenv("GCP_VERTEX_SA_JSON") or "").strip()
    if not raw:
        raise VertexEUNotConfigured(
            "Vertex EU: GCP_VERTEX_SA_JSON ist nicht gesetzt (Service-Account-Schluessel fehlt)."
        )
    try:
        if raw.startswith("{"):
            sa = json.loads(raw)
        else:
            with open(raw, "r", encoding="utf-8") as fh:
                sa = json.load(fh)
    except (OSError, ValueError) as exc:
        # Inhalt nie ausgeben — nur die Art des Fehlers.
        raise VertexEUNotConfigured(
            f"Vertex EU: GCP_VERTEX_SA_JSON nicht lesbar ({type(exc).__name__})."
        ) from None
    missing = [k for k in ("client_email", "private_key", "project_id") if not sa.get(k)]
    if missing:
        raise VertexEUNotConfigured(
            "Vertex EU: Service-Account-JSON ohne " + ", ".join(missing) + "."
        )
    return sa


def readiness() -> None:
    """Wirft VertexEUNotConfigured, wenn der Weg hier nicht bedienbar ist.

    Vom backend_router vor jedem Aufruf gefragt: ein Pin ohne Schalter oder
    ohne Zugangsdaten endet so als 503 ``user_provider_override_unavailable``,
    nie auf einem anderen Backend."""
    if not is_enabled():
        raise VertexEUNotConfigured(
            "Vertex EU backend is disabled on this Bridge (VERTEX_EU_ENABLED is off)."
        )
    _load_service_account()


def vertex_model_id(resolved_model: str) -> str:
    try:
        return VERTEX_MODEL_IDS[resolved_model]
    except KeyError:
        raise VertexEUError(
            400, "vertex_eu_model_not_mapped",
            f"Model {resolved_model!r} has no Vertex EU mapping "
            f"(available: {sorted(VERTEX_MODEL_IDS)}). No fallback model is chosen.",
            False,
        ) from None


class _TokenCache:
    def __init__(self) -> None:
        self.token: Optional[str] = None
        self.expires_at: float = 0.0
        self.key: Optional[str] = None
        self.lock = asyncio.Lock()


_token_cache = _TokenCache()


def reset_token_cache() -> None:
    global _token_cache
    _token_cache = _TokenCache()


def _http_client() -> httpx.AsyncClient:
    """Eigene Fabrik, damit Tests den Transport ersetzen koennen."""
    return httpx.AsyncClient(timeout=_HTTP_TIMEOUT)


async def _access_token(sa: Dict[str, Any]) -> str:
    import jwt  # pyjwt, mit cryptography fuer RS256

    cache = _token_cache
    key = sa["client_email"]
    now = time.time()
    if cache.token and cache.key == key and cache.expires_at - 300 > now:
        return cache.token
    async with cache.lock:
        now = time.time()
        if cache.token and cache.key == key and cache.expires_at - 300 > now:
            return cache.token
        token_uri = sa.get("token_uri") or "https://oauth2.googleapis.com/token"
        assertion = jwt.encode(
            {
                "iss": sa["client_email"],
                "scope": OAUTH_SCOPE,
                "aud": token_uri,
                "iat": int(now),
                "exp": int(now) + 3600,
            },
            sa["private_key"],
            algorithm="RS256",
            headers={"kid": sa["private_key_id"]} if sa.get("private_key_id") else None,
        )
        try:
            async with _http_client() as client:
                resp = await client.post(token_uri, data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": assertion,
                })
        except httpx.HTTPError as exc:
            raise VertexEUError(
                502, "vertex_eu_oauth_unreachable",
                f"Vertex EU: OAuth token endpoint unreachable ({type(exc).__name__}).",
                True,
            ) from exc
        if resp.status_code != 200:
            # Antworttext von Google enthaelt keine Geheimnisse (error/error_description).
            raise VertexEUError(
                424, "vertex_eu_oauth_rejected",
                f"Vertex EU: service account token rejected by Google "
                f"(HTTP {resp.status_code}): {resp.text[:300]}",
                False,
            )
        body = resp.json()
        cache.token = body["access_token"]
        cache.expires_at = now + int(body.get("expires_in", 3600))
        cache.key = key
        return cache.token


def _quota_details(payload: Any) -> Dict[str, Optional[str]]:
    """Quota-Name und -Grenze aus einem Google-Fehlerkoerper (ErrorInfo/QuotaFailure)."""
    out: Dict[str, Optional[str]] = {"metric": None, "limit_value": None}
    err = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        err = payload[0].get("error")
    if not isinstance(err, dict):
        return out
    for d in err.get("details") or []:
        if not isinstance(d, dict):
            continue
        meta = d.get("metadata") or {}
        out["metric"] = out["metric"] or meta.get("quota_metric") or meta.get("quota_limit")
        out["limit_value"] = out["limit_value"] or meta.get("quota_limit_value")
        for v in d.get("violations") or []:
            if isinstance(v, dict):
                out["metric"] = out["metric"] or v.get("quotaMetric") or v.get("subject")
    if not out["metric"]:
        msg = err.get("message") or ""
        marker = "eu_multi_region_online_prediction_requests_per_base_model"
        if marker in msg:
            out["metric"] = marker
    return out


def classify_http_error(status: int, text: str) -> VertexEUError:
    """Google-Ablehnung → Status/Code/Urteil. Eine Quote steht immer beim Namen."""
    try:
        payload = json.loads(text) if text else {}
    except ValueError:
        payload = {}
    snippet = text[:500]
    if status == 429:
        q = _quota_details(payload)
        metric = q["metric"] or "unbekannte Quote"
        if q["limit_value"] == "0":
            return VertexEUError(
                429, "vertex_eu_quota_zero",
                f"Vertex EU quota is 0 ({metric}) — no request can succeed until the "
                "GCP quota is raised. Not retryable. Google: " + snippet,
                False,
            )
        return VertexEUError(
            429, "vertex_eu_quota_exceeded",
            f"Vertex EU quota/rate limit exceeded ({metric}). Google: " + snippet,
            True,
        )
    if status in (401, 403):
        return VertexEUError(
            424, "vertex_eu_permission_denied",
            f"Vertex EU rejected the service account (HTTP {status}) — IAM role "
            "'Vertex AI User' or Model-Garden enablement missing. Google: " + snippet,
            False,
        )
    if status == 404:
        return VertexEUError(
            424, "vertex_eu_model_not_found",
            "Vertex EU: model/location not found (HTTP 404) — model not enabled for "
            "this project or not offered in locations/eu. Google: " + snippet,
            False,
        )
    if status == 400:
        return VertexEUError(400, "vertex_eu_invalid_request", "Vertex EU: " + snippet, False)
    if status in (500, 502, 503, 504, 529):
        return VertexEUError(
            502 if status != 503 else 503, "vertex_eu_upstream_error",
            f"Vertex EU upstream error (HTTP {status}). Google: " + snippet, True,
        )
    return VertexEUError(502, "vertex_eu_unexpected_status",
                         f"Vertex EU: unexpected HTTP {status}. Google: " + snippet, False)


def build_body(request: ChatCompletionRequest, resolved_model: str) -> Dict[str, Any]:
    system_prompt, messages = convert_messages_to_anthropic(request.messages)
    if not request.max_tokens:
        logger.warning("Vertex EU: kein max_tokens im Request — Vorgabe %d wird gesetzt",
                       DEFAULT_MAX_TOKENS)
    body: Dict[str, Any] = {
        "anthropic_version": VERTEX_ANTHROPIC_VERSION,
        "messages": messages,
        "max_tokens": request.max_tokens or DEFAULT_MAX_TOKENS,
    }
    if system_prompt:
        body["system"] = system_prompt
    if request.temperature is not None and request.temperature != 1.0:
        if model_supports_temperature(resolved_model):
            body["temperature"] = request.temperature
        else:
            logger.warning("Vertex EU: temperature=%s verworfen, %r lehnt es ab",
                           request.temperature, resolved_model)
    if request.top_p is not None and request.top_p != 1.0:
        body["top_p"] = request.top_p
    if request.stop:
        body["stop_sequences"] = request.stop if isinstance(request.stop, list) else [request.stop]
    if request.thinking is not None:
        body["thinking"] = request.thinking
    if request.output_config is not None:
        body["output_config"] = request.output_config
    adapt_model_request(resolved_model, body)
    return body


def _resolve(request: ChatCompletionRequest) -> tuple[str, str]:
    resolved_model, _ = resolve_model(request.model)
    if not resolved_model:
        raise VertexEUError(400, "unknown_model", f"Unknown model: {request.model}", False)
    return resolved_model, vertex_model_id(resolved_model)


def _endpoint(sa: Dict[str, Any], model_id: str, stream: bool) -> tuple[str, str]:
    base = (os.getenv("VERTEX_EU_ENDPOINT") or DEFAULT_ENDPOINT).rstrip("/")
    location = os.getenv("VERTEX_EU_LOCATION") or DEFAULT_LOCATION
    verb = "streamRawPredict" if stream else "rawPredict"
    url = (f"{base}/v1/projects/{sa['project_id']}/locations/{location}"
           f"/publishers/anthropic/models/{model_id}:{verb}")
    return url, location


def map_stop_reason(stop_reason: Optional[str]) -> str:
    if not stop_reason:
        raise VertexEUStreamAborted("Vertex EU response without stop_reason — end not confirmed")
    return _STOP_REASON_MAP.get(stop_reason, stop_reason)


def map_content(blocks: List[Dict[str, Any]]) -> tuple[str, List[Dict[str, Any]]]:
    """Anthropic-Inhaltsbloecke → (Text, OpenAI tool_calls)."""
    text = ""
    tool_calls: List[Dict[str, Any]] = []
    for block in blocks or []:
        btype = block.get("type")
        if btype == "text":
            text += block.get("text", "")
        elif btype == "tool_use":
            tool_calls.append({
                "id": block.get("id"),
                "type": "function",
                "function": {
                    "name": block.get("name"),
                    "arguments": json.dumps(block.get("input") or {}),
                },
            })
    return text, tool_calls


async def call_vertex_eu(request: ChatCompletionRequest) -> Dict[str, Any]:
    """Nicht-Strom-Aufruf. Liefert ein OpenAI-kompatibles Antwort-Dict.

    Wirft HTTPException (Status/Code/Urteil aus classify_http_error)."""
    try:
        sa = _load_service_account()
        resolved_model, model_id = _resolve(request)
        body = build_body(request, resolved_model)
        url, location = _endpoint(sa, model_id, stream=False)
        token = await _access_token(sa)
        logger.info("Calling Vertex EU: model=%s location=%s", model_id, location)
        try:
            async with _http_client() as client:
                resp = await client.post(url, json=body, headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                })
        except httpx.HTTPError as exc:
            raise VertexEUError(502, "vertex_eu_unreachable",
                                f"Vertex EU unreachable ({type(exc).__name__}: {exc})",
                                True) from exc
        if resp.status_code != 200:
            err = classify_http_error(resp.status_code, resp.text)
            logger.error("Vertex EU error %s: %s", err.code, err)
            raise err
        data = resp.json()
        try:
            finish_reason = map_stop_reason(data.get("stop_reason"))
        except VertexEUStreamAborted as exc:
            raise VertexEUError(502, "vertex_eu_no_stop_reason", str(exc), False) from exc
    except VertexEUNotConfigured as exc:
        raise HTTPException(status_code=503, detail={
            "message": str(exc), "type": "configuration_error",
            "code": "vertex_eu_not_configured", **fields(False),
        }) from exc
    except VertexEUError as exc:
        raise exc.to_http() from exc

    text, tool_calls = map_content(data.get("content") or [])
    usage = data.get("usage") or {}
    message: Dict[str, Any] = {"role": "assistant", "content": text}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex[:16]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": resolved_model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0) or 0,
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0) or 0,
        },
        "x_backend_info": {
            "backend": "vertex_eu",
            "region": location,
            "privacy_applied": False,
            "model_id_used": model_id,
            "provider_request_id": data.get("id"),
        },
    }


def _sse(response_id: str, model: str, delta: Dict[str, Any],
         finish_reason: Optional[str] = None) -> str:
    return "data: " + json.dumps({
        "id": response_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }) + "\n\n"


def _stream_error(exc: BaseException) -> str:
    if isinstance(exc, VertexEUError):
        return error_event(str(exc), "vertex_eu_error", exc.code, fields(exc.retryable))
    if isinstance(exc, VertexEUNotConfigured):
        return error_event(str(exc), "configuration_error", "vertex_eu_not_configured", fields(False))
    if isinstance(exc, VertexEUStreamAborted):
        return error_event(str(exc), "vertex_eu_stream_error", "stream_aborted", fields(exc.retryable))
    if isinstance(exc, httpx.HTTPError):
        return error_event(f"Vertex EU transport error: {exc}", "vertex_eu_stream_error",
                           type(exc).__name__, fields(True))
    return error_event(str(exc) or type(exc).__name__, "vertex_eu_stream_error",
                       type(exc).__name__, fields(UNCLASSIFIED_RETRYABLE))


async def stream_vertex_eu(
    request: ChatCompletionRequest,
    usage_sink: Optional[Dict[str, Any]] = None,
) -> AsyncGenerator[str, None]:
    """Strom ueber ``:streamRawPredict`` (Anthropic-SSE) → OpenAI-SSE.

    usage_sink wie bei stream_bedrock: input_tokens, output_tokens,
    cache_*_tokens, vertex_model_id, location, provider_request_id, status
    ('success' erst nach message_stop), error_message/error_code."""
    if usage_sink is None:
        usage_sink = {}
    usage_sink.setdefault("status", "error")
    response_id = f"chatcmpl-{uuid.uuid4().hex[:16]}"
    try:
        sa = _load_service_account()
        resolved_model, model_id = _resolve(request)
        usage_sink["vertex_model_id"] = model_id
        body = build_body(request, resolved_model)
        body["stream"] = True
        url, location = _endpoint(sa, model_id, stream=True)
        usage_sink["location"] = location
        token = await _access_token(sa)
        logger.info("Streaming from Vertex EU: model=%s location=%s", model_id, location)

        stop_reason: Optional[str] = None
        final_sent = False
        # Anthropic-Blockindex → OpenAI tool_calls-Index
        tool_index: Dict[int, int] = {}
        async with _http_client() as client:
            async with client.stream("POST", url, json=body, headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            }) as resp:
                if resp.status_code != 200:
                    text = (await resp.aread()).decode("utf-8", "replace")
                    raise classify_http_error(resp.status_code, text)
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    raw = line[5:].strip()
                    if not raw:
                        continue
                    try:
                        ev = json.loads(raw)
                    except ValueError:
                        continue
                    etype = ev.get("type")
                    if etype == "error":
                        err = ev.get("error") or {}
                        raise VertexEUStreamAborted(
                            f"Vertex EU stream error: {json.dumps(err)[:300]}",
                            retryable=isinstance(err, dict)
                            and err.get("type") in _TRANSIENT_STREAM_ERROR_TYPES,
                        )
                    if etype == "message_start":
                        msg = ev.get("message") or {}
                        usage_sink["provider_request_id"] = msg.get("id")
                        u = msg.get("usage") or {}
                        if "input_tokens" in u:
                            usage_sink["input_tokens"] = u["input_tokens"]
                        usage_sink["cache_read_tokens"] = u.get("cache_read_input_tokens", 0) or 0
                        usage_sink["cache_creation_tokens"] = u.get("cache_creation_input_tokens", 0) or 0
                        if "output_tokens" in u:
                            usage_sink["output_tokens"] = u["output_tokens"]
                    elif etype == "content_block_start":
                        block = ev.get("content_block") or {}
                        if block.get("type") == "tool_use":
                            idx = len(tool_index)
                            tool_index[ev.get("index", idx)] = idx
                            yield _sse(response_id, resolved_model, {"tool_calls": [{
                                "index": idx,
                                "id": block.get("id"),
                                "type": "function",
                                "function": {"name": block.get("name"), "arguments": ""},
                            }]})
                    elif etype == "content_block_delta":
                        delta = ev.get("delta") or {}
                        if delta.get("type") == "text_delta" and delta.get("text"):
                            yield _sse(response_id, resolved_model, {"content": delta["text"]})
                        elif delta.get("type") == "input_json_delta":
                            idx = tool_index.get(ev.get("index"))
                            if idx is not None:
                                yield _sse(response_id, resolved_model, {"tool_calls": [{
                                    "index": idx,
                                    "function": {"arguments": delta.get("partial_json", "")},
                                }]})
                    elif etype == "message_delta":
                        u = ev.get("usage") or {}
                        if "output_tokens" in u:
                            usage_sink["output_tokens"] = u["output_tokens"]
                        if (ev.get("delta") or {}).get("stop_reason"):
                            stop_reason = ev["delta"]["stop_reason"]
                    elif etype == "message_stop":
                        yield _sse(response_id, resolved_model, {},
                                   finish_reason=map_stop_reason(stop_reason))
                        final_sent = True
        if not final_sent:
            raise VertexEUStreamAborted(
                "Vertex EU stream ended without message_stop — response is truncated",
                retryable=True,
            )
        usage_sink["status"] = "success"
        yield "data: [DONE]\n\n"
    except Exception as exc:  # noqa: BLE001 — wird als error-Ereignis gemeldet, nie geschluckt
        logger.error("Vertex EU streaming error: %s", exc)
        usage_sink["error_message"] = str(exc)
        usage_sink["error_code"] = getattr(exc, "code", type(exc).__name__)
        yield _stream_error(exc)
