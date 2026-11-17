"""Schema-Generator: zugelassene EU-Routen (Bedrock EU, Vertex EU), fail-closed, kein Wechsel zwischen Routen.

c9a50d3b, 17.11.2026 (Rafael: Homepage "fuers Erste non-Bedrock", EU-Vorgabe unveraendert).
Reine Funktionen aus src/routing/schema_bedrock_guard.py; keine Netz-/DB-Abhaengigkeit.
"""
from enum import Enum

import pytest

from src.routing.schema_bedrock_guard import (
    ALLOWED_SCHEMA_ROUTES,
    EXPECTED_MODEL,
    SchemaBedrockExpectationError,
    VERTEX_EU_ENDPOINT_HOST,
    assert_schema_expected_bedrock,
    assert_schema_expected_route,
)


class _BT(str, Enum):
    BEDROCK = "bedrock"
    VERTEX_EU = "vertex_eu"
    ANTHROPIC = "anthropic"
    ANTHROPIC_DIRECT = "anthropic_direct"


EU_HOST = f"https://{VERTEX_EU_ENDPOINT_HOST}"


def _route(**kw):
    base = dict(effective_backend=_BT.VERTEX_EU, operator_pin="vertex_eu", region="eu",
                model_id=EXPECTED_MODEL, vertex_endpoint=EU_HOST)
    base.update(kw)
    return assert_schema_expected_route(**base)


def _code(**kw):
    with pytest.raises(SchemaBedrockExpectationError) as e:
        _route(**kw)
    return e.value.code


def test_allowed_routes_are_exactly_bedrock_and_vertex_eu():
    assert ALLOWED_SCHEMA_ROUTES == {"bedrock": "eu-central-1", "vertex_eu": "eu"}


def test_vertex_eu_route_passes():
    _route()
    _route(vertex_endpoint=EU_HOST + "/")


def test_bedrock_route_still_passes_unchanged():
    _route(effective_backend=_BT.BEDROCK, operator_pin="bedrock", region="eu-central-1", vertex_endpoint=None)
    assert_schema_expected_bedrock(effective_backend=_BT.BEDROCK, operator_pin="bedrock",
                                   region="eu-central-1", model_id=EXPECTED_MODEL)


@pytest.mark.parametrize("backend", [_BT.ANTHROPIC, "openai_compatible", None, ""])
def test_non_eu_backends_refused(backend):
    assert _code(effective_backend=backend, operator_pin=getattr(backend, "value", backend)) == "schema_route_not_allowed"


def test_anthropic_direct_refused_without_allowlist():
    # Nicht-EU-Testweg ohne Allowlist (Standard): abgewiesen, auch mit passendem Pin.
    assert _code(effective_backend=_BT.ANTHROPIC_DIRECT, operator_pin="anthropic_direct") == "schema_noneu_test_user_not_allowed"


@pytest.mark.parametrize("backend,pin", [
    (_BT.VERTEX_EU, "bedrock"),     # kein Bedrock-Pin auf Vertex-Weg
    (_BT.BEDROCK, "vertex_eu"),     # kein Vertex-Pin auf Bedrock-Weg (kein Rueckfall)
    (_BT.VERTEX_EU, None),          # ohne Operator-Pin
    (_BT.VERTEX_EU, ""),
])
def test_pin_must_match_resolved_route(backend, pin):
    region = "eu" if backend == _BT.VERTEX_EU else "eu-central-1"
    assert _code(effective_backend=backend, operator_pin=pin, region=region) == "schema_requires_matching_operator_pin"


@pytest.mark.parametrize("region", ["europe-west1", "europe-west4", "us", "global", "us-central1", None, ""])
def test_vertex_region_must_be_eu_multiregion(region):
    assert _code(region=region) == "schema_route_region_mismatch"


def test_bedrock_region_must_be_frankfurt():
    assert _code(effective_backend=_BT.BEDROCK, operator_pin="bedrock", region="eu-west-1") == "schema_route_region_mismatch"


