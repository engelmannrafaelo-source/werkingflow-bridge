"""Finanzierungsquelle + Ledger-Luecke (KD 03.11.2026), ohne Netz.

Contract: docs/contracts/ledger-finanzierungsquelle.md (Pyramiden-Szenario).
tests/billing/test_ledger_real_cost.py bleibt bewusst unberuehrt (Hausregel);
die neue Regel lebt hier.

Covers:
  * resolve_ledger_cost: research-cloud + Pool-Nachweis -> real 0, hyp bleibt;
    ohne Nachweis / nur App-Marker / Bedrock / pay_per_token unveraendert.
  * credit_pool: jede committete Reservierung landet im Buchungsprotokoll,
    ohne Konto/Org/Schluessel in der Ledger-Meta.
  * persist_ai_call_activity: Pool-Buchung ohne tenants-Zeile wird unter der
    anonymen Identitaet mit no_tenant=true geschrieben, ohne Abzug; andere
    tenantlose Calls bleiben skipped:no_tenant; fehlende Identitaet = owed.
  * Hop (ADR-0011): fremde Herkunft -> bridge_origin/bridge_executed_on.
  * Szenario: Recherche mit drei Pool-Runden ohne Mandant -> genau eine Zeile
    mit allen Reservierungen, workflow_id, Job-ID, real 0.
"""
from __future__ import annotations

import sys
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

for _mod in ["src.db.client"]:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

import src.activity.ai_call_writer as writer  # noqa: E402
from src.activity import ledger_client, ledger_spool  # noqa: E402
from src.activity.providers import (  # noqa: E402
    PROVIDER_ANTHROPIC,
    PROVIDER_BEDROCK,
    PROVIDER_RESEARCH_CLOUD,
)
from src.attribution import ANONYMOUS_USER_ID  # noqa: E402
from src.providers import credit_pool, funding_policy  # noqa: E402

USER = "11111111-1111-4111-8111-111111111111"
TENANT = "tenant-a"
ANON_TENANT = "bridge-anonymous"
POOL_META = {
    "funding_policy": "promotional_only",
    "automatic_paid_fallback": False,
    "credit_pool_reservations": ["r1", "r2"],
    "credit_pool_grants": ["g1"],
}


# ── resolve_ledger_cost ───────────────────────────────────────────────────

def test_research_cloud_pool_funded_books_real_zero():
    assert writer.resolve_ledger_cost(
        "subscription", PROVIDER_RESEARCH_CLOUD, 12.5, provider_meta=POOL_META
    ) == ("flat_rate_estimated", 0.0)


def test_research_cloud_single_reservation_shape_counts():
    meta = {"funding_policy": "promotional_only", "credit_pool_reservation": "r9"}
    assert writer.resolve_ledger_cost("subscription", PROVIDER_RESEARCH_CLOUD, 3.0,
                                      provider_meta=meta)[1] == 0.0


def test_research_cloud_without_pool_evidence_keeps_real_cost():
    assert writer.resolve_ledger_cost("subscription", PROVIDER_RESEARCH_CLOUD, 4.0) == (
        "flat_rate_estimated", 4.0)
    # Policy marker alone (stampable from the app) is NOT evidence.
    assert writer.resolve_ledger_cost(
        "subscription", PROVIDER_RESEARCH_CLOUD, 4.0,
        provider_meta={"funding_policy": "promotional_only"})[1] == 4.0
    # Reservation without the promotional policy is not evidence either.
    assert writer.resolve_ledger_cost(
        "subscription", PROVIDER_RESEARCH_CLOUD, 4.0,
        provider_meta={"credit_pool_reservations": ["r1"]})[1] == 4.0
    assert writer.resolve_ledger_cost(
        "subscription", PROVIDER_RESEARCH_CLOUD, 4.0,
        provider_meta={"funding_policy": "promotional_only", "credit_pool_reservations": []})[1] == 4.0


