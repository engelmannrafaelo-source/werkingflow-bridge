"""Tests für resolve_ledger_cost + den usage_events-Write-Pfad.

Schützt die 1:1-Abrechnungs-Invariante: Bedrock-Calls werden bei AWS
pro Token bezahlt — unabhängig vom Plan des Tenants. Eine Bedrock-Row
mit real_cost_eur=0 bedeutet: das Billing-Audit liest €0 während die
AWS-Rechnung wächst. Genau dieser Bug hat bis 2026-07-05 existiert
(flat_rate_estimated nullte real_cost für JEDEN Provider).

Coverage:
- resolve_ledger_cost: alle 4 (billing_mode × provider)-Quadranten
- Invariante: bedrock trägt real cost für JEDEN billing_mode-Text
- persist_ai_call_activity: die Geldzeile eines bedrock-Calls trägt
  real_cost > 0 und aws_request_id in provider_metadata (subscription-
  Tenant!), anthropic-Row desselben Tenants trägt 0.
  Geprüft wird, was der Worker an platform-api zu schreiben verlangt —
  seit ADR-0009 Schritt 2c hält er selbst keine DB-Verbindung mehr.
  Die Kostenbindung ist dabei absichtlich WORKER-seitig geblieben
  (resolve_ledger_cost): platform-api protokolliert die Entscheidung,
  es trifft sie nicht.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import os
os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

import pytest

from src.activity.ai_call_writer import persist_ai_call_activity, resolve_ledger_cost


# ---------------------------------------------------------------------------
# resolve_ledger_cost — pure mapping
# ---------------------------------------------------------------------------

COST = 0.123456


@pytest.mark.parametrize(
    "billing_mode_text,provider,expected_enum,expected_real",
    [
        ("subscription", "anthropic", "flat_rate_estimated", 0.0),
        ("subscription", "bedrock", "flat_rate_estimated", COST),
        ("subscription", "research-cloud", "flat_rate_estimated", COST),
        ("pay_per_token", "anthropic", "pay_per_token", COST),
        ("pay_per_token", "bedrock", "pay_per_token", COST),
        ("pay_per_token", "research-cloud", "pay_per_token", COST),
    ],
)
def test_resolve_ledger_cost_quadrants(
    billing_mode_text, provider, expected_enum, expected_real
):
    bm_enum, real = resolve_ledger_cost(billing_mode_text, provider, COST)
    assert bm_enum == expected_enum
    assert real == expected_real


@pytest.mark.parametrize(
    "billing_mode_text",
    ["subscription", "pay_per_token", "", "unknown-future-mode", "trial"],
)
def test_bedrock_always_carries_real_cost(billing_mode_text):
    """Die Invariante: egal welcher (auch zukünftiger) billing_mode-Text —
    ein Bedrock-Call darf NIE mit real_cost=0 im Ledger landen."""
    _, real = resolve_ledger_cost(billing_mode_text, "bedrock", COST)
    assert real == COST, (
        f"bedrock call with billing_mode={billing_mode_text!r} lost its real "
        f"cost — AWS bills this per token, the ledger must show it"
    )


@pytest.mark.parametrize(
    "billing_mode_text",
    ["subscription", "pay_per_token", "", "unknown-future-mode", "trial"],
)
def test_research_cloud_always_carries_real_cost(billing_mode_text):
    """Same invariant as Bedrock: research-cloud is pay-per-use to Anthropic
    directly (own API key, no subscription coverage) — real_cost must never
    be zeroed regardless of the tenant's billing_mode text."""
    _, real = resolve_ledger_cost(billing_mode_text, "research-cloud", COST)
    assert real == COST, (
        f"research-cloud call with billing_mode={billing_mode_text!r} lost its "
        f"real cost — Anthropic bills this per token, the ledger must show it"
    )


