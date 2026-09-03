"""
Tests for the two-mode unreachable-privacy policy (Rafael, 2026-08-01):

  dev  — PRIVACY_SERVICE_FALLBACK_URL set: a transport failure against the
         primary (GPU host) retries against the local container, loudly.
  prod — no fallback configured: the request fails with 424 Failed Dependency,
         and a durable job carrying the same work is DEFERRED until the
         dependency returns rather than failing terminally.

The sharp edges these pin down:
  * only TRANSPORT failures may fall back. An HTTP error response means the
    service is alive and gave a verdict — retrying that against a different
    detector would change which PII is found, not fix an outage.
  * a deferred job must not consume the worker-crash retry budget, or a long
    outage terminates the very work it was supposed to preserve.
  * deferral is bounded, so a permanently dead dependency still fails loud.
"""

import importlib
import os
import sys
from unittest.mock import AsyncMock, MagicMock as _MM

import httpx
import pytest

for _mod in [
    "claude_code_sdk",
    "claude_code_sdk._errors",
    "claude_code_sdk._internal",
    "claude_code_sdk._internal.client",
    "src.identity.routes",
    "src.db.client",
]:
    if _mod not in sys.modules:
        sys.modules[_mod] = _MM()

import src.privacy_client as pc  # noqa: E402


def _client_with(handler, base_url="http://primary:8100"):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url=base_url)


def _ok(request):
    return httpx.Response(200, json={"ok": True, "served_by": str(request.url)})


def _boom(request):
    raise httpx.ConnectError("All connection attempts failed", request=request)


# ---------------------------------------------------------------------------
# dev: fallback on transport failure
# ---------------------------------------------------------------------------
class TestDevFallback:
    async def test_uses_fallback_when_primary_unreachable(self):
        c = pc.PrivacyServiceClient()
        c.fallback_url = "http://local:8100"
        c._client = _client_with(_boom)
        c._fallback_client = _client_with(_ok, base_url="http://local:8100")

        r = await c.post("/smart-anonymize", json={"text": "x"})
        assert r.status_code == 200
        assert "local:8100" in r.json()["served_by"]

    async def test_primary_is_preferred_while_healthy(self):
        c = pc.PrivacyServiceClient()
        c.fallback_url = "http://local:8100"
        c._client = _client_with(_ok)
        c._fallback_client = _client_with(
            lambda rq: pytest.fail("fallback must not be used while primary is healthy"),
            base_url="http://local:8100",
        )
        r = await c.post("/smart-anonymize", json={"text": "x"})
        assert "primary:8100" in r.json()["served_by"]

    async def test_http_error_is_returned_not_failed_over(self):
        """THE guardrail: a 503 from the service is its VERDICT, not an outage.

        Falling back here would silently re-run PII detection on a different
        detector — an availability trick that changes a GDPR outcome.
        """
        c = pc.PrivacyServiceClient()
        c.fallback_url = "http://local:8100"
        c._client = _client_with(lambda rq: httpx.Response(503, json={"detail": "model loading"}))
        c._fallback_client = _client_with(
            lambda rq: pytest.fail("an HTTP error must never trigger the fallback"),
            base_url="http://local:8100",
        )
        r = await c.post("/smart-anonymize", json={"text": "x"})
        assert r.status_code == 503

    async def test_raises_when_both_are_unreachable(self):
        c = pc.PrivacyServiceClient()
        c.fallback_url = "http://local:8100"
        c._client = _client_with(_boom)
        c._fallback_client = _client_with(_boom, base_url="http://local:8100")

        with pytest.raises(pc.PrivacyServiceUnavailable) as ei:
            await c.post("/smart-anonymize", json={"text": "x"})
        assert len(ei.value.tried) == 2

    async def test_fallback_hop_is_logged_loudly(self, caplog):
        c = pc.PrivacyServiceClient()
        c.fallback_url = "http://local:8100"
        c._client = _client_with(_boom)
        c._fallback_client = _client_with(_ok, base_url="http://local:8100")

        with caplog.at_level("WARNING"):
            await c.post("/smart-anonymize", json={"text": "x"})
        assert any("PRIVACY FALLBACK" in r.message for r in caplog.records), (
            "a detector substitution must be greppable after the fact"
        )


# ---------------------------------------------------------------------------
# prod: no fallback configured
# ---------------------------------------------------------------------------
class TestProdNoFallback:
    async def test_unreachable_raises_typed_error(self):
        c = pc.PrivacyServiceClient()
        c.fallback_url = ""
        c._client = _client_with(_boom)

        with pytest.raises(pc.PrivacyServiceUnavailable):
            await c.post("/smart-anonymize", json={"text": "x"})

    def test_fallback_is_off_by_default(self):
        """Production safety: the fallback must require explicit opt-in, so a
        host that simply never sets the var cannot silently degrade."""
        os.environ.pop("PRIVACY_SERVICE_FALLBACK_URL", None)
        reloaded = importlib.reload(pc)
        assert reloaded.PRIVACY_FALLBACK_URL == ""
        assert reloaded.PrivacyServiceClient().fallback_url == ""


