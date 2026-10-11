import base64
import copy
import hashlib

import pytest
from pydantic import ValidationError

from src.models import ResearchRequest, ResearchResponse


def payload(**overrides):
    result = dict(
        query="Sortiere Titel eines Filmarchivs.",
        model="synthetic",
        max_turns=4,
        max_tokens=40,
        antwort_im_text=True,
        async_mode=False,
        agentischer_lauf={
            "schema": "agentischer-lauf/1",
            "lauf_id": "run-1",
            "systemtext": "App-eigener Wortlaut.",
            "material": [],
            "werkzeuge": [],
            "ausgabe": {"modus": "text", "artefakte": []},
            "grenzen": {"zeit_s": 20, "tokens_gesamt": 500},
        },
    )
    result.update(overrides)
    return result


def material(path="notes/catalog.txt", data=b"title"):
    return {
        "id": path,
        "ziel": path,
        "sha256": hashlib.sha256(data).hexdigest(),
        "bytes": len(data),
        "quelle": {"art": "inline", "content_base64": base64.b64encode(data).decode()},
    }


def test_new_mode_not_silently_dropped():
    body = ResearchRequest(**payload())
    assert body.agentischer_lauf.lauf_id == "run-1"


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_tokens", True),
        ("max_turns", "4"),
        ("max_tokens", 0),
        ("max_turns", 1.5),
        ("async_mode", True),
        ("async_mode", 0),
        ("antwort_im_text", "true"),
        ("query", "  "),
        ("model", None),
        ("research_mode", "standard"),
        ("depth", "standard"),
        ("strategy", "unified"),
        ("max_hops", None),
        ("source_filter", None),
        ("output_path", None),
        ("cloud_overflow", False),
    ],
)
def test_invalid_request(field, value):
    with pytest.raises(ValidationError):
        ResearchRequest(**payload(**{field: value}))


@pytest.mark.parametrize(
    "path", ["/host/a", "../a", "a/../b", "a//b", "a/./b", "a\\b", "a/", "a:stream", "a\x00b"]
)
def test_paths(path):
    body = payload()
    body["agentischer_lauf"]["material"] = [material(path)]
    with pytest.raises(ValidationError):
        ResearchRequest(**body)


@pytest.mark.parametrize(
    "change",
    [
        "unknown",
        "duplicate",
        "bool_size",
        "null_review",
        "negative_limit",
        "external_ref",
        "bad_schema",
    ],
)
def test_nested_contract(change):
    body = payload()
    run = body["agentischer_lauf"]
    if change == "unknown":
        run["fachrolle"] = "x"
    elif change == "duplicate":
        run["material"] = [material(), material()]
    elif change == "bool_size":
        run["material"] = [material()]
        run["material"][0]["bytes"] = True
    elif change == "null_review":
        run["pruefung"] = None
    elif change == "negative_limit":
        run["grenzen"]["tokens_gesamt"] = -1
    else:
        run["ausgabe"] = {
            "modus": "json",
            "artefakte": [],
            "json_schema": (
                {"$ref": "https://invalid.example/schema"}
                if change == "external_ref"
                else {"type": "invalid-type"}
            ),
        }
    with pytest.raises(ValidationError):
        ResearchRequest(**body)


def test_legacy_and_general_errors():
    assert ResearchRequest(query="legacy").agentischer_lauf is None
    assert (
        ResearchResponse(
            status="error", query="q", model="m", fehler_code="grenze_tokens"
        ).retryable
        is False
    )


def test_legacy_prompts_byte_identical_to_frozen_base():
    from pathlib import Path

    raw = (Path(__file__).parents[2] / "src/erkunder/prompts.py").read_bytes()
    assert (
        hashlib.sha256(raw).hexdigest()
        == "72895db295ca5841f3340532750f1b68ad2a46b47d57dc961a7088007b951388"
    )