def test_other_providers_and_pay_per_token_unchanged():
    assert writer.resolve_ledger_cost("subscription", PROVIDER_BEDROCK, 2.0,
                                      provider_meta=POOL_META) == ("flat_rate_estimated", 2.0)
    assert writer.resolve_ledger_cost("subscription", PROVIDER_ANTHROPIC, 2.0,
                                      provider_meta=POOL_META) == ("flat_rate_estimated", 0.0)
    assert writer.resolve_ledger_cost("pay_per_token", PROVIDER_RESEARCH_CLOUD, 2.0,
                                      provider_meta=POOL_META) == ("pay_per_token", 2.0)


def test_pool_reservations_helper_ignores_garbage():
    assert funding_policy.pool_reservations(None) == []
    assert funding_policy.pool_reservations({"credit_pool_reservations": "r1"}) == []
    assert funding_policy.pool_reservations(
        {"credit_pool_reservation": "a", "credit_pool_reservations": ["b", None, ""]}) == ["a", "b"]


# ── credit_pool booking log ───────────────────────────────────────────────

def test_booking_ledger_meta_has_no_account_identity():
    meta = credit_pool.booking_ledger_meta(
        [{"reservation": "r1", "grant": "g1"}, {"reservation": "r2", "grant": "g1"},
         {"reservation": "r3", "grant": "g2"}])
    assert meta == {
        "funding_policy": "promotional_only", "automatic_paid_fallback": False,
        "credit_pool_reservations": ["r1", "r2", "r3"], "credit_pool_grants": ["g1", "g2"],
    }
    assert credit_pool.booking_ledger_meta([]) == {}
    assert credit_pool.booking_ledger_meta(None) == {}


def _fake_pool(monkeypatch, *, message_status: int = 200):
    """Coordinator + Anthropic doubles, no network."""
    calls: List[tuple] = []

    async def coordinator(method, path, data=None):
        calls.append((method, path, data))
        if path == "/accounts":
            return {"accounts": [{"id": "acc-label", "org_id": "org-secret", "grant_id": "g1",
                                  "api_key": "sk-never-in-ledger"}]}
        return {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/count_tokens"):
            return httpx.Response(200, json={"input_tokens": 10})
        return httpx.Response(message_status, json={
            "id": "m", "type": "message", "role": "assistant", "model": "claude-x",
            "content": [{"type": "text", "text": "ok"}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 5}})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(credit_pool, "coordinator", coordinator)
    monkeypatch.setattr(credit_pool.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler)))
    return calls


async def test_post_messages_notes_every_committed_reservation(monkeypatch):
    _fake_pool(monkeypatch)
    log = credit_pool.start_booking_log()
    body = {"model": "claude-x", "max_tokens": 10, "messages": [{"role": "user", "content": "q"}]}
    _, meta1 = await credit_pool.post_messages(body, 5)
    _, meta2 = await credit_pool.post_messages(body, 5)
    assert [e["reservation"] for e in log] == [meta1["credit_pool_reservation"],
                                               meta2["credit_pool_reservation"]]
    ledger = credit_pool.booking_ledger_meta(log)
    flat = repr(ledger)
    assert "acc-label" not in flat and "org-secret" not in flat and "sk-never" not in flat
    assert ledger["credit_pool_grants"] == ["g1"]


async def test_post_messages_without_log_is_unchanged(monkeypatch):
    _fake_pool(monkeypatch)
    body = {"model": "claude-x", "max_tokens": 10, "messages": [{"role": "user", "content": "q"}]}
    credit_pool._booking_log.set(None)  # no booking log active → nothing collected, no error
    resp, meta = await credit_pool.post_messages(body, 5)
    assert resp.status_code == 200 and meta["credit_pool_reservation"]


# ── persist_ai_call_activity ──────────────────────────────────────────────

class _Seam:
    def __init__(self, contexts: Dict[str, Optional[dict]]):
        self.contexts = contexts
        self.rows: List[Dict[str, Any]] = []
        self.asked: List[str] = []

    async def load_billing_context(self, user_id):
        self.asked.append(user_id)
        return self.contexts.get(user_id)

    async def write_ai_call(self, payload):
        self.rows.append(payload)
        return "written"

    async def anonymous_identity_present(self):
        return True