@pytest.mark.parametrize("model", ["claude-haiku-5-5", "claude-sonnet-5", None])
def test_model_must_be_approved(model):
    assert _code(model_id=model) == "schema_route_model_mismatch"


@pytest.mark.parametrize("endpoint", [
    "https://aiplatform.googleapis.com",                       # global
    "https://aiplatform.us.rep.googleapis.com",                # US
    "https://europe-west1-aiplatform.googleapis.com",          # Region ohne Claude-EU-Verarbeitung
    "https://aiplatform.eu.rep.googleapis.com.evil.example",   # Suffix
    "https://evil.example/aiplatform.eu.rep.googleapis.com",   # Pfad
    "http://aiplatform.eu.rep.googleapis.com",                 # kein TLS
    "https://aiplatform.eu.rep.googleapis.com:8443",           # Port
    "https://x@aiplatform.eu.rep.googleapis.com",              # Benutzerteil
    "https://aiplatform.eu.rep.googleapis.com/v1",             # Pfad
    "",
    None,
])
def test_vertex_endpoint_must_be_exact_eu_host(endpoint):
    assert _code(vertex_endpoint=endpoint) == "schema_vertex_endpoint_not_eu"


def test_strict_bound_covers_vertex_price_for_same_tokens():
    """Die Obergrenze (mit Vertex-Faktor) liegt nie unter dem, was das Ledger fuer Vertex bucht."""
    from src.budget import strict_reservation as sr
    from src.models import ChatCompletionRequest
    from src.pricing import VERTEX_EU_PRICE_MULT, cost_eur

    body = ChatCompletionRequest(model=EXPECTED_MODEL, max_tokens=sr.MAX_TOKENS_CAP,
                                 messages=[{"role": "user", "content": "x" * 4000}])
    grenze = round(sr.cost_upper_bound_eur(EXPECTED_MODEL, body, versuche=1) * max(1.0, float(VERTEX_EU_PRICE_MULT)), 6)
    # Ledger-Preis Vertex fuer denselben Aufruf mit maximaler Ausgabe; Eingabe <= Byte-Obergrenze (4000):
    ledger = cost_eur(EXPECTED_MODEL, 4000, sr.MAX_TOKENS_CAP) * VERTEX_EU_PRICE_MULT
    assert grenze >= ledger > 0
    assert VERTEX_EU_PRICE_MULT >= 1.0


# --- Privater Nicht-EU-Testweg (anthropic_direct), Rafael 17.11.2026: nur Allowlist-Benutzer + Operator-Pin ---
from src.routing.schema_bedrock_guard import NONEU_TEST_PROVIDER, noneu_test_principals, noneu_test_users  # noqa: E402

RAFAEL_TEST = "0f0e0d0c-0b0a-4908-8706-050403020100"
ANDERER = "11111111-2222-4333-8444-555555555555"
TEST_PRINCIPAL = "schema-rafael-test"
PUBLIC_PRINCIPAL = "schema-homepage-prod"


def _direct(**kw):
    base = dict(effective_backend=_BT.ANTHROPIC_DIRECT, operator_pin="anthropic_direct", region=None,
                model_id=EXPECTED_MODEL, vertex_endpoint=None, user_id=RAFAEL_TEST,
                noneu_test_users=noneu_test_users(RAFAEL_TEST), principal_name=TEST_PRINCIPAL,
                noneu_test_principals=noneu_test_principals(TEST_PRINCIPAL))
    base.update(kw)
    return assert_schema_expected_route(**base)


def _direct_code(**kw):
    with pytest.raises(SchemaBedrockExpectationError) as e:
        _direct(**kw)
    return e.value.code


def test_noneu_route_is_not_an_eu_route():
    assert NONEU_TEST_PROVIDER not in ALLOWED_SCHEMA_ROUTES


def test_noneu_route_passes_only_for_listed_user_with_pin():
    _direct()
    _direct(user_id=RAFAEL_TEST.upper())


