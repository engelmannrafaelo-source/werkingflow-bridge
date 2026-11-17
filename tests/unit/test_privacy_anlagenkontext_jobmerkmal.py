"""POST /v1/jobs setzt das Merkmal ``schema_app_principal_verifiziert`` NUR serverseitig (c9a50d3b, 17.11.2026).

Echte Bytes von src/jobs/routes.py + src/auth.py + src/principals.py; ersetzt nur Principal-DB (synthetische
Tokens), Job-Store (In-Memory), spawn und Test-KI-Deckel (Muster: 51c8185c probe_fremdjob.py). Kein Netz.
"""
import os
import sys
from pathlib import Path

os.environ.update({"BRIDGE_PRINCIPALS_ENABLED": "true", "BRIDGE_GENERIC_JOBS_ENABLED": "true",
                   "BRIDGE_ORIGIN_ID": "dev", "API_KEY": "shared-synth-key-000000000000000000"})
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import pytest  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import src.principals as pr  # noqa: E402
from src.principals import Principal  # noqa: E402
from src.jobs import routes as R, store_client as sc, registry as reg  # noqa: E402
import src.budget.test_ki_deckel as tkd  # noqa: E402
from src.privacy_postprocess.anlagenkontext import JOB_SCHEMA_VERIFIZIERT_KEY as K  # noqa: E402

SCHEMA = "werking-schema-generator"
TOK = {"wsg_synthetisch-schema-aaaaaaaaaaaa": Principal(id="s", name="probe-schema", allowed_apps=[SCHEMA]),
       "tok-wild-synthetisch-bbbbbbbbbbbbbb": Principal(id="w", name="probe-wild", allowed_apps=["*"]),
       "tok-report-synthetisch-cccccccccccc": Principal(id="r", name="probe-report", allowed_apps=["werking-report"])}
MEM = {}


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    MEM.clear()

    async def resolve(t): return TOK.get(t)
    monkeypatch.setattr(pr, "resolve_principal_by_token", resolve)
    from src import auth as A
    monkeypatch.setattr(A.auth_manager, "get_api_key", lambda: "shared-synth-key-000000000000000000")

    async def create_job(job_id, kind, payload, attribution, **kw):
        MEM[job_id] = {"job_id": job_id, "attribution": attribution}
    monkeypatch.setattr(sc, "create_job", create_job)
    monkeypatch.setattr(sc, "is_store_available", lambda: True)
    monkeypatch.setattr(R, "spawn", lambda coro: coro.close())

    async def kein_test(request, art): return None
    monkeypatch.setattr(tkd, "job_anlegen_pruefen", kein_test)
    async def ex(*a, **k): return {}
    if "proxy" not in reg.registered_kinds():
        reg.register_executor("proxy", ex)
    R.set_attribution_extractor(lambda req: {"app_id": req.headers.get("x-app-id"),
                                             "user_id": req.headers.get("x-user-id")})


def _post(tok, app, attribution=None):
    app_ = FastAPI(); app_.include_router(R.router)
    c = TestClient(app_)
    body = {"kind": "proxy", "payload": {"path": "/v1/privacy/smart-anonymize",
                                          "body": {"text": "x", "context_hint": "technical_system_description"}}}
    if attribution is not None:
        body["attribution"] = attribution
    return c.post("/v1/jobs", json=body, headers={"Authorization": f"Bearer {tok}", "X-App-ID": app,
                                                  "X-User-ID": "11111111-2222-3333-4444-555555555555"})


def _attr():
    assert len(MEM) == 1, MEM
    return next(iter(MEM.values()))["attribution"] or {}


def test_J1_schema_principal_setzt_merkmal():
    r = _post("wsg_synthetisch-schema-aaaaaaaaaaaa", SCHEMA)
    assert r.status_code in (200, 202), r.text
    assert _attr().get(K) is True


def test_J2_nur_kopf_schema_setzt_kein_merkmal():
    r = _post("tok-wild-synthetisch-bbbbbbbbbbbbbb", SCHEMA)
    assert r.status_code in (200, 202), r.text
    assert K not in _attr()


def test_J3_rumpf_kann_merkmal_nicht_setzen():
    r = _post("tok-wild-synthetisch-bbbbbbbbbbbbbb", SCHEMA, attribution={K: True})
    assert r.status_code in (200, 202), r.text
    assert K not in _attr()


def test_J4_andere_app_kein_merkmal():
    r = _post("tok-report-synthetisch-cccccccccccc", "werking-report", attribution={K: True})
    assert r.status_code in (200, 202, 400, 403), r.text
    if MEM:
        assert K not in _attr()
