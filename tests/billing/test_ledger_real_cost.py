"""Tests für resolve_ledger_cost + den usage_events-Write-Pfad.

Schützt die 1:1-Abrechnungs-Invariante: Bedrock-Calls werden bei AWS
pro Token bezahlt — unabhängig vom Plan des Tenants. Eine Bedrock-Row
mit real_cost_eur=0 bedeutet: das Billing-Audit liest €0 während die
AWS-Rechnung wächst. Genau dieser Bug hat bis 2026-07-05 existiert
(flat_rate_estimated nullte real_cost für JEDEN Provider).

Coverage:
- resolve_ledger_cost: alle 4 (billing_mode × provider)-Quadranten
- Invariante: bedrock trägt real cost für JEDEN billing_mode-Text
- persist_ai_call_activity: INSERT-Row eines bedrock-Calls trägt
  real_cost > 0 und aws_request_id in provider_metadata (subscription-
  Tenant!), anthropic-Row desselben Tenants trägt 0
"""
from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

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
        ("pay_per_token", "anthropic", "pay_per_token", COST),
        ("pay_per_token", "bedrock", "pay_per_token", COST),
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


def test_subscription_anthropic_stays_free():
    """Gegenprobe: Abo-Kunden über unsere Anthropic-Abos = 0 Grenzkosten.
    Wenn das kippt, würde das Admin-Panel Phantom-Kosten anzeigen."""
    _, real = resolve_ledger_cost("subscription", "anthropic", COST)
    assert real == 0.0


# ---------------------------------------------------------------------------
# persist_ai_call_activity — der echte Write-Pfad (DB gemockt)
# ---------------------------------------------------------------------------

USER_ID = str(uuid.uuid4())
TENANT_ID = str(uuid.uuid4())


def _mock_pool():
    """Pool-Mock nach dem Muster von test_project_credits: fetchrow liefert
    den tenant-lookup (subscription!), execute fängt die INSERTs."""
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(
        return_value={"tenant_id": TENANT_ID, "billing_mode": "subscription"}
    )

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool, conn


def _usage_insert_args(conn):
    """Extrahiert die Args des usage_events-INSERTs aus allen execute-Calls."""
    for call in conn.execute.call_args_list:
        sql = call.args[0]
        if "INSERT INTO usage_events" in sql:
            return sql, call.args[1:]
    raise AssertionError("no usage_events INSERT executed")


async def _run_persist(provider: str, provider_meta=None):
    pool, conn = _mock_pool()
    with patch("src.activity.ai_call_writer.get_pool", return_value=pool), patch(
        "src.activity.ai_call_writer._deduct_call_cost", new=AsyncMock()
    ):
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
        )
    return conn


@pytest.mark.asyncio
async def test_bedrock_row_carries_real_cost_for_subscription_tenant():
    aws_request_id = str(uuid.uuid4())
    conn = await _run_persist(
        "bedrock",
        provider_meta={
            "bedrock_model_id": "eu.anthropic.claude-haiku-4-5-20251001-v1:0",
            "region": "eu-central-1",
            "aws_request_id": aws_request_id,
        },
    )
    sql, args = _usage_insert_args(conn)

    # Positionsbindung siehe INSERT in ai_call_writer:
    # $6=provider, $12=billing_mode, $13=real_cost, $14=hypothetical, $16=metadata
    provider = args[5]
    bm_enum = args[11]
    real_cost = args[12]
    hypothetical = args[13]
    metadata = json.loads(args[15])

    assert provider == "bedrock"
    assert bm_enum == "flat_rate_estimated"  # Kundenvertrag bleibt Abo …
    assert real_cost > 0, "bedrock row wrote €0 real cost — 1:1 audit is blind"
    assert real_cost == hypothetical  # … aber unsere AWS-Kosten sind voll da
    assert metadata["aws_request_id"] == aws_request_id, (
        "aws_request_id missing — call-level join with AWS invocation logs broken"
    )


@pytest.mark.asyncio
async def test_anthropic_row_stays_free_for_subscription_tenant():
    conn = await _run_persist("anthropic")
    _, args = _usage_insert_args(conn)
    assert args[5] == "anthropic"
    assert args[12] == 0.0  # real_cost
    assert args[13] > 0  # hypothetical bleibt bepreist