@pytest.fixture
def seam():
    s = _Seam({ANONYMOUS_USER_ID: {"tenantId": ANON_TENANT, "billingMode": "subscription"}})
    deduct = AsyncMock(return_value=True)
    with (patch.object(ledger_client, "load_billing_context", s.load_billing_context),
          patch.object(ledger_client, "write_ai_call", s.write_ai_call),
          patch.object(ledger_client, "anonymous_identity_present", s.anonymous_identity_present),
          patch.object(ledger_spool, "spool_enabled", return_value=False),
          patch.object(writer, "_deduct_call_cost", deduct),
          patch.object(writer, "cost_eur", MagicMock(return_value=1.5))):
        s.deduct = deduct
        writer._skip_counts.clear()
        yield s


async def _persist(provider=PROVIDER_ANTHROPIC, meta=None, user=USER, **kw):
    return await writer.persist_ai_call_activity(
        app_id="werking-energy", user_id=user, agent_id="a", workflow_id=kw.get("wf", "wf-1"),
        model="claude-sonnet-5", input_tokens=10, output_tokens=5, status="success",
        duration_ms=1, app_env="staging", provider=provider, provider_meta=meta,
    )


async def test_pool_booking_without_tenant_writes_marked_row(seam):
    meta = {"credit_pool_reservation": "r1", "credit_pool_grant": "g1",
            "funding_policy": "promotional_only"}
    out = await _persist(meta=meta)
    assert out == "written"
    row = seam.rows[0]
    assert row["tenant_id"] == ANON_TENANT and row["actor_user_id"] == ANONYMOUS_USER_ID
    pm = row["provider_metadata"]
    assert pm["no_tenant"] is True and pm["no_tenant_user_id"] == USER
    assert pm["credit_pool_reservation"] == "r1" and pm["workflow_id"] == "wf-1"
    assert row["audit_payload"]["noTenant"] is True
    assert row["hypothetical_cost_eur"] == 1.5 and row["real_cost_eur"] == 0.0
    seam.deduct.assert_not_awaited()  # no customer budget behind the anonymous identity


async def test_research_pool_row_without_tenant_real_zero(seam):
    await _persist(provider=PROVIDER_RESEARCH_CLOUD, meta=dict(POOL_META))
    row = seam.rows[0]
    assert row["real_cost_eur"] == 0.0 and row["hypothetical_cost_eur"] == 1.5
    assert row["provider_metadata"]["credit_pool_reservations"] == ["r1", "r2"]


async def test_non_pool_call_without_tenant_still_skipped(seam):
    out = await _persist(meta={"usage_source": "cli"})
    assert out == "skipped:no_tenant" and seam.rows == []
    assert seam.asked == [USER]  # the anonymous identity is not even asked


async def test_pool_booking_without_anonymous_identity_stays_owed(seam):
    seam.contexts.pop(ANONYMOUS_USER_ID)
    out = await _persist(meta={"credit_pool_reservation": "r1", "funding_policy": "promotional_only"})
    assert out == "failed" and seam.rows == []


async def test_platform_unreachable_on_fallback_never_raises(seam):
    from src.platform_client import PlatformUnavailable

    async def boom(user_id):
        if user_id == ANONYMOUS_USER_ID:
            raise PlatformUnavailable("down (test)")
        return None

    with patch.object(ledger_client, "load_billing_context", boom):
        out = await _persist(meta={"credit_pool_reservation": "r1"})
    assert out == "failed"  # owed, user-facing call unaffected


async def test_tenant_present_row_unchanged_and_deducted(seam):
    seam.contexts[USER] = {"tenantId": TENANT, "billingMode": "subscription"}
    await _persist(provider=PROVIDER_RESEARCH_CLOUD, meta={"depth": "quick"})
    row = seam.rows[0]
    assert row["tenant_id"] == TENANT and row["real_cost_eur"] == 1.5
    pm = row["provider_metadata"]
    assert "no_tenant" not in pm and "bridge_origin" not in pm
    seam.deduct.assert_awaited_once()


