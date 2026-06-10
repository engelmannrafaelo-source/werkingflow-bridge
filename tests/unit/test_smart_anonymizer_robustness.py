"""
Regression tests for the smart-anonymize >5 KB HTTP 500 (root cause: the Haiku
refinement self-call truncated its JSON response and the exception bubbled up
uncaught).

Contract under test (defensive, fail-fast, NO silent truncation):
  * Refinement is batched so large inputs do not overflow the response budget.
  * ANY refinement problem — non-200, timeout, TRUNCATION (finish_reason=length),
    unparseable / non-list JSON — raises ``RefinementError``. Never a partial or
    silently-degraded result.

These tests drive ``refine_anonymization`` with a faked detection result, so
they need neither Presidio/spaCy nor a running Bridge.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.privacy import smart_anonymizer as sa  # noqa: E402
from src.privacy.smart_anonymizer import RefinementError  # noqa: E402


def _fake_result(n: int = 3):
    """A duck-typed stand-in for AnonymizationResult (no Presidio needed)."""
    ents = [
        SimpleNamespace(placeholder=f"ANON_PERSON_{i:03d}", entity_type="PERSON", confidence=0.9)
        for i in range(1, n + 1)
    ]
    return SimpleNamespace(
        detected_entities=ents,
        anonymized_text="Text mit " + " ".join(e.placeholder for e in ents),
    )


class _MockResp:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json


class _MockAsyncClient:
    """Async context manager standing in for httpx.AsyncClient."""

    calls: list = []

    def __init__(self, handler):
        self._handler = handler

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, headers=None, json=None):  # noqa: A002 — match httpx api
        _MockAsyncClient.calls.append(json)
        return self._handler(json)  # may return a _MockResp or raise


def _patch(monkeypatch, handler):
    _MockAsyncClient.calls = []
    monkeypatch.setattr(sa.httpx, "AsyncClient", lambda *a, **kw: _MockAsyncClient(handler))


def _response(content: str, finish_reason: str = "stop") -> _MockResp:
    return _MockResp(200, {
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": {},
    })


async def test_http_500_raises(monkeypatch):
    """A non-200 self-call is a hard error, not a silent fallback."""
    _patch(monkeypatch, lambda body: _MockResp(500, text="upstream boom"))
    with pytest.raises(RefinementError, match="HTTP 500"):
        await sa.refine_anonymization(_fake_result(3))


async def test_timeout_raises(monkeypatch):
    """An httpx timeout (the real >5 KB failure mode) is a hard error."""
    def _raise(body):
        raise httpx.ReadTimeout("simulated slow refinement")
    _patch(monkeypatch, _raise)
    with pytest.raises(RefinementError, match="transport error"):
        await sa.refine_anonymization(_fake_result(2))


async def test_truncation_is_a_hard_error(monkeypatch):
    """finish_reason=length MUST raise — never a silent truncation."""
    # Even with otherwise-valid-looking JSON, a length finish must be rejected.
    valid_but_truncated = '[{"placeholder": "ANON_PERSON_001", "decision": "KEEP", "reason": "x"}]'
    _patch(monkeypatch, lambda body: _response(valid_but_truncated, finish_reason="length"))
    with pytest.raises(RefinementError, match="TRUNCATED"):
        await sa.refine_anonymization(_fake_result(2))


async def test_unparseable_json_raises(monkeypatch):
    """A complete-but-malformed (or parse-failing) response is a hard error."""
    truncated = '[{"placeholder": "ANON_PERSON_001", "decision": "RESTORE", "reason": "Wien'  # no closing ]
    _patch(monkeypatch, lambda body: _response(truncated, finish_reason="stop"))
    with pytest.raises(RefinementError, match="not valid JSON"):
        await sa.refine_anonymization(_fake_result(2))


async def test_non_list_json_raises(monkeypatch):
    """Haiku returning an object instead of an array is a hard error."""
    _patch(monkeypatch, lambda body: _response('{"oops": "not a list"}'))
    with pytest.raises(RefinementError, match="expected a list"):
        await sa.refine_anonymization(_fake_result(1))


async def test_valid_json_restores_selected(monkeypatch):
    """Happy path: a well-formed decision array is honoured."""
    decisions = (
        '[{"placeholder": "ANON_PERSON_001", "decision": "RESTORE", "reason": "Ortsname"},'
        ' {"placeholder": "ANON_PERSON_002", "decision": "KEEP", "reason": "Personenname"}]'
    )
    _patch(monkeypatch, lambda body: _response(decisions))
    result = await sa.refine_anonymization(_fake_result(2))
    assert result["restore_placeholders"] == ["ANON_PERSON_001"]
    assert result["keep_placeholders"] == ["ANON_PERSON_002"]


async def test_large_input_is_chunked_and_merged(monkeypatch):
    """Many entities → multiple batched calls, decisions merged (no truncation)."""
    def _handler(body):
        # Echo a KEEP decision for exactly the placeholders in THIS batch's prompt.
        prompt = body["messages"][0]["content"]
        import re
        phs = re.findall(r"ANON_PERSON_\d{3}", prompt.split("<erkannte_entitaeten>")[1].split("</erkannte_entitaeten>")[0])
        arr = ",".join(f'{{"placeholder": "{p}", "decision": "KEEP", "reason": "x"}}' for p in phs)
        return _response("[" + arr + "]")

    n = sa.REFINE_BATCH_SIZE * 2 + 5  # forces 3 batches
    _patch(monkeypatch, _handler)
    result = await sa.refine_anonymization(_fake_result(n))
    # 3 self-calls, all N decisions present, none lost across batch boundaries.
    assert len(_MockAsyncClient.calls) == 3
    assert len(result["keep_placeholders"]) == n


async def test_batch_max_tokens_capped(monkeypatch):
    """Per-batch response budget scales with batch size but stays under the cap."""
    _patch(monkeypatch, lambda body: _response("[]"))
    await sa.refine_anonymization(_fake_result(sa.REFINE_BATCH_SIZE))
    sent = _MockAsyncClient.calls[0]
    assert sent["max_tokens"] <= sa.MAX_REFINE_TOKENS
    assert sent["max_tokens"] > 2000  # would have truncated at the old fixed 2000


async def test_one_failing_batch_fails_whole_request(monkeypatch):
    """If any batch fails, the whole refinement fails (no partial result)."""
    state = {"n": 0}

    def _handler(body):
        state["n"] += 1
        if state["n"] == 2:  # second batch truncates
            return _response("[]", finish_reason="length")
        return _response("[]")

    _patch(monkeypatch, _handler)
    with pytest.raises(RefinementError, match="batch 2/"):
        await sa.refine_anonymization(_fake_result(sa.REFINE_BATCH_SIZE * 2))


async def test_empty_entities_short_circuits(monkeypatch):
    """No detected entities → no self-call at all."""
    def _explode(body):
        raise AssertionError("self-call must not happen when there are no entities")
    _patch(monkeypatch, _explode)
    result = await sa.refine_anonymization(SimpleNamespace(detected_entities=[], anonymized_text="x"))
    assert result == {"decisions": [], "restore_placeholders": [], "keep_placeholders": []}
