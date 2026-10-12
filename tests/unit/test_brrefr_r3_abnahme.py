"""BRREFR R3 (Codex, 12.10.2026): ``vollstaendig`` is a promise and must hold.

True only with a completion marker, no model stop, a CLI result that is not
unfinished and a complete finish reason; an unfinished run never reads
"stop". The stream ends through annehmen(); old job results without the
field are never true; research-cloud hands out only a complete end.

Codex' six probes (local-storage/brrefr-r3-evidence/test_brrefr_r3_review.py)
with the escape hatches removed — each states the one expected outcome —
plus the unit rules of LaufAbschluss / annehmen.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

# First: its sys.modules stand-ins for claude_code_sdk must be in place
# before src.main is imported.
from tests.unit import test_chat_modell_abbruch as b  # isort: skip
from tests.unit.test_chat_modell_abbruch import frischer_zustand  # noqa: F401,E402  isort: skip (autouse)

from src.lauf_abschluss import (  # noqa: E402
    LaufAbschluss,
    LaufUnfertig,
    abschluss_aus_stop_reason,
    annehmen,
)


def unfertiger_verlauf():
    """Partial text without a stop_reason, then the CLI result error_max_turns."""
    msgs = b.normal_verlauf(stop_reason=None)
    msgs[1] = b._parsed("msg_partial", [{"type": "text", "text": "Partial progress before the turn limit."}],
                        None, b.USAGE_ZUG_1)
    msgs[-1] = b.sdk.result("error_max_turns", num_turns=1)
    return msgs


# ── R3-1 unfinished is never complete, never "stop" ─────────────────────────

def test_sync_unfertig_ist_length_und_unvollstaendig(monkeypatch, tmp_path):
    resp = b._sync_chat(monkeypatch, tmp_path, unfertiger_verlauf())
    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert body["choices"][0]["finish_reason"] == "length"
    assert body["x_bridge_finish"]["vollstaendig"] is False
    assert "error_max_turns" in body["x_bridge_finish"]["unfertig"]
    assert body["choices"][0]["message"]["content"] == "Partial progress before the turn limit."


async def test_stream_unfertig_ist_length_und_unvollstaendig(monkeypatch, tmp_path):
    out, data = await b._stream(monkeypatch, tmp_path, unfertiger_verlauf())
    assert not any("event: error" in c for c in out)
    assert data[-1]["choices"][0]["finish_reason"] == "length"
    assert data[-1]["x_bridge_finish"]["vollstaendig"] is False


async def test_job_unfertig_ist_unvollstaendig(monkeypatch, tmp_path):
    from tests.jobs.test_error_verdict_br9 import _job, _poll

    resp = b._sync_chat(monkeypatch, tmp_path, unfertiger_verlauf())
    body = await _poll(_job(status="done", result=resp.json()), monkeypatch)
    assert body["abschluss"]["vollstaendig"] is False
    assert body["abschluss"]["finish_reason"] == "length"


def test_sauberer_lauf_ohne_stop_reason_bleibt_stop_und_vollstaendig(monkeypatch, tmp_path):
    """The supported CLI form: no stop_reason, clean success result."""
    resp = b._sync_chat(monkeypatch, tmp_path, b.normal_verlauf(stop_reason=None))
    body = resp.json()
    assert body["choices"][0]["finish_reason"] == "stop"
    assert body["x_bridge_finish"]["vollstaendig"] is True


# ── R3-2 the stream ends through annehmen() ─────────────────────────────────

async def test_stream_transportabbruch_endet_mit_fehler_event(monkeypatch, tmp_path):
    out, data = await b._stream(monkeypatch, tmp_path, b.normal_verlauf()[:-1])
    assert any("event: error" in c for c in out)
    assert "data: [DONE]\n\n" not in out
    assert not any(d["choices"][0].get("finish_reason") == "stop" for d in data)


# ── R3-3 old job results ────────────────────────────────────────────────────

async def test_altes_job_ergebnis_mit_r1_details_ist_unvollstaendig(monkeypatch):
    from tests.jobs.test_error_verdict_br9 import _job, _poll

    alt = {"choices": [{"finish_reason": "content_filter", "message": {"content": "partial"}}],
           "x_bridge_finish": {"stop_reason": "refusal", "category": None, "folgezuege": 1}}
    body = await _poll(_job(status="done", result=alt), monkeypatch)
    assert body["abschluss"]["vollstaendig"] is False
    assert body["abschluss"]["vollstaendig_unbekannt"] is True
    assert body["abschluss"]["grund"] == "x_bridge_finish_ohne_vollstaendig"


async def test_r1_ergebnis_mit_stop_ist_trotzdem_nicht_vollstaendig(monkeypatch):
    from tests.jobs.test_error_verdict_br9 import _job, _poll

    alt = {"choices": [{"finish_reason": "stop", "message": {"content": "x"}}],
           "x_bridge_finish": {"stop_reason": "end_turn"}}
    body = await _poll(_job(status="done", result=alt), monkeypatch)
    assert body["abschluss"]["vollstaendig"] is False


# ── R3-4 research-cloud: only a complete end ────────────────────────────────

@pytest.mark.parametrize("stop_reason", [None, "model_context_window_exceeded", "max_tokens"])
async def test_cloud_ohne_vollstaendiges_ende_ist_fehler(stop_reason):
    from src.research_cloud.executor import (
        ResearchCloudExecutorError,
        run_research_cloud,
    )
    from tests.research_cloud.test_executor import _response, _usage

    client = MagicMock()
    client.post = AsyncMock(return_value=_response(200, {
        "model": "claude-sonnet-5", "stop_reason": stop_reason, "usage": _usage(),
        "content": [{"type": "text", "text": "partial report"}]}))
    with pytest.raises(ResearchCloudExecutorError):
        await run_research_cloud("q", "s", api_key="test-only", client=client)


async def test_cloud_pause_turn_bleibt_zwischenschritt():
    from src.research_cloud.executor import run_research_cloud
    from tests.research_cloud.test_executor import _response, _usage

    client = MagicMock()
    client.post = AsyncMock(side_effect=[
        _response(200, {"model": "claude-sonnet-5", "stop_reason": "pause_turn", "usage": _usage(),
                        "content": [{"type": "text", "text": "searching"}]}),
        _response(200, {"model": "claude-sonnet-5", "stop_reason": "end_turn", "usage": _usage(),
                        "content": [{"type": "text", "text": "full report"}]}),
    ])
    res = await run_research_cloud("q", "s", api_key="test-only", client=client)
    assert res.status == "success" and res.content == "full report"


# ── the rules themselves ────────────────────────────────────────────────────

@pytest.mark.parametrize("abschluss,erwartet", [
    (LaufAbschluss(finish_reason="stop", stop_reason="end_turn"), True),
    (LaufAbschluss(finish_reason="tool_calls", stop_reason="tool_use"), True),
    (LaufAbschluss(finish_reason="stop", stop_reason=None, unfertig="error_max_turns"), False),
    (LaufAbschluss(finish_reason="stop", stop_reason="end_turn", transport="no_completion_marker"), False),
    (LaufAbschluss(finish_reason="content_filter", stop_reason="refusal"), False),
    (LaufAbschluss(finish_reason="length", stop_reason="max_tokens"), False),
    (abschluss_aus_stop_reason(None), False),
])
def test_vollstaendig_nur_wenn_alles_erfuellt(abschluss, erwartet):
    assert abschluss.vollstaendig is erwartet
    assert abschluss.details()["vollstaendig"] is erwartet


def test_annehmen_ganz_fertig_verlangt_vollstaendig():
    with pytest.raises(LaufUnfertig):
        annehmen(abschluss_aus_stop_reason("model_context_window_exceeded"), ganz_fertig=True)
    with pytest.raises(LaufUnfertig):
        annehmen(abschluss_aus_stop_reason(None), ganz_fertig=True)
    ok = abschluss_aus_stop_reason("end_turn")
    assert annehmen(ok, ganz_fertig=True) is ok
