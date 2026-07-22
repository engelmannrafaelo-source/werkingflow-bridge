"""
Unit tests for the document/privacy-service capacity metrics feature:

- PrivacyServiceClient.track_call() — per-worker concurrency gauge for calls
  into the privacy-pdf-service container (src/privacy_client.py).
- PromptMetricsCollector busy-rate tracking + agent_ids filter
  (src/middleware/prompt_metrics.py), which backs the new
  /v1/metrics/document-performance capacity endpoint.

WICHTIG: Diese Tests testen NUR die neue Capacity-Metrik-Logik, nicht die
bereits bestehende persist_ai_call_activity-Business-Activity (siehe
test_infra_endpoint_tracking.py dafür).
"""

import sys
from unittest.mock import MagicMock as _MM

# Stub heavy deps before any src.* import (same pattern as
# test_infra_endpoint_tracking.py — avoids pulling in claude_code_sdk etc.
# just to exercise these two small, dependency-free modules).
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

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.privacy_client import PrivacyServiceClient
from src.middleware.prompt_metrics import PromptMetricsCollector, DOCUMENT_AGENT_IDS


# ============================================================================
# PrivacyServiceClient.track_call()
# ============================================================================

class TestPrivacyServiceClientTrackCall:
    @pytest.mark.asyncio
    async def test_no_overlap_yields_zero(self):
        client = PrivacyServiceClient()
        async with client.track_call() as concurrent_before:
            assert concurrent_before == 0
            assert client.active_calls == 1
        assert client.active_calls == 0

    @pytest.mark.asyncio
    async def test_nested_calls_see_overlap(self):
        """A second call starting while the first is still open sees
        concurrent_before=1 — this is the 'busy' signal fed into
        concurrent_calls_at_start."""
        client = PrivacyServiceClient()
        async with client.track_call() as first_before:
            assert first_before == 0
            async with client.track_call() as second_before:
                assert second_before == 1
                assert client.active_calls == 2
            assert client.active_calls == 1
        assert client.active_calls == 0

    @pytest.mark.asyncio
    async def test_active_calls_decrements_on_exception(self):
        """Concurrency gauge must not leak on error — otherwise every failed
        call would permanently inflate the busy signal."""
        client = PrivacyServiceClient()
        with pytest.raises(RuntimeError):
            async with client.track_call():
                raise RuntimeError("downstream boom")
        assert client.active_calls == 0


# ============================================================================
# PromptMetricsCollector — busy-rate + agent_ids filter
# ============================================================================

@pytest.fixture
def collector(tmp_path, monkeypatch):
    """Fresh collector writing to a throwaway tmp dir (avoids touching the
    real /app/logs mount and avoids cross-test JSONL pollution)."""
    monkeypatch.setenv("METRICS_DIR", str(tmp_path))
    monkeypatch.setenv("INSTANCE_NAME", "test-worker")
    import src.middleware.prompt_metrics as pm
    monkeypatch.setattr(pm, "METRICS_DIR", str(tmp_path / "bridge-metrics"))
    return PromptMetricsCollector()


class TestBusyRateStats:
    def test_busy_rate_computed_from_concurrent_calls_at_start(self, collector):
        collector.record(
            app_id="werking-report", agent_id="anonymisierung", workflow_id=None,
            duration_ms=100, status="success", model="privacy-service",
            concurrent_calls_at_start=0,
        )
        collector.record(
            app_id="werking-report", agent_id="anonymisierung", workflow_id=None,
            duration_ms=200, status="success", model="privacy-service",
            concurrent_calls_at_start=2,
        )
        stats = collector.get_stats(hours=0)
        agent = next(a for a in stats["agents"] if a["agent_id"] == "anonymisierung")
        assert agent["busy_samples"] == 2
        assert agent["busy_starts"] == 1
        assert agent["busy_rate_pct"] == 50.0

    def test_busy_rate_none_when_not_tracked(self, collector):
        """LLM chat calls never pass concurrent_calls_at_start — busy_rate_pct
        must stay None (not 0), so a dashboard can distinguish "not measured"
        from "measured, 0% busy"."""
        collector.record(
            app_id="werking-report", agent_id="chat", workflow_id=None,
            duration_ms=100, status="success", model="claude-sonnet-4-5",
        )
        stats = collector.get_stats(hours=0)
        agent = next(a for a in stats["agents"] if a["agent_id"] == "chat")
        assert agent["busy_samples"] == 0
        assert agent["busy_rate_pct"] is None

    def test_agent_ids_filter_scopes_to_document_agents(self, collector):
        collector.record(
            app_id="werking-report", agent_id="anonymisierung", workflow_id=None,
            duration_ms=100, status="success", model="privacy-service",
            concurrent_calls_at_start=0,
        )
        collector.record(
            app_id="werking-report", agent_id="chat", workflow_id=None,
            duration_ms=100, status="success", model="claude-sonnet-4-5",
        )
        stats = collector.get_stats(hours=0, agent_ids=DOCUMENT_AGENT_IDS)
        agent_ids_seen = {a["agent_id"] for a in stats["agents"]}
        assert agent_ids_seen == {"anonymisierung"}
        assert stats["summary"]["total_calls"] == 1

    def test_unfiltered_get_stats_still_includes_everything(self, collector):
        """agent_ids=None (the default, used by /v1/metrics/prompt-performance)
        must not change existing behaviour."""
        collector.record(
            app_id="werking-report", agent_id="chat", workflow_id=None,
            duration_ms=100, status="success", model="claude-sonnet-4-5",
        )
        stats = collector.get_stats(hours=0)
        assert stats["summary"]["total_calls"] == 1


