"""BRREFR R4 (Codex, 12.10.2026): the doc-agent uses the strict acceptance
contract like every path whose result is final as it stands. A run that is
not complete (error_max_turns, max_tokens) is status=error, not retryable —
never a partial answer as success — and the doc-agent job takes that over.

Codex' two probes (local-storage/brrefr-r4-evidence/test_brrefr_r4_review.py)
plus the job and a complete control run.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

# First: its sys.modules stand-ins for claude_code_sdk must be in place
# before src.main is imported.
from tests.unit import test_chat_modell_abbruch as b  # noqa: F401  isort: skip
from tests.unit.test_chat_modell_abbruch import frischer_zustand  # noqa: F401,E402  isort: skip (autouse)

import src.main as main  # noqa: E402
from src.claude_cli import lauf_abschluss  # noqa: E402
from src.error_contract import JobFailure  # noqa: E402
from src.jobs import executors  # noqa: E402
from tests import test_research_antwort_im_text as r  # noqa: E402


def _req():
    return SimpleNamespace(files=[SimpleNamespace(name="test.md", content="neutral")], question="q",
                           model="claude-sonnet-5", max_turns=3)


def _verlauf(ende):
    if ende == "error_max_turns":
        return [dict(r._assistant("Partial answer before interruption"), stop_reason=None),
                dict(r._OK, subtype="error_max_turns")]
    if ende == "max_tokens":
        return [dict(r._assistant("Partial answer before interruption"), stop_reason="max_tokens"), r._OK]
    return [dict(r._assistant("Complete answer"), stop_reason="end_turn"), r._OK]


async def _doc_agent(ende):
    with (
        patch.object(main.claude_cli, "run_completion", return_value=r._stream(*_verlauf(ende))),
        patch("src.activity.ai_call_writer.persist_ai_call_activity", new=AsyncMock()),
    ):
        return await main._execute_doc_agent_impl(_req())


@pytest.mark.parametrize("ende", ["error_max_turns", "max_tokens"])
async def test_doc_agent_teilantwort_ist_kein_erfolg(ende):
    res = await _doc_agent(ende)
    assert res.status == "error", res.model_dump()
    assert res.answer is None
    assert res.retryable is False


@pytest.mark.parametrize("ende", ["error_max_turns", "max_tokens"])
async def test_doc_agent_job_endet_als_fehler(ende):
    res = await _doc_agent(ende)
    with pytest.raises(JobFailure) as ei:
        executors._raise_on_status_error("doc-agent", res.model_dump())
    assert ei.value.retryable is False


async def test_doc_agent_vollstaendiger_lauf_bleibt_erfolg():
    res = await _doc_agent("end_turn")
    assert res.status == "success" and res.answer == "Complete answer"


def test_roher_cli_result_ohne_num_turns_ist_vollstaendig():
    a = lauf_abschluss([r._assistant("answer"), {"type": "result", "subtype": "success", "is_error": False}])
    assert a.vollstaendig is True
