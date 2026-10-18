"""R9: actual SDK forms, actual hooks and child/parent failure contract, offline."""

import asyncio
import json

import pytest

from src.routing import bild_policy as bp


@pytest.fixture(autouse=True)
def energy():
    token = bp.CURRENT_APP.set("werking-energy")
    yield
    bp.CURRENT_APP.reset(token)


@pytest.mark.parametrize(
    "block",
    [
        {"type": "document", "source": {"type": "file", "file_id": "file_offline"}},
        {
            "type": "document",
            "source": {"type": "base64", "media_type": "text/plain", "data": "abc"},
        },
        {
            "type": "document",
            "source": {"type": "url", "url": "https://offline.invalid/plain.txt"},
        },
        {"type": "input_file", "file_id": "file_offline"},
        {"type": "future_sdk_block", "text": "seems harmless"},
        {"file_id": "file_offline"},
        {"type": "text", "text": "text", "file_id": "file_offline"},
    ],
)
def test_unproved_content_never_passes(block):
    with pytest.raises(bp.ImageEgressRefused) as error:
        bp.guard_provider_payload(
            "anthropic", {"messages": [{"role": "user", "content": [block]}]}
        )
    assert error.value.status == 403


def test_native_sdk_file_source():
    from anthropic.types.document_block_param import DocumentBlockParam
    from anthropic.types.file_document_source_param import FileDocumentSourceParam

    source = FileDocumentSourceParam(type="file", file_id="file_offline")
    block = DocumentBlockParam(type="document", source=source)
    with pytest.raises(bp.ImageEgressRefused):
        bp.guard_provider_payload("anthropic", block)


@pytest.mark.parametrize(
    "content",
    [
        "text",
        [{"type": "text", "text": "text"}],
        [{"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": "pwd"}}],
        [
            {
                "type": "tool_result",
                "tool_use_id": "t",
                "content": [{"type": "text", "text": "result"}],
            }
        ],
        [
            {
                "type": "document",
                "source": {"type": "text", "data": "plain", "media_type": "text/plain"},
            }
        ],
    ],
)
def test_proved_text_passes(content):
    bp.guard_provider_payload(
        "anthropic", {"messages": [{"role": "user", "content": content}]}
    )


@pytest.mark.parametrize(
    "data",
    [b"%PDF-1.7\nASCII PDF", b"\x89PNG\r\n\x1a\nX", b"\xff\xd8\xff", b"text\x00binary"],
)
def test_read_checks_bytes_not_suffix(tmp_path, data):
    path = tmp_path / "innocent.txt"
    path.write_bytes(data)
    with pytest.raises(bp.ImageEgressRefused, match="Read abgewiesen"):
        bp.check_read_text({"file_path": str(path)}, str(tmp_path))


def test_guarded_sdk_round_and_exact_parent_reason(monkeypatch, tmp_path):
    import claude_code_sdk as sdk

    from src.erkunder import kind
    from src.erkunder.platz import child_failure_reason

    monkeypatch.setattr("src.sdk_parser.install_resilient_parser", lambda: None)
    text = tmp_path / "values.csv"
    text.write_text("time,value\n0,13\n1,17\n")
    body = {
        "ordner": str(tmp_path),
        "max_turns": 2,
        "claude_token": "offline",
        "prompt": "Rechne",
    }
    result = sdk.ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id="offline",
        total_cost_usd=0,
        usage={},
    )
    state = {"binary": False, "sent": 0}

    async def query(*, prompt, options):
        pre = options.hooks["PreToolUse"][-1].hooks[0]
        post = options.hooks["PostToolUse"][-1].hooks[0]
        event = {"tool_name": "Read", "tool_input": {"file_path": str(text)}}
        decision = await pre(event, None, None)
        if decision:
            assert decision["continue"] is False
            assert decision["hookSpecificOutput"]["permissionDecision"] == "deny"
            yield result
            return
        state["sent"] += 1
        assert (
            await post(
                {
                    "tool_name": "Bash",
                    "tool_response": {
                        "stdout": "30",
                        "stderr": "",
                        "interrupted": False,
                    },
                },
                None,
                None,
            )
            == {}
        )
        yield result

    monkeypatch.setattr(sdk, "query", query)
    assert asyncio.run(kind.run(body))["zuege"] == 1
    assert state["sent"] == 1
    text.write_bytes(b"%PDF-1.7")
    with pytest.raises(bp.ImageEgressRefused) as error:
        asyncio.run(kind.run(body))
    assert state["sent"] == 1
    reason = kind.failure_reason(error.value)
    assert child_failure_reason(json.dumps({"fehler": reason}).encode()) == reason
    assert "Read abgewiesen" in reason


def test_tool_result_unknown_and_image_block_stop():
    for response in (
        [{"type": "image"}],
        {"type": "future"},
        {"stdout": "", "isImage": True},
    ):
        with pytest.raises(bp.ImageEgressRefused):
            bp.check_tool_response("Bash", response)


def test_policy_reason_is_accepted_by_status_schema():
    from src.erkunder.models import Schritt
    reason = "image_tool_access_refused: Read abgewiesen: PDF-Signatur"
    step = Schritt(
        name="erkunder-1", versuch=1, status="abbruch", abbruch_grund=reason,
        dauer_s=0, zuege=0,
        tokens={"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0},
        ram_spitze_mb=0, worker="offline",
    )
    assert step.model_dump()["abbruch_grund"] == reason


def test_tool_result_may_not_smuggle_another_tool_call():
    with pytest.raises(bp.ImageEgressRefused):
        bp.guard_provider_payload("anthropic", {
            "type": "tool_result", "tool_use_id": "t",
            "content": [{"type": "tool_use", "id": "x", "name": "Read", "input": {}}],
        })


def test_actual_response_hook_stops_unknown_payload(tmp_path):
    from src.erkunder.kind import sdk_options
    options = sdk_options({
        "ordner": str(tmp_path), "max_turns": 1, "claude_token": "offline",
    })
    after = options.hooks["PostToolUse"][-1].hooks[0]
    decision = asyncio.run(after({
        "tool_name": "Read", "tool_response": {
            "type": "document", "source": {"type": "file", "file_id": "file_offline"},
        },
    }, None, None))
    assert decision["continue"] is False
    assert decision["decision"] == "block"
    assert options._image_policy_failures[0].code == "image_tool_access_refused"
