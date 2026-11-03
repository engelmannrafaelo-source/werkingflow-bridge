"""Energy-Abo-Modus: Job-Platzierung bei 90-%-Reserve (N1F P2, 03.11.2026).

Befund: Recherche als POST /v1/jobs (kind=research) landete auf Workern, deren
Konto über der 90-%-Reserve lag; der Job wurde angelegt und starb im
Selbstaufruf mit 503 "Nutzerreserve". Jetzt lehnt der Job-POST eines
abo-finanzierten Jobs auf einem solchen Worker VOR dem Anlegen mit 429 ab;
nginx wiederholt 429 auf /v1/jobs beim nächsten Worker (proxy_next_upstream
http_429 non_idempotent). Ohne Netz; nginx wird als Schleife über Worker
nachgebildet. Platzhalter-IDs.
"""
from __future__ import annotations

from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import sys
for _n in ['claude_code_sdk', 'claude_code_sdk._errors', 'claude_code_sdk._internal',
           'claude_code_sdk._internal.client', 'src.identity.routes', 'src.db.client']:
    if _n not in sys.modules:
        sys.modules[_n] = MagicMock()

import src.main as main  # noqa: E402,F401 — registers executors / extractor
from src.jobs import registry, routes, store_client  # noqa: E402
from src.providers import credit_pool, funding_policy as fp  # noqa: E402

USER = "00000000-0000-4000-8000-0000000000aa"
ENERGY = {"X-App-ID": "werking-energy", "X-User-ID": USER}
ABO = {**ENERGY, "X-Funding-Source": "subscription"}
_REAL_CLIENT = httpx.AsyncClient


@pytest.fixture
def bridge(monkeypatch):
    monkeypatch.setenv(fp.FALLBACK_SWITCH_ENV, "1")
    monkeypatch.setenv("BRIDGE_ORIGIN_ID", "dev")
    created: List[Dict[str, Any]] = []
    keyed: Dict[str, str] = {}

    async def create_job(job_id, kind, payload, attribution):
        created.append({"job_id": job_id, "kind": kind, "attribution": attribution})

    async def find_job_by_idempotency_key(owner, key, kind, payload):
        jid = keyed.get(key)
        return None if jid is None else {"job_id": jid, "kind": kind, "status": "pending"}

    async def create_job_idempotent(job_id, kind, payload, attribution, owner, key):
        if key in keyed:
            return {"created": False, "job": {"job_id": keyed[key], "kind": kind, "status": "pending"}}
        keyed[key] = job_id
        created.append({"job_id": job_id, "kind": kind, "attribution": attribution, "key": key})
        return {"created": True, "job": {"job_id": job_id, "kind": kind, "status": "pending"}}

    def _spawn(coro):
        coro.close()

    monkeypatch.setattr(routes, "verify_api_key", AsyncMock(return_value=True))
    monkeypatch.setattr(routes, "_require_enabled", lambda: None)
    monkeypatch.setattr(routes, "_attribution_extractor", main.extract_attribution_context)
    monkeypatch.setattr(routes, "spawn", _spawn)
    monkeypatch.setattr(store_client, "create_job", create_job)
    monkeypatch.setattr(store_client, "find_job_by_idempotency_key", find_job_by_idempotency_key)
    monkeypatch.setattr(store_client, "create_job_idempotent", create_job_idempotent)
    monkeypatch.setattr(routes, "_job_runs_off_pool", AsyncMock(return_value=False))
    for kind in ("chat", "research", "doc-agent", "ping", "convert-html-to-pdf"):
        monkeypatch.setitem(registry._EXECUTORS, kind, AsyncMock())
    trap = AsyncMock(side_effect=AssertionError("credit pool touched"))
    monkeypatch.setattr(credit_pool, "coordinator", trap)
    monkeypatch.setattr(credit_pool, "post_messages", trap)
    state = {"weekly": {}}

    class _Limiter:
        def _refresh_account_usage(self):
            pass

        @property
        def _account_usage(self):
            import os
            w = os.environ.get("INSTANCE_NAME")
            return {"known": True, "weekly_pct": state["weekly"][w]}

    monkeypatch.setattr("src.middleware.adaptive_limiter.get_adaptive_limiter", lambda: _Limiter())
    return created, keyed, state, trap


async def _post_on(monkeypatch, worker, headers, body):
    from fastapi import FastAPI
    monkeypatch.setenv("INSTANCE_NAME", worker)
    app = FastAPI()
    app.include_router(routes.router)
    async with _REAL_CLIENT(transport=httpx.ASGITransport(app=app), base_url="http://bridge") as c:
        return await c.post("/v1/jobs", json=body, headers=headers)


