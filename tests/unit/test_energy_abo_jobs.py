"""Energy-Abo-Modus über /v1/jobs (Zweitprüfung B1, 03.11.2026).

Der Job-POST validiert ``X-Funding-Source`` wie die Direktrouten (400/409),
speichert den geprüften Wert serverseitig (nie aus dem Rumpf) und der
Selbstaufruf trägt ihn weiter. Job-Chat und Job-Research laufen mit Header
bzw. Schalter + Allowlist am Pool vorbei. Ohne Netz, Pool als Falle,
Platzhalter-IDs.
"""
from __future__ import annotations

from contextlib import ExitStack
from typing import Any, Dict, List
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from tests.unit.test_energy_abo_bildweg import _handler, _img_body, abo, dev_bridge, prod_bridge  # noqa: F401
from tests.unit.test_energy_abo_modus import USER_A, pool_trap, switch  # noqa: F401
import src.main as main  # noqa: E402
from src.jobs import executors, registry, routes, store_client  # noqa: E402
from src.models import ResearchRequest  # noqa: E402
from src.providers import funding_policy as fp  # noqa: E402

ENERGY = {"X-App-ID": "werking-energy", "X-User-ID": USER_A}
# Echter Client vor jedem Fixture-Patch (der abo-Fixture sperrt httpx.AsyncClient).
_REAL_CLIENT = httpx.AsyncClient


@pytest.fixture
def jobs(monkeypatch):
    created: List[Dict[str, Any]] = []

    async def create_job(job_id, kind, payload, attribution):
        created.append({"kind": kind, "attribution": attribution})

    def _spawn(coro):
        coro.close()

    monkeypatch.setattr(routes, "verify_api_key", AsyncMock(return_value=True))
    monkeypatch.setattr(routes, "_require_enabled", lambda: None)
    monkeypatch.setattr(routes, "_attribution_extractor", main.extract_attribution_context)
    monkeypatch.setattr(routes, "spawn", _spawn)
    monkeypatch.setitem(registry._EXECUTORS, "abo-probe", AsyncMock())
    monkeypatch.setattr(store_client, "create_job", create_job)
    return created


async def _post(headers, body=None):
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(routes.router)
    transport = httpx.ASGITransport(app=app)
    async with _REAL_CLIENT(transport=transport, base_url="http://bridge") as c:
        return await c.post("/v1/jobs", json=body or {"kind": "abo-probe", "payload": {}}, headers=headers)


# --- Job-POST --------------------------------------------------------------

async def test_job_post_invalid_funding_source_400(dev_bridge, jobs):
    r = await _post({**ENERGY, "X-Funding-Source": "paid"})
    assert r.status_code == 400 and jobs == []


async def test_job_post_subscription_where_not_allowed_409(prod_bridge, jobs):
    r = await _post({**ENERGY, "X-Funding-Source": "subscription"})
    assert r.status_code == 409 and jobs == []


async def test_job_post_stores_validated_value_not_body(dev_bridge, jobs):
    r = await _post({**ENERGY, "X-Funding-Source": "ABO"})
    assert r.status_code == 200, r.text
    assert jobs[-1]["attribution"]["funding_source"] == "subscription"
    body = {"kind": "abo-probe", "payload": {},
            "attribution": {"app_id": "werking-energy", "funding_source": "subscription"}}
    r = await _post(ENERGY, body=body)
    # Rumpfwert verworfen; ohne Header und ohne Schalter: ausdrücklich Pool.
    assert r.status_code == 200 and jobs[-1]["attribution"]["funding_source"] == "pool"


async def test_job_post_without_header_unchanged(dev_bridge, jobs):
    """Ohne Angabe: Energy-Job wird ausdrücklich als Pool gespeichert (= heutiges
    Verhalten; der Selbstaufruf kann nicht selbst umschalten)."""
    r = await _post(ENERGY)
    assert r.status_code == 200 and jobs[-1]["attribution"]["funding_source"] == "pool"


def test_self_call_carries_funding_source():
    h = executors._build_headers({"app_id": "werking-energy", "user_id": USER_A, "funding_source": "subscription"})
    assert h["X-Funding-Source"] == "subscription" and h["X-User-ID"] == USER_A
    assert "X-Funding-Source" not in executors._build_headers({"app_id": "werking-energy"})


# --- Selbstaufruf: Job-Chat und Job-Research am Pool vorbei ------------------

def _self_call_request(attribution, path):
    from starlette.requests import Request
    headers = executors._build_headers(attribution)
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "method": "POST", "path": path, "headers": raw, "client": ("172.18.0.5", 1)})


ATTR_HEADER = {"app_id": "werking-energy", "user_id": "00000000-0000-4000-8000-0000000000cc",
               "funding_source": "subscription"}
ATTR_SWITCH = {"app_id": "werking-energy", "user_id": USER_A}


@pytest.mark.parametrize("variante", ["header", "switch"])
async def test_job_chat_self_call_bypasses_pool(dev_bridge, abo, pool_trap, monkeypatch, variante):
    if variante == "switch":
        monkeypatch.setenv(fp.SUBSCRIPTION_ONLY_ENV, "1")
        monkeypatch.setenv(fp.SUBSCRIPTION_ONLY_USERS_ENV, USER_A)
    cli, _, _ = abo
    req = _self_call_request(ATTR_HEADER if variante == "header" else ATTR_SWITCH, "/v1/chat/completions")
    with ExitStack() as stack:
        _handler(stack, AsyncMock(return_value=None))
        out = await main._chat_completions_finanziert(_img_body(), req, None)
    pool_trap.assert_not_called()
    assert len(cli.calls) == 1 and out["x_funding"]["pool_umgangen"] is True


