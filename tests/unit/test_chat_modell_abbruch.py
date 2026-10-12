"""BRREF (12.10.2026): the bridge reports how the model ended, never a fixed "stop".

Befund P6CUT: on dev/worker1, 12.10.2026 09:58Z (CLI 2.1.285, claude-sonnet-5-5),
Anthropic's safety classifier stopped an answer — stop_reason "refusal",
stop_details {type: refusal, category: reasoning_extraction}. The CLI then
appended a meta user turn ("Your response above was stopped by a safety
classifier …") and ran a second model call that answered with a 232-char
apology. The chat route returned the cut first message with finish_reason
"stop" (hard-wired in main.py) and HTTP 200; usage was the sum of both calls
(848,563 input instead of 421,559 per call).

The refusal run below is rebuilt from the STRUCTURE of that transcript
(local-storage/p6cut-20261012/bridge-worker1-cli-struktur.txt: message types,
ids, stop fields, usage, text lengths) — no customer text. Streams go through
the REAL run_completion (only `query` is replaced) and the real chat routes.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock as _MagicMock

for _mod_name in [
    "claude_code_sdk",
    "claude_code_sdk._errors",
    "claude_code_sdk._internal",
    "claude_code_sdk._internal.client",
]:
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = _MagicMock()

from unittest.mock import AsyncMock, MagicMock, patch  # noqa: E402

import pytest  # noqa: E402

import src.claude_cli as claude_cli  # noqa: E402
import src.main as main  # noqa: E402
import src.middleware.capacity_lock as capacity_lock_mod  # noqa: E402
from src.claude_cli import (  # noqa: E402
    ClaudeCodeCLI,
    antwort_ende,
    finish_reason_fuer_lauf,
    rate_limit_tracker,
    usage_je_zug,
)
from src.sdk_parser import resilient_parse_message  # noqa: E402
from tests import sdk_strom as sdk  # noqa: E402

WORKER = "worker-brref"

# Lengths and usage as measured (struktur.txt lines 13, 15, 16/17); the texts
# are placeholders of the same length.
GESPERRT_TEXT = "<teil nr=\"1\">\n" + "x" * (9258 - 14)
FOLGE_TEXT = "F" * 232
META_TEXT = (
    "Your response above was stopped by a safety classifier — this is not a "
    "tool or API error. The rest of it was withheld, and tool calls in it that "
    "had not finished did not run. Do not produce that content again, even reworded."
)
STOP_DETAILS = {
    "type": "refusal",
    "category": "reasoning_extraction",
    "explanation": "This request was blocked as it seems to violate …",
}
USAGE_ZUG_1 = {"input_tokens": 2, "cache_creation_input_tokens": 421557,
               "cache_read_input_tokens": 0, "output_tokens": 5374}
USAGE_ZUG_2 = {"input_tokens": 4, "cache_creation_input_tokens": 5660,
               "cache_read_input_tokens": 421340, "output_tokens": 251}
USAGE_SUMME = {"input_tokens": 6, "cache_creation_input_tokens": 427217,
               "cache_read_input_tokens": 421340, "output_tokens": 5625}


@dataclass
class ThinkingBlock:
    thinking: str
    signature: str = ""


@dataclass
class UserMessage:
    content: list


def _roh_assistent(msg_id, blocks, stop_reason, usage, stop_details=None):
    """The stream-json assistant line as the CLI writes it."""
    message = {"id": msg_id, "model": "claude-sonnet-5-5", "content": blocks,
               "stop_reason": stop_reason, "usage": usage}
    if stop_details is not None:
        message["stop_details"] = stop_details
    return {"type": "assistant", "message": message, "parent_tool_use_id": None,
            "session_id": "7b000a8d"}


def _sdk_parse(data):
    """claude-code-sdk 0.0.22: AssistantMessage(content, model), no stop fields."""
    if data.get("type") != "assistant":
        raise AssertionError("only assistant lines here")
    blocks = []
    for b in data["message"]["content"]:
        if b["type"] == "text":
            blocks.append(sdk.TextBlock(text=b["text"]))
        else:
            blocks.append(ThinkingBlock(thinking=b.get("thinking", "")))
    return sdk.AssistantMessage(content=blocks, model=data["message"]["model"])


def _parsed(*args, **kwargs):
    return resilient_parse_message(_roh_assistent(*args, **kwargs), _sdk_parse)


def refusal_verlauf(mit_stopfeldern: bool = True):
    """struktur.txt lines 13–17 plus result, as run_completion receives them."""
    if mit_stopfeldern:
        gesperrt = _parsed("msg_011Cfx9N8", [{"type": "text", "text": GESPERRT_TEXT}],
                           "refusal", USAGE_ZUG_1, STOP_DETAILS)
    else:
        gesperrt = sdk.AssistantMessage(content=[sdk.TextBlock(text=GESPERRT_TEXT)],
                                        model="claude-sonnet-5-5")
    result = sdk.result("success", usage=USAGE_SUMME, num_turns=2)
    return [
        sdk.init(),
        gesperrt,
        UserMessage(content=[sdk.TextBlock(text=META_TEXT)]),
        _parsed("msg_011Cfx9Ry", [{"type": "thinking", "thinking": ""}], "end_turn", USAGE_ZUG_2),
        _parsed("msg_011Cfx9Ry", [{"type": "text", "text": FOLGE_TEXT}], "end_turn", USAGE_ZUG_2),
        result,
    ]


def normal_verlauf(stop_reason="end_turn"):
    usage = {"input_tokens": 10, "cache_creation_input_tokens": 0,
             "cache_read_input_tokens": 0, "output_tokens": 20}
    return [
        sdk.init(),
        _parsed("msg_1", [{"type": "text", "text": "Fertige Antwort."}], stop_reason, usage),
        sdk.result("success", usage=usage, num_turns=1),
    ]


def _make_cli(tmp_path: Path) -> ClaudeCodeCLI:
    cli = object.__new__(ClaudeCodeCLI)
    cli.timeout = 30
    cli.cwd = tmp_path
    cli.claude_env_vars = {}
    cli.cache_dir = tmp_path
    cli.max_cache_size_mb = 10
    cli.file_discovery = MagicMock()
    return cli


def _query(messages):
    async def query(prompt, options):
        for m in messages:
            yield m
    return query


@pytest.fixture(autouse=True)
def frischer_zustand(tmp_path, monkeypatch):
    monkeypatch.setenv("INSTANCE_NAME", WORKER)
    monkeypatch.setattr(claude_cli, "MessageParseError", type("MessageParseError", (Exception,), {}))
    monkeypatch.setattr(
        capacity_lock_mod.CapacityLock, "PERSIST_PATH", str(tmp_path / "cap_lock.json")
    )
    monkeypatch.setattr(capacity_lock_mod, "_INSTANCE", None)
    rate_limit_tracker._rate_limits.pop(WORKER, None)
    rate_limit_tracker._hard_limits.discard(WORKER)
    yield
    monkeypatch.setattr(capacity_lock_mod, "_INSTANCE", None)


def _echter_cli(monkeypatch, tmp_path, messages):
    cli = _make_cli(tmp_path)
    monkeypatch.setattr(claude_cli, "query", _query(messages))
    monkeypatch.setattr(main.claude_cli, "run_completion", cli.run_completion)


async def _chunks(monkeypatch, tmp_path, messages):
    monkeypatch.setattr(claude_cli, "query", _query(messages))
    return [c async for c in _make_cli(tmp_path).run_completion(prompt="p", model="m")]


# ── run_completion + evaluation ──────────────────────────────────────────────

async def test_lauf_mit_refusal_ergibt_content_filter(monkeypatch, tmp_path):
    chunks = await _chunks(monkeypatch, tmp_path, refusal_verlauf())
    finish, details = finish_reason_fuer_lauf(chunks)
    assert finish == "content_filter"
    assert details == {"stop_reason": "refusal", "category": "reasoning_extraction",
                       "folgezuege": 1}
    assert antwort_ende(chunks) == "refusal"


async def test_refusal_ohne_stopfelder_wird_am_folgezug_erkannt(monkeypatch, tmp_path):
    """Second net: stop fields lost, the CLI's meta turn still marks the follow-up."""
    chunks = await _chunks(monkeypatch, tmp_path, refusal_verlauf(mit_stopfeldern=False))
    finish, details = finish_reason_fuer_lauf(chunks)
    assert finish == "content_filter"
    assert details["folgezuege"] == 1