# ---------------------------------------------------------------------------
# Durable-job deferral
# ---------------------------------------------------------------------------
import src.jobs.registry as registry  # noqa: E402
# NOTE: the runner reaches the store through store_client (ADR-0009 Schritt
# 2d) — patching registry.store would patch a module the runner no longer
# calls, and the test would pass while measuring nothing.
from src.jobs.executors import ExecutorHTTPError  # noqa: E402


class TestDependencyDeferral:
    async def test_424_defers_instead_of_failing(self, monkeypatch):
        """The whole point: the check must survive the outage and run later."""
        deferred, errored = [], []
        monkeypatch.setattr(registry.store_client, "get_job",
                            AsyncMock(return_value={"defer_count": 0}))
        monkeypatch.setattr(registry.store_client, "defer_job",
                            AsyncMock(side_effect=lambda *a, **k: deferred.append(a)))
        monkeypatch.setattr(registry.store_client, "mark_error",
                            AsyncMock(side_effect=lambda *a, **k: errored.append(a)))

        ok = await registry._defer_job(
            "job1", "proxy", "privacy unreachable",
            registry.DEPENDENCY_RETRY_DELAY_S, "dependency unreachable",
        )
        assert ok is True
        assert deferred and not errored

    async def test_defer_budget_is_bounded(self, monkeypatch):
        """A permanently dead dependency must still fail loud eventually."""
        monkeypatch.setattr(registry.store_client, "get_job", AsyncMock(
            return_value={"defer_count": registry.DEPENDENCY_MAX_DEFERS}))
        monkeypatch.setattr(registry.store_client, "defer_job",
                            AsyncMock(side_effect=lambda *a, **k: pytest.fail("must not defer")))

        assert await registry._defer_job(
            "job1", "proxy", "still down",
            registry.DEPENDENCY_RETRY_DELAY_S, "dependency unreachable",
        ) is False

    async def test_patience_outlasts_a_real_outage(self):
        """2026-08-01 lasted ~23 min; the budget must comfortably exceed that."""
        total_s = registry.DEPENDENCY_MAX_DEFERS * registry.DEPENDENCY_RETRY_DELAY_S
        assert total_s >= 3600, f"only {total_s}s of patience — shorter than a real outage"

    def test_dependency_status_is_not_in_nginx_failover_list(self):
        """424 must stay out of proxy_next_upstream (500/502/503/504/429), or
        every worker burns on the same unreachable hop and the answer comes
        back as the misleading 'bridge at capacity' envelope."""
        assert registry.DEPENDENCY_UNAVAILABLE_STATUS not in (429, 500, 502, 503, 504)

    async def test_non_dependency_error_still_fails_terminally(self, monkeypatch):
        """Deferral must not swallow real bugs: a 400 stays terminal."""
        errored = []
        monkeypatch.setattr(registry.store_client, "mark_running", AsyncMock())
        monkeypatch.setattr(registry.store_client, "mark_done", AsyncMock())
        monkeypatch.setattr(registry.store_client, "heartbeat", AsyncMock())
        monkeypatch.setattr(registry.store_client, "update_progress", AsyncMock())
        monkeypatch.setattr(registry.store_client, "mark_error",
                            AsyncMock(side_effect=lambda *a, **k: errored.append(k.get("code"))))
        monkeypatch.setattr(registry.store_client, "defer_job",
                            AsyncMock(side_effect=lambda *a, **k: pytest.fail("400 must not defer")))

        async def _bad(payload, attribution, report_progress):
            raise ExecutorHTTPError(400, "validation failed")

        registry.register_executor("t-bad", _bad)
        await registry._run_body("j", "t-bad", {}, None)
        assert errored == ["UPSTREAM_HTTP_400"]


class TestStoreQueryGuards:
    """The deferral guards live in SQL; assert they are actually in the query
    text (a DB-backed test needs Postgres, which unit tests do not have)."""

    def test_claim_skips_deferred_and_discounts_defers(self):
        import inspect
        import src.jobs.store as store
        src = inspect.getsource(store.claim_stale_job)
        assert "_NOT_DEFERRED" in src or "deferred_until" in src
        assert "_CRASH_ATTEMPTS" in src

    def test_abandoned_skips_deferred(self):
        import inspect
        import src.jobs.store as store
        src = inspect.getsource(store.find_abandoned)
        assert "_NOT_DEFERRED" in src or "deferred_until" in src

    def test_crash_budget_excludes_defers(self):
        import src.jobs.store as store
        assert store._CRASH_ATTEMPTS == "(attempts - defer_count)"