# ============================================================================
# End-to-end: real endpoint -> real PrivacyServiceClient.track_call() ->
# real PromptMetricsCollector -> /v1/metrics/document-performance
# ============================================================================

import src.main  # noqa: E402 — safe after stubs above


class TestConvertPdfEndpointFeedsCapacityMetrics:
    """Exercises convert_pdf_endpoint with a REAL PrivacyServiceClient (only
    the downstream httpx client is faked) so the track_call() concurrency
    signal and get_prompt_metrics() recording run for real, end to end —
    not just the mocked-boundary tests in test_infra_endpoint_tracking.py."""

    @pytest.mark.asyncio
    async def test_two_concurrent_calls_produce_busy_signal(self, tmp_path, monkeypatch):
        monkeypatch.setenv("INSTANCE_NAME", "test-worker-e2e")
        import src.middleware.prompt_metrics as pm
        monkeypatch.setattr(pm, "METRICS_DIR", str(tmp_path / "bridge-metrics"))
        monkeypatch.setattr(pm, "_collector", None)  # fresh singleton for this test

        real_privacy_client = PrivacyServiceClient()
        release_first = asyncio.Event()
        first_call_started = asyncio.Event()
        call_count = {"n": 0}

        async def fake_post(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # First call: block until the second call has started, so the
                # two calls genuinely overlap (proving track_call() sees it).
                first_call_started.set()
                await release_first.wait()
            resp = MagicMock()
            resp.status_code = 200
            resp.raise_for_status = MagicMock()
            resp.json = MagicMock(return_value={"status": "success", "markdown": "# x"})
            return resp

        inner_client = AsyncMock()
        inner_client.post = AsyncMock(side_effect=fake_post)
        inner_client.is_closed = False  # else _get_client() sees a truthy Mock and swaps in a real httpx.AsyncClient
        real_privacy_client._client = inner_client

        def _make_request():
            req = AsyncMock()
            req.form = AsyncMock(return_value=MagicMock(
                get=MagicMock(return_value=MagicMock(filename="a.pdf", read=AsyncMock(return_value=b"%PDF-1"))),
            ))
            req.headers = {}
            return req

        async def second_call_once_first_started():
            await first_call_started.wait()
            with patch("src.main.get_privacy_client", return_value=real_privacy_client), \
                 patch("src.main.verify_api_key", new=AsyncMock()), \
                 patch("src.main.extract_attribution_context", return_value={
                     "app_id": "werking-report", "user_id": None, "workflow_id": None,
                     "session_id": None, "job_id": None,
                 }):
                await src.main.convert_pdf_endpoint(request=_make_request(), credentials=None)
            release_first.set()

        with patch("src.main.get_privacy_client", return_value=real_privacy_client), \
             patch("src.main.verify_api_key", new=AsyncMock()), \
             patch("src.main.extract_attribution_context", return_value={
                 "app_id": "werking-report", "user_id": None, "workflow_id": None,
                 "session_id": None, "job_id": None,
             }):
            await asyncio.gather(
                src.main.convert_pdf_endpoint(request=_make_request(), credentials=None),
                second_call_once_first_started(),
            )

        stats = pm.get_prompt_metrics().get_stats(hours=0, agent_ids=DOCUMENT_AGENT_IDS)
        agent = next(a for a in stats["agents"] if a["agent_id"] == "pdf-konvertierung")
        assert agent["calls"] == 2
        assert agent["busy_samples"] == 2
        assert agent["busy_starts"] == 1  # exactly one of the two overlapped
        assert real_privacy_client.active_calls == 0  # gauge fully unwound