async def test_usage_je_zug_statt_nur_summe(monkeypatch, tmp_path):
    chunks = await _chunks(monkeypatch, tmp_path, refusal_verlauf())
    zuege = usage_je_zug(chunks)
    assert [z["message_id"] for z in zuege] == ["msg_011Cfx9N8", "msg_011Cfx9Ry"]
    assert [z["stop_reason"] for z in zuege] == ["refusal", "end_turn"]
    eingang = [z["input_tokens"] + z["cache_creation_tokens"] + z["cache_read_tokens"] for z in zuege]
    assert eingang == [421559, 427004]
    assert sum(eingang) == 848563  # the number Energy saw as one prompt


@pytest.mark.parametrize("stop_reason,finish", [
    ("end_turn", "stop"), ("stop_sequence", "stop"), ("max_tokens", "length"),
    ("model_context_window_exceeded", "length"), ("pause_turn", "pause_turn"),
])
async def test_normaler_lauf_bildet_stop_reason_ab(monkeypatch, tmp_path, stop_reason, finish):
    chunks = await _chunks(monkeypatch, tmp_path, normal_verlauf(stop_reason))
    assert finish_reason_fuer_lauf(chunks) == (finish, {"stop_reason": stop_reason})
    assert usage_je_zug(chunks)[0]["output_tokens"] == 20


def test_ohne_gemeldeten_stop_reason_bleibt_stop():
    """The CLI reported none (older stream shape): not invented as an error."""
    chunks = [{"type": "assistant", "content": [{"type": "text", "text": "a"}]},
              {"type": "result", "subtype": "success", "is_error": False, "num_turns": 1}]
    assert finish_reason_fuer_lauf(chunks) == ("stop", {"stop_reason": None})


