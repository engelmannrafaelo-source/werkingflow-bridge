"""BRREFUSAL R2 (BRREFUSALR R1 P1): the CLI's refusal fallback to ANOTHER model.

Befund R1 (real CLI 2.1.285 / 2.1.296 against the Messages-API mock): besides
the same-model retry (CLAUDE_CODE_DISABLE_REFUSAL_RETRY, off since BRREFUSAL)
the CLI has a fallback to another model after a refusal — system message
"model_refusal_fallback" (direction "retry", original_model, fallback_model,
api_refusal_category), own switch CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK. For
category cyber and a refusal without text it fired, and the bridge answered
finish_reason=stop: the refused beginning plus the fallback model's answer,
under the requested model's name.

Here: the switch is set per run (own way back), and the system message is a
structured refusal signal that ends the run at once as modell_abbruch — the
second net for a CLI that ignores the switch. Also P2-d: stop_details.type
names the refusal on the API-error frame like stop_reason does.
"""

from __future__ import annotations

import json

import pytest

from src.middleware.bridge_error import CliModellAbbruch, classify_exception
from tests.unit.test_brrefusal_modell_abbruch import (  # noqa: F401  (umgebung: autouse fixture)
    CATEGORY,
    FLAGGED,
    WORKER,
    _chat,
    _error_frame,
    _error_result,
    _gebucht_mit,
    _laufen,
    _ledger,
    _make_cli,
    _ok_result,
    _turn,
    claude_cli,
    sdk,
    umgebung,
)

ERSATZ = "claude-sonnet-5"


@pytest.fixture(autouse=True)
def ohne_rueckweg(monkeypatch):
    monkeypatch.delenv("BRIDGE_CLI_REFUSAL_FALLBACK", raising=False)


def _fallback(category="cyber", direction="retry"):
    return sdk.SystemMessage(subtype="model_refusal_fallback", data={
        "type": "system", "subtype": "model_refusal_fallback", "trigger": "refusal",
        "direction": direction, "scope": "session",
        "original_model": "claude-sonnet-5-5", "fallback_model": ERSATZ,
        "request_id": "req_x", "api_refusal_category": category,
        "content": "Switched to Sonnet 5 …"})


def _ersatz_antwort():
    m = sdk.AssistantMessage(content=[sdk.TextBlock(text="OK vom Ersatzmodell")], model=ERSATZ)
    m._bridge_message_id = "msg_ersatz"
    m._bridge_message_usage = {"input_tokens": 2, "output_tokens": 3}
    m._bridge_stop_reason = "end_turn"
    return m


# The R1 shape: refused beginning (the stop fields of the streamed chunk are
# null with the real CLI), the fallback notice, the other model's answer, a
# successful result.
def ERSATZMODELL():
    return (sdk.init(), _turn("Teil B angefangen"), _fallback(), _ersatz_antwort(),
            _ok_result())


# ── 1. the switch ───────────────────────────────────────────────────────────


async def test_beide_cli_folgezuege_aus(monkeypatch, tmp_path):
    _, seen = await _laufen(monkeypatch, tmp_path, (sdk.init(), _turn("ok"), _ok_result()),
                            max_thinking_tokens=1024)
    env = seen[0].env
    assert env["CLAUDE_CODE_DISABLE_REFUSAL_RETRY"] == "1"
    assert env["CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK"] == "1"
    assert env["MAX_THINKING_TOKENS"] == "1024"


@pytest.mark.parametrize("schalter,bleibt", [
    ("BRIDGE_CLI_REFUSAL_FALLBACK", "CLAUDE_CODE_DISABLE_REFUSAL_RETRY"),
    ("BRIDGE_CLI_REFUSAL_RETRY", "CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK"),
])
async def test_rueckweg_je_schalter(monkeypatch, tmp_path, schalter, bleibt):
    monkeypatch.setenv(schalter, "true")
    _, seen = await _laufen(monkeypatch, tmp_path, (sdk.init(), _turn("ok"), _ok_result()))
    env = seen[0].env
    assert set(env) & {"CLAUDE_CODE_DISABLE_REFUSAL_RETRY",
                       "CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK"} == {bleibt}


# ── 2. the signal ───────────────────────────────────────────────────────────


