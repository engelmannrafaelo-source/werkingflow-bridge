"""
Regression tests for the privacy-service connect/read timeout split
(src/privacy_client.py) and the deploy smoke's dependency classification
(scripts/bridge_smoke.py).

Both guard the same 2026-08-01 incident:

PRIVACY_SERVICE_URL on the hetzner bridge pointed at a Tailscale-remote host
that became unreachable for ~23 minutes. Every caller passed a SCALAR
``timeout=600.0`` to httpx, and httpx applies a scalar to all four phases —
connect, read, write, pool. So an unreachable service did not fail after "600s
of work"; it fell through to the kernel, whose unanswered-SYN backoff
(tcp_syn_retries=6 → ~63s of retransmits + a final ~64s wait) raised
ConnectError only after ~134s. Measured that day: 133.5s / 134.3s / 135.1s /
136.4s across four different endpoints — a constant, not a coincidence.

Consequence chain: each request pinned a worker slot for ~134s, nginx saw the
resulting 500 and relabelled it as a 503 "bridge at capacity", and the deploy
smoke read that as a broken endpoint and rolled a GOOD image back. The
endpoints recovered ~30s BEFORE the rollback started, on the very image the
smoke had just condemned.

So there are two distinct defects to hold down:
  1. a dead dependency must fail on the connect clock, not the read clock;
  2. a dependency outage must not be reported as a regression in the image.
"""

import importlib
import os
import sys
from unittest.mock import MagicMock as _MM

import httpx
import pytest

# Stub heavy deps before any src.* import (same pattern as
# test_document_capacity_metrics.py).
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

import src.privacy_client as privacy_client  # noqa: E402


# ---------------------------------------------------------------------------
# 1. The timeout split itself
# ---------------------------------------------------------------------------
class TestPrivacyTimeout:
    def test_returns_httpx_timeout_not_scalar(self):
        """A scalar is what caused the incident — the helper must not return one."""
        t = privacy_client.privacy_timeout(600.0)
        assert isinstance(t, httpx.Timeout)

    def test_long_read_budget_is_preserved(self):
        """Slow inference must keep its full budget; only connect is bounded."""
        t = privacy_client.privacy_timeout(1200.0)
        assert t.read == 1200.0
        assert t.write == 1200.0
        assert t.pool == 1200.0

    def test_connect_is_bounded_far_below_the_kernel_syn_timeout(self):
        """THE regression guard.

        The kernel gives up on an unanswered SYN after ~127s. If the connect
        timeout is ever allowed to exceed that again, an unreachable privacy
        service goes back to burning ~134s of a worker slot per request.
        """
        t = privacy_client.privacy_timeout(1200.0)
        assert t.connect is not None
        assert t.connect < 30.0, (
            f"connect timeout {t.connect}s must stay well under the kernel's "
            "~127s unanswered-SYN timeout, or a dead dependency stalls a "
            "worker slot instead of failing fast"
        )

    def test_connect_is_generous_enough_for_a_real_link(self):
        """Must not be so tight that a cold Tailscale/WireGuard path flaps.

        Healthy paths measured 2026-08-01: local container /health ~2ms,
        Tailscale-remote GPU host ~50ms.
        """
        assert privacy_client.privacy_timeout().connect >= 5.0

    def test_connect_timeout_is_env_overridable(self):
        """Operators must be able to retune without a code change."""
        os.environ["PRIVACY_SERVICE_CONNECT_TIMEOUT_S"] = "7"
        try:
            reloaded = importlib.reload(privacy_client)
            assert reloaded.privacy_timeout().connect == 7.0
        finally:
            del os.environ["PRIVACY_SERVICE_CONNECT_TIMEOUT_S"]
            importlib.reload(privacy_client)

    @pytest.mark.asyncio
    async def test_client_is_built_with_the_split_timeout(self):
        """The shared AsyncClient default must carry the bounded connect too.

        /v1/privacy/status passes no per-call timeout, so it inherits this —
        that is why the status endpoint hung 30s during the incident and the
        20s-timeout monitor got nothing back at all.
        """
        client = await privacy_client.PrivacyServiceClient()._get_client()
        try:
            assert isinstance(client.timeout, httpx.Timeout)
            assert client.timeout.connect < 30.0
        finally:
            await client.aclose()

    @pytest.mark.asyncio
    async def test_unreachable_host_fails_on_the_connect_clock(self):
        """End-to-end proof, no network: a black-holed address must fail
        bounded by the CONNECT timeout even though the caller asked for a
        600s read budget.

        192.0.2.0/24 is TEST-NET-1 (RFC 5737) — guaranteed unroutable, so the
        SYN goes unanswered exactly like the outage.

        The exception type differs by who gives up first and both are correct
        fail-fast: httpx's own timer raises ConnectTimeout (this test), while
        in the incident no timer was set below the kernel's, so the OS gave up
        and httpx surfaced ConnectError. Assert their shared parent and let
        the ELAPSED TIME carry the actual contract.
        """
        import time

        os.environ["PRIVACY_SERVICE_CONNECT_TIMEOUT_S"] = "1"
        try:
            reloaded = importlib.reload(privacy_client)
            async with httpx.AsyncClient(base_url="http://192.0.2.1:8100") as c:
                t0 = time.monotonic()
                with pytest.raises(httpx.TransportError):
                    await c.post("/convert-html-to-pdf", json={"html": "<h1>x</h1>"},
                                 timeout=reloaded.privacy_timeout(600.0))
                elapsed = time.monotonic() - t0
            # Bounded by connect (1s), NOT by read (600s) and not by the
            # kernel's ~127s SYN backoff.
            assert elapsed < 15.0, (
                f"took {elapsed:.1f}s — the long read budget is leaking into "
                "the connect phase again"
            )
        finally:
            os.environ.pop("PRIVACY_SERVICE_CONNECT_TIMEOUT_S", None)
            importlib.reload(privacy_client)


# ---------------------------------------------------------------------------
# 2. No caller may reintroduce a scalar timeout
# ---------------------------------------------------------------------------
class TestNoScalarTimeoutsRemain:
    def test_main_passes_no_bare_float_timeout_to_the_privacy_client(self):
        """Static guard over src/main.py.

        Every privacy-service call must route its timeout through
        privacy_timeout(). A bare float silently restores the 134s stall.
        """
        import re

        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        main_py = os.path.join(os.path.dirname(here), "src", "main.py")
        with open(main_py, encoding="utf-8") as fh:
            lines = fh.readlines()

        offenders = []
        for i, line in enumerate(lines, 1):
            if not re.search(r"timeout=\d+(\.\d+)?\s*[,)]", line):
                continue
            # Only the privacy/document proxy block is in scope; find the
            # nearest preceding call to the privacy client.
            window = "".join(lines[max(0, i - 12):i])
            if "privacy_client" in window or "client.post(" in window:
                offenders.append(f"{i}: {line.strip()}")

        assert not offenders, (
            "privacy-service calls must use privacy_timeout(...), not a bare "
            "float (a scalar applies the long budget to connect too):\n  "
            + "\n  ".join(offenders)
        )
