"""CXA2 regression contract: strict request and BRTRUNC2's single transport."""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from src.models import ResearchRequest
from src.error_contract import JobFailure, job_error_fields
from src.internal_routes import InternalJobError, internal_mark_error
from src.jobs import store, store_client
from src.jobs.executors import research_executor


def request_payload():
    return dict(query="Sort a fictional book index.", model="synthetic", max_turns=2,
                max_tokens=40, antwort_im_text=True, async_mode=False,
                agentischer_lauf={"schema": "agentischer-lauf/1", "lauf_id": "cxa2-test",
                    "material": [], "werkzeuge": [],
                    "ausgabe": {"modus": "text", "artefakte": []},
                    "grenzen": {"zeit_s": 20, "tokens_gesamt": 500}})


@pytest.mark.parametrize("instructions", [None, "", {}, "Synthetic app instruction"])
def test_research_instructions_rejected(instructions):
    with pytest.raises(ValidationError, match="auftrag_ungueltig"):
        ResearchRequest(**request_payload(), research_instructions=instructions)


CAUSE = {
    "fehler_code": "grenze_tokens", "antwort_ende": "max_tokens_fortgesetzt",
    "teilantwort": "synthetic partial answer",
    "usage": {"input_tokens": 7, "output_tokens": 2,
              "cache_read_input_tokens": 3, "cache_creation_input_tokens": 1,
              "cost_eur": None, "cost_source": "unknown"},
    "diagnose_ref": "/v1/research/cxa2-test/content?lauf_artifact=diagnose",
}


def test_details_keep_usage_and_diagnostic_but_not_prompt():
    failure = JobFailure("failure", retryable=False,
                         details={**CAUSE, "prompt": "private input", "content": "private output"})
    assert job_error_fields(failure) == {
        "code": "EXECUTOR_ERROR", "retryable": False, "retry_after_s": None, **CAUSE}


@pytest.mark.parametrize("local_fallback", [False, True])
async def test_executor_error_reaches_store_unchanged(local_fallback):
    from src.platform_client import PlatformUnavailable

    answer = {"status": "error", "error": "synthetic failure", "retryable": False, **CAUSE}
    with patch("src.jobs.executors._self_post_json", AsyncMock(return_value=answer)):
        with pytest.raises(JobFailure) as caught:
            await research_executor(request_payload(), {}, AsyncMock())
    fields = job_error_fields(caught.value)

    async def platform(method, path, json, timeout_s):
        if local_fallback:
            raise PlatformUnavailable("synthetic outage")
        await internal_mark_error("synthetic-job", InternalJobError(**json), _claims=None)
        return type("Response", (), {"status_code": 204, "json": None})()

    conn = AsyncMock()
    conn.execute.return_value = "UPDATE 1"
    pool = MagicMock()
    pool.acquire.return_value.__aenter__ = AsyncMock(return_value=conn)
    pool.acquire.return_value.__aexit__ = AsyncMock(return_value=False)
    with patch.object(store_client, "call_platform", platform), \
         patch.object(store_client, "_fallback_or_raise"), \
         patch.object(store, "get_pool", return_value=pool):
        await store_client.mark_error("synthetic-job", "synthetic failure", **fields)
    saved = json.loads(conn.execute.call_args.args[2])
    assert saved == {"message": "synthetic failure", "code": "EXECUTOR_ERROR",
                     "retryable": False, "retry_after_s": None, **CAUSE}
