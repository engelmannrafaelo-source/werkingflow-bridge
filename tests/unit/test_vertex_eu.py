"""Vertex EU (backend=vertex_eu) — Pin, Gate, Schalter, Fehler, Strom, tool_use.

Alles gegen einen gemockten Endpunkt (httpx.MockTransport): der echte
Endpunkt antwortete am 21.10.2026 mit 429 (Quote
eu_multi_region_online_prediction_requests_per_base_model = 0).
"""
import json
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

import src.vertex_eu_service as vx
from src.activity.providers import PROVIDER_VERTEX_EU, REAL_COST_PROVIDERS, ledger_provider_for_backend
from src.models import BackendType, ChatCompletionRequest, Message
from src.routing.backend_router import resolve_backend_config
from src.routing.user_provider_override import (
    SUPPORTED_PROVIDERS,
    UserProviderOverrideError,
    VertexEUGateError,
    apply_user_provider_override,
    assert_vertex_eu_allowed,
)

PROJECT = "test-projekt-123"


@pytest.fixture(scope="module")
def sa_json():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    return json.dumps({
        "type": "service_account",
        "project_id": PROJECT,
        "private_key_id": "kid-1",
        "private_key": pem,
        "client_email": "bridge@test-projekt-123.iam.gserviceaccount.com",
        "token_uri": "https://oauth2.example.test/token",
    })


@pytest.fixture
def vertex_env(monkeypatch, sa_json):
    monkeypatch.setenv("VERTEX_EU_ENABLED", "1")
    monkeypatch.setenv("GCP_VERTEX_SA_JSON", sa_json)
    monkeypatch.delenv("VERTEX_EU_ALLOW_NON_PROD", raising=False)
    monkeypatch.delenv("VERTEX_EU_ENDPOINT", raising=False)
    monkeypatch.delenv("VERTEX_EU_LOCATION", raising=False)
    vx.reset_token_cache()
    yield
    vx.reset_token_cache()


class Mock:
    """Zeichnet Aufrufe auf; antwortet je nach Pfad."""

    def __init__(self, predict):
        self.calls = []
        self.predict = predict

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if request.url.host == "oauth2.example.test":
            return httpx.Response(200, json={"access_token": "tok-abc", "expires_in": 3600})
        return self.predict(request)


@pytest.fixture
def mock_http(monkeypatch):
    holder = {}

    def install(predict):
        m = Mock(predict)
        monkeypatch.setattr(
            vx, "_http_client",
            lambda: httpx.AsyncClient(transport=httpx.MockTransport(m.handler)),
        )
        holder["m"] = m
        return m

    return install


def _req(**kw):
    base = dict(model="claude-sonnet-5-5", messages=[Message(role="user", content="Hallo")], max_tokens=100)
    base.update(kw)
    return ChatCompletionRequest(**base)


# --- Pin / Schalter -------------------------------------------------------

def test_provider_ist_unterstuetzt_und_ledger_eigene_vokabel():
    assert "vertex_eu" in SUPPORTED_PROVIDERS
    assert ledger_provider_for_backend(BackendType.VERTEX_EU) == PROVIDER_VERTEX_EU == "vertex-eu"
    assert PROVIDER_VERTEX_EU in REAL_COST_PROVIDERS


def test_pin_setzt_backend_und_raeumt_tier_ab():
    body = SimpleNamespace(backend=BackendType.ANTHROPIC, provider_tier="claude-premium",
                           bedrock_region="eu-central-1", enable_tools=False)
    assert apply_user_provider_override(body, {"provider": "vertex_eu"}, app_env="prod") == "vertex_eu"
    assert body.backend == BackendType.VERTEX_EU
    assert body.provider_tier is None and body.bedrock_region is None


def test_pin_wird_ausserhalb_prod_nicht_still_heruntergestuft():
    body = SimpleNamespace(backend=BackendType.ANTHROPIC, provider_tier=None,
                           bedrock_region=None, enable_tools=False)
    assert apply_user_provider_override(body, {"provider": "vertex_eu"}, app_env="staging") == "vertex_eu"
    assert body.backend == BackendType.VERTEX_EU


def test_pin_mit_tools_wird_abgewiesen():
    body = SimpleNamespace(backend=None, provider_tier=None, bedrock_region=None, enable_tools=True)
    with pytest.raises(UserProviderOverrideError, match="enable_tools"):
        apply_user_provider_override(body, {"provider": "vertex_eu"}, app_env="prod")


