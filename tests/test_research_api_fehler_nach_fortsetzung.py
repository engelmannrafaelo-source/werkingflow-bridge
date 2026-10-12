"""API error after a CLI continuation (CACHEFIX2, 11.10.2026).

The CLI continues a cut answer by itself (meta user turn, BRTRUNC; and after
a stream break, BRTRUNC2). When the continuation fails, the run ends in the
CLI's API-error turn — measured with CLI 2.1.285 (BRTRUNC2 fixture
abbruch_immer): "API Error: Connection lost mid-response", ``error:
server_error``, ``is_api_error_message: true``, then a result with is_error and
terminal_reason "api_error", without api_error_status.

CACHEFIX turned that turn into CliApiError inside run_completion — before
research could see that the answer had been continued. Guarded here, through
the real run_completion and the real research handler, with the CLI's real
error flags:
  * antwort_im_text + continued answer + API error → the truncation verdict
    (fehler_code antwort_abgeschnitten, not retryable, antwort_ende), the API
    error named as the cause;
  * file contract → the API error, with antwort_ende of what had streamed;
  * no continuation → the API error, antwort_ende of what had streamed;
  * with BRTRUNC2 in the tree: the real abbruch_immer stream → abbruch_fortgesetzt
    with teilantwort (skipped while BRTRUNC2 is not merged).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

import src.claude_cli as claude_cli
import src.main as main_module
from tests import sdk_strom as sdk
from tests.test_research_antwort_ende import _META_TURN, _req

_ANFANG = 'Hier:\n```pruefwissen-auswahl\n{"dokument_ids": ["kw-1", "kw-2", '
_REST = '"kw-3", '
_CLI_FEHLER = (
    "API Error: Connection lost mid-response. The response above may be incomplete."
)
STROEME = Path(__file__).parent / "fixtures" / "cli_2_1_285_stroeme"
BRTRUNC2 = (
    hasattr(claude_cli, "ABBRUCH_FORTSETZUNG_KEY")
    and (STROEME / "abbruch_immer.jsonl").is_file()
)


@dataclass
class UserMessage:
    content: Any


def _api_fehler_zug():
    """The CLI's API-error turn as the patched SDK parser hands it on."""
    msg = sdk.AssistantMessage(
        content=[sdk.TextBlock(text=_CLI_FEHLER)], model="<synthetic>"
    )
    msg._bridge_cli_error = "server_error"
    msg._bridge_is_api_error_message = True
    msg._bridge_stop_reason = "stop_sequence"
    return msg


def _api_fehler_result():
    r = sdk.result("success", is_error=True, num_turns=4)
    r.result = _CLI_FEHLER
    r._bridge_terminal_reason = "api_error"
    r._bridge_stop_reason = "stop_sequence"
    return r


def _text(t):
    return sdk.AssistantMessage(content=[sdk.TextBlock(text=t)], model="m")


async def _research(monkeypatch, tmp_path, messages, req):
    monkeypatch.setenv("INSTANCE_NAME", "worker-cachefix2")
    monkeypatch.setattr(
        claude_cli, "MessageParseError", type("MessageParseError", (Exception,), {})
    )

    async def query(prompt, options):
        for m in messages:
            yield m

    monkeypatch.setattr(claude_cli, "query", query)
    cli = object.__new__(claude_cli.ClaudeCodeCLI)
    cli.timeout = 30
    cli.cwd = tmp_path
    cli.claude_env_vars = {}
    cli.cache_dir = tmp_path
    cli.max_cache_size_mb = 10
    cli.file_discovery = MagicMock()
    cli.file_discovery.discover_files_from_sdk_messages.return_value = []
    cli.file_discovery.discover_files_from_directory_scan.return_value = []
    monkeypatch.setattr(main_module.claude_cli, "run_completion", cli.run_completion)
    monkeypatch.setattr(
        "src.activity.ai_call_writer.persist_ai_call_activity", AsyncMock()
    )
    monkeypatch.setattr("src.claude_cli.detect_quota_exhaustion", lambda *a, **k: False)
    return await main_module._execute_research_impl(req, None, attribution_ctx=None)


def _fortgesetzt_dann_api_fehler():
    return [
        sdk.init(),
        _text(_ANFANG),
        UserMessage(content=[sdk.TextBlock(text=_META_TURN)]),
        _text(_REST),
        _api_fehler_zug(),
        _api_fehler_result(),
    ]


async def test_textvertrag_fortgesetzt_dann_api_fehler_ist_abgeschnitten(
    monkeypatch, tmp_path
):
    res = await _research(monkeypatch, tmp_path, _fortgesetzt_dann_api_fehler(), _req())
    assert res.status == "error"
    assert res.fehler_code == "antwort_abgeschnitten"
    assert res.retryable is False
    assert res.antwort_ende == "max_tokens_fortgesetzt"
    assert res.content is None
    assert "Connection lost mid-response" in res.error  # the cause stays


