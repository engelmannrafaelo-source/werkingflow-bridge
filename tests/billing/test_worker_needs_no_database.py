"""Das Abnahmekriterium von ADR-0009 Schritt 2c, direkt geprüft:

    Ein Worker fährt den kompletten Geldpfad — Abrechnungszeile UND
    Budget-Abzug — ohne BRIDGE_DB_URL.

Das ist der Punkt der ganzen Übung. Zieht ein Worker auf einen eigenen Host,
darf er die Kundendatenbank nicht mehr brauchen; sonst müsste ein roher
Postgres-Port übers Netz geöffnet werden, was dem Zweck des Umzugs
zuwiderläuft.

Die Prüfung ist bewusst brutal: `get_pool` wird so verbogen, dass JEDER Zugriff
auf den Verbindungspool den Test sprengt. Ein übersehener DB-Aufruf auf dem
Pfad kann sich damit nicht als "geht doch" tarnen — auf einem Entwickler-Rechner
MIT Datenbank wäre er sonst unsichtbar und fiele erst nach dem Umzug auf.
"""
from __future__ import annotations

import os
import uuid
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

from src.activity import ai_call_writer as writer
from src.activity import ledger_client
from src.activity.providers import PROVIDER_ANTHROPIC
from src.platform_client import PlatformResponse

USER_ID = str(uuid.uuid4())
TENANT_ID = str(uuid.uuid4())


class _PlatformApi:
    """Beantwortet jeden Innen-API-Aufruf und merkt sich die Pfade."""

    def __init__(self) -> None:
        self.paths: list[str] = []
        self.bodies: dict = {}

    async def __call__(self, method, path, *, json=None, params=None, **kw):
        self.paths.append(path)
        self.bodies[path] = json

        if path == "/v1/internal/identity/anonymous":
            return PlatformResponse(200, {"present": True})
        if path.endswith("/billing-context"):
            return PlatformResponse(
                200, {"context": {"tenantId": TENANT_ID, "billingMode": "subscription"}}
            )
        if path == "/v1/internal/usage/ai-call":
            return PlatformResponse(200, {"outcome": "written", "auditWritten": True})
        if path == "/v1/internal/app-tier-policy":
            return PlatformResponse(200, {"policy": None})
        if path == "/v1/budget/deduct":
            return PlatformResponse(
                200,
                {
                    "fromMonthly": 0.01, "fromTopUp": 0.0, "newMonthlyUsed": 0.01,
                    "newTopUpBalance": 0.0, "effectivePlanId": "report-standard",
                },
            )
        raise AssertionError(f"unerwarteter platform-api-Aufruf: {method} {path}")


# Module auf dem Geldpfad, die `get_pool` beim IMPORT an sich binden. Sie
# einzeln zu verbiegen ist nicht Umständlichkeit, sondern nötig: ein
# `from x import y` kopiert die Referenz, ein Patch auf src.db.client allein
# ginge an ihnen vorbei — und der Test wäre eine Attrappe, die alles bestätigt.
_POOL_BINDINGS = (
    "src.db.client.get_pool",                      # späte Importer (in Funktionen)
    "src.activity.ledger_db.get_pool",             # platform-api-Seite
    "src.identity.user_resolver.get_pool",         # E-Mail-Identität (Fallback)
    "src.billing.project_budgets_service.get_pool",  # Projekt-Topf (Fallback)
    "src.budget.routes.get_pool",                  # Monats-Topf
    "src.principals.get_pool",
)


class _NoDatabase:
    """Jeder Griff zum Verbindungspool ist ein Testfehler."""

    def __enter__(self):
        def _boom(*_a, **_kw):
            raise AssertionError(
                "der Geldpfad hat get_pool() angefasst — genau das darf ein "
                "Worker ohne BRIDGE_DB_URL nicht mehr tun"
            )

        self._patches = [patch(t, side_effect=_boom) for t in _POOL_BINDINGS]
        for pt in self._patches:
            pt.start()
        return self

    def __exit__(self, *exc):
        for pt in reversed(self._patches):
            pt.stop()
        return False


