"""Vertex-Claude (EU): nur mit Flag erreichbar, Fehler laut, Token gecacht.

HTTP ist gemockt (httpx.MockTransport); der RSA-Schluessel wird pro Testlauf
erzeugt und ist KEIN echter Schluessel.
"""
import json

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from src.models import BackendType, ChatCompletionRequest, Message
from src.providers import vertex_claude as vc
from src.providers.registry import (
    DEFAULT_TIER,
    PROVIDERS,
    UnknownProviderTierError,
    get_provider,
    list_available_providers,
)
from src.routing.backend_router import resolve_backend_config

TIER = "claude-vertex-eu"
TOKEN_URI = "https://oauth2.example.test/token"


@pytest.fixture(scope="module")
def pem() -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


@pytest.fixture
def sa_json(pem) -> str:
    return json.dumps({
        "client_email": "svc@proj-x.iam.example.test",
        "private_key": pem,
        "token_uri": TOKEN_URI,
        "project_id": "proj-x",
    })


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in (vc.ENABLE_FLAG_ENV, vc.SA_JSON_ENV, vc.SA_JSON_FILE_ENV,
              "GCP_VERTEX_PROJECT", "GCP_VERTEX_LOCATION", "GCP_VERTEX_HOST"):
        monkeypatch.delenv(k, raising=False)
    vc._token_cache.reset()


@pytest.fixture
def enabled(monkeypatch, sa_json):
    monkeypatch.setenv(vc.ENABLE_FLAG_ENV, "1")
    monkeypatch.setenv(vc.SA_JSON_ENV, sa_json)


def _request(**kw) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        model=kw.pop("model", "claude-sonnet-5-5"),
        messages=[Message(role="system", content="sei knapp"), Message(role="user", content="hi")],
        max_tokens=16,
        **kw,
    )


def _install_transport(monkeypatch, handler):
    """Alle httpx.AsyncClient-Instanzen im Modul laufen ueber den Mock."""
    real = httpx.AsyncClient

    def factory(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real(*a, **kw)

    monkeypatch.setattr(vc.httpx, "AsyncClient", factory)


def _google(calls, predict):
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(req)
        if str(req.url) == TOKEN_URI:
            return httpx.Response(200, json={"access_token": "tok-1", "expires_in": 3600})
        return predict(req)
    return handler


OK_BODY = {
    "model": "claude-sonnet-5-5",
    "content": [{"type": "text", "text": "hallo"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 5, "output_tokens": 2},
}


# --- Flag aus = Weg nicht erreichbar, Standard unveraendert -----------------

def test_flag_off_tier_is_unknown_to_callers():
    # Klassenidentitaet nicht pruefen: unter pytest wird die Registry je nach
    # Reihenfolge mehrfach geladen (siehe test_unknown_provider_tier_fails_loud).
    with pytest.raises(RuntimeError, match="nicht eingeschaltet") as ei:
        get_provider(TIER)
    assert type(ei.value).__name__ == UnknownProviderTierError.__name__


def test_flag_off_resolve_backend_config_refuses():
    with pytest.raises(RuntimeError):
        resolve_backend_config(
            BackendType.ANTHROPIC, "claude-sonnet-5", provider_tier=TIER,
        )


def test_flag_off_provider_list_has_no_vertex_entry():
    assert TIER not in {p["tier_id"] for p in list_available_providers()}


def test_default_routing_unchanged_with_flag_off():
    assert get_provider(None).tier_id == DEFAULT_TIER
    cfg = resolve_backend_config(BackendType.ANTHROPIC, "claude-sonnet-5")
    assert cfg.backend == BackendType.ANTHROPIC and cfg.provider_tier is None


def test_default_routing_unchanged_with_flag_on(enabled):
    """Auch eingeschaltet aendert der Tier nichts, solange er nicht angefordert wird."""
    assert get_provider(None).tier_id == DEFAULT_TIER
    cfg = resolve_backend_config(BackendType.ANTHROPIC, "claude-sonnet-5")
    assert cfg.backend == BackendType.ANTHROPIC


def test_vertex_tier_is_in_no_fallback_chain():
    from src.providers.fallback import FALLBACK_CHAINS
    assert TIER not in FALLBACK_CHAINS
    assert all(TIER not in chain for chain in FALLBACK_CHAINS.values())


def test_flag_on_but_unconfigured_is_loud(monkeypatch):
    monkeypatch.setenv(vc.ENABLE_FLAG_ENV, "1")
    with pytest.raises(RuntimeError):
        resolve_backend_config(BackendType.ANTHROPIC, "claude-sonnet-5", provider_tier=TIER)


def test_flag_on_resolves_to_vertex_backend(enabled):
    cfg = resolve_backend_config(BackendType.ANTHROPIC, "claude-sonnet-5", provider_tier=TIER)
    assert cfg.backend == BackendType.VERTEX_CLAUDE
    assert cfg.provider_model == "claude-sonnet-5-5"
    assert TIER in {p["tier_id"] for p in list_available_providers()}


async def test_call_with_flag_off_raises_without_http(monkeypatch):
    def boom(req):
        raise AssertionError("kein HTTP erlaubt")
    _install_transport(monkeypatch, boom)
    with pytest.raises(vc.VertexClaudeRefused):
        await vc.call_vertex_claude(_request(), "claude-sonnet-5-5")


# --- Erfolg ------------------------------------------------------------------

async def test_success_builds_correct_request_and_openai_response(enabled, monkeypatch):
    calls = []
    _install_transport(monkeypatch, _google(calls, lambda r: httpx.Response(200, json=OK_BODY)))
    out = await vc.call_vertex_claude(_request(), "claude-sonnet-5-5")

    assert out["choices"][0]["message"]["content"] == "hallo"
    assert out["usage"]["prompt_tokens"] == 5 and out["usage"]["completion_tokens"] == 2
    predict = calls[-1]
    assert str(predict.url) == (
        "https://aiplatform.eu.rep.googleapis.com/v1/projects/proj-x/locations/eu/"
        "publishers/anthropic/models/claude-sonnet-5-5:rawPredict"
    )
    assert predict.headers["authorization"] == "Bearer tok-1"
    body = json.loads(predict.content)
    assert body["anthropic_version"] == "vertex-2023-10-16"
    assert body["max_tokens"] == 16
    assert body["system"] == "sei knapp"
    assert [m["role"] for m in body["messages"]] == ["user"]
    assert "model" not in body


async def test_project_and_location_come_from_config(enabled, monkeypatch):
    monkeypatch.setenv("GCP_VERTEX_PROJECT", "anderes-projekt")
    monkeypatch.setenv("GCP_VERTEX_LOCATION", "europe-west4")
    monkeypatch.setenv("GCP_VERTEX_HOST", "host.example.test")
    calls = []
    _install_transport(monkeypatch, _google(calls, lambda r: httpx.Response(200, json=OK_BODY)))
    await vc.call_vertex_claude(_request(model="claude-haiku-5-5"), "claude-sonnet-5-5")
    assert str(calls[-1].url).startswith(
        "https://host.example.test/v1/projects/anderes-projekt/locations/europe-west4/"
        "publishers/anthropic/models/claude-haiku-5-5:rawPredict"
    )


async def test_unlisted_request_model_uses_tier_model(enabled, monkeypatch):
    calls = []
    _install_transport(monkeypatch, _google(calls, lambda r: httpx.Response(200, json=OK_BODY)))
    await vc.call_vertex_claude(_request(model="claude-sonnet-5"), "claude-sonnet-5-5")
    assert "/models/claude-sonnet-5-5:rawPredict" in str(calls[-1].url)


# --- Fehler laut -------------------------------------------------------------

async def test_429_is_raised_loudly_with_original_status(enabled, monkeypatch):
    err = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "Quota exceeded"}}
    calls = []
    _install_transport(monkeypatch, _google(calls, lambda r: httpx.Response(429, json=err)))
    with pytest.raises(vc.VertexClaudeError) as ei:
        await vc.call_vertex_claude(_request(), "claude-sonnet-5-5")
    assert ei.value.status_code == 429
    assert "RESOURCE_EXHAUSTED" in str(ei.value)
    # kein Retry, kein zweiter Predict-Aufruf
    assert sum(1 for c in calls if str(c.url) != TOKEN_URI) == 1


