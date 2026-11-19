"""Wiring-Regression Live-FAIL 7740f932 (c9a50d3b, 18.11.2026): ECHTER Pfad registry._ausfuehren -> proxy_executor ->
_self_post_json -> _post_self -> _build_headers -> kontext_job, ohne Attrappe des Job-Kopfes.

Ersetzt wird nur die unterste Transportfunktion ``_post_with_capacity_redispatch``: sie bekommt die Koepfe, die der
echte Executor baut, formt daraus den Request des Endpunkts (Peer 127.0.0.1, wie der Selbstaufruf an localhost) und
ruft damit die echte ``kontext_verifiziert`` - genau die Pruefung, die in 7740f932 still False lieferte
(anlagenkontext.py, kein Job-Kopf, weil proxy nicht in JOB_ABBRECHBAR_IM_LAUF). Job-Store: In-Memory. Kein Netz.
"""
import asyncio
import os
import sys
from pathlib import Path

os.environ.update({"BRIDGE_GENERIC_JOBS_ENABLED": "true", "BRIDGE_ORIGIN_ID": "dev",
                   "API_KEY": "shared-synth-key-000000000000000000"})
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import httpx  # noqa: E402
import pytest  # noqa: E402
from starlette.requests import Request  # noqa: E402

from src.jobs import executors as ex, registry as reg, store_client as sc  # noqa: E402
from src.jobs.selbstaufruf import JOB_KONTEXT_HEADER, JOB_LAUF_HEADER  # noqa: E402
from src.privacy_postprocess import anlagenkontext as ak  # noqa: E402

SCHEMA = "werking-schema-generator"
USER = "81d0594a-7580-4da6-91e9-6832a2de93fa"  # synthetischer Testnutzer
JOB = "job_dev_synthetisch00000000000000000000"
PAYLOAD = {"path": "/v1/privacy/smart-anonymize",
           "body": {"text": "Der Kaltwasserverteiler KV-01 ist neu.", "language": "de",
                    "context_hint": ak.ANLAGENKONTEXT}}


def _attr(**kw):
    a = {"app_id": SCHEMA, "user_id": USER, "app_env": "staging", "eigentuemer": "v1:synthetisch",
         ak.JOB_SCHEMA_VERIFIZIERT_KEY: True}
    a.update(kw)
    return {k: v for k, v in a.items() if v is not None}


def _request(headers, peer="127.0.0.1"):
    scope = {"type": "http", "method": "POST", "path": PAYLOAD["path"], "query_string": b"",
             "headers": [(k.lower().encode(), str(v).encode()) for k, v in headers.items()],
             "client": (peer, 40000), "server": ("localhost", 8000), "scheme": "http"}
    return Request(scope)


def _lauf(monkeypatch, kind, attribution, job_status="running", peer="127.0.0.1"):
    """Fuehrt den echten Executor-Pfad aus; liefert (gesendete Koepfe, Urteil kontext_verifiziert)."""
    monkeypatch.setenv(ak.FLAG_ENV, "true")
    from src import auth as A
    monkeypatch.setattr(A.auth_manager, "get_api_key", lambda: "shared-synth-key-000000000000000000")
    store = {JOB: {"job_id": JOB, "status": job_status, "attribution": attribution}}

    async def get_job(job_id):
        return store.get(job_id)
    monkeypatch.setattr(sc, "get_job", get_job)
    gesehen = {}

    async def transport(client, path, body, headers):
        gesehen["headers"] = dict(headers)
        req = _request(headers, peer)
        gesehen["urteil"] = await ak.kontext_verifiziert(
            req, body.get("context_hint"), aufrufer_user_id=req.headers.get("X-User-ID"))
        return httpx.Response(200, json={"status": "success"}), None
    monkeypatch.setattr(ex, "_post_with_capacity_redispatch", transport)

    async def progress(_):
        return None
    executor = ex.proxy_executor if kind == "proxy" else ex.chat_executor
    asyncio.run(reg._ausfuehren(JOB, kind, executor, PAYLOAD, attribution, progress))
    return gesehen["headers"], gesehen["urteil"]


def test_W1_schema_proxy_job_traegt_kontextkopf_und_wird_verifiziert(monkeypatch):
    # 7740f932-Klasse: kind=proxy, Merkmal gesetzt, Selbstaufruf an localhost -> Kontext JETZT verifiziert
    h, urteil = _lauf(monkeypatch, "proxy", _attr())
    assert h.get(JOB_KONTEXT_HEADER) == JOB
    assert JOB_LAUF_HEADER not in h  # proxy bleibt NICHT abbrechbar (BR10b unveraendert)
    assert urteil is True


