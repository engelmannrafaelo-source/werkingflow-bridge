"""Tests für src/attribution.py — fail-closed Attribution (Geld-Invariante).

Deckt ab:
  - Marker-Klassifikation (anonymous:<grund>, legacy '_anonymous', undefined/null)
  - Middleware OFF: nichts wird abgelehnt, unattributed wird gezählt
  - Middleware ON: missing → 400, anonymous/user → durchgelassen
  - Nur POST + enforced path + Authorization werden gemessen
  - Fail-open: kaputter Header-Parse bricht den Request nicht
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from src.attribution import (
    AttributionEnforcementMiddleware,
    ENFORCED_PATHS,
    anonymous_reason,
    classify_user_id,
    snapshot,
    _anonymous,
    _rejected,
    _unattributed,
    _unattributed_sources,
)


def _reset_counters():
    _unattributed.clear()
    _unattributed_sources.clear()
    _anonymous.clear()
    _rejected.clear()


# ---------------------------------------------------------------------------
# Klassifikation
# ---------------------------------------------------------------------------

def test_anonymous_reason_marker():
    assert anonymous_reason("anonymous:public-check-funnel") == "public-check-funnel"
    assert anonymous_reason("_anonymous") == "legacy-underscore-alias"
    assert anonymous_reason("anonymous:") is None       # leerer Grund = kein Marker
    assert anonymous_reason("anonymous:   ") is None
    assert anonymous_reason(None) is None
    assert anonymous_reason("u-123") is None


def test_classify_user_id():
    assert classify_user_id("3f2a-uuid-egal") == "user"
    assert classify_user_id("user@example.com") == "user"
    assert classify_user_id("anonymous:funnel") == "anonymous"
    assert classify_user_id("_anonymous") == "anonymous"
    assert classify_user_id(None) == "missing"
    assert classify_user_id("") == "missing"
    assert classify_user_id("   ") == "missing"
    assert classify_user_id("undefined") == "missing"   # JS-Interpolation eines fehlenden Werts
    assert classify_user_id("null") == "missing"
    assert classify_user_id("anonymous:") == "missing"  # Marker ohne Grund = fehlend


# ---------------------------------------------------------------------------
# Middleware-Harness
# ---------------------------------------------------------------------------

def _scope(path="/v1/chat/completions", method="POST", headers=None):
    return {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [(k.encode(), v.encode()) for k, v in (headers or {}).items()],
    }


async def _run(mw, scope):
    """Führt die Middleware aus; returns (reached_app, responses)."""
    reached = {"app": False}
    sent = []

    async def inner_app(scope, receive, send):
        reached["app"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"{}"})

    mw.app = inner_app

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    await mw(scope, receive, send)
    return reached["app"], sent


AUTH = {"authorization": "Bearer k-123", "x-app-id": "test-app"}


@pytest.mark.asyncio
async def test_off_missing_user_counts_but_passes():
    _reset_counters()
    mw = AttributionEnforcementMiddleware(app=None)
    with patch.dict("os.environ", {"BRIDGE_ATTRIBUTION_ENFORCE": "false"}):
        reached, _ = await _run(mw, _scope(headers=AUTH))
    assert reached is True
    snap = snapshot()
    assert snap["unattributed_total"] == 1
    assert snap["unattributed_by_app"]["test-app"]["/v1/chat/completions"] == 1
    assert snap["rejected_total"] == 0


@pytest.mark.asyncio
async def test_on_missing_user_rejected_400():
    _reset_counters()
    mw = AttributionEnforcementMiddleware(app=None)
    with patch.dict("os.environ", {"BRIDGE_ATTRIBUTION_ENFORCE": "true"}):
        reached, sent = await _run(mw, _scope(headers=AUTH))
    assert reached is False
    start = next(m for m in sent if m["type"] == "http.response.start")
    assert start["status"] == 400
    body = json.loads(next(m for m in sent if m["type"] == "http.response.body")["body"])
    assert body["error"]["code"] == "missing_user_attribution"
    assert "anonymous:<grund>" in body["error"]["message"]
    assert snapshot()["rejected_total"] == 1


@pytest.mark.asyncio
async def test_on_anonymous_marker_passes_and_is_bucketed():
    _reset_counters()
    mw = AttributionEnforcementMiddleware(app=None)
    with patch.dict("os.environ", {"BRIDGE_ATTRIBUTION_ENFORCE": "true"}):
        reached, _ = await _run(mw, _scope(headers={**AUTH, "x-user-id": "anonymous:public-check-funnel"}))
    assert reached is True
    snap = snapshot()
    assert snap["anonymous_by_app"]["test-app"]["public-check-funnel"] == 1
    assert snap["unattributed_total"] == 0


@pytest.mark.asyncio
async def test_on_legacy_underscore_alias_passes():
    _reset_counters()
    mw = AttributionEnforcementMiddleware(app=None)
    with patch.dict("os.environ", {"BRIDGE_ATTRIBUTION_ENFORCE": "true"}):
        reached, _ = await _run(mw, _scope(headers={**AUTH, "x-user-id": "_anonymous"}))
    assert reached is True
    assert snapshot()["anonymous_by_app"]["test-app"]["legacy-underscore-alias"] == 1


@pytest.mark.asyncio
async def test_on_real_user_passes_untouched():
    _reset_counters()
    mw = AttributionEnforcementMiddleware(app=None)
    with patch.dict("os.environ", {"BRIDGE_ATTRIBUTION_ENFORCE": "true"}):
        reached, _ = await _run(mw, _scope(headers={**AUTH, "x-user-id": "3d5e0f1a-user"}))
    assert reached is True
    snap = snapshot()
    assert snap["unattributed_total"] == 0
    assert snap["anonymous_total"] == 0


@pytest.mark.asyncio
async def test_no_authorization_header_not_measured():
    """Scanner-Rauschen ohne Bearer wird weder gezählt noch abgelehnt (401t downstream)."""
    _reset_counters()
    mw = AttributionEnforcementMiddleware(app=None)
    with patch.dict("os.environ", {"BRIDGE_ATTRIBUTION_ENFORCE": "true"}):
        reached, _ = await _run(mw, _scope(headers={}))
    assert reached is True
    assert snapshot()["unattributed_total"] == 0


@pytest.mark.asyncio
async def test_non_enforced_path_untouched():
    _reset_counters()
    mw = AttributionEnforcementMiddleware(app=None)
    with patch.dict("os.environ", {"BRIDGE_ATTRIBUTION_ENFORCE": "true"}):
        reached, _ = await _run(mw, _scope(path="/v1/models", method="GET", headers=AUTH))
    assert reached is True
    assert snapshot()["unattributed_total"] == 0


@pytest.mark.asyncio
async def test_get_jobs_poll_not_enforced():
    """GET /v1/jobs (Poll) ist nicht kostentragend — nur POST wird gemessen."""
    _reset_counters()
    mw = AttributionEnforcementMiddleware(app=None)
    with patch.dict("os.environ", {"BRIDGE_ATTRIBUTION_ENFORCE": "true"}):
        reached, _ = await _run(mw, _scope(path="/v1/jobs", method="GET", headers=AUTH))
    assert reached is True
    assert snapshot()["unattributed_total"] == 0


@pytest.mark.asyncio
async def test_client_id_fallback_names_the_app():
    _reset_counters()
    mw = AttributionEnforcementMiddleware(app=None)
    headers = {"authorization": "Bearer k", "x-client-id": "werking-report/check/analyze"}
    with patch.dict("os.environ", {"BRIDGE_ATTRIBUTION_ENFORCE": "false"}):
        await _run(mw, _scope(headers=headers))
    assert snapshot()["unattributed_by_app"]["werking-report"]["/v1/chat/completions"] == 1


def test_enforced_paths_cover_the_money_surface():
    # Kern-Endpunkte, die nie aus dem Enforce-Set fallen dürfen
    for p in ("/v1/chat/completions", "/v1/research", "/v1/jobs", "/v1/convert-html-to-pdf"):
        assert p in ENFORCED_PATHS


# ---------------------------------------------------------------------------
# Call-Site-Granularität (X-Agent-ID / X-Client-ID im Leak-Detail)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_unattributed_sources_capture_agent_and_client():
    """Ein Leak trägt agent+client im Source-Detail — Zuordnung ohne Log-Forensik."""
    _reset_counters()
    mw = AttributionEnforcementMiddleware(app=None)
    headers = {**AUTH, "x-agent-id": "pdf-export", "x-client-id": "engelmann/export/pdf"}
    with patch.dict("os.environ", {"BRIDGE_ATTRIBUTION_ENFORCE": "false"}):
        await _run(mw, _scope(headers=headers))
        await _run(mw, _scope(headers=headers))
    snap = snapshot()
    assert snap["unattributed_total"] == 2
    assert snap["unattributed_sources"] == [{
        "app_id": "test-app",
        "path": "/v1/chat/completions",
        "agent_id": "pdf-export",
        "client_id": "engelmann/export/pdf",
        "count": 2,
    }]


@pytest.mark.asyncio
async def test_unattributed_sources_dash_when_headers_absent():
    _reset_counters()
    mw = AttributionEnforcementMiddleware(app=None)
    with patch.dict("os.environ", {"BRIDGE_ATTRIBUTION_ENFORCE": "false"}):
        await _run(mw, _scope(headers=AUTH))
    src = snapshot()["unattributed_sources"]
    assert src == [{
        "app_id": "test-app",
        "path": "/v1/chat/completions",
        "agent_id": "-",
        "client_id": "-",
        "count": 1,
    }]


@pytest.mark.asyncio
async def test_unattributed_sources_distinguish_call_sites():
    """Zwei Call-Sites derselben App bleiben im Detail getrennt (by_app aggregiert)."""
    _reset_counters()
    mw = AttributionEnforcementMiddleware(app=None)
    with patch.dict("os.environ", {"BRIDGE_ATTRIBUTION_ENFORCE": "false"}):
        await _run(mw, _scope(headers={**AUTH, "x-client-id": "test-app/site-a"}))
        await _run(mw, _scope(headers={**AUTH, "x-client-id": "test-app/site-b"}))
    snap = snapshot()
    assert snap["unattributed_by_app"]["test-app"]["/v1/chat/completions"] == 2
    clients = {s["client_id"] for s in snap["unattributed_sources"]}
    assert clients == {"test-app/site-a", "test-app/site-b"}
