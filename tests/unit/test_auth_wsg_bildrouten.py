"""wsg_-Schluessel: Job-Pfade + GENAU POST /v1/privacy/image-mask|image-receipt (Kanzler 19.11.2026, c041d05c).

Die wsg_-Sperre in verify_api_key laeuft VOR jeder anderen Pruefung. "weiter" heisst hier: die Sperre greift nicht,
verify_api_key laeuft in die naechste Stufe (hier ohne konfigurierten API-Schluessel -> True). Alle anderen
Nicht-Job-Pfade und andere Methoden auf den Bildrouten bleiben 403; Nicht-wsg_-Schluessel sind unberuehrt.
"""
from unittest.mock import patch

import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from starlette.requests import Request

import src.auth as auth

WSG = "wsg_" + "a" * 64
HEX = "b" * 64


def _req(method: str, path: str) -> Request:
    return Request({"type": "http", "method": method, "path": path, "raw_path": path.encode(),
                    "query_string": b"", "headers": [], "scheme": "http", "server": ("t", 80)})


async def _rufe(method, path, token=WSG):
    cred = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    with patch.object(auth.auth_manager, "get_api_key", return_value=None):
        return await auth.verify_api_key(_req(method, path), cred)


@pytest.mark.parametrize("path", ["/v1/privacy/image-mask", "/v1/privacy/image-receipt"])
async def test_wsg_post_bildrouten_weiter(path):
    assert await _rufe("POST", path) is True


@pytest.mark.parametrize("method,path", [("POST", "/v1/jobs"), ("GET", "/v1/jobs/job_dev_" + "0" * 32)])
async def test_wsg_jobpfade_unveraendert_weiter(method, path):
    assert await _rufe(method, path) is True


@pytest.mark.parametrize("method,path", [
    ("POST", "/v1/chat/completions"),
    ("POST", "/v1/privacy/smart-anonymize"),
    ("POST", "/v1/research"),
    ("POST", "/v1/privacy/image-other"),
    ("POST", "/v1/privacy/image-mask/x"),
    ("POST", "/v1/privacy/image-"),
    ("POST", "/v1/privacy/image-maskx"),
    ("GET", "/v1/privacy/image-mask"),
    ("PUT", "/v1/privacy/image-receipt"),
    ("DELETE", "/v1/privacy/image-receipt"),
    ("GET", "/v1/models"),
])
async def test_wsg_alle_anderen_pfade_und_methoden_403(method, path):
    with pytest.raises(HTTPException) as e:
        await _rufe(method, path)
    assert e.value.status_code == 403


async def test_nicht_wsg_schluessel_unberuehrt():
    assert await _rufe("POST", "/v1/chat/completions", token=HEX) is True
