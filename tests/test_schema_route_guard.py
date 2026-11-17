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


@pytest.mark.parametrize("backend", [_BT.ANTHROPIC, _BT.ANTHROPIC_DIRECT, "openai_compatible", None, ""])
def test_non_eu_backends_refused(backend):
    assert _code(effective_backend=backend, operator_pin=getattr(backend, "value", backend)) == "schema_route_not_allowed"


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