def test_schalter_standardmaessig_aus(monkeypatch, sa_json):
    monkeypatch.delenv("VERTEX_EU_ENABLED", raising=False)
    monkeypatch.setenv("GCP_VERTEX_SA_JSON", sa_json)
    # RuntimeError → main.py: 503 user_provider_override_unavailable fuer Gepinnte
    with pytest.raises(RuntimeError, match="VERTEX_EU_ENABLED"):
        resolve_backend_config(backend=BackendType.VERTEX_EU, model="claude-sonnet-5-5")


def test_pin_ohne_credentials_ist_runtimeerror(monkeypatch):
    monkeypatch.setenv("VERTEX_EU_ENABLED", "1")
    monkeypatch.delenv("GCP_VERTEX_SA_JSON", raising=False)
    with pytest.raises(RuntimeError, match="GCP_VERTEX_SA_JSON"):
        resolve_backend_config(backend=BackendType.VERTEX_EU, model="claude-sonnet-5-5")


def test_kaputtes_sa_json_verraet_keinen_inhalt(monkeypatch):
    monkeypatch.setenv("VERTEX_EU_ENABLED", "1")
    monkeypatch.setenv("GCP_VERTEX_SA_JSON", '{"private_key": "GEHEIM-xyz"')
    with pytest.raises(RuntimeError) as ei:
        resolve_backend_config(backend=BackendType.VERTEX_EU, model="claude-sonnet-5-5")
    assert "GEHEIM" not in str(ei.value)


def test_router_mit_schalter_und_credentials(vertex_env):
    cfg = resolve_backend_config(backend=BackendType.VERTEX_EU, model="claude-haiku-5-5")
    assert cfg.backend == BackendType.VERTEX_EU
    assert cfg.region == "eu"
    assert cfg.provider_model == "claude-haiku-5-5"
    assert cfg.privacy_enabled is False


def test_router_unbekanntes_modell_400(vertex_env):
    with pytest.raises(HTTPException) as ei:
        resolve_backend_config(backend=BackendType.VERTEX_EU, model="claude-opus-4-5")
    assert ei.value.status_code == 400


# --- Gate ----------------------------------------------------------------

def test_gate_ohne_pin_403():
    with pytest.raises(VertexEUGateError) as ei:
        assert_vertex_eu_allowed(BackendType.VERTEX_EU, None, app_env="prod", app_id="x")
    assert ei.value.status == 403 and ei.value.code == "vertex_eu_requires_operator_pin"


def test_gate_app_regel_pin_zaehlt_nicht():
    with pytest.raises(VertexEUGateError):
        assert_vertex_eu_allowed(BackendType.VERTEX_EU, "bedrock", app_env="prod", app_id="x")


def test_gate_staging_nur_mit_freigabe(monkeypatch):
    monkeypatch.delenv("VERTEX_EU_ALLOW_NON_PROD", raising=False)
    with pytest.raises(VertexEUGateError) as ei:
        assert_vertex_eu_allowed(BackendType.VERTEX_EU, "vertex_eu", app_env="staging", app_id="x")
    assert ei.value.code == "vertex_eu_requires_production_env"
    monkeypatch.setenv("VERTEX_EU_ALLOW_NON_PROD", "1")
    assert_vertex_eu_allowed(BackendType.VERTEX_EU, "vertex_eu", app_env="staging", app_id="x")


def test_gate_ohne_app_env_nie(monkeypatch):
    monkeypatch.setenv("VERTEX_EU_ALLOW_NON_PROD", "1")
    with pytest.raises(VertexEUGateError):
        assert_vertex_eu_allowed(BackendType.VERTEX_EU, "vertex_eu", app_env=None, app_id="x")


def test_gate_ohne_app_id_400():
    with pytest.raises(VertexEUGateError) as ei:
        assert_vertex_eu_allowed(BackendType.VERTEX_EU, "vertex_eu", app_env="prod", app_id=None)
    assert ei.value.status == 400


def test_gate_andere_backends_unberuehrt():
    assert_vertex_eu_allowed(BackendType.ANTHROPIC, None, app_env=None, app_id=None)