@pytest.mark.parametrize("raw", [None, "", " ", ",", "nicht-uuid", ANDERER, "*", "all"])
def test_noneu_route_refused_when_user_not_listed(raw):
    assert _direct_code(noneu_test_users=noneu_test_users(raw)) == "schema_noneu_test_user_not_allowed"


@pytest.mark.parametrize("user", [None, "", ANDERER, RAFAEL_TEST + "x"])
def test_noneu_route_refused_for_other_users(user):
    assert _direct_code(user_id=user) == "schema_noneu_test_user_not_allowed"


@pytest.mark.parametrize("pin", [None, "", "bedrock", "vertex_eu", "anthropic"])
def test_noneu_route_requires_direct_pin(pin):
    assert _direct_code(operator_pin=pin) == "schema_requires_matching_operator_pin"


@pytest.mark.parametrize("name", [None, "", PUBLIC_PRINCIPAL, "legacy", "*", TEST_PRINCIPAL + "x"])
def test_noneu_route_requires_own_test_principal(name):
    # A1: oeffentlicher Homepage-Principal / Legacy oeffnen nie, auch mit Test-UUID in X-User-ID und Pin.
    assert _direct_code(principal_name=name) == "schema_noneu_test_principal_not_allowed"


@pytest.mark.parametrize("raw", [None, "", "legacy", "*", "LEGACY", PUBLIC_PRINCIPAL])
def test_noneu_route_principal_list_never_admits_legacy(raw):
    for name in ("legacy", "*", TEST_PRINCIPAL):
        assert _direct_code(principal_name=name, noneu_test_principals=noneu_test_principals(raw)) \
            == "schema_noneu_test_principal_not_allowed"


def test_noneu_route_model_must_be_approved():
    assert _direct_code(model_id="claude-haiku-5-5") == "schema_route_model_mismatch"


def test_allowlist_does_not_open_other_non_eu_backends():
    # Eine Allowlist macht nur anthropic_direct moeglich, nie das Abo-/SDK-Backend.
    with pytest.raises(SchemaBedrockExpectationError) as e:
        _route(effective_backend=_BT.ANTHROPIC, operator_pin="anthropic", user_id=RAFAEL_TEST,
               noneu_test_users=noneu_test_users(RAFAEL_TEST))
    assert e.value.code == "schema_route_not_allowed"


def test_eu_routes_unchanged_by_allowlist():
    _route(user_id=ANDERER, noneu_test_users=noneu_test_users(RAFAEL_TEST))


def test_allowlist_parser_strict():
    assert noneu_test_users(f" {RAFAEL_TEST} , {ANDERER},foo") == frozenset({RAFAEL_TEST, ANDERER})
    assert noneu_test_principals(f" {TEST_PRINCIPAL} ,legacy,*,a b") == frozenset({TEST_PRINCIPAL})


# --- Schema-Direktweg = Prod-eigener Schluessel, nie Pool (Kanzler-Wort 17.11.2026) ---
import asyncio  # noqa: E402


def _vp(monkeypatch, key, pool_url):
    from src import vision_provider as vp
    monkeypatch.setenv("ANTHROPIC_CREDIT_POOL_URL", pool_url) if pool_url else monkeypatch.delenv("ANTHROPIC_CREDIT_POOL_URL", raising=False)
    monkeypatch.setenv("ANTHROPIC_VISION_API_KEY", key) if key else monkeypatch.delenv("ANTHROPIC_VISION_API_KEY", raising=False)
    return vp, vp.VisionProvider()


class _Resp:
    status_code = 200
    text = ""

    def json(self):
        return {"content": [{"type": "text", "text": "ok"}], "model": EXPECTED_MODEL, "stop_reason": "end_turn",
                "usage": {"input_tokens": 3, "output_tokens": 1}}