@pytest.mark.parametrize("variante", ["header", "switch"])
async def test_job_research_self_call_bypasses_pool(dev_bridge, abo, pool_trap, monkeypatch, variante):
    if variante == "switch":
        monkeypatch.setenv(fp.SUBSCRIPTION_ONLY_ENV, "1")
        monkeypatch.setenv(fp.SUBSCRIPTION_ONLY_USERS_ENV, USER_A)
    req = _self_call_request(ATTR_HEADER if variante == "header" else ATTR_SWITCH, "/v1/research")
    admit = AsyncMock(return_value=JSONResponse(status_code=429, content={"error": "abo-limiter stop"}))
    cloud = AsyncMock(side_effect=AssertionError("research-cloud (pool) path used"))
    with ExitStack() as stack:
        stack.enter_context(patch.object(main, "verify_api_key", AsyncMock(return_value=None)))
        stack.enter_context(patch("src.routing.user_provider_override.enforce_user_provider_override",
                                  AsyncMock(return_value=None)))
        stack.enter_context(patch.object(main, "_admit_research_to_pool", admit))
        stack.enter_context(patch.object(main, "_execute_research_cloud_with_pool_fallback", cloud))
        stack.enter_context(patch("src.activity.ai_call_writer.persist_ai_call_activity", AsyncMock()))
        await main.research(ResearchRequest(query="Testfrage", model="claude-sonnet-5-5"), req, None)
    admit.assert_awaited_once()
    cloud.assert_not_called()
    pool_trap.assert_not_called()
    assert fp.pool_umgangen(req)


# --- Nicht-Blocker 1: Abo-Entscheid beim POST, nicht aus body.attribution ----

USER_FREMD = "00000000-0000-4000-8000-0000000000dd"


async def test_job_post_implicit_mode_decided_from_headers(dev_bridge, switch, jobs):
    """Header-User in der Allowlist, Rumpf nennt einen anderen user_id: der
    Job wird trotzdem als Abo gespeichert (kein Ausweichen auf den Pool)."""
    body = {"kind": "abo-probe", "payload": {},
            "attribution": {"app_id": "werking-energy", "user_id": USER_FREMD}}
    r = await _post(ENERGY, body=body)
    assert r.status_code == 200, r.text
    assert jobs[-1]["attribution"]["funding_source"] == "subscription"


async def test_job_post_body_user_cannot_gain_subscription(dev_bridge, switch, jobs):
    """Umgekehrt: Header-User nicht in der Liste, Rumpf nennt den gelisteten —
    der Job bleibt ausdrücklich beim Pool (heutiges Verhalten)."""
    body = {"kind": "abo-probe", "payload": {},
            "attribution": {"app_id": "werking-energy", "user_id": USER_A}}
    r = await _post({"X-App-ID": "werking-energy", "X-User-ID": USER_FREMD}, body=body)
    assert r.status_code == 200, r.text
    assert jobs[-1]["attribution"]["funding_source"] == "pool"


async def test_body_user_mismatch_self_call_still_bypasses_pool(dev_bridge, switch, jobs, abo, pool_trap):
    """Ende zu Ende mit Pool-Falle: gespeicherte Attribution → Selbstaufruf."""
    cli, _, _ = abo
    body = {"kind": "abo-probe", "payload": {},
            "attribution": {"app_id": "werking-energy", "user_id": USER_FREMD}}
    await _post(ENERGY, body=body)
    req = _self_call_request(jobs[-1]["attribution"], "/v1/chat/completions")
    with ExitStack() as stack:
        _handler(stack, AsyncMock(return_value=None))
        out = await main._chat_completions_finanziert(_img_body(), req, None)
    pool_trap.assert_not_called()
    assert len(cli.calls) == 1 and out["x_funding"]["pool_umgangen"] is True


async def test_body_user_listed_self_call_stays_pool_first(dev_bridge, switch, jobs, abo, pool_trap):
    body = {"kind": "abo-probe", "payload": {},
            "attribution": {"app_id": "werking-energy", "user_id": USER_A}}
    await _post({"X-App-ID": "werking-energy", "X-User-ID": USER_FREMD}, body=body)
    req = _self_call_request(jobs[-1]["attribution"], "/v1/chat/completions")
    with ExitStack() as stack:
        _handler(stack, AsyncMock(return_value=None), cap=AsyncMock(return_value=(False, 0.0, 0.0)))
        with pytest.raises(Exception):
            await main._chat_completions_finanziert(_img_body(), req, None)
    assert pool_trap.call_count >= 1   # Pool zuerst, kein erzwungenes Abo


async def test_other_app_jobs_get_no_funding_header(dev_bridge, switch, jobs):
    r = await _post({"X-App-ID": "werking-report", "X-User-ID": USER_A})
    assert r.status_code == 200 and "funding_source" not in (jobs[-1]["attribution"] or {})
