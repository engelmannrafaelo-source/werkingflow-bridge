"""/v1/jobs research: the cause fields reach the job error (BRTRUNC2 item 7).

A direct /v1/research caller gets fehler_code / antwort_ende / teilantwort; over
/v1/jobs the error object carried only message, code and the verdict, so
Energy matched the text "Research answer cut off at the output limit". The
job's error object now carries the same fields as the endpoint's answer, along
the whole chain: research_executor → JobFailure → job_error_fields →
store_client.mark_error → platform-api InternalJobError → store.mark_error →
GET /v1/jobs/{id}.

Unified with develop's JOBENDE contract (BRUMBAU, 13.10.2026): fehler_code /
antwort_ende / session_id travel in `details`, teilantwort / diagnose_ref flat
beside them, usage / usage_fehlt flat as RUSAGE has them.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx

from src.error_contract import JobFailure, job_error_fields
from src.jobs import store, store_client
from src.platform_client import PlatformUnavailable
from tests.jobs.test_error_verdict_br9 import _job, _poll, _run, _Seam


class _Resp:
    def __init__(self, status_code, json=None):
        self.status_code = status_code
        self.json = json


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
    assert kw["details"] == {"fehler_code": "antwort_abgeschnitten",
                             "antwort_ende": "abbruch_fortgesetzt"}
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
    assert "fehler_code" not in kw["details"] and "teilantwort" not in kw
    assert kw["details"]["antwort_ende"] == "end_turn"


def test_jobfailure_nimmt_nur_die_ursachenfelder():
    e = JobFailure("m", retryable=False, details={**_ABGESCHNITTEN, "content": "x"})
    fields = job_error_fields(e)
    assert set(fields) == {"code", "retryable", "retry_after_s", "details", "teilantwort"}
    assert fields["details"] == {"fehler_code": "antwort_abgeschnitten",
                                 "antwort_ende": "abbruch_fortgesetzt"}


async def test_store_client_schickt_die_felder_an_platform_api():
    sent = {}

    async def fake_call(method, path, json=None, timeout_s=None):
        sent.update(json)
        return _Resp(
            200, {"ursache_felder": ["antwort_ende", "fehler_code", "teilantwort"]}
        )

    with patch.object(store_client, "call_platform", fake_call):
        await store_client.mark_error(
            "j",
            "m",
            code="EXECUTOR_ERROR",
            retryable=False,
            details={"fehler_code": "antwort_abgeschnitten", "antwort_ende": "max_tokens"},
            teilantwort="t",
        )
    assert sent["details"] == {"fehler_code": "antwort_abgeschnitten",
                               "antwort_ende": "max_tokens"}
    assert sent["teilantwort"] == "t"
    assert sent["ursache_bestaetigen"] is True


async def test_store_client_lehnt_unbekannte_felder_ab():
    """A field outside the contract is a caller bug, not something to send
    and hope (the same rule as store.mark_error)."""
    import pytest

    with pytest.raises(TypeError, match="fehler_code"):
        await store_client.mark_error("j", "m", fehler_code="antwort_abgeschnitten")


async def test_platform_api_route_speichert_die_felder():
    from src import internal_routes
    from src.internal_routes import InternalJobError
    from src.jobs import store

    body = InternalJobError(
        message="m",
        code="EXECUTOR_ERROR",
        retryable=False,
        details={"fehler_code": "antwort_abgeschnitten", "antwort_ende": "abbruch_fortgesetzt"},
        teilantwort="t",
        ursache_bestaetigen=True,
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
        resp = await internal_routes.internal_mark_error("j", body, None)
    # BRTRUNC2N: the route confirms what it stored.
    assert resp.status_code == 200
    assert json.loads(resp.body) == {
        "ursache_felder": ["antwort_ende", "fehler_code", "teilantwort"]
    }
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


# --- BRTRUNC2N: version gap platform-api <-> worker, loud -------------------------


def _pool_mit(conn):
    class _Pool:
        def acquire(self):
            class _Ctx:
                async def __aenter__(self_inner):
                    return conn

                async def __aexit__(self_inner, *a):
                    return False

            return _Ctx()

    return _Pool()


async def test_route_ohne_ursachenfelder_bleibt_204():
    """A worker from before BRTRUNC2N accepts only 204 — it must keep getting it
    (deploy order: the new platform-api meets old workers first)."""
    from src import internal_routes
    from src.internal_routes import InternalJobError

    conn = AsyncMock()
    conn.execute.return_value = "UPDATE 1"
    with patch.object(store, "get_pool", return_value=_pool_mit(conn)):
        resp = await internal_routes.internal_mark_error(
            "j", InternalJobError(message="m", code="EXECUTOR_ERROR"), None
        )
    assert resp.status_code == 204


def _alte_platform_api(calls):
    """POST /v1/internal/jobs/{id}/error of platform-api 276d146: its
    InternalJobError knows no cause fields (pydantic drops them), 204."""
    from pydantic import BaseModel

    class InternalJobErrorVorBRTRUNC2(BaseModel):
        message: str
        code: str | None = None
        retryable: bool | None = None
        retry_after_s: int | None = None

    async def fake_call(method, path, json=None, timeout_s=None):
        calls.append(InternalJobErrorVorBRTRUNC2(**json).model_dump())
        return _Resp(204)

    return fake_call


async def test_alte_platform_api_verwirft_felder_laut(caplog):
    calls = []
    before = store_client.retention_snapshot()["cause_fields_dropped"].get(
        "fehler_code", 0
    )
    with patch.object(store_client, "call_platform", _alte_platform_api(calls)):
        await store_client.mark_error(
            "j-alt",
            "Research answer cut off mid-stream",
            code="EXECUTOR_ERROR",
            retryable=False,
            details={"fehler_code": "antwort_abgeschnitten",
                     "antwort_ende": "abbruch_fortgesetzt"},
            teilantwort="t",
        )
    # Loud in the log ...
    fehler = [
        r
        for r in caplog.records
        if r.levelname == "ERROR" and "j-alt" in r.getMessage()
    ]
    assert fehler and "BRTRUNC2" in fehler[0].getMessage()
    # ... counted ...
    dropped = store_client.retention_snapshot()["cause_fields_dropped"]
    assert dropped["fehler_code"] == before + 1
    # ... and at the job: the second write is what the row keeps.
    assert len(calls) == 2
    zeile = calls[-1]
    assert zeile["code"] == "EXECUTOR_ERROR" and zeile["retryable"] is False
    assert zeile["message"].startswith(
        "Research answer cut off mid-stream [bridge-vertrag:"
    )
    assert "fehler_code=antwort_abgeschnitten" in zeile["message"]
    assert "antwort_ende=abbruch_fortgesetzt" in zeile["message"]
    assert "teilantwort" in zeile["message"] and "teilantwort=" not in zeile["message"]


async def test_bestaetigung_unvollstaendig_ist_laut(caplog):
    calls = []

    async def fake_call(method, path, json=None, timeout_s=None):
        calls.append(json)
        if len(calls) == 1:
            return _Resp(200, {"ursache_felder": ["fehler_code"]})
        return _Resp(204)

    with patch.object(store_client, "call_platform", fake_call):
        await store_client.mark_error(
            "j-teil",
            "m",
            code="EXECUTOR_ERROR",
            retryable=False,
            details={"fehler_code": "antwort_abgeschnitten", "antwort_ende": "max_tokens"},
        )
    assert len(calls) == 2
    assert "antwort_ende=max_tokens" in calls[1]["message"]
    assert "fehler_code=" not in calls[1]["message"]
    assert any(
        r.levelname == "ERROR" and "j-teil" in r.getMessage() for r in caplog.records
    )


async def test_bestaetigt_kein_zweiter_schreibvorgang(caplog):
    calls = []

    async def fake_call(method, path, json=None, timeout_s=None):
        calls.append(json)
        return _Resp(200, {"ursache_felder": ["fehler_code", "antwort_ende"]})

    with patch.object(store_client, "call_platform", fake_call):
        await store_client.mark_error(
            "j-neu",
            "m",
            code="EXECUTOR_ERROR",
            retryable=False,
            details={"fehler_code": "antwort_abgeschnitten", "antwort_ende": "max_tokens"},
        )
    assert len(calls) == 1
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


async def test_ohne_ursachenfelder_reicht_204():
    calls = []

    async def fake_call(method, path, json=None, timeout_s=None):
        calls.append(json)
        return _Resp(204)

    with patch.object(store_client, "call_platform", fake_call):
        await store_client.mark_error("j", "m", code="EXECUTOR_ERROR", retryable=False)
    assert len(calls) == 1


async def test_rueckfallweg_reicht_die_felder_durch():
    """platform-api unreachable, direct-DB fallback (BRTRUNC2R mutant MX3)."""
    conn = AsyncMock()
    conn.execute.return_value = "UPDATE 1"
    with (
        patch.object(
            store_client,
            "call_platform",
            AsyncMock(side_effect=PlatformUnavailable("down")),
        ),
        patch.object(store_client, "is_db_enabled", return_value=True),
        patch.object(store, "get_pool", return_value=_pool_mit(conn)),
    ):
        await store_client.mark_error(
            "j",
            "m",
            code="EXECUTOR_ERROR",
            retryable=False,
            details={"fehler_code": "antwort_abgeschnitten",
                     "antwort_ende": "abbruch_fortgesetzt"},
            teilantwort="t",
        )
    row = json.loads(conn.execute.await_args.args[2])
    assert row["fehler_code"] == "antwort_abgeschnitten"
    assert row["antwort_ende"] == "abbruch_fortgesetzt"
    assert row["teilantwort"] == "t"