# --- Aufruf ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_rawpredict_url_body_token_und_antwort(vertex_env, mock_http):
    def predict(req):
        return httpx.Response(200, json={
            "id": "msg_vrtx_1", "type": "message", "role": "assistant",
            "content": [{"type": "text", "text": "Hallo zurück"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 12, "output_tokens": 5},
        })

    m = mock_http(predict)
    resp = await vx.call_vertex_eu(_req(messages=[
        Message(role="system", content="Sei knapp."), Message(role="user", content="Hallo"),
    ]))
    token_req, call = m.calls
    assert token_req.url.host == "oauth2.example.test"
    assert b"jwt-bearer" in token_req.content
    assert str(call.url) == (
        "https://aiplatform.eu.rep.googleapis.com/v1/projects/" + PROJECT +
        "/locations/eu/publishers/anthropic/models/claude-sonnet-5-5:rawPredict"
    )
    assert call.headers["authorization"] == "Bearer tok-abc"
    body = json.loads(call.content)
    assert body["anthropic_version"] == "vertex-2023-10-16"
    assert body["max_tokens"] == 100
    assert body["system"] == "Sei knapp."
    assert "model" not in body  # Modell steht in der URL
    assert resp["choices"][0]["message"]["content"] == "Hallo zurück"
    assert resp["choices"][0]["finish_reason"] == "stop"
    assert resp["usage"]["prompt_tokens"] == 12 and resp["usage"]["completion_tokens"] == 5
    assert resp["x_backend_info"]["backend"] == "vertex_eu"
    assert resp["x_backend_info"]["provider_request_id"] == "msg_vrtx_1"


@pytest.mark.asyncio
async def test_token_wird_gecacht(vertex_env, mock_http):
    m = mock_http(lambda r: httpx.Response(200, json={
        "content": [], "stop_reason": "end_turn", "usage": {}}))
    await vx.call_vertex_eu(_req())
    await vx.call_vertex_eu(_req())
    assert sum(1 for c in m.calls if c.url.host == "oauth2.example.test") == 1


@pytest.mark.asyncio
async def test_tool_use_wird_zu_tool_calls(vertex_env, mock_http):
    mock_http(lambda r: httpx.Response(200, json={
        "content": [
            {"type": "text", "text": "Ich schaue nach."},
            {"type": "tool_use", "id": "toolu_1", "name": "wetter", "input": {"ort": "Wien"}},
        ],
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 1, "output_tokens": 2},
    }))
    resp = await vx.call_vertex_eu(_req())
    msg = resp["choices"][0]["message"]
    assert resp["choices"][0]["finish_reason"] == "tool_calls"
    assert msg["tool_calls"] == [{
        "id": "toolu_1", "type": "function",
        "function": {"name": "wetter", "arguments": json.dumps({"ort": "Wien"})},
    }]


QUOTA_ZERO = {"error": {
    "code": 429,
    "message": "Quota exceeded for aiplatform.googleapis.com/"
               "eu_multi_region_online_prediction_requests_per_base_model with base model: anthropic-claude-sonnet-5-5.",
    "status": "RESOURCE_EXHAUSTED",
    "details": [{
        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
        "reason": "RATE_LIMIT_EXCEEDED",
        "metadata": {
            "quota_metric": "aiplatform.googleapis.com/eu_multi_region_online_prediction_requests_per_base_model",
            "quota_limit_value": "0",
        },
    }],
}}


@pytest.mark.asyncio
async def test_quote_null_laut_und_nicht_wiederholbar(vertex_env, mock_http):
    mock_http(lambda r: httpx.Response(429, json=QUOTA_ZERO))
    with pytest.raises(HTTPException) as ei:
        await vx.call_vertex_eu(_req())
    d = ei.value.detail
    assert ei.value.status_code == 429
    assert d["code"] == "vertex_eu_quota_zero"
    assert "eu_multi_region_online_prediction_requests_per_base_model" in d["message"]
    assert d["retryable"] is False


