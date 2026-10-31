"""
/v1/sandbox/lease-token mit Projekt-/Auftragsbindung (Folgebau Assistent-Lease, 31.10.).

Vertrag:
  1. App mit NUR Projektplan (Energy) ohne projectId → 400 project_binding_required,
     kein Konto wird vergeben (fail-closed, kein stiller Durchlass).
  2. projectId + Auftragsdeckel (hard_cap, Rest 0,0001 EUR) → 402
     project_budget_exhausted (hardCap) — die Schätzung "sandbox-lease" (1,00 EUR)
     schlägt die 0-Schätzung des Lease.
  3. projectId ohne angelegten Topf (nie bezahlt / Kopie ohne Auftragsbudget) →
     402 project_budget_unallocated (require_allocation).
  4. projectId mit Topf und Rest → Lease wird vergeben; das Gate sah project_id
     und call_kind.
  5. App mit Monatsplan (Engelmann) ohne projectId → unverändert: Lease, Gate ohne
     project_id.
  6. Gate-Einheit: call_kind "sandbox-lease" nimmt bei Schätzung 0 den Standardwert
     1,00 EUR.
"""

import uuid
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from src.budget.plans import PlanConfig

USER = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
LEASE = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

ENERGY_PROJEKT = PlanConfig(
    id="energy_projekt",
    app_id="werking-energy",
    name="Energy Projekt",
    price=990.0,
    interval="project",
    api_budget_eur=100.0,
    description="",
)
ENGELMANN_MONAT = PlanConfig(
    id="engelmann_basis",
    app_id="engelmann",
    name="Engelmann",
    price=0.0,
    interval="month",
    api_budget_eur=10.0,
    description="",
)
KATALOG = {ENERGY_PROJEKT.id: ENERGY_PROJEKT, ENGELMANN_MONAT.id: ENGELMANN_MONAT}


def _pool():
    conn = AsyncMock()
    conn.fetchrow.return_value = {
        "tenant_id": "tenant-x",
        "billing_mode": "subscription",
    }
    conn.execute.return_value = None
    pool = MagicMock()

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool.acquire = _acquire
    return pool, conn


class _Picked:
    account_id = "office"
    tier = "subscription"


@asynccontextmanager
async def _lease_umgebung(*, plan=None, eval_result=None):
    """Alle Nebenwege gestubbt; zurück kommen die Spione auf Gate und Kontowahl."""
    pool, _conn = _pool()
    pick = AsyncMock(return_value=_Picked())
    eval_project = AsyncMock(
        return_value=eval_result
        or {
            "exists": False,
            "allowed": False,
            "remainingEur": 0.0,
            "topUpRemainingEur": 0.0,
        }
    )
    resolve_plan = AsyncMock(return_value=plan)
    with (
        patch("src.sandbox.routes.resolve_user_id", AsyncMock(return_value=USER)),
        patch("src.sandbox.routes.get_pool", return_value=pool),
        patch(
            "src.sandbox.routes._ls.get_tenant_info",
            AsyncMock(
                return_value={"tenant_id": "tenant-x", "billing_mode": "subscription"}
            ),
        ),
        patch(
            "src.sandbox.routes._ls.get_recent_lease_counts", AsyncMock(return_value={})
        ),
        patch("src.sandbox.routes._ar.pick_account", pick),
        patch("src.sandbox.routes._ls.read_oauth_token", return_value="sk-fake"),
        patch("src.sandbox.routes._ls.create_lease", AsyncMock(return_value=LEASE)),
        patch("src.budget.plans.PLANS", KATALOG),
        patch("src.budget.gate.resolve_user_id", AsyncMock(return_value=USER)),
        patch("src.budget.gate.is_foreign_origin", return_value=False),
        patch("src.budget.gate.resolve_billing_plan", resolve_plan),
        patch(
            "src.billing.project_budgets_service.evaluate_via_platform", eval_project
        ),
    ):
        yield {"pick": pick, "eval": eval_project, "resolve_plan": resolve_plan}


async def _lease(**body):
    from src.sandbox.routes import LeaseTokenRequest, lease_token

    return await lease_token(LeaseTokenRequest(**body), _claims=MagicMock())