def _no_database():
    return _NoDatabase()


def _platform(api):
    """call_platform wird an zwei Stellen gebunden: ledger_client importiert es
    beim Modul-Import, die übrigen Aufrufer erst im Funktionsrumpf."""
    return (
        patch.object(ledger_client, "call_platform", new=api),
        patch("src.platform_client.call_platform", new=api),
    )


async def _run(**over):
    api = _PlatformApi()
    p1, p2 = _platform(api)
    args = dict(
        provider=PROVIDER_ANTHROPIC,
        app_id="werking-report",
        user_id=USER_ID,
        agent_id=None,
        workflow_id=None,
        model="claude-sonnet-5",
        input_tokens=1000,
        output_tokens=500,
        status="success",
        duration_ms=800,
        app_env="prod",
        _call_uid="uid-dbfree",
        _call_ts=1_700_000_000.0,
    )
    args.update(over)
    with _no_database(), p1, p2:
        outcome = await writer.persist_ai_call_activity(**args)
    return outcome, api


@pytest.mark.asyncio
async def test_full_money_path_runs_without_a_database(monkeypatch):
    """Abrechnungszeile geschrieben und Abzug gebucht — ohne einen einzigen
    DB-Zugriff."""
    monkeypatch.delenv("BRIDGE_DB_URL", raising=False)
    monkeypatch.setenv("BRIDGE_LEDGER_SPOOL_ENABLED", "false")

    plan = type("P", (), {"id": "report-standard", "interval": "month"})()
    with patch(
        "src.budget.plan_resolution.resolve_billing_plan",
        new=AsyncMock(return_value=plan),
    ):
        outcome, api = await _run()

    assert outcome == writer.OUTCOME_WRITTEN
    assert "/v1/internal/usage/ai-call" in api.paths, "keine Geldzeile angefordert"
    assert "/v1/budget/deduct" in api.paths, "kein Budget-Abzug gebucht"


@pytest.mark.asyncio
async def test_the_money_row_carries_the_calls_origin_time_over_the_wire(monkeypatch):
    """Die Ursprungszeit überlebt den Sprung auf HTTP — sonst wandert ein Call
    vom Monatsletzten beim Nachlauf still in den nächsten Monat."""
    monkeypatch.delenv("BRIDGE_DB_URL", raising=False)
    monkeypatch.setenv("BRIDGE_LEDGER_SPOOL_ENABLED", "false")

    with patch(
        "src.budget.plan_resolution.resolve_billing_plan",
        new=AsyncMock(return_value=None),
    ):
        _, api = await _run()

    row = api.bodies["/v1/internal/usage/ai-call"]
    assert row["recorded_at"].startswith("2023-11-14T22:13:20")
    assert row["idempotency_key"] == "uid-dbfree"
    assert row["tenant_id"] == TENANT_ID


@pytest.mark.asyncio
async def test_app_tier_policy_is_asked_over_http_too(monkeypatch):
    """Die Policy entscheidet, WER zahlt (internes billing_account statt Kunde).
    Sie las bis Schritt 2c direkt aus app_tier_policies — ohne DB hätte sie
    lautlos "keine Policy" geliefert und damit den Kunden belastet."""
    monkeypatch.delenv("BRIDGE_DB_URL", raising=False)
    monkeypatch.setenv("BRIDGE_LEDGER_SPOOL_ENABLED", "false")
    monkeypatch.setenv("BRIDGE_APP_TIER_POLICY_ENABLED", "true")

    from src.routing import app_tier_policy

    app_tier_policy.invalidate_cache()
    with patch(
        "src.budget.plan_resolution.resolve_billing_plan",
        new=AsyncMock(return_value=None),
    ):
        _, api = await _run()
    app_tier_policy.invalidate_cache()

    assert "/v1/internal/app-tier-policy" in api.paths