@pytest.mark.asyncio
async def test_quote_ueberschritten_wiederholbar(vertex_env, mock_http):
    body = json.loads(json.dumps(QUOTA_ZERO))
    body["error"]["details"][0]["metadata"]["quota_limit_value"] = "60"
    mock_http(lambda r: httpx.Response(429, json=body))
    with pytest.raises(HTTPException) as ei:
        await vx.call_vertex_eu(_req())
    assert ei.value.detail["code"] == "vertex_eu_quota_exceeded"
    assert ei.value.detail["retryable"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("status,http,code", [
    (403, 424, "vertex_eu_permission_denied"),
    (404, 424, "vertex_eu_model_not_found"),
    (400, 400, "vertex_eu_invalid_request"),
    (503, 503, "vertex_eu_upstream_error"),
])
async def test_fehlerzuordnung(vertex_env, mock_http, status, http, code):
    mock_http(lambda r: httpx.Response(status, json={"error": {"message": "x"}}))
    with pytest.raises(HTTPException) as ei:
        await vx.call_vertex_eu(_req())
    assert ei.value.status_code == http and ei.value.detail["code"] == code


@pytest.mark.asyncio
async def test_oauth_abgelehnt_424(vertex_env, monkeypatch):
    def handler(req):
        return httpx.Response(400, json={"error": "invalid_grant"})
    monkeypatch.setattr(vx, "_http_client",
                        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(HTTPException) as ei:
        await vx.call_vertex_eu(_req())
    assert ei.value.status_code == 424
    assert ei.value.detail["code"] == "vertex_eu_oauth_rejected"


@pytest.mark.asyncio
async def test_ohne_stop_reason_502(vertex_env, mock_http):
    mock_http(lambda r: httpx.Response(200, json={"content": [{"type": "text", "text": "abgesch"}]}))
    with pytest.raises(HTTPException) as ei:
        await vx.call_vertex_eu(_req())
    assert ei.value.status_code == 502


# --- Strom ----------------------------------------------------------------

def _sse(events):
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


STREAM_OK = [
    {"type": "message_start", "message": {"id": "msg_vrtx_s", "usage": {"input_tokens": 9, "output_tokens": 1}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hal"}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "lo"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "content_block_start", "index": 1,
     "content_block": {"type": "tool_use", "id": "toolu_9", "name": "suche", "input": {}}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "{\"q\":"}},
    {"type": "content_block_delta", "index": 1, "delta": {"type": "input_json_delta", "partial_json": "\"x\"}"}},
    {"type": "content_block_stop", "index": 1},
    {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 7}},
    {"type": "message_stop"},
]


def _chunks(out):
    return [json.loads(c[6:]) for c in out if c.startswith("data: {")]


@pytest.mark.asyncio
async def test_strom_text_tool_calls_usage(vertex_env, mock_http):
    m = mock_http(lambda r: httpx.Response(200, content=_sse(STREAM_OK),
                                           headers={"content-type": "text/event-stream"}))
    sink = {}
    out = [c async for c in vx.stream_vertex_eu(_req(stream=True), usage_sink=sink)]
    call = m.calls[-1]
    assert str(call.url).endswith("claude-sonnet-5-5:streamRawPredict")
    assert json.loads(call.content)["stream"] is True
    chunks = _chunks(out)
    text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)
    assert text == "Hallo"
    tc = [c["choices"][0]["delta"]["tool_calls"][0] for c in chunks if "tool_calls" in c["choices"][0]["delta"]]
    assert tc[0]["id"] == "toolu_9" and tc[0]["function"]["name"] == "suche"
    assert "".join(t["function"]["arguments"] for t in tc) == '{"q":"x"}'
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert out[-1] == "data: [DONE]\n\n"
    assert sink["status"] == "success"
    assert sink["input_tokens"] == 9 and sink["output_tokens"] == 7
    assert sink["provider_request_id"] == "msg_vrtx_s"
    assert sink["vertex_model_id"] == "claude-sonnet-5-5" and sink["location"] == "eu"


@pytest.mark.asyncio
async def test_strom_abgerissen_ist_fehler(vertex_env, mock_http):
    mock_http(lambda r: httpx.Response(200, content=_sse(STREAM_OK[:4])))
    sink = {}
    out = [c async for c in vx.stream_vertex_eu(_req(stream=True), usage_sink=sink)]
    assert out[-1].startswith("event: error")
    assert "[DONE]" not in "".join(out)
    assert sink["status"] == "error"
    assert sink["input_tokens"] == 9  # bis zum Abriss gezaehlt, wird gebucht


@pytest.mark.asyncio
async def test_strom_quote_null_als_error_event(vertex_env, mock_http):
    mock_http(lambda r: httpx.Response(429, json=QUOTA_ZERO))
    sink = {}
    out = [c async for c in vx.stream_vertex_eu(_req(stream=True), usage_sink=sink)]
    assert len(out) == 1 and out[0].startswith("event: error")
    payload = json.loads(out[0].split("data: ", 1)[1])
    assert payload["error"]["code"] == "vertex_eu_quota_zero"
    assert payload["error"]["retryable"] is False
    assert sink["status"] == "error" and sink["error_code"] == "vertex_eu_quota_zero"


@pytest.mark.asyncio
async def test_strom_schalter_aus_sofort_error(monkeypatch):
    monkeypatch.delenv("GCP_VERTEX_SA_JSON", raising=False)
    out = [c async for c in vx.stream_vertex_eu(_req(stream=True), usage_sink={})]
    assert out[0].startswith("event: error") and "vertex_eu_not_configured" in out[0]
