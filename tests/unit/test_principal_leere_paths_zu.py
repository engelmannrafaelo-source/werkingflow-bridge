"""Leere allowed_paths = ZU (Kanzler 18.11.2026, c041d05c).

Vorher: `may_use_path` liess bei [] alles durch, und die Lader machten aus [] per `or [WILDCARD]` still einen Voll-Joker.
Jetzt: nur "*" oeffnet alles; [] erlaubt nichts; nur NULL (Altpfad) wird zum Joker; die Admin-API lehnt [] mit 422 ab.
Jede Probe deckt einen der drei Wege (Objekt, beide Lader, API/Repo) ab, damit ein Rueckfall an EINER Stelle rot wird.
"""
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.principals as principals
from src.platform_client import PlatformResponse


@pytest.fixture(autouse=True)
def _reset_cache():
    principals.invalidate_cache()
    yield
    principals.invalidate_cache()


# --- 1) Objekt -----------------------------------------------------------------------------------------------------
def test_leere_liste_ist_zu_fuer_jeden_pfad():
    p = principals.Principal(id="x", name="leer", allowed_apps=["a"], allowed_paths=[])
    for pfad in ("/v1/chat/completions", "/v1/research", "/v1/jobs", "/v1/jobs/abc", None, ""):
        assert p.may_use_path(pfad) is False, pfad


def test_joker_bleibt_offen_und_explizite_pfade_unveraendert():
    assert principals.Principal(id="x", name="j", allowed_paths=["*"]).may_use_path("/v1/anything") is True
    p = principals.Principal(id="x", name="e", allowed_paths=["/v1/chat/completions", "/v1/jobs/*"])
    assert p.may_use_path("/v1/chat/completions") is True
    assert p.may_use_path("/v1/jobs/job_1") is True
    assert p.may_use_path("/v1/research") is False
    assert p.may_use_path(None) is False


def test_default_ohne_angabe_bleibt_joker():
    assert principals.Principal(id="x", name="d").allowed_paths == ["*"]


# --- 2) Lader: DB-Zeile, platform-api-JSON, get_principal_row_by_hash -----------------------------------------------
class _Row(dict):
    pass


def _row(paths):
    return _Row(id="11111111-1111-1111-1111-111111111111", name="leer", allowed_apps=["a"],
                allowed_paths=paths, monthly_cap_eur=None)


def test_db_zeile_mit_leerer_liste_bleibt_leer_und_zu():
    p = principals._row_to_principal(_row([]))
    assert p.allowed_paths == []
    assert p.may_use_path("/v1/chat/completions") is False


def test_db_zeile_null_wird_joker_altpfad():
    assert principals._row_to_principal(_row(None)).allowed_paths == ["*"]


async def test_platform_api_json_mit_leerer_liste_bleibt_zu():
    js = {"id": "11111111-1111-1111-1111-111111111111", "name": "leer", "allowed_apps": ["a"],
          "allowed_paths": [], "monthly_cap_eur": None}
    mock_call = AsyncMock(return_value=PlatformResponse(status_code=200, json={"principal": js}))
    with patch.object(principals, "call_platform", mock_call):
        p = await principals.resolve_principal_by_token("tok-leer")
    assert p is not None and p.allowed_paths == []
    assert p.may_use_path("/v1/research") is False


async def test_get_principal_row_by_hash_reicht_leere_liste_unveraendert_durch():
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=_row([]))

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    with patch.object(principals, "get_pool", return_value=pool):
        d = await principals.get_principal_row_by_hash("h")
    assert d["allowed_paths"] == []


# --- 3) Schreibweg: Repo + API -------------------------------------------------------------------------------------
async def test_repo_create_lehnt_leere_liste_ab_vor_jedem_db_zugriff():
    gp = MagicMock(side_effect=AssertionError("DB darf nicht erreicht werden"))
    with patch.object(principals, "get_pool", gp):
        with pytest.raises(ValueError):
            await principals.create_principal(name="n", token="t" * 64, allowed_apps=["a"], allowed_paths=[])
    gp.assert_not_called()


def _client():
    import src.principals_routes as routes
    from src.api_auth.deps import require_admin
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[require_admin] = lambda: object()
    return app, routes


def test_api_leere_liste_422_ohne_anlage():
    app, routes = _client()
    create = AsyncMock()
    with patch.object(routes, "_require_db", lambda: None), patch.object(routes.principals_repo, "create_principal", create):
        r = TestClient(app).post("/v1/admin/service-principals",
                                 json={"name": "n", "allowed_apps": ["a"], "allowed_paths": []})
    assert r.status_code == 422, r.text
    create.assert_not_awaited()


def test_api_ohne_feld_bleibt_joker_weg():
    app, routes = _client()
    p = principals.Principal(id="1", name="n", allowed_apps=["a"], allowed_paths=["*"])
    create = AsyncMock(return_value=p)
    with patch.object(routes, "_require_db", lambda: None), patch.object(routes.principals_repo, "create_principal", create):
        r = TestClient(app).post("/v1/admin/service-principals", json={"name": "n", "allowed_apps": ["a"]})
    assert r.status_code == 201, r.text
    assert create.await_args.kwargs["allowed_paths"] is None
