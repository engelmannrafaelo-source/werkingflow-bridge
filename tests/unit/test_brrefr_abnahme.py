"""BRREFR R1 (Codex, 12.10.2026): the five reproduced paths and two transport
controls as acceptance tests. The end of a run is established once
(claude_cli.lauf_abschluss / AbbruchFilter); every output path must carry it:
tool-leak retry, streaming with several blocks of the refused call, research
with either answer contract, the research error schema and the job wrapping.

Probes adopted from local-storage/brrefr-evidence/test_brrefr_review.py; the
research-job and retry-usage assertions are added.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

# First: its sys.modules stand-ins for claude_code_sdk must be in place
# before src.main is imported through the research test module.
from tests.unit import test_chat_modell_abbruch as b  # isort: skip
from tests.unit.test_chat_modell_abbruch import frischer_zustand  # noqa: F401,E402  isort: skip (autouse)

from src.error_contract import JobFailure  # noqa: E402
from src.jobs import executors  # noqa: E402
from tests import test_research_antwort_im_text as r  # noqa: E402


def _refusal(text):
    return dict(r._assistant(text), stop_reason="refusal")


# ── 1. tool-leak retry ───────────────────────────────────────────────────────

def test_retry_refusal_ist_content_filter_mit_usage_beider_laeufe(monkeypatch, tmp_path):
    calls = 0

    def factory(ignored):
        async def query(prompt, options):
            nonlocal calls
            calls += 1
            first = b.normal_verlauf()
            first[1] = b._parsed("msg_intro", [{"type": "text", "text": "I will generate the file now."}],
                                 "end_turn", b.USAGE_ZUG_1)
            for m in (first if calls == 1 else b.refusal_verlauf()):
                yield m
        return query

    monkeypatch.setattr(b, "_query", factory)
    with patch("src.main.MessageAdapter.messages_to_prompt", return_value=("p" * 20000, None)):
        resp = b._sync_chat(monkeypatch, tmp_path, [])
    assert resp.status_code == 200, resp.text
    assert calls == 2
    body = resp.json()
    choice = body["choices"][0]
    assert choice["finish_reason"] == "content_filter", body
    assert body["x_bridge_finish"]["category"] == "reasoning_extraction"
    assert choice["message"]["content"].startswith("<teil")
    assert b.FOLGE_TEXT not in choice["message"]["content"]
    # Every paid call is listed; usage is the sum of both runs.
    assert [z.get("lauf") for z in body["x_bridge_usage_zuege"]] == [None, "tool_leak_retry", "tool_leak_retry"]
    assert body["x_bridge_model_calls"] == 3
    assert body["usage"]["completion_tokens"] == 20 + 5625


# ── 4. streaming keeps every block of the refused call ───────────────────────

async def test_stream_behaelt_alle_bloecke_der_gesperrten_nachricht(monkeypatch, tmp_path):
    msgs = b.refusal_verlauf()
    msgs.insert(1, b._parsed("msg_011Cfx9N8", [{"type": "thinking", "thinking": ""}],
                             "refusal", b.USAGE_ZUG_1, b.STOP_DETAILS))
    _, data = await b._stream(monkeypatch, tmp_path, msgs)
    text = "".join(d["choices"][0]["delta"].get("content", "") for d in data)
    assert text.startswith("<teil"), repr(text[:40])
    assert b.FOLGE_TEXT not in text
    assert data[-1]["choices"][0]["finish_reason"] == "content_filter"


# ── 2/3/5. research ─────────────────────────────────────────────────────────

async def test_research_text_refusal_ist_modell_abbruch():
    res, _ = await r._run([_refusal("partial"), r._assistant("Sorry"), r._OK], r._req())
    assert res.status == "error"
    assert res.fehler_code == "modell_abbruch", res.model_dump()
    assert res.antwort_ende == "refusal"
    assert res.retryable is False


async def test_research_leerer_refusal_ist_modell_abbruch():
    res, _ = await r._run([_refusal(""), r._OK], r._req())
    assert res.status == "error"
    assert res.fehler_code == "modell_abbruch", res.model_dump()


async def test_research_dateivertrag_refusal_ist_kein_erfolg(tmp_path):
    res, _ = await r._run(
        [_refusal("partial"), r._metadata(tmp_path, ("output.md", "Unfinished report")), r._OK],
        r._req(antwort_im_text=False),
    )
    assert res.status == "error", res.model_dump()
    assert res.fehler_code == "modell_abbruch"
    assert "Unfinished report" not in (res.content or "")


async def test_research_job_endet_als_fehler_mit_fehler_code(tmp_path):
    """The job executor reads the endpoint's body: status=error ends the job
    as an error, fehler_code reaches its details, retryable stays false."""
    res, _ = await r._run(
        [_refusal("partial"), r._metadata(tmp_path, ("output.md", "Unfinished report")), r._OK],
        r._req(antwort_im_text=False),
    )
    with pytest.raises(JobFailure) as ei:
        executors._raise_on_status_error("research", res.model_dump())
    assert ei.value.retryable is False
    assert ei.value.details.get("fehler_code") == "modell_abbruch"
    assert ei.value.details.get("antwort_ende") == "refusal"


# ── transport controls: emptiness alone is no model stop ─────────────────────

def test_leerer_lauf_ohne_refusal_bleibt_503(monkeypatch, tmp_path):
    resp = b._sync_chat(monkeypatch, tmp_path, [b.sdk.init(), b.sdk.result("success")])
    assert resp.status_code == 503, resp.text


def test_refusal_ohne_abschlussmarker_bleibt_transportfehler(monkeypatch, tmp_path):
    resp = b._sync_chat(monkeypatch, tmp_path, b.refusal_verlauf()[:2])
    assert resp.status_code == 503, resp.text
