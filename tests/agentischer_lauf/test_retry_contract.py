"""ARETRY: persisted failures, real resubmission, CX3 merge and wire contract."""
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from src.agentischer_lauf import http
from src.agentischer_lauf.adapter import RunError
from src.agentischer_lauf.runtime import request_digest
from src.models import ResearchRequest
from .test_contract import payload
from .test_runtime import runtime, reply


@pytest.mark.parametrize("reserve", [False, True])
async def test_retryable_failure_reexecutes_after_restart(tmp_path, monkeypatch, reserve):
    rt, sdk = runtime(tmp_path, [])
    sdk.complete = AsyncMock(side_effect=[RunError("provider_busy", retryable=True), reply()])
    request = ResearchRequest(**payload())
    clock = [1000.0]
    monkeypatch.setattr("src.agentischer_lauf.runtime.time.time", lambda: clock[0])
    first = await rt.execute(request, "a")
    assert first["retryable"] and first["fehler_code"] == "provider_busy"
    # Reading a finished job/diagnostic does not run the model again.
    ref = first["diagnose_ref"].rsplit("=", 1)[-1]
    assert json.loads(rt.storage.artifact("a", "run-1", ref))["result"] == first
    assert sdk.complete.await_count == 1
    rt = type(rt)(rt.adapters, rt.storage, revision=rt.revision, max_bytes=rt.max_bytes)
    clock[0] += 100
    if reserve:
        rt.reserve(request, "a")
        accepted = rt.storage.load(rt.storage.location("a", "run-1"))["accepted_at"]
        clock[0] += 1
        rt.reserve(request, "a")
        assert rt.storage.load(rt.storage.location("a", "run-1"))["accepted_at"] == accepted
    second = await rt.execute(request, "a")
    assert second["status"] == "success"
    assert second["herkunft"]["auftrag_sha256"] == first["herkunft"]["auftrag_sha256"]
    assert not second["herkunft"]["cache"]["hit"]
    assert sdk.complete.await_count == 2
    assert (await rt.execute(request, "a"))["herkunft"]["cache"]["hit"]
    assert sdk.complete.await_count == 2
    archived = rt.storage.load(rt.storage.location("a", "run-1") / "attempts" / "1")
    assert archived["result"] == first


@pytest.mark.parametrize("stage,retryable", [("finished", False), ("started", True)])
async def test_terminal_or_uncertain_failure_never_reexecutes(tmp_path, stage, retryable):
    rt, sdk = runtime(tmp_path, [])
    sdk.complete = AsyncMock(side_effect=RunError("provider_busy", retryable=retryable))
    request = ResearchRequest(**payload())
    await rt.execute(request, "a")
    directory = rt.storage.location("a", "run-1")
    state = rt.storage.load(directory)
    state["stage"] = stage
    rt.storage.save(directory, state)
    rt.reserve(request, "a")
    assert (await rt.execute(request, "a"))["status"] == "error"
    assert sdk.complete.await_count == 1


async def test_retry_keeps_digest_conflict_and_lock(tmp_path):
    rt, sdk = runtime(tmp_path, [])
    sdk.complete = AsyncMock(side_effect=RunError("provider_busy", retryable=True))
    request = ResearchRequest(**payload())
    await rt.execute(request, "a")
    changed = ResearchRequest(**payload(query="changed"))
    with pytest.raises(RunError, match="lauf_konflikt"):
        rt.reserve(changed, "a")
    with pytest.raises(RunError, match="lauf_konflikt"):
        await rt.execute(changed, "a")
    with rt.storage.lock("a", "run-1"):
        with pytest.raises(RunError, match="lauf_belegt"):
            await rt.execute(request, "a")
    assert sdk.complete.await_count == 1


@pytest.mark.parametrize("extra", [{}, {"research_instructions": "Caller policy", "privacy_allowlist": []}])
def test_legacy_model_dump_validate_roundtrip(extra):
    request = ResearchRequest(query="synthetic", **extra)
    assert ResearchRequest.model_validate(request.model_dump()) == request
    for name, value in extra.items():
        assert getattr(request, name) == value
    assert "Request model" in ResearchRequest.__doc__
    assert "Request model" in ResearchRequest.model_json_schema()["description"]