def test_W2_vor_dem_fix_fehlte_der_kopf_genau_hier():
    # Ursache 7740f932, als Faktum festgehalten: proxy ist kein abbrechbarer Lauf -> kein Lauf-Kopf
    assert "proxy" not in reg.JOB_ABBRECHBAR_IM_LAUF
    assert "proxy" in reg.JOB_KONTEXT_KINDS


@pytest.mark.parametrize("attribution,grund", [
    (_attr(app_id="werking-report", **{ak.JOB_SCHEMA_VERIFIZIERT_KEY: None}), "andere App ohne Merkmal"),
    (_attr(app_id="engelmann", **{ak.JOB_SCHEMA_VERIFIZIERT_KEY: None}), "Works ohne Merkmal"),
    (_attr(**{ak.JOB_SCHEMA_VERIFIZIERT_KEY: "true"}), "Merkmal als String (nicht serverseitig)"),
    (_attr(**{ak.JOB_SCHEMA_VERIFIZIERT_KEY: False}), "Merkmal False"),
    (_attr(**{ak.JOB_SCHEMA_VERIFIZIERT_KEY: None}), "Schema ohne Merkmal"),
])
def test_N1_andere_apps_kein_kopf_kein_kontext(monkeypatch, attribution, grund):
    h, urteil = _lauf(monkeypatch, "proxy", attribution)
    assert JOB_KONTEXT_HEADER not in h, grund
    assert urteil is False, grund


def test_N2_andere_apps_koepfe_byte_gleich_zum_stand_vor_dem_fix(monkeypatch):
    # Ohne Merkmal sendet der Executor exakt die Koepfe von vorher (kein zusaetzlicher Kopf)
    a = _attr(app_id="werking-report", **{ak.JOB_SCHEMA_VERIFIZIERT_KEY: None})
    h, _ = _lauf(monkeypatch, "proxy", a)
    erwartet = {"Content-Type", "Authorization", "X-App-ID", "X-User-ID", "X-App-Env"}
    assert set(h) == erwartet


@pytest.mark.parametrize("status", ["done", "pending", "failed", "cancelled"])
def test_N3_job_nicht_laufend_kein_kontext(monkeypatch, status):
    _, urteil = _lauf(monkeypatch, "proxy", _attr(), job_status=status)
    assert urteil is False


def test_N4_anderer_nutzer_oder_andere_app_im_job(monkeypatch):
    # Kopf vorhanden, aber Job gehoert einem anderen Nutzer bzw. einer anderen App -> False
    a = _attr()
    monkeypatch.setenv(ak.FLAG_ENV, "true")
    h, _ = _lauf(monkeypatch, "proxy", a)
    for fremd in ({**a, "user_id": "00000000-0000-4000-8000-000000000000"}, {**a, "app_id": "werking-report"}):
        async def get_job(job_id, fremd=fremd):
            return {"job_id": job_id, "status": "running", "attribution": fremd}
        monkeypatch.setattr(sc, "get_job", get_job)
        req = _request(h)
        assert asyncio.run(ak.kontext_verifiziert(req, ak.ANLAGENKONTEXT,
                                                  aufrufer_user_id=req.headers.get("X-User-ID"))) is False


def test_N5_kopf_von_aussen_ohne_loopback_wirkt_nicht(monkeypatch):
    _, urteil = _lauf(monkeypatch, "proxy", _attr(), peer="172.18.0.4")  # LB-Peer
    assert urteil is False


def test_N6_flag_aus_oder_ohne_hint(monkeypatch):
    h, _ = _lauf(monkeypatch, "proxy", _attr())
    req = _request(h)
    monkeypatch.setenv(ak.FLAG_ENV, "false")
    assert asyncio.run(ak.kontext_verifiziert(req, ak.ANLAGENKONTEXT, aufrufer_user_id=USER)) is False
    monkeypatch.setenv(ak.FLAG_ENV, "true")
    assert asyncio.run(ak.kontext_verifiziert(req, None, aufrufer_user_id=USER)) is False


def test_N7_kontextvariable_nach_dem_lauf_zurueckgesetzt(monkeypatch):
    _lauf(monkeypatch, "proxy", _attr())
    assert reg.kontext_job() is None


def test_W3_abbrechbarer_lauf_unveraendert_kein_kontextkopf():
    # abbrechbare Art (chat/research/doc-agent): Lauf-Kopf wie bisher, kein zusaetzlicher Kontextkopf
    token = reg._job_lauf.set(JOB)
    try:
        h = ex._build_headers(_attr())
    finally:
        reg._job_lauf.reset(token)
    assert h.get(JOB_LAUF_HEADER) == JOB and JOB_KONTEXT_HEADER not in h
