"""BRREFUSAL R2 (BRREFUSALR P2-c): a model refusal on the explorer's CLI path.

Before: kind.py set no refusal switch and read no signal. A refusal ended as
result.is_error → "cli_fehler: SDK-Lauf: RuntimeError", the coordinator tried
the step a second time, and each attempt could carry the CLI's same-model
retry and its other-model fallback (~6 unnamed sends); a fallback model's
answer would have been taken as the step's result.

Now: both CLI follow-ups off, the CLI's refusal system message ends the step
at once as "modell_abbruch", no second attempt, and the job ends final with
details.fehler_code = modell_abbruch.
"""

import sys
import types
from unittest.mock import AsyncMock

import conftest
import httpx
import pytest
from test_leitstand import finish
from test_leitstand import setup as leitstand_setup
from test_weiterreicher import mock_http

from src.erkunder import kind
from src.erkunder.models import Schritt
from src.erkunder.platz import child_failure_reason
from src.jobs.executors import erkunder_executor


def _system(subtype, **data):
    return conftest.real_sdk.SystemMessage(
        subtype=subtype, data={"type": "system", "subtype": subtype, **data}
    )


FALLBACK = _system(
    "model_refusal_fallback", trigger="refusal", direction="retry",
    original_model="claude-sonnet-5-5", fallback_model="claude-sonnet-5",
    api_refusal_category="cyber",
)
NO_FALLBACK = _system(
    "model_refusal_no_fallback", original_model="claude-sonnet-5-5",
    api_refusal_category="reasoning_extraction",
)


def test_sdk_options_schalten_beide_folgezuege_ab(monkeypatch, tmp_path):
    monkeypatch.delenv("BRIDGE_CLI_REFUSAL_RETRY", raising=False)
    monkeypatch.delenv("BRIDGE_CLI_REFUSAL_FALLBACK", raising=False)
    env = kind.sdk_options(
        {"ordner": str(tmp_path), "max_turns": 1, "claude_token": "synthetic"}
    ).env
    assert env["CLAUDE_CODE_DISABLE_REFUSAL_RETRY"] == "1"
    assert env["CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK"] == "1"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "synthetic"  # rest of the env kept


def test_rueckweg_je_schalter(monkeypatch, tmp_path):
    monkeypatch.setenv("BRIDGE_CLI_REFUSAL_FALLBACK", "true")
    monkeypatch.delenv("BRIDGE_CLI_REFUSAL_RETRY", raising=False)
    env = kind.sdk_options(
        {"ordner": str(tmp_path), "max_turns": 1, "claude_token": "synthetic"}
    ).env
    assert "CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK" not in env
    assert env["CLAUDE_CODE_DISABLE_REFUSAL_RETRY"] == "1"


@pytest.mark.parametrize("signal", [FALLBACK, NO_FALLBACK], ids=["fallback", "no_fallback"])
async def test_kind_endet_benannt(monkeypatch, tmp_path, signal):
    """The step stops at the CLI's refusal message: the fallback model's
    answer that would follow is never read, and the reason is named."""
    gelesen = []

    class ResultMessage:
        is_error = False
        usage = {}
        num_turns = 2

    async def query(**_kwargs):
        yield signal
        gelesen.append("danach")
        yield ResultMessage()

    monkeypatch.setattr(kind, "install_resilient_parser", lambda: None)
    monkeypatch.setitem(sys.modules, "claude_code_sdk", types.SimpleNamespace(
        AssistantMessage=conftest.real_sdk.AssistantMessage,
        ToolUseBlock=conftest.real_sdk.ToolUseBlock,
        ClaudeCodeOptions=lambda **kw: types.SimpleNamespace(**kw),
        ResultMessage=ResultMessage,
        query=query,
    ))
    with pytest.raises(kind.ModellAbbruch) as err:
        await kind.run({"ordner": str(tmp_path), "prompt": "synthetic",
                        "max_turns": 1, "claude_token": "synthetic"})
    assert gelesen == []
    assert kind.failure_reason(err.value) == "modell_abbruch"
    assert child_failure_reason(b'{"fehler":"modell_abbruch"}') == "modell_abbruch"
    # anything else from the child stays masked
    assert child_failure_reason(b'{"fehler":"modell_abbruch: x"}') == "cli_fehler: Kindprozess"


def test_schritt_vertrag_kennt_den_grund():
    Schritt.model_validate({
        "name": "erkunder-1", "versuch": 1, "status": "abbruch",
        "abbruch_grund": "modell_abbruch", "dauer_s": 0.0, "zuege": 0,
        "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0},
        "ram_spitze_mb": 0, "worker": "worker2",
    })


async def test_leitstand_wiederholt_einen_refusal_nicht(tmp_path):
    """Same prompt, same classifier: one attempt, and the step ends with the
    named reason (here the harmonisation, which every report needs)."""
    service, places, _ = await leitstand_setup(
        tmp_path, failures={"harmonisierung": 2}, grund="modell_abbruch"
    )
    try:
        result = await finish(service)
        assert result["zustand"] == "abbruch"
        assert places.attempts["harmonisierung"] == 1
        state = service.states["bericht-123"]
        assert state["fehler"] == "modell_abbruch"
    finally:
        await service.shutdown()


async def test_leitstand_wiederholt_andere_gruende_weiter(tmp_path):
    service, places, _ = await leitstand_setup(tmp_path, failures={"harmonisierung": 2})
    try:
        assert (await finish(service))["zustand"] == "abbruch"
        assert places.attempts["harmonisierung"] == 2
    finally:
        await service.shutdown()


async def test_job_endet_endgueltig_mit_fehler_code(monkeypatch, tmp_path, auftrag):
    tokenfile = tmp_path / "token"
    tokenfile.write_text("synthetic-secret-never-log")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN_FILE", str(tokenfile))
    monkeypatch.setenv("ERKUNDER_INTERNAL_TOKEN", "internal-synthetic")
    monkeypatch.setenv("ERKUNDER_URL", "http://coordinator.test")
    monkeypatch.setenv("INSTANCE_NAME", "worker2")
    from src.error_contract import JobFailure

    mock_http(monkeypatch, lambda req: httpx.Response(200, json=(
        {"angehaengt": True} if req.url.path == "/start" else
        {"zustand": "abbruch", "schritt": "harmonisierung", "fertig": 3, "gesamt": 5,
         "fehler": "modell_abbruch"})))
    with pytest.raises(JobFailure) as err:
        await erkunder_executor(auftrag, None, AsyncMock())
    assert str(err.value) == "erkunder abbruch: harmonisierung: modell_abbruch"
    assert err.value.retryable is False
    assert err.value.details == {"fehler_code": "modell_abbruch"}
