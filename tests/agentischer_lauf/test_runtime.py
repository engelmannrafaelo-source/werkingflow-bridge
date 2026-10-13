import asyncio
import copy
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from src.models import ResearchRequest
from src.agentischer_lauf.adapter import Adapters, ModelReply, RunError
from src.agentischer_lauf.runtime import Runtime, request_digest
from src.agentischer_lauf.storage import Storage
from .test_contract import payload, material


def reply(text="alpha", end="end_turn", calls=None, complete=True):
    calls = calls or []
    blocks = [{"type": "tool_use", **c} for c in calls]
    chunks = [{"type": "assistant", "content": blocks, "stop_reason": end}]
    return ModelReply(
        chunks=chunks,
        text=text,
        calls=calls,
        complete_log=complete,
        usage={
            "input_tokens": 3,
            "output_tokens": 2,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cost_eur": None,
            "cost_source": "unknown",
        },
    )


class SDK:
    hard_token_bound = True
    tools_are_requests = True

    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    async def input_bound(self, model, messages, tools):
        return 3

    async def complete(self, **kwargs):
        self.calls.append(kwargs)
        return next(self.replies)


def runtime(tmp_path, replies):
    sdk = SDK(replies)
    adapters = Adapters()
    adapters.register("synthetic", sdk)
    rt = Runtime(
        adapters, Storage(tmp_path / "protected"), revision="test-revision", max_bytes=100000
    )
    return rt, sdk


@pytest.mark.parametrize(
    "instruction", ["Sortiere Titel eines Filmarchivs.", "Vergleiche zwei Schacheroeffnungen."]
)
async def test_foreign_domain_unchanged_instruction(tmp_path, instruction):
    rt, sdk = runtime(tmp_path, [reply()])
    body = ResearchRequest(**payload(query=instruction))
    result = await rt.execute(body, "tenant-a")
    assert result["status"] == "success"
    assert sdk.calls[0]["messages"][:2] == [
        {"role": "system", "content": "App-eigener Wortlaut."},
        {"role": "user", "content": instruction},
    ]
    assert result["herkunft"]["antwort"]["bytes"] == 5
    assert result["usage"]["cost_eur"] is None
    assert result["werkzeugaufrufe"]["vollstaendig"]


@pytest.mark.parametrize(
    "end,code",
    [
        ("max_tokens", "antwort_abgeschnitten"),
        ("max_tokens_fortgesetzt", "antwort_abgeschnitten"),
        (None, "ende_unbekannt"),
        ("tool_use", "lauf_unvollstaendig"),
        ("error_max_turns", "lauf_unvollstaendig"),
    ],
)
async def test_final_ends(tmp_path, end, code):
    rt, _ = runtime(tmp_path, [reply(end=end)])
    out = await rt.execute(ResearchRequest(**payload()), "a")
    assert out["fehler_code"] == code
    assert out["content"] is None and out["retryable"] is False
    assert out["antwort_ende"] == end


@pytest.mark.parametrize(
    "text,status", [(' {"ok":false} ', "success"), ('{"ok":"bad"}', "error"), ("broken", "error")]
)
async def test_json_schema_no_repair(tmp_path, text, status):
    body = payload()
    body["agentischer_lauf"]["ausgabe"] = {
        "modus": "json",
        "artefakte": [],
        "json_schema": {
            "type": "object",
            "properties": {"ok": {"type": "boolean"}},
            "required": ["ok"],
            "additionalProperties": False,
        },
    }
    rt, _ = runtime(tmp_path, [reply(text)])
    result = await rt.execute(ResearchRequest(**body), "a")
    assert result["status"] == status
    assert result["content"] == (text if status == "success" else None)