async def test_token_failure_is_loud_and_leaks_no_key(enabled, monkeypatch, pem):
    def handler(req):
        return httpx.Response(401, json={"error": "invalid_grant"})
    _install_transport(monkeypatch, handler)
    with pytest.raises(vc.VertexClaudeError) as ei:
        await vc.call_vertex_claude(_request(), "claude-sonnet-5-5")
    assert ei.value.status_code == 401
    assert "PRIVATE KEY" not in str(ei.value) and pem[:40] not in str(ei.value)


def test_broken_sa_json_error_does_not_echo_content(monkeypatch):
    monkeypatch.setenv(vc.ENABLE_FLAG_ENV, "1")
    monkeypatch.setenv(vc.SA_JSON_ENV, "geheim-kein-json")
    with pytest.raises(vc.VertexClaudeRefused) as ei:
        vc._load_service_account()
    assert "geheim" not in str(ei.value)


# --- Token-Cache -------------------------------------------------------------

async def test_token_is_cached_across_calls(enabled, monkeypatch):
    calls = []
    _install_transport(monkeypatch, _google(calls, lambda r: httpx.Response(200, json=OK_BODY)))
    await vc.call_vertex_claude(_request(), "claude-sonnet-5-5")
    await vc.call_vertex_claude(_request(), "claude-sonnet-5-5")
    assert sum(1 for c in calls if str(c.url) == TOKEN_URI) == 1


async def test_token_refreshed_shortly_before_expiry(enabled, monkeypatch):
    calls = []
    _install_transport(monkeypatch, _google(calls, lambda r: httpx.Response(200, json=OK_BODY)))
    await vc.call_vertex_claude(_request(), "claude-sonnet-5-5")
    vc._token_cache._expires_at -= 3600 - 60  # nur noch 60 s Restlaufzeit (< Marge)
    await vc.call_vertex_claude(_request(), "claude-sonnet-5-5")
    assert sum(1 for c in calls if str(c.url) == TOKEN_URI) == 2


def test_ledger_maps_vertex_backend_to_google():
    from src.activity.providers import PROVIDER_GEMINI, ledger_provider_for_backend
    assert ledger_provider_for_backend(BackendType.VERTEX_CLAUDE) == PROVIDER_GEMINI


def test_existing_tiers_untouched():
    for tier in ("claude-premium", "claude-dsgvo", "claude-direct-notools", "openrouter-claude"):
        assert tier in PROVIDERS