async def test_dateivertrag_fortgesetzt_dann_api_fehler_ist_der_api_fehler(
    monkeypatch, tmp_path
):
    res = await _research(
        monkeypatch,
        tmp_path,
        _fortgesetzt_dann_api_fehler(),
        _req(antwort_im_text=False),
    )
    assert res.status == "error"
    assert res.fehler_code is None
    assert res.antwort_ende == "max_tokens_fortgesetzt"
    assert res.retryable is True  # no status from the CLI: transient
    assert res.retry_after_s == 30
    assert "Connection lost mid-response" in res.error


async def test_ohne_fortsetzung_bleibt_es_der_api_fehler(monkeypatch, tmp_path):
    msgs = [sdk.init(), _text(_ANFANG), _api_fehler_zug(), _api_fehler_result()]
    res = await _research(monkeypatch, tmp_path, msgs, _req())
    assert res.status == "error"
    assert res.fehler_code is None
    assert res.retryable is True
    assert "Connection lost mid-response" in res.error


# --- with BRTRUNC2 merged: the real exhausted stream ---------------------------


def _roh_zu_sdk(d: dict):
    """A real stream-json line (BRTRUNC2 fixture) → what the patched parser hands on."""
    if d["type"] == "user":
        return UserMessage(
            content=[sdk.TextBlock(text=b["text"]) for b in d["message"]["content"]]
        )
    if d["type"] == "assistant":
        m = d["message"]
        msg = sdk.AssistantMessage(
            content=[sdk.TextBlock(text=b["text"]) for b in m["content"]],
            model=m["model"],
        )
        if d.get("error"):
            msg._bridge_cli_error = d["error"]
        if d.get("is_api_error_message") is True:
            msg._bridge_is_api_error_message = True
        if "stop_reason" in m:
            msg._bridge_stop_reason = m["stop_reason"]
        return msg
    r = sdk.result(
        d["subtype"], is_error=d["is_error"], num_turns=d.get("num_turns", 1)
    )
    r.result = d.get("result")
    for k in ("terminal_reason", "stop_reason", "api_error", "api_error_status"):
        if d.get(k) is not None:
            setattr(r, f"_bridge_{k}", d[k])
    return r


@pytest.mark.skipif(
    not BRTRUNC2, reason="BRTRUNC2 (abbruch_fortgesetzt) not in this tree"
)
async def test_brtrunc2_echter_strom_abbruch_immer(monkeypatch, tmp_path):
    raw = [
        json.loads(z)
        for z in (STROEME / "abbruch_immer.jsonl").read_text().splitlines()
        if z.strip()
    ]
    letzte = [d for d in raw if d["type"] == "assistant"][-1]
    assert letzte["is_api_error_message"] is True and letzte["error"] == "server_error"
    msgs = [sdk.init(), *(_roh_zu_sdk(d) for d in raw)]
    res = await _research(monkeypatch, tmp_path, msgs, _req())
    assert res.status == "error"
    assert res.fehler_code == "antwort_abgeschnitten"
    assert res.retryable is False
    assert res.antwort_ende == "abbruch_fortgesetzt"
    assert res.teilantwort == _ANFANG + "T-weiter " * 3
    assert "API Error" not in res.teilantwort
    assert res.content is None


# --- BRREFUSAL R2 (BRREFUSALR P2-e): a refusal in the continuation -------------


def _refusal_fehlerframe():
    msg = sdk.AssistantMessage(
        content=[sdk.TextBlock(text="Claude's safeguards flagged this message.")],
        model="<synthetic>",
    )
    msg._bridge_cli_error = "unknown"
    msg._bridge_is_api_error_message = True
    msg._bridge_stop_reason = "refusal"
    msg._bridge_stop_details = {"type": "refusal", "category": "cyber"}
    return msg


async def test_refusal_in_der_fortsetzung_bleibt_modell_abbruch(monkeypatch, tmp_path):
    """Under antwort_im_text the continuation was refused: the classifier
    ended the answer, not its length — named modell_abbruch, final, and the
    end of the answer still reported."""
    monkeypatch.delenv("BRIDGE_CLI_REFUSAL_RETRY", raising=False)
    msgs = [
        sdk.init(),
        _text(_ANFANG),
        UserMessage(content=[sdk.TextBlock(text=_META_TURN)]),
        _text(_REST),
        _refusal_fehlerframe(),
        _api_fehler_result(),
    ]
    res = await _research(monkeypatch, tmp_path, msgs, _req())
    assert res.status == "error"
    assert res.fehler_code == "modell_abbruch"
    assert res.retryable is False
    assert res.antwort_ende == "max_tokens_fortgesetzt"
    assert res.content is None