async def test_file_package_download_and_independent_negative_review(tmp_path):
    body = payload(antwort_im_text=False)
    run = body["agentischer_lauf"]
    run["material"] = [material("freely/chosen.txt")]
    run["werkzeuge"] = [{"name": "Write", "parameter": {}}]
    run["ausgabe"] = {"modus": "datei", "datei": "result.txt", "artefakte": ["data"]}
    run["pruefung"] = {
        "auftrag": "Pruefe diesen Vergleich.",
        "systemtext": "Eigener Pruefer.",
        "model": "synthetic",
        "werkzeuge": [{"name": "Read", "parameter": {}}],
        "ausgabe": {"modus": "text", "artefakte": []},
        "max_turns": 2,
        "max_tokens": 40,
    }
    calls = [
        {"id": "c1", "name": "Write", "input": {"path": "result.txt", "content": "complete"}},
        {"id": "c2", "name": "Write", "input": {"path": "data/all.csv", "content": "a,b\n1,2"}},
    ]
    rt, sdk = runtime(
        tmp_path, [reply("private draft", "tool_use", calls), reply(), reply("Fachurteil: negativ")]
    )
    result = await rt.execute(ResearchRequest(**body), "a")
    assert result["status"] == "success" and result["content"] == "complete"
    assert result["pruefung"]["content"] == "Fachurteil: negativ"
    assert result["usage"]["input_tokens"] == 9
    assert result["pruefung"]["usage"]["input_tokens"] == 3
    review_messages = json.dumps(sdk.calls[-1]["messages"])
    assert "private draft" not in review_messages
    assert "complete" in review_messages and "data/all.csv" in review_messages
    assert len(result["artefakte"]) == 2
    artifact = next(a for a in result["artefakte"] if a["pfad"] == "data/all.csv")
    ref = artifact["ref"].rsplit("=", 1)[-1]
    assert rt.storage.artifact("a", "run-1", ref) == b"a,b\n1,2"
    with pytest.raises(RunError):
        rt.storage.artifact("other", "run-1", ref)
    assert result["werkzeugaufrufe"]["eintraege"][0]["status"] == "ok"


@pytest.mark.parametrize(
    "mode",
    [
        "missing_log",
        "hidden_call",
        "denied_tool",
        "readonly",
        "hash",
        "missing_file",
        "turns",
        "tokens",
        "too_many_tokens",
    ],
)
async def test_fail_closed(tmp_path, mode):
    body = payload()
    run = body["agentischer_lauf"]
    response = reply()
    expected = "protokoll_unvollstaendig"
    if mode == "missing_log":
        response.complete_log = False
    elif mode == "hidden_call":
        response.chunks[0]["content"] = [
            {"type": "tool_use", "name": "Bash", "id": "x", "input": {}}
        ]
    elif mode in ("denied_tool", "readonly", "turns"):
        run["werkzeuge"] = [{"name": "Write", "parameter": {}}]
        run["material"] = [material("locked.txt")]
        run["ausgabe"]["artefakte"] = ["locked.txt"]
        response = reply(
            end="tool_use",
            calls=[
                {
                    "id": "x",
                    "name": "Write" if mode == "readonly" else "Bash",
                    "input": {"path": "locked.txt", "content": "changed"},
                }
            ],
        )
        expected = "werkzeug_fehler"
        if mode == "turns":
            body["max_turns"] = 1
            run["werkzeuge"] = [{"name": "List", "parameter": {}}]
            response = reply(end="tool_use", calls=[{"id": "x", "name": "List", "input": {}}])
            expected = "lauf_unvollstaendig"
    elif mode == "hash":
        run["material"] = [material()]
        run["material"][0]["sha256"] = "0" * 64
        expected = "material_integritaet"
    elif mode == "missing_file":
        run["ausgabe"]["artefakte"] = ["absent"]
        expected = "antwort_fehlt"
    elif mode == "tokens":
        run["grenzen"]["tokens_gesamt"] = 3
        expected = "grenze_tokens"
    elif mode == "too_many_tokens":
        response.usage["output_tokens"] = 500
        expected = "grenze_tokens"
    rt, sdk = runtime(tmp_path, [response])
    out = await rt.execute(ResearchRequest(**body), "a")
    assert out["status"] == "error" and out["fehler_code"] == expected
    assert out["content"] is None
    if mode in ("hash", "tokens"):
        assert sdk.calls == []


