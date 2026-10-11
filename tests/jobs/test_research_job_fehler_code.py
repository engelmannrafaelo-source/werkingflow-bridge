"""/v1/jobs research: the cause fields reach the job error (BRTRUNC2 item 7).

A direct /v1/research caller gets fehler_code / antwort_ende / teilantwort; over
/v1/jobs the error object carried only message, code and the verdict, so
Energy matched the text "Research answer cut off at the output limit". The
job's error object now carries the same fields as the endpoint's answer, along
the whole chain: research_executor → JobFailure → job_error_fields →
store_client.mark_error → platform-api InternalJobError → store.mark_error →
GET /v1/jobs/{id}.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx

from src.error_contract import JobFailure, job_error_fields
from src.jobs import store_client
from tests.jobs.test_error_verdict_br9 import _job, _poll, _run, _Seam

_ABGESCHNITTEN = {
    "status": "error",
    "query": "q",
    "model": "m",
    "error": "Research answer cut off mid-stream (antwort_ende=abbruch_fortgesetzt)",
    "retryable": False,
    "retry_after_s": None,
    "fehler_code": "antwort_abgeschnitten",
    "antwort_ende": "abbruch_fortgesetzt",
    "teilantwort": '```x\n{"a": 1, "b": 2}\n```',
}


async def test_research_job_fehler_traegt_fehler_code_und_antwort_ende():
    seam = _Seam()
    await _run("research", seam, answer=httpx.Response(200, json=_ABGESCHNITTEN))
    kw = seam.mark_error.await_args.kwargs
    assert kw["code"] == "EXECUTOR_ERROR" and kw["retryable"] is False
    assert kw["fehler_code"] == "antwort_abgeschnitten"
    assert kw["antwort_ende"] == "abbruch_fortgesetzt"
    assert kw["teilantwort"] == _ABGESCHNITTEN["teilantwort"]


async def test_anderer_research_fehler_ohne_ursachenfelder():
    seam = _Seam()
    await _run(
        "research",
        seam,
        answer=httpx.Response(
            200,
            json={
                "status": "error",
                "query": "q",
                "model": "m",
                "error": "no report",
                "retryable": False,
                "fehler_code": None,
                "antwort_ende": "end_turn",
            },
        ),
    )
    kw = seam.mark_error.await_args.kwargs
    assert "fehler_code" not in kw and "teilantwort" not in kw
    assert kw["antwort_ende"] == "end_turn"


def test_jobfailure_nimmt_nur_die_ursachenfelder():
    e = JobFailure("m", retryable=False, details={**_ABGESCHNITTEN, "content": "x"})
    fields = job_error_fields(e)
    assert set(fields) == {
        "code",
        "retryable",
        "retry_after_s",
        "fehler_code",
        "antwort_ende",
        "teilantwort",
    }


async def test_store_client_schickt_die_felder_an_platform_api():
    sent = {}

    async def fake_call(method, path, json=None, timeout_s=None):
        sent.update(json)
        return type("R", (), {"status_code": 204, "json": None})()

    with patch.object(store_client, "call_platform", fake_call):
        await store_client.mark_error(
            "j",
            "m",
            code="EXECUTOR_ERROR",
            retryable=False,
            fehler_code="antwort_abgeschnitten",
            antwort_ende="max_tokens",
            teilantwort="t",
        )
    assert sent["fehler_code"] == "antwort_abgeschnitten"
    assert sent["antwort_ende"] == "max_tokens"
    assert sent["teilantwort"] == "t"


async def test_platform_api_route_speichert_die_felder():
    from src import internal_routes
    from src.internal_routes import InternalJobError
    from src.jobs import store

    body = InternalJobError(
        message="m",
        code="EXECUTOR_ERROR",
        retryable=False,
        fehler_code="antwort_abgeschnitten",
        antwort_ende="abbruch_fortgesetzt",
        teilantwort="t",
    )
    conn = AsyncMock()
    conn.execute.return_value = "UPDATE 1"

    class _Pool:
        def acquire(self):
            class _Ctx:
                async def __aenter__(self_inner):
                    return conn

                async def __aexit__(self_inner, *a):
                    return False

            return _Ctx()

    with patch.object(store, "get_pool", return_value=_Pool()):
        await internal_routes.internal_mark_error("j", body, None)
    row = json.loads(conn.execute.await_args.args[2])
    assert row == {
        "message": "m",
        "code": "EXECUTOR_ERROR",
        "retryable": False,
        "retry_after_s": None,
        "fehler_code": "antwort_abgeschnitten",
        "antwort_ende": "abbruch_fortgesetzt",
        "teilantwort": "t",
    }


async def test_poll_zeigt_die_felder(monkeypatch):
    stored = {
        "message": "m",
        "code": "EXECUTOR_ERROR",
        "retryable": False,
        "retry_after_s": None,
        "fehler_code": "antwort_abgeschnitten",
        "antwort_ende": "max_tokens_fortgesetzt",
        "teilantwort": "t",
    }
    out = await _poll(_job(kind="research", status="error", error=stored), monkeypatch)
    assert out["error"]["fehler_code"] == "antwort_abgeschnitten"
    assert out["error"]["antwort_ende"] == "max_tokens_fortgesetzt"
    assert out["error"]["teilantwort"] == "t"