async def test_hopped_call_names_executing_bridge(seam, monkeypatch):
    from src import federation
    seam.contexts[USER] = {"tenantId": TENANT, "billingMode": "subscription"}
    monkeypatch.setenv("BRIDGE_ORIGIN_ID", "dev")
    monkeypatch.setenv("INSTANCE_NAME", "worker2")
    federation.set_request_origin("prod")
    try:
        await _persist(meta={"credit_pool_reservation": "r1"})
    finally:
        federation.set_request_origin(None)
    pm = seam.rows[0]["provider_metadata"]
    assert pm["bridge_origin"] == "prod" and pm["bridge_executed_on"] == "dev/worker2"


def _import_main():
    for mod in ["claude_code_sdk", "claude_code_sdk._errors", "claude_code_sdk._internal",
                "claude_code_sdk._internal.client", "src.identity.routes", "src.db.client"]:
        if mod not in sys.modules:
            sys.modules[mod] = MagicMock()
    import src.main
    return src.main


async def test_failed_document_still_books_its_pool_vision_calls():
    """describe_in_worker ran triage + 1 description, then the deadline hit
    (504): the two pool calls are booked (status success, document_status
    error), followed by the unchanged docling error row."""
    from fastapi.responses import JSONResponse
    main = _import_main()
    req = MagicMock()
    req.state = MagicMock(spec=[])
    log = [
        {"kind": "triage", "model": "claude-haiku", "input_tokens": 50, "output_tokens": 5,
         "credit_meta": {"credit_pool_reservation": "r1"}},
        {"kind": "describe", "model": "claude-haiku", "input_tokens": 70, "output_tokens": 30,
         "credit_meta": {"credit_pool_reservation": "r2"}},
    ]

    async def proxy(request, *a, **kw):
        request.state.document_vision_usage = None
        request.state.document_vision_usage_failed = log
        return JSONResponse(status_code=504, content={"status": "error", "error": "Frist"})

    persist = AsyncMock()
    with (patch.object(main, "_proxy_document_endpoint", proxy),
          patch.object(main, "verify_api_key", AsyncMock()),
          patch.object(main, "extract_attribution_context",
                       return_value={"app_id": "werking-energy", "user_id": USER, "workflow_id": "wf"}),
          patch("src.activity.ai_call_writer.persist_ai_call_activity", persist)):
        out = await main.convert_document_endpoint(req, None)
    assert out.status_code == 504
    rows = [c.kwargs for c in persist.await_args_list]
    assert len(rows) == 2
    vision, docling = rows
    assert vision["status"] == "success" and vision["input_tokens"] == 120
    assert vision["provider_meta"]["credit_pool_reservations"] == ["r1", "r2"]
    assert vision["provider_meta"]["document_status"] == "error"
    assert docling["model"] == "docling" and docling["status"] == "error"


# ── Szenario (Pyramide, docs/contracts/ledger-finanzierungsquelle.md) ────

async def test_szenario_pool_recherche_ohne_mandant_eine_zeile_je_lauf(seam, monkeypatch):
    """Nachbau 02.11. (phasentest): Recherche-Lauf mit drei Pool-Runden, Nutzer
    ohne tenants-Zeile. Vorher: 0 Zeilen, Reservierungen ohne Job. Jetzt: genau
    eine Zeile mit allen drei Reservierungen, workflow_id und Job-ID, real 0."""
    main = _import_main()

    _fake_pool(monkeypatch)
    log = credit_pool.start_booking_log()
    body = {"model": "claude-x", "max_tokens": 10, "messages": [{"role": "user", "content": "q"}]}
    for _ in range(3):
        await credit_pool.post_messages(body, 5)
    req = MagicMock()
    req.headers = {"X-Bridge-Job-Lauf": "job-42"}
    meta = main._research_pool_ledger_meta(req, log)
    await _persist(provider=PROVIDER_RESEARCH_CLOUD, meta=meta, wf="muehl-pt-p3")
    assert len(seam.rows) == 1
    row = seam.rows[0]
    pm = row["provider_metadata"]
    assert len(pm["credit_pool_reservations"]) == 3
    assert pm["bridge_job_id"] == "job-42" and pm["workflow_id"] == "muehl-pt-p3"
    assert pm["no_tenant"] is True and row["real_cost_eur"] == 0.0
    assert row["hypothetical_cost_eur"] > 0