@pytest.mark.parametrize("field,values", [
    ("research_instructions", [None, "", "Caller policy"]),
    ("privacy_allowlist", [None, [], ["Synthetic"]]),
])
def test_cx3_options_forbidden_even_when_empty(field, values):
    for value in values:
        with pytest.raises(ValidationError, match="auftrag_ungueltig"):
            ResearchRequest(**payload(**{field: value}))


async def test_null_run_job_dispatches_legacy(monkeypatch):
    legacy = AsyncMock(return_value="legacy")
    monkeypatch.setattr(http, "authorized", AsyncMock(side_effect=AssertionError("agentic auth")))
    body = SimpleNamespace(kind="research", payload=ResearchRequest(query="q").model_dump())
    assert await http.job_dispatch(legacy)(body, None) == "legacy"
    legacy.assert_awaited_once()


def test_wire_digest_matches_normative_contract():
    document = (Path(__file__).parents[2] / "docs/agentischer-lauf.md").read_text()
    marker = "```json wire-digest-contract\n"
    assert marker in document, "CXA must define the wire digest, including its vector"
    contract = json.loads(document.split(marker, 1)[1].split("```", 1)[0])
    vector = contract["vector"]
    normalized = vector["normalized"]
    encoded = json.dumps(normalized, **contract["json_options"]).encode(contract["encoding"])
    expected = hashlib.sha256(encoded).hexdigest()
    assert expected == vector["sha256"]
    assert list(sorted(normalized)) == sorted(contract["fields"])
    assert "lauf_id" in normalized["agentischer_lauf"]
    assert request_digest(ResearchRequest(**vector["request"])) == expected
    explicit = copy.deepcopy(vector["request"])
    explicit["agentischer_lauf"] = copy.deepcopy(normalized["agentischer_lauf"])
    explicit["agentischer_lauf"].pop("pruefung")
    explicit["agentischer_lauf"]["ausgabe"].pop("datei")
    explicit["agentischer_lauf"]["ausgabe"].pop("json_schema")
    explicit["agentischer_lauf"]["material"][0]["quelle"] = {
        "art": "verweis", "url": "https://material.example/synthetic"}
    assert request_digest(ResearchRequest(**explicit)) == expected
    explicit["agentischer_lauf"]["lauf_id"] = "other-run"
    assert request_digest(ResearchRequest(**explicit)) != expected


async def test_repeated_retry_and_archive_failure_are_loud(tmp_path, monkeypatch):
    rt, sdk = runtime(tmp_path, [])
    sdk.complete = AsyncMock(side_effect=RunError("provider_busy", retryable=True))
    request = ResearchRequest(**payload())
    for _ in range(3):
        assert (await rt.execute(request, "a"))["retryable"]
    assert sdk.complete.await_count == 3
    directory = rt.storage.location("a", "run-1")
    assert rt.storage.load(directory)["attempt"] == 3
    assert rt.storage.load(directory / "attempts" / "1")["result"]["retryable"]
    assert rt.storage.load(directory / "attempts" / "2")["result"]["retryable"]
    def fail_archive(*args):
        raise OSError("synthetic disk failure")
    monkeypatch.setattr(rt.storage, "archive", fail_archive)
    with pytest.raises(OSError, match="synthetic disk failure"):
        await rt.execute(request, "a")
    assert sdk.complete.await_count == 3
    assert rt.storage.load(directory)["attempt"] == 3


def test_http_retry_smoke(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from .test_transport import app
    rt, sdk = runtime(tmp_path, [])
    sdk.complete = AsyncMock(side_effect=[RunError("provider_busy", retryable=True), reply()])
    monkeypatch.setattr(http, "authorized", AsyncMock())
    monkeypatch.setattr(http, "_runtime", rt)
    monkeypatch.setattr(http, "_admit", AsyncMock(return_value="a"))
    with TestClient(app()) as client:
        first = client.post("/v1/research", json=payload()).json()
        assert first["retryable"] and first["status"] == "error"
        assert client.get(first["diagnose_ref"]).json()["result"]["status"] == "error"
        assert sdk.complete.await_count == 1
        second = client.post("/v1/research", json=payload()).json()
        assert second["status"] == "success" and not second["herkunft"]["cache"]["hit"]
        assert sdk.complete.await_count == 2
