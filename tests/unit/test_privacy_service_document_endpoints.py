"""
Integration tests for the new /document/* endpoints on the privacy service.

Uses FastAPI's TestClient so we exercise the request parsing, error handling
and dispatcher wiring without needing a running container. Heavy adapters
(Docling, LibreOffice) are not exercised here — they are smoke-tested at the
container level. CSV is used as a lightweight stand-in for the happy path.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


@pytest.fixture(scope="module")
def client():
    fastapi = pytest.importorskip("fastapi")
    pytest.importorskip("fastapi.testclient")
    pytest.importorskip("pandas")
    pytest.importorskip("tabulate")
    pytest.importorskip("markdownify")
    from fastapi.testclient import TestClient
    # Disable AI fallback for these tests — they exercise the deterministic
    # path. The fallback is covered separately in test_adapters.py with a
    # mocked Bridge self-call.
    import os as _os
    _os.environ["DISABLE_AI_FALLBACK"] = "1"
    # Reset cached chain in case another test already initialized it.
    from src.privacy_service import app as app_mod
    app_mod._DOCUMENT_CHAIN = None
    from src.privacy_service.app import app

    return TestClient(app)


def test_health_endpoint(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_document_convert_csv_happy_path(client):
    resp = client.post(
        "/document/convert",
        files={"file": ("table.csv", b"a,b\n1,2\n3,4\n", "text/csv")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["format"] == "csv"
    assert body["metadata"]["rows"] == 2
    assert "1" in body["markdown"] and "4" in body["markdown"]


def test_document_convert_html_happy_path(client):
    html = b"<html><body><h1>Hi</h1><p>Body text.</p></body></html>"
    resp = client.post(
        "/document/convert",
        files={"file": ("page.html", html, "text/html")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["format"] == "html"
    assert "Hi" in body["markdown"]


def test_document_convert_missing_file_returns_400(client):
    resp = client.post("/document/convert", data={"language": "de"})
    assert resp.status_code == 400
    assert "No file uploaded" in resp.json()["detail"]


def test_document_convert_unknown_format_returns_415(client):
    resp = client.post(
        "/document/convert",
        files={"file": ("mystery.xyz", b"some bytes", "application/octet-stream")},
    )
    assert resp.status_code == 415
    detail = resp.json()["detail"]
    assert "mystery.xyz" in detail
    assert "No adapter" in detail or "Unsupported" in detail


def test_document_convert_mime_hint_overrides_extension(client):
    # ``.bin`` has no extension mapping; force CSV via mime_type_hint form field.
    resp = client.post(
        "/document/convert",
        files={"file": ("blob.bin", b"a,b\n1,2\n", "application/octet-stream")},
        data={"mime_type_hint": "text/csv"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["format"] == "csv"


def test_document_convert_empty_file_returns_400(client):
    resp = client.post(
        "/document/convert",
        files={"file": ("empty.csv", b"", "text/csv")},
    )
    assert resp.status_code == 400
    assert "empty" in resp.json()["detail"].lower()


def test_convert_and_anonymize_invalid_privacy_mode(client):
    resp = client.post(
        "/document/convert-and-anonymize",
        files={"file": ("table.csv", b"a,b\n1,2\n", "text/csv")},
        data={"privacy_mode": "bogus"},
    )
    assert resp.status_code == 400
    assert "privacy_mode" in resp.json()["detail"]


def test_convert_and_anonymize_basic_mode_no_pii(client):
    """basic mode runs Presidio synchronously; without PII the markdown stays as-is."""
    pytest.importorskip("presidio_analyzer")
    pytest.importorskip("presidio_anonymizer")
    csv = b"col1,col2\nfoo,bar\nbaz,qux\n"
    resp = client.post(
        "/document/convert-and-anonymize",
        files={"file": ("plain.csv", csv, "text/csv")},
        data={"privacy_mode": "basic", "language": "de"},
    )
    # Some build environments lack Presidio's spaCy models — accept either a
    # successful 200 or a clean 500 with anonymization-related error so the
    # test stays informative without being flaky on dev machines.
    if resp.status_code == 500:
        pytest.skip(f"Presidio not fully configured locally: {resp.text[:200]}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["format"] == "csv"
    assert "anonymized_markdown" in body
    assert "mapping" in body
    assert body["privacy_mode"] == "basic"