class TestProjektbindungPflicht:
    @pytest.mark.asyncio
    async def test_energy_ohne_projectid_wird_benannt_abgelehnt(self):
        async with _lease_umgebung(plan=ENERGY_PROJEKT) as spione:
            with pytest.raises(HTTPException) as exc:
                await _lease(userId=str(USER), app="werking-energy")
        assert exc.value.status_code == 400
        assert exc.value.detail["error"] == "project_binding_required"
        assert exc.value.detail["app"] == "werking-energy"
        spione["pick"].assert_not_awaited()  # kein Konto vergeben

    def test_bindungspflicht_nur_fuer_projektplan_ohne_monatsplan(self):
        from src.sandbox.routes import lease_braucht_projektbindung

        with patch("src.budget.plans.PLANS", KATALOG):
            assert lease_braucht_projektbindung("werking-energy") is True
            assert lease_braucht_projektbindung("engelmann") is False
            assert (
                lease_braucht_projektbindung("werking-report-coach") is False
            )  # kein Plan → ungebunden wie bisher


class TestProjektbindungAmGate:
    @pytest.mark.asyncio
    async def test_auftragsdeckel_sperrt_das_lease(self):
        h3 = {
            "exists": True,
            "allowed": True,
            "remainingEur": 0.0001,
            "topUpRemainingEur": 0.0,
            "hardCap": True,
        }
        async with _lease_umgebung(plan=ENERGY_PROJEKT, eval_result=h3) as spione:
            with pytest.raises(HTTPException) as exc:
                await _lease(
                    userId=str(USER), app="werking-energy", projectId="K1-kopie"
                )
        assert exc.value.status_code == 402
        assert exc.value.detail["reason"] == "project_budget_exhausted"
        assert exc.value.detail["hardCap"] is True
        assert exc.value.detail["projectId"] == "K1-kopie"
        assert exc.value.detail["estimatedCostEur"] == 1.00
        spione["eval"].assert_awaited_once_with(
            USER, ENERGY_PROJEKT.id, "K1-kopie", 0.0
        )
        spione["pick"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_ohne_topf_kein_lease(self):
        async with _lease_umgebung(plan=ENERGY_PROJEKT) as spione:
            with pytest.raises(HTTPException) as exc:
                await _lease(
                    userId=str(USER), app="werking-energy", projectId="nie-bezahlt"
                )
        assert exc.value.status_code == 402
        assert exc.value.detail["error"] == "project_budget_unallocated"
        assert exc.value.detail["projectId"] == "nie-bezahlt"
        spione["pick"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_mit_topf_und_rest_wird_vergeben(self):
        topf = {
            "exists": True,
            "allowed": True,
            "remainingEur": 80.0,
            "topUpRemainingEur": 0.0,
        }
        async with _lease_umgebung(plan=ENERGY_PROJEKT, eval_result=topf) as spione:
            res = await _lease(
                userId=str(USER), app="werking-energy", projectId="job-123"
            )
        assert res.leaseId == LEASE and res.accountId == "office"
        spione["resolve_plan"].assert_awaited_once_with(
            "werking-energy", USER, "job-123"
        )
        spione["pick"].assert_awaited_once()

    @pytest.mark.asyncio
    async def test_monatsplan_app_ohne_projectid_unveraendert(self):
        async with _lease_umgebung(plan=ENGELMANN_MONAT) as spione:
            with patch(
                "src.budget.gate.evaluate_budget",
                AsyncMock(return_value={"allowed": True}),
            ):
                res = await _lease(userId=str(USER), app="engelmann")
        assert res.leaseId == LEASE
        spione["resolve_plan"].assert_awaited_once_with("engelmann", USER, None)
        spione["eval"].assert_not_awaited()  # Monatsweg, kein Projekttopf
        spione["pick"].assert_awaited_once()


class TestGateSchaetzungSandboxLease:
    @pytest.mark.asyncio
    async def test_call_kind_sandbox_lease_hat_standardschaetzung(self):
        from src.budget.gate import HARD_CAP_DEFAULT_ESTIMATE_EUR, enforce_budget

        assert HARD_CAP_DEFAULT_ESTIMATE_EUR["sandbox-lease"] == 1.00
        knapp = {
            "exists": True,
            "allowed": True,
            "remainingEur": 0.5,
            "topUpRemainingEur": 0.0,
            "hardCap": True,
        }
        reicht = {
            "exists": True,
            "allowed": True,
            "remainingEur": 5.0,
            "topUpRemainingEur": 0.0,
            "hardCap": True,
        }
        with (
            patch("src.budget.plans.PLANS", KATALOG),
            patch("src.budget.gate.resolve_user_id", AsyncMock(return_value=USER)),
            patch("src.budget.gate.is_foreign_origin", return_value=False),
            patch(
                "src.budget.gate.resolve_billing_plan",
                AsyncMock(return_value=ENERGY_PROJEKT),
            ),
        ):
            with patch(
                "src.billing.project_budgets_service.evaluate_via_platform",
                AsyncMock(return_value=knapp),
            ):
                with pytest.raises(HTTPException) as exc:
                    await enforce_budget(
                        str(USER),
                        "werking-energy",
                        0.0,
                        project_id="p",
                        call_kind="sandbox-lease",
                    )
                assert (
                    exc.value.status_code == 402 and exc.value.detail["hardCap"] is True
                )
            with patch(
                "src.billing.project_budgets_service.evaluate_via_platform",
                AsyncMock(return_value=reicht),
            ):
                assert (
                    await enforce_budget(
                        str(USER),
                        "werking-energy",
                        0.0,
                        project_id="p",
                        call_kind="sandbox-lease",
                    )
                    is None
                )

    @pytest.mark.asyncio
    async def test_ohne_require_allocation_bleibt_erster_aufruf_frei(self):
        """Abrechenbare Aufrufe wie bisher: ohne Topf → durch (lazy allocation)."""
        from src.budget.gate import enforce_budget

        leer = {
            "exists": False,
            "allowed": False,
            "remainingEur": 0.0,
            "topUpRemainingEur": 0.0,
        }
        with (
            patch("src.budget.plans.PLANS", KATALOG),
            patch("src.budget.gate.resolve_user_id", AsyncMock(return_value=USER)),
            patch("src.budget.gate.is_foreign_origin", return_value=False),
            patch(
                "src.budget.gate.resolve_billing_plan",
                AsyncMock(return_value=ENERGY_PROJEKT),
            ),
            patch(
                "src.billing.project_budgets_service.evaluate_via_platform",
                AsyncMock(return_value=leer),
            ),
        ):
            assert (
                await enforce_budget(str(USER), "werking-energy", 0.3, project_id="p")
                is None
            )
            with pytest.raises(HTTPException) as exc:
                await enforce_budget(
                    str(USER),
                    "werking-energy",
                    0.0,
                    project_id="p",
                    require_allocation=True,
                )
            assert exc.value.detail["error"] == "project_budget_unallocated"


class TestNachbesserungR1:
    """R1-Nachbesserung: M1 (Lease-Identität = Topf-Eigentümer), H1 (Topf leer), H4."""

    @pytest.mark.asyncio
    async def test_m1_topf_wird_unter_der_lease_identitaet_gesucht(self):
        """Der Daemon sendet als userId den Eigentümer (billingUserId). Das Gate sucht
        den Topf genau unter dieser uid — nicht unter irgendeinem anderen Nutzer."""
        topf = {"exists": True, "allowed": True, "remainingEur": 80.0,
                "topUpRemainingEur": 0.0}
        async with _lease_umgebung(plan=ENERGY_PROJEKT, eval_result=topf) as spione:
            res = await _lease(userId=str(USER), app="werking-energy",
                               projectId="job-1")
        assert res.leaseId == LEASE
        spione["resolve_plan"].assert_awaited_once_with("werking-energy", USER, "job-1")
        spione["eval"].assert_awaited_once_with(USER, ENERGY_PROJEKT.id, "job-1", 0.0)

    @pytest.mark.asyncio
    async def test_m1_fremder_topf_bleibt_unerreichbar(self):
        """Topf unter einer ANDEREN uid (anderer Mandant/Nutzer): unter der Lease-uid
        gibt es keine Zeile → exists=False → 402, kein Konto vergeben."""
        kein_topf = {"exists": False, "allowed": False, "remainingEur": 0.0,
                     "topUpRemainingEur": 0.0}
        async with _lease_umgebung(plan=ENERGY_PROJEKT,
                                   eval_result=kein_topf) as spione:
            with pytest.raises(HTTPException) as exc:
                await _lease(userId=str(USER), app="werking-energy", projectId="fremd")
        assert exc.value.status_code == 402
        assert exc.value.detail["error"] == "project_budget_unallocated"
        spione["eval"].assert_awaited_once_with(USER, ENERGY_PROJEKT.id, "fremd", 0.0)
        spione["pick"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_h1_leerer_topf_ohne_deckel_sperrt_das_lease(self):
        leer = {"exists": True, "allowed": True, "remainingEur": 0.0,
                "topUpRemainingEur": 0.0}
        async with _lease_umgebung(plan=ENERGY_PROJEKT, eval_result=leer) as spione:
            with pytest.raises(HTTPException) as exc:
                await _lease(userId=str(USER), app="werking-energy", projectId="leer")
        assert exc.value.status_code == 402
        assert exc.value.detail["error"] == "budget_exhausted"
        assert exc.value.detail["reason"] == "project_budget_exhausted"
        assert exc.value.detail["totalRemainingEur"] == 0.0
        spione["pick"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_h1_topup_rest_traegt_das_lease(self):
        topup = {"exists": True, "allowed": True, "remainingEur": 0.0,
                 "topUpRemainingEur": 3.0}
        async with _lease_umgebung(plan=ENERGY_PROJEKT, eval_result=topup) as spione:
            res = await _lease(userId=str(USER), app="werking-energy",
                               projectId="topup")
        assert res.leaseId == LEASE
        spione["pick"].assert_awaited_once()

    @pytest.mark.asyncio
    async def test_h1_abrechenbarer_aufruf_auf_leerem_topf_unveraendert(self):
        """Ohne call_kind sandbox-lease bleibt evaluate() maßgeblich (0 <= 0 → durch,
        der Aufruf bucht nachträglich) — der bestehende Vertrag für Chat/Pipeline."""
        from src.budget.gate import enforce_budget
        leer = {"exists": True, "allowed": True, "remainingEur": 0.0,
                "topUpRemainingEur": 0.0}
        with patch("src.budget.plans.PLANS", KATALOG), \
             patch("src.budget.gate.resolve_user_id", AsyncMock(return_value=USER)), \
             patch("src.budget.gate.is_foreign_origin", return_value=False), \
             patch("src.budget.gate.resolve_billing_plan",
                   AsyncMock(return_value=ENERGY_PROJEKT)), \
             patch("src.billing.project_budgets_service.evaluate_via_platform",
                   AsyncMock(return_value=leer)):
            assert await enforce_budget(
                str(USER), "werking-energy", 0.0, project_id="p",
            ) is None
            with pytest.raises(HTTPException) as exc:
                await enforce_budget(str(USER), "werking-energy", 0.0, project_id="p",
                                     call_kind="sandbox-lease")
            assert exc.value.detail["reason"] == "project_budget_exhausted"

    @pytest.mark.asyncio
    async def test_h4_widerspruechlicher_katalog_wird_409(self):
        from src.budget.plans import AmbiguousPlanCatalog
        async with _lease_umgebung(plan=ENERGY_PROJEKT) as spione:
            with patch("src.sandbox.routes.find_monthly_plan_for_app",
                       side_effect=AmbiguousPlanCatalog("zwei Monatspläne")), \
                 pytest.raises(HTTPException) as exc:
                await _lease(userId=str(USER), app="werking-energy")
        assert exc.value.status_code == 409
        assert exc.value.detail["reason"] == "ambiguous_plan_catalog"
        spione["pick"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_h4_katalog_nicht_geladen_bleibt_fail_open(self):
        """Wie im Gate: nicht geladener Katalog = Infra → keine Bindungspflicht, kein
        500; das Gate selbst (ebenfalls fail-open) lässt das Lease durch."""
        from src.budget.plans import PlanCatalogUnavailable
        async with _lease_umgebung(plan=ENGELMANN_MONAT) as spione:
            with patch("src.sandbox.routes.find_monthly_plan_for_app",
                       side_effect=PlanCatalogUnavailable("nicht geladen")), \
                 patch("src.budget.gate.find_monthly_plan_for_app",
                       side_effect=PlanCatalogUnavailable("nicht geladen")):
                res = await _lease(userId=str(USER), app="werking-energy")
        assert res.leaseId == LEASE
        spione["pick"].assert_awaited_once()