async def _nginx(monkeypatch, workers, headers, body=None):
    """/v1/jobs wie nginx: bei 429 der nächste Worker (http_429 non_idempotent)."""
    body = body or {"kind": "research", "payload": {"query": "Testfrage"}}
    seen = []
    for w in workers:
        r = await _post_on(monkeypatch, w, headers, body)
        seen.append((w, r.status_code))
        if r.status_code != 429:
            return r, seen
    return r, seen


W = ["worker1", "worker2", "worker3", "worker4"]


async def test_abo_job_lands_on_free_worker_once(bridge, monkeypatch):
    created, _, state, trap = bridge
    state["weekly"] = {"worker1": 95.0, "worker2": 91.0, "worker3": 16.0, "worker4": 90.0}
    r, seen = await _nginx(monkeypatch, W, ABO)
    assert seen == [("worker1", 429), ("worker2", 429), ("worker3", 200)]
    assert r.json()["status"] == "pending" and r.json()["job_id"].startswith("job_dev_")
    assert len(created) == 1 and created[0]["attribution"]["funding_source"] == "subscription"
    trap.assert_not_called()


async def test_abo_job_via_switch_and_allowlist(bridge, monkeypatch):
    created, _, state, _ = bridge
    monkeypatch.setenv(fp.SUBSCRIPTION_ONLY_ENV, "1")
    monkeypatch.setenv(fp.SUBSCRIPTION_ONLY_USERS_ENV, USER)
    state["weekly"] = {"worker1": 99.0, "worker2": 10.0}
    r, seen = await _nginx(monkeypatch, ["worker1", "worker2"], ENERGY)
    assert seen == [("worker1", 429), ("worker2", 200)] and len(created) == 1


async def test_keyed_abo_job_no_double_and_replay(bridge, monkeypatch):
    created, keyed, state, _ = bridge
    state["weekly"] = {"worker1": 92.0, "worker2": 20.0, "worker3": 20.0}
    hdr = {**ABO, "Idempotency-Key": "energy-n1f:p2:0123456789abcdef"}
    r, seen = await _nginx(monkeypatch, W[:3], hdr)
    assert seen == [("worker1", 429), ("worker2", 201)]
    jid = r.json()["job_id"]
    # Wiederholung desselben Schlüssels (auch auf einem reservierten Worker):
    # die vorhandene Antwort, kein zweiter Job.
    r2 = await _post_on(monkeypatch, "worker1", hdr, {"kind": "research", "payload": {"query": "Testfrage"}})
    assert r2.status_code == 200 and r2.json()["job_id"] == jid
    assert len(created) == 1 and keyed["energy-n1f:p2:0123456789abcdef"] == jid


async def test_all_reserved_stays_loud_no_pool(bridge, monkeypatch):
    created, _, state, trap = bridge
    state["weekly"] = {w: 90.0 for w in W}
    r, seen = await _nginx(monkeypatch, W, ABO)
    assert [s for _, s in seen] == [429, 429, 429, 429]
    body = r.json()
    assert "weekly user reserve" in str(body) and created == []
    trap.assert_not_called()


async def test_unknown_usage_counts_as_reserve(bridge, monkeypatch):
    created, _, state, _ = bridge

    class _Unknown:
        def _refresh_account_usage(self):
            pass
        _account_usage = {"known": False}
    monkeypatch.setattr("src.middleware.adaptive_limiter.get_adaptive_limiter", lambda: _Unknown())
    r = await _post_on(monkeypatch, "worker1", ABO, {"kind": "chat", "payload": {}})
    assert r.status_code == 429 and created == []


@pytest.mark.parametrize("headers,kind", [
    (ENERGY, "research"),                                   # Energy ohne Abo: Pool zuerst, wie bisher
    ({"X-App-ID": "werking-report", "X-User-ID": USER}, "research"),
    ({"X-App-ID": "werking-report", "X-User-ID": USER}, "chat"),
    (ABO, "ping"),                                          # kein LLM-Job
    (ABO, "convert-html-to-pdf"),
])
async def test_no_veto_outside_abo_llm_jobs(bridge, monkeypatch, headers, kind):
    created, _, state, _ = bridge
    state["weekly"] = {"worker1": 99.0}
    monkeypatch.setitem(registry._EXECUTORS, "ping", AsyncMock())
    r = await _post_on(monkeypatch, "worker1", headers, {"kind": kind, "payload": {}})
    assert r.status_code == 200, r.text
    assert len(created) == 1


async def test_cloud_research_job_not_vetoed(bridge, monkeypatch):
    created, _, state, _ = bridge
    state["weekly"] = {"worker1": 99.0}
    monkeypatch.setattr(routes, "_job_runs_off_pool", AsyncMock(return_value=True))
    r = await _post_on(monkeypatch, "worker1", ABO, {"kind": "research", "payload": {}})
    assert r.status_code == 200 and len(created) == 1