async def test_cap_before_call_and_idempotency_conflict_tenant(tmp_path):
    body = payload()
    body["agentischer_lauf"]["grenzen"]["tokens_gesamt"] = 8
    rt, sdk = runtime(tmp_path, [reply(), reply()])
    req = ResearchRequest(**body)
    first = await rt.execute(req, "a")
    assert sdk.calls[0]["max_tokens"] == 5
    second = await rt.execute(req, "a")
    assert len(sdk.calls) == 1 and second["herkunft"]["cache"]["hit"]
    assert second["usage"]["cost_eur"] == 0
    await rt.execute(req, "b")
    assert len(sdk.calls) == 2
    changed = payload(query="Different")
    with pytest.raises(RunError, match="lauf_konflikt"):
        await rt.execute(ResearchRequest(**changed), "a")


async def test_incomplete_checkpoint_never_replays(tmp_path):
    rt, sdk = runtime(tmp_path, [reply()])
    req = ResearchRequest(**payload())
    await rt.execute(req, "a")
    directory = rt.storage.location("a", "run-1")
    state = rt.storage.load(directory)
    state["stage"] = "started"
    rt.storage.save(directory, state)
    out = await rt.execute(req, "a")
    assert out["fehler_code"] == "lauf_unvollstaendig" and len(sdk.calls) == 1


async def test_failure_not_success_cache_and_review_failure(tmp_path):
    body = payload()
    body["agentischer_lauf"]["pruefung"] = {
        "auftrag": "Review",
        "model": "synthetic",
        "werkzeuge": [],
        "ausgabe": {"modus": "text", "artefakte": []},
        "max_turns": 1,
        "max_tokens": 40,
    }
    rt, sdk = runtime(tmp_path, [reply(), reply(end="max_tokens")])
    req = ResearchRequest(**body)
    out = await rt.execute(req, "a")
    assert out["status"] == "error" and out["content"] is None
    assert out["pruefung"]["antwort_ende"] == "max_tokens"
    cached = await rt.execute(req, "a")
    assert not cached["herkunft"]["cache"]["hit"] and len(sdk.calls) == 2


async def test_preflight_does_not_launch_unsupported_sdk(tmp_path):
    rt, sdk = runtime(tmp_path, [])
    with pytest.raises(RunError, match="faehigkeit_nicht_verfuegbar"):
        await rt.execute(ResearchRequest(**payload(model="legacy-cli")), "a")
    assert sdk.calls == []


async def test_deadline(tmp_path):
    rt, sdk = runtime(tmp_path, [])

    async def wait(**kw):
        await asyncio.sleep(10)

    sdk.complete = wait
    body = payload()
    body["agentischer_lauf"]["grenzen"]["zeit_s"] = 1
    out = await rt.execute(ResearchRequest(**body), "a")
    assert out["fehler_code"] == "grenze_zeit"


async def test_durable_acceptance_deadline_survives_deferral(tmp_path):
    rt, sdk = runtime(tmp_path, [reply()])
    body = ResearchRequest(**payload())
    rt.reserve(body, "a")
    directory = rt.storage.location("a", "run-1")
    state = rt.storage.load(directory)
    state["accepted_at"] -= 100
    rt.storage.save(directory, state)
    result = await rt.execute(body, "a")
    assert result["fehler_code"] == "grenze_zeit"
    assert sdk.calls == []


async def test_unknown_usage_and_unsupported_tool_fail_before_success(tmp_path):
    response = reply()
    response.usage["input_tokens"] = None
    rt, _ = runtime(tmp_path, [response])
    result = await rt.execute(ResearchRequest(**payload()), "a")
    assert result["fehler_code"] == "grenze_tokens"
    body = payload()
    body["agentischer_lauf"]["werkzeuge"] = [{"name": "Bash", "parameter": {}}]
    with pytest.raises(RunError, match="faehigkeit_nicht_verfuegbar"):
        await rt.execute(ResearchRequest(**body), "b")


async def test_material_url_excluded_but_content_changes_digest(tmp_path):
    a = payload()
    item = material()
    item["quelle"] = {"art": "verweis", "url": "https://material.example/file?token=one"}
    a["agentischer_lauf"]["material"] = [item]
    b = copy.deepcopy(a)
    b["agentischer_lauf"]["material"][0]["quelle"][
        "url"
    ] = "https://material.example/file?token=two"
    assert request_digest(ResearchRequest(**a)) == request_digest(ResearchRequest(**b))
    b["agentischer_lauf"]["material"][0]["sha256"] = "1" * 64
    assert request_digest(ResearchRequest(**a)) != request_digest(ResearchRequest(**b))