def test_api_worker_mapper_kennt_refusal():
    """API worker (providers/anthropic_direct) and vision use finish_reason_for."""
    from src.vision_provider import finish_reason_for

    assert finish_reason_for("refusal") == "content_filter"
    assert finish_reason_for("max_tokens") == "length"
    assert finish_reason_for("end_turn") == "stop"


# ── chat routes ──────────────────────────────────────────────────────────────

def _sync_chat(monkeypatch, tmp_path, messages):
    from starlette.testclient import TestClient

    from src.middleware.adaptive_limiter import adaptive_limit_dependency

    monkeypatch.setenv("API_KEY", "")
    monkeypatch.setenv("CLAUDE_SKIP_AUTH", "1")
    _echter_cli(monkeypatch, tmp_path, messages)

    async def _kein_limiter():
        return None

    main.app.dependency_overrides[adaptive_limit_dependency] = _kein_limiter
    try:
        with (
            patch("src.main.validate_claude_code_auth", return_value=(True, {"method": "test"})),
            patch("src.main.verify_api_key", new_callable=AsyncMock),
            patch("src.main.enforce_pool_admission", new_callable=AsyncMock),
            patch("src.main._cross_worker_retry", new=AsyncMock(return_value=None)),
            patch("src.providers.fallback.get_fallback_tiers", side_effect=lambda t, **kw: [t]),
        ):
            return TestClient(main.app, raise_server_exceptions=False).post(
                "/v1/chat/completions",
                json={"model": "claude-sonnet-4-5", "messages": [{"role": "user", "content": "ping"}]},
            )
    finally:
        main.app.dependency_overrides.clear()


def test_sync_chat_refusal_ist_content_filter(monkeypatch, tmp_path):
    resp = _sync_chat(monkeypatch, tmp_path, refusal_verlauf())
    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    choice = body["choices"][0]
    assert choice["finish_reason"] == "content_filter"
    assert body["x_bridge_finish"]["category"] == "reasoning_extraction"
    # The answer ends with the refused message; the CLI's follow-up is not in it.
    assert choice["message"]["content"].startswith("<teil")
    assert FOLGE_TEXT not in choice["message"]["content"]
    # usage: the sum, flagged as such, and per call.
    assert body["usage"]["prompt_tokens"] == 848563
    assert body["x_bridge_model_calls"] == 2
    assert [z["output_tokens"] for z in body["x_bridge_usage_zuege"]] == [5374, 251]


def test_sync_chat_refusal_ohne_text_ist_kein_503(monkeypatch, tmp_path):
    """Classifier stop before any text: content_filter with empty content — not
    'SDK terminated early, retry' (the same request meets the same classifier)."""
    verlauf = refusal_verlauf()
    verlauf[1] = _parsed("msg_011Cfx9N8", [{"type": "thinking", "thinking": ""}],
                         "refusal", USAGE_ZUG_1, STOP_DETAILS)
    resp = _sync_chat(monkeypatch, tmp_path, verlauf)
    assert resp.status_code == 200, resp.text[:300]
    choice = resp.json()["choices"][0]
    assert choice["finish_reason"] == "content_filter"
    assert choice["message"]["content"] == ""


@pytest.mark.parametrize("stop_reason,finish", [("end_turn", "stop"), ("max_tokens", "length")])
def test_sync_chat_normal_und_length(monkeypatch, tmp_path, stop_reason, finish):
    resp = _sync_chat(monkeypatch, tmp_path, normal_verlauf(stop_reason))
    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert body["choices"][0]["finish_reason"] == finish
    assert body["choices"][0]["message"]["content"] == "Fertige Antwort."
    assert body["x_bridge_model_calls"] == 1


async def _stream(monkeypatch, tmp_path, messages):
    from src.models import ChatCompletionRequest, Message

    _echter_cli(monkeypatch, tmp_path, messages)
    req = ChatCompletionRequest(model="claude-sonnet-5",
                                messages=[Message(role="user", content="hi")], stream=True)
    out = [c async for c in main.generate_streaming_response(req, "req-brref")]
    daten = [json.loads(c[6:]) for c in out if c.startswith("data: {")]
    return out, daten


async def test_streaming_refusal_ist_content_filter_ohne_folgezug(monkeypatch, tmp_path):
    out, daten = await _stream(monkeypatch, tmp_path, refusal_verlauf())
    text = "".join(d["choices"][0]["delta"].get("content", "") for d in daten)
    assert text.startswith("<teil")
    assert FOLGE_TEXT not in text
    final = daten[-1]
    assert final["choices"][0]["finish_reason"] == "content_filter"
    assert final["x_bridge_finish"]["category"] == "reasoning_extraction"
    assert "data: [DONE]\n\n" in out


@pytest.mark.parametrize("stop_reason,finish", [("end_turn", "stop"), ("max_tokens", "length")])
async def test_streaming_normal_und_length(monkeypatch, tmp_path, stop_reason, finish):
    _, daten = await _stream(monkeypatch, tmp_path, normal_verlauf(stop_reason))
    assert daten[-1]["choices"][0]["finish_reason"] == finish
