"""
CORS hardening: security-audit-live-findings-20260818.md L10c/B.4 — both the
worker app (src.main) and platform-api (src.platform_main) configured
`allow_origins=["*"]` together with `allow_credentials=True`. Starlette
cannot literally echo "*" for a credentialed response per the CORS spec, so
it reflects the request's Origin header instead — meaning ANY origin was
implicitly trusted for cookie-bearing requests, even though no legitimate
caller here ever sends credentials (every caller uses a Bearer token /
X-Bridge-Service-Token header, never a cookie — see the allow_credentials
comment in src/main.py).

This test imports the REAL composed apps (the exact objects production
serves) and drives an actual cross-origin request through TestClient,
asserting on the response headers a browser would actually see — not just
the middleware's stored kwargs, so a future refactor that changes the
mechanism (not just flips a flag) still gets caught.
"""
from __future__ import annotations

import os

os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

import pytest
from fastapi.testclient import TestClient
from starlette.middleware.cors import CORSMiddleware


def _cors_middleware(app):
    for m in app.user_middleware:
        if m.cls is CORSMiddleware:
            return m
    raise AssertionError("CORSMiddleware not found in app.user_middleware")


@pytest.fixture(scope="module")
def platform_app():
    from src.platform_main import app
    return app


@pytest.fixture(scope="module")
def worker_app():
    from src.main import app
    return app


class TestPlatformApiCorsConfig:
    def test_credentials_disabled(self, platform_app):
        mw = _cors_middleware(platform_app)
        assert mw.kwargs["allow_credentials"] is False

    def test_origins_still_wildcard_open_for_bearer_callers(self, platform_app):
        """The fix must not silently break the wildcard's purpose — Bearer-
        token cross-origin callers (browser ai-bridge-client) still need
        allow_origins to accept their Origin."""
        mw = _cors_middleware(platform_app)
        assert mw.kwargs["allow_origins"] == ["*"]

    def test_no_credentials_header_even_with_cookie_present(self, platform_app):
        """The actual browser-visible symptom: a credentialed cross-origin
        request must not get Access-Control-Allow-Credentials back."""
        client = TestClient(platform_app)
        resp = client.options(
            "/v1/auth/session",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "GET",
                "Cookie": "session=irrelevant",
            },
        )
        assert resp.headers.get("access-control-allow-credentials") != "true"

    def test_bearer_style_preflight_still_allowed(self, platform_app):
        """A normal cross-origin preflight for a Bearer-authenticated call
        (Authorization header, no cookies) must keep working — this is the
        real legitimate traffic shape."""
        client = TestClient(platform_app)
        resp = client.options(
            "/v1/auth/session",
            headers={
                "Origin": "https://werking-report.vercel.app",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization",
            },
        )
        assert resp.status_code == 200
        assert resp.headers.get("access-control-allow-origin") in ("*", "https://werking-report.vercel.app")


class TestWorkerCorsConfig:
    def test_credentials_disabled(self, worker_app):
        mw = _cors_middleware(worker_app)
        assert mw.kwargs["allow_credentials"] is False

    def test_origins_still_wildcard_open_for_bearer_callers(self, worker_app):
        mw = _cors_middleware(worker_app)
        assert mw.kwargs["allow_origins"] == ["*"]

    def test_no_credentials_header_even_with_cookie_present(self, worker_app):
        client = TestClient(worker_app)
        resp = client.options(
            "/health",
            headers={
                "Origin": "https://evil.example",
                "Access-Control-Request-Method": "GET",
                "Cookie": "session=irrelevant",
            },
        )
        assert resp.headers.get("access-control-allow-credentials") != "true"