def _run_analyze(monkeypatch, *, key, pool_url, prod_key_only):
    from src.providers import credit_pool
    vp, prov = _vp(monkeypatch, key, pool_url)
    calls = {"pool": 0, "direct": 0, "key": None}

    async def _pool(body, timeout):
        calls["pool"] += 1
        return _Resp(), {"credit_pool_account": "x"}

    async def _post(self, url, headers=None, json=None, **kw):
        calls["direct"] += 1
        calls["key"] = (headers or {}).get("x-api-key")
        return _Resp()

    monkeypatch.setattr(credit_pool, "post_messages", _pool)
    monkeypatch.setattr(vp.httpx.AsyncClient, "post", _post)
    tok = credit_pool.PROD_KEY_ONLY.set(prod_key_only)
    try:
        asyncio.run(prov.analyze(messages=[{"role": "user", "content": "synthetisch"}], model=EXPECTED_MODEL, max_tokens=1))
    finally:
        credit_pool.PROD_KEY_ONLY.reset(tok)
    return calls


def test_schema_direct_uses_prod_key_never_pool(monkeypatch):
    c = _run_analyze(monkeypatch, key="k-prod", pool_url="http://pool.invalid", prod_key_only=True)
    assert c["pool"] == 0 and c["direct"] == 1 and c["key"] == "k-prod"


def test_schema_direct_without_prod_key_fails_closed_no_pool(monkeypatch):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as e:
        _run_analyze(monkeypatch, key=None, pool_url="http://pool.invalid", prod_key_only=True)
    assert e.value.status_code == 424 and e.value.detail["error"]["code"] == "schema_prod_key_missing"


def test_other_apps_keep_pool_unchanged(monkeypatch):
    c = _run_analyze(monkeypatch, key="k-prod", pool_url="http://pool.invalid", prod_key_only=False)
    assert c["pool"] == 1 and c["direct"] == 0


def test_pool_for_this_call_flag(monkeypatch):
    from src.providers import credit_pool
    monkeypatch.setenv("ANTHROPIC_CREDIT_POOL_URL", "http://pool.invalid")
    assert credit_pool.pool_for_this_call() is True
    tok = credit_pool.PROD_KEY_ONLY.set(True)
    try:
        assert credit_pool.pool_for_this_call() is False
    finally:
        credit_pool.PROD_KEY_ONLY.reset(tok)
    assert credit_pool.PROD_KEY_ONLY.get() is False


# --- F2(a) 17.11.2026: anthropic_direct-Pin auf staging NUR fuer werking-schema-generator ---
from types import SimpleNamespace  # noqa: E402

from src.routing.user_provider_override import STAGING_DIRECT_PIN_APPS, apply_user_provider_override  # noqa: E402


def _body():
    return SimpleNamespace(backend=None, provider_tier=None, enable_tools=False, bedrock_region=None)


def test_staging_direct_pin_only_for_schema_app():
    assert STAGING_DIRECT_PIN_APPS == frozenset({"werking-schema-generator"})
    b = _body()
    assert apply_user_provider_override(b, {"provider": "anthropic_direct"}, app_env="staging",
                                        app_id="werking-schema-generator") == "anthropic_direct"
    assert b.provider_tier == "claude-direct-notools"


@pytest.mark.parametrize("app_env,app_id", [
    ("staging", "werking-report"), ("staging", "werking-energy"), ("staging", None), ("staging", ""),
    ("staging", "Werking-Schema-Generator"),            # exakt, kein Case-Folding
    ("local", "werking-schema-generator"),               # local bleibt unveraendert ignoriert
])
def test_direct_pin_still_ignored_elsewhere_in_non_prod(app_env, app_id):
    b = _body()
    assert apply_user_provider_override(b, {"provider": "anthropic_direct"}, app_env=app_env, app_id=app_id) == "anthropic"
    assert b.provider_tier is None


@pytest.mark.parametrize("pin", [{"provider": "bedrock", "region": "eu-central-1"}])
def test_bedrock_pin_on_staging_unchanged_for_schema_app(pin):
    b = _body()
    assert apply_user_provider_override(b, pin, app_env="staging", app_id="werking-schema-generator") == "anthropic"


def test_prod_direct_pin_unchanged():
    b = _body()
    assert apply_user_provider_override(b, {"provider": "anthropic_direct"}, app_env="prod", app_id="werking-report") == "anthropic_direct"
