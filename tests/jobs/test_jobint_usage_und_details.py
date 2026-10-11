"""JOBINT: RUSAGE (usage / usage_fehlt) and JOBENDE (fehler_code /
antwort_ende / session_id) on the same failed job.

Both extend the job error path through the same five files. A failed research
run carries both at once; neither may displace the other, and usage must stay
flat in the row — inside `details` (extra=forbid) it would be a 422."""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from src.jobs import executors, store, store_client
from tests.jobs.test_error_verdict_br9 import (
    CUT,
    _job,
    _platform_app,
    _poll,
    _run,
    _Seam,
)

USAGE = {"input_tokens": 1200, "output_tokens": 64000}


@pytest.mark.parametrize("kind", ["research", "doc-agent"])
@pytest.mark.parametrize("usage_felder", [
    {"usage": USAGE, "usage_fehlt": None},  # RUSAGE passes both keys together
    {"usage": None, "usage_fehlt": "cli_lieferte_keine_usage"},
])
async def test_usage_and_details_reach_the_row_side_by_side(kind, usage_felder,
                                                            monkeypatch):
    """Executor → runner → store_client → platform-api route (ASGI) → store row
    → GET /v1/jobs, with usage fields and details in the same answer."""
    app = _platform_app()
    conn = AsyncMock()
    conn.execute.return_value = "UPDATE 1"

    class _Pool:
        @asynccontextmanager
        async def acquire(self):
            yield conn

    sent = []

    async def call(method, path, json=None, timeout_s=None):
        sent.append(json)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            r = await c.request(method, path, json=json)
        return SimpleNamespace(status_code=r.status_code,
                               json=r.json() if r.content else None)

    seam = _Seam()
    seam.mark_error.side_effect = store_client.mark_error
    answer = httpx.Response(200, json={"status": "error", "error": "original",
                                       "retryable": False, **CUT, **usage_felder})
    with patch.object(store_client, "call_platform", call), \
         patch.object(store, "get_pool", return_value=_Pool()):
        await _run(kind, seam, answer=answer,
                   executor=(executors.doc_agent_executor
                             if kind == "doc-agent" else None))
    # On the wire: usage flat, details nested and free of usage.
    assert sent[0]["details"] == CUT
    assert {k: sent[0][k] for k in usage_felder} == usage_felder
    row = json.loads(conn.execute.await_args.args[2])
    expected = {"message": f"{kind} self-call returned status=error: original",
                "code": "EXECUTOR_ERROR", "retryable": False, "retry_after_s": None,
                **CUT, **usage_felder}
    assert row == expected
    out = await _poll(_job(status="error", error=row), monkeypatch)
    for key, value in {**CUT, **usage_felder}.items():
        assert out["error"][key] == value


async def test_store_keeps_both_and_still_refuses_unknown_fields():
    conn = AsyncMock()
    conn.execute.return_value = "UPDATE 1"

    class _Pool:
        @asynccontextmanager
        async def acquire(self):
            yield conn

    with patch.object(store, "get_pool", return_value=_Pool()):
        await store.mark_error("j", "m", code="X", retryable=False,
                               details=CUT, usage=USAGE)
        row = json.loads(conn.execute.await_args.args[2])
        assert row["usage"] == USAGE and {k: row[k] for k in CUT} == CUT
        with pytest.raises(TypeError):
            await store.mark_error("j", "m", details=CUT, fremd=1)


async def test_usage_inside_details_is_refused_by_the_route():
    transport = httpx.ASGITransport(app=_platform_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.post("/v1/internal/jobs/j/error",
                         json={"message": "m", "details": {**CUT, "usage": USAGE}})
    assert r.status_code == 422