def test_subscription_anthropic_stays_free():
    """Gegenprobe: Abo-Kunden über unsere Anthropic-Abos = 0 Grenzkosten.
    Wenn das kippt, würde das Admin-Panel Phantom-Kosten anzeigen."""
    _, real = resolve_ledger_cost("subscription", "anthropic", COST)
    assert real == 0.0


# ---------------------------------------------------------------------------
# persist_ai_call_activity — der echte Write-Pfad (Naht zu platform-api gemockt)
# ---------------------------------------------------------------------------

USER_ID = str(uuid.uuid4())
TENANT_ID = str(uuid.uuid4())


async def _run_persist(seam, provider: str, provider_meta=None, search_count: int = 0):
    seam.context = {"tenantId": TENANT_ID, "billingMode": "subscription"}
    with patch("src.activity.ai_call_writer._deduct_call_cost", new=AsyncMock()):
        await persist_ai_call_activity(
            app_id="werking-report",
            user_id=USER_ID,
            agent_id=None,
            workflow_id=None,
            model="claude-haiku-4-5-20251001",
            input_tokens=1000,
            output_tokens=500,
            status="success",
            duration_ms=800,
            app_env="prod",
            provider=provider,
            provider_meta=provider_meta,
            region="eu-central-1" if provider == "bedrock" else None,
            search_count=search_count,
        )
    return seam.row


@pytest.mark.asyncio
async def test_bedrock_row_carries_real_cost_for_subscription_tenant(ledger_seam):
    aws_request_id = str(uuid.uuid4())
    row = await _run_persist(
        ledger_seam,
        "bedrock",
        provider_meta={
            "bedrock_model_id": "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
            "region": "eu-central-1",
            "aws_request_id": aws_request_id,
        },
    )

    assert row["provider"] == "bedrock"
    assert row["billing_mode"] == "flat_rate_estimated"  # Kundenvertrag bleibt Abo …
    assert row["real_cost_eur"] > 0, "bedrock row wrote €0 real cost — 1:1 audit is blind"
    # … aber unsere AWS-Kosten sind voll da
    assert row["real_cost_eur"] == row["hypothetical_cost_eur"]
    assert row["provider_metadata"]["aws_request_id"] == aws_request_id, (
        "aws_request_id missing — call-level join with AWS invocation logs broken"
    )


@pytest.mark.asyncio
async def test_research_cloud_row_carries_real_cost_for_subscription_tenant(ledger_seam):
    row = await _run_persist(
        ledger_seam,
        "research-cloud",
        provider_meta={"searches": 12, "fetches": 3, "container_id": "container-abc"},
    )

    assert row["provider"] == "research-cloud"
    assert row["billing_mode"] == "flat_rate_estimated"  # Kundenvertrag bleibt Abo …
    assert row["real_cost_eur"] > 0, (
        "research-cloud row wrote €0 real cost — 1:1 audit is blind"
    )
    # … aber die Anthropic-API-Kosten sind voll da
    assert row["real_cost_eur"] == row["hypothetical_cost_eur"]
    assert row["provider_metadata"]["searches"] == 12


@pytest.mark.asyncio
async def test_search_count_adds_to_real_cost(ledger_seam):
    """web_search fees must show up in the booked cost, not just tokens."""
    ohne = await _run_persist(ledger_seam, "research-cloud", search_count=0)
    real_ohne = ohne["real_cost_eur"]
    ledger_seam.ledger_calls.clear()
    mit = await _run_persist(ledger_seam, "research-cloud", search_count=15)

    assert mit["real_cost_eur"] > real_ohne, (
        "15 web_search calls did not increase real_cost_eur — search fee not billed"
    )


@pytest.mark.asyncio
async def test_anthropic_row_stays_free_for_subscription_tenant(ledger_seam):
    row = await _run_persist(ledger_seam, "anthropic")
    assert row["provider"] == "anthropic"
    assert row["real_cost_eur"] == 0.0
    assert row["hypothetical_cost_eur"] > 0  # hypothetical bleibt bepreist