def test_fallback_meldung_ist_ein_refusal_signal():
    sig = claude_cli.cli_refusal_signal(_fallback())
    assert sig == {"stop_reason": "refusal", "category": "cyber",
                   "quelle": "model_refusal_fallback", "direction": "retry",
                   "original_model": "claude-sonnet-5-5", "fallback_model": ERSATZ}
    assert claude_cli.cli_refusal_signal(sdk.chunk(_fallback()))["fallback_model"] == ERSATZ
    assert claude_cli.cli_refusal_signal(
        {"type": "system", "subtype": "model_refusal_fallback",
         "api_refusal_category": None})["quelle"] == "model_refusal_fallback"
    # other model switches of the CLI (overloaded, model_not_found) are not refusals
    assert claude_cli.cli_refusal_signal(
        {"type": "system", "subtype": "model_fallback", "trigger": "overloaded"}) is None


def test_stop_details_type_allein_ist_signal():
    """P2-d: the frame names the refusal only in stop_details.type."""
    m = _error_frame(refusal=False)
    m._bridge_stop_reason = None
    m._bridge_stop_details = {"type": "refusal", "category": CATEGORY}
    turn = claude_cli.detect_cli_api_error(m)
    sig = claude_cli.cli_refusal_signal_of_turn(turn)
    assert sig == {"stop_reason": "refusal", "category": CATEGORY, "quelle": "api_error_turn"}
    # a frame whose details name something else stays capacity
    m._bridge_stop_details = {"type": "other"}
    assert claude_cli.cli_refusal_signal_of_turn(claude_cli.detect_cli_api_error(m)) is None


# ── 3. run_completion: the fallback model's answer never goes out ───────────


async def test_ersatzmodell_antwort_wird_nie_geliefert(monkeypatch, tmp_path):
    monkeypatch.setattr(claude_cli, "query", lambda prompt, options: _gen(ERSATZMODELL()))
    got = []
    with pytest.raises(CliModellAbbruch) as ei:
        async for c in _make_cli(tmp_path).run_completion(prompt="Hallo", model="m"):
            got.append(c)
    e = ei.value
    assert e.refusal["quelle"] == "model_refusal_fallback"
    assert e.refusal["fallback_model"] == ERSATZ and e.refusal["category"] == "cyber"
    assert e.retryable is False
    assert "OK vom Ersatzmodell" not in json.dumps(got, default=str)


async def _gen(messages):
    for m in messages:
        yield m


async def test_fallback_nach_fehlerframe_bleibt_refusal(monkeypatch, tmp_path):
    """Order of the R1 'cyber, always refused' shape: fallback notice, then
    no_fallback and the frame of the fallback model's refusal."""
    with pytest.raises(CliModellAbbruch) as ei:
        await _laufen(monkeypatch, tmp_path, (
            sdk.init(), _turn("Teil B angefangen"), _fallback(),
            _error_frame(), _error_result()))
    assert ei.value.refusal["quelle"] == "model_refusal_fallback"


def test_umschlag_nennt_das_ersatzmodell():
    e = CliModellAbbruch(None, None, "", WORKER, claude_cli.cli_refusal_signal(_fallback()))
    resp = classify_exception(e)
    err = json.loads(resp.body)["error"]
    assert resp.status_code == 422 and "retry-after" not in {k.lower() for k in resp.headers}
    assert err["code"] == "modell_abbruch" and err["retryable"] is False
    assert err["refusal_quelle"] == "model_refusal_fallback"
    assert err["fallback_model"] == ERSATZ


# ── 4. chat: 422, audit names the fallback ──────────────────────────────────


@pytest.mark.parametrize("stream", [False, True])
def test_chat_ersatzmodell_wird_422(monkeypatch, tmp_path, stream):
    buch = _ledger(monkeypatch)
    resp = _chat(monkeypatch, tmp_path, stream, ERSATZMODELL())
    assert "OK vom Ersatzmodell" not in resp.text
    if stream:
        # the refused beginning was already streamed: event: error, no [DONE]
        assert resp.status_code == 200
        errs = [b for b in resp.text.split("\n\n") if b.startswith("event: error")]
        assert len(errs) == 1 and "data: [DONE]" not in resp.text
        err = json.loads(errs[0].split("data: ", 1)[1])["error"]
    else:
        assert resp.status_code == 422, resp.text[:300]
        err = resp.json()["error"]
    assert err["code"] == "modell_abbruch" and err["fallback_model"] == ERSATZ
    rows = _gebucht_mit(buch, "modell_abbruch")
    assert len(rows) == 1
    assert rows[0]["provider_meta"]["refusal_quelle"] == "model_refusal_fallback"
    assert rows[0]["provider_meta"]["fallback_model"] == ERSATZ


def test_satz_bleibt_ungelesen():
    """Still no word filter: the sentence in a normal turn is no signal."""
    assert claude_cli.cli_refusal_signal(_turn(FLAGGED)) is None
