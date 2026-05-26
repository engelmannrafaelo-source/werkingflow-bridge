"""
Tests for src.identity.webhook_dispatcher — the background loop that POSTs
HMAC-signed auth-token webhooks to app receivers.

The dispatcher's contract surface is `process_one_pending(pool, client)` —
a single end-to-end iteration. The tests drive that function with mocked
DB pool + httpx client and assert:

  - delivered  ← 2xx
  - failed     ← 4xx (no retry)
  - retry      ← 5xx with exponential backoff, attempts++
  - abandoned  ← after MAX_ATTEMPTS retries
  - transport-error path matches 5xx/retry semantics
  - HMAC signature headers are present and correct
  - cleartext is scrubbed (set to NULL) on every terminal state
"""
from __future__ import annotations

import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

import pytest

import src.identity.webhook_dispatcher as wd
from src.identity import webhook_config
from src.identity.webhook_signature import verify


# ---------------------------------------------------------------------------
# Test infrastructure: DB pool + httpx mocks
# ---------------------------------------------------------------------------

def _make_pool(claim_row: Optional[Dict[str, Any]]):
    """
    Build a pool where the next `_claim_and_load_one` call returns
    `claim_row` (or None for "no work"), and `execute` is a recorder.
    """
    conn = AsyncMock()
    conn.execute = AsyncMock(return_value=None)
    conn.fetchrow = AsyncMock(side_effect=[claim_row, None])

    @asynccontextmanager
    async def _tx():
        yield None

    conn.transaction = _tx

    @asynccontextmanager
    async def _acquire():
        yield conn

    pool = MagicMock()
    pool.acquire = _acquire
    return pool, conn


def _make_row(
    *,
    delivery_id: Optional[uuid.UUID] = None,
    token_id: Optional[uuid.UUID] = None,
    user_id: Optional[uuid.UUID] = None,
    app_id: str = "werking-report",
    kind: str = "reset",
    attempts: int = 0,
    cleartext: str = "deadbeef-cleartext",
    email: str = "alice@example.com",
) -> Dict[str, Any]:
    return {
        "delivery_id": delivery_id or uuid.uuid4(),
        "token_id": token_id or uuid.uuid4(),
        "app_id": app_id,
        "kind": kind,
        "attempts": attempts,
        "token_cleartext": cleartext,
        "expires_at": datetime.now(timezone.utc) + timedelta(hours=24),
        "user_id": user_id or uuid.uuid4(),
        "email": email,
        "anonymized_at": None,
    }


class _HttpxResponseStub:
    def __init__(self, status: int, body: str = ""):
        self.status_code = status
        self.text = body


class _HttpxClientStub:
    """
    Asynchronous httpx.AsyncClient stand-in. Records POSTs and returns a
    queued response. Failures (transport errors) are queued as exceptions.
    """
    def __init__(self, responses: List[Any]):
        self._responses = list(responses)
        self.calls: List[Dict[str, Any]] = []

    async def post(self, url, content, headers, timeout):
        self.calls.append(
            {"url": url, "content": content, "headers": dict(headers), "timeout": timeout}
        )
        next_resp = self._responses.pop(0)
        if isinstance(next_resp, Exception):
            raise next_resp
        return next_resp


@pytest.fixture
def configured_webhook(monkeypatch):
    """Wire one app's webhook config so `get_webhook_config` returns it."""
    monkeypatch.setenv("BRIDGE_WEBHOOK_URL_WERKING_REPORT", "https://app.example/auth-webhook")
    monkeypatch.setenv("BRIDGE_WEBHOOK_SECRET_WERKING_REPORT", "shared-secret")
    monkeypatch.setenv("BRIDGE_WEBHOOK_URL_WERKING_ENERGY", "https://energy.example/wh")
    monkeypatch.setenv("BRIDGE_WEBHOOK_SECRET_WERKING_ENERGY", "energy-secret")
    monkeypatch.setenv("BRIDGE_WEBHOOK_URL_WERKING_SAFETY", "https://safety.example/wh")
    monkeypatch.setenv("BRIDGE_WEBHOOK_SECRET_WERKING_SAFETY", "safety-secret")
    monkeypatch.setenv("BRIDGE_WEBHOOK_URL_WERKING_NOISE", "https://noise.example/wh")
    monkeypatch.setenv("BRIDGE_WEBHOOK_SECRET_WERKING_NOISE", "noise-secret")
    webhook_config.reset_for_tests()
    webhook_config.init_webhook_configs()
    yield
    webhook_config.reset_for_tests()


# ---------------------------------------------------------------------------
# Backoff schedule sanity
# ---------------------------------------------------------------------------

class TestBackoff:
    def test_attempts_1_to_5_use_schedule(self):
        # ADR schedule: 1m, 5m, 30m, 2h, 12h
        assert wd._backoff_seconds(1) == 60
        assert wd._backoff_seconds(2) == 5 * 60
        assert wd._backoff_seconds(3) == 30 * 60
        assert wd._backoff_seconds(4) == 2 * 3600
        assert wd._backoff_seconds(5) == 12 * 3600

    def test_attempts_zero_or_negative_clamps(self):
        # Defensive: an unexpected 0/-1 must NOT IndexError or KeyError.
        assert wd._backoff_seconds(0) == 60
        assert wd._backoff_seconds(-3) == 60

    def test_attempts_above_schedule_clamps_to_last(self):
        assert wd._backoff_seconds(99) == 12 * 3600


class TestClassify:
    def test_2xx_delivered(self):
        assert wd._classify(200) == "delivered"
        assert wd._classify(204) == "delivered"
        assert wd._classify(299) == "delivered"

    def test_4xx_failed(self):
        assert wd._classify(400) == "failed"
        assert wd._classify(401) == "failed"
        assert wd._classify(404) == "failed"
        assert wd._classify(422) == "failed"
        assert wd._classify(499) == "failed"

    def test_5xx_retry(self):
        assert wd._classify(500) == "retry"
        assert wd._classify(502) == "retry"
        assert wd._classify(599) == "retry"

    def test_none_retry(self):
        # Transport error == None status — treated as retry.
        assert wd._classify(None) == "retry"


# ---------------------------------------------------------------------------
# Payload + signature
# ---------------------------------------------------------------------------

class TestBuildPayload:
    def test_payload_shape_matches_adr(self):
        row = _make_row(email="bob@example.com", kind="reset", cleartext="t0k3n")
        payload = wd._build_payload(row)
        assert payload["token"] == "t0k3n"
        assert payload["kind"] == "reset"
        assert payload["email"] == "bob@example.com"
        assert payload["userId"] == str(row["user_id"])
        assert isinstance(payload["expiresAt"], str)

    def test_missing_cleartext_raises(self):
        """The CHECK constraint should make this unreachable; still defensive."""
        row = _make_row(cleartext="")
        row["token_cleartext"] = None
        with pytest.raises(RuntimeError):
            wd._build_payload(row)


# ---------------------------------------------------------------------------
# End-to-end: process_one_pending
# ---------------------------------------------------------------------------

class TestProcessOnePending:
    @pytest.mark.asyncio
    async def test_no_pending_returns_none(self, configured_webhook):
        pool, conn = _make_pool(claim_row=None)
        client = _HttpxClientStub(responses=[])
        result = await wd.process_one_pending(pool, client)
        assert result is None
        # No HTTP POST, no UPDATE
        assert client.calls == []
        conn.execute.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_happy_path_2xx_marks_delivered_and_scrubs_cleartext(
        self, configured_webhook
    ):
        row = _make_row(app_id="werking-report")
        pool, conn = _make_pool(claim_row=row)
        client = _HttpxClientStub(responses=[_HttpxResponseStub(204, "")])

        status = await wd.process_one_pending(pool, client)
        assert status == "delivered"

        assert len(client.calls) == 1
        call = client.calls[0]
        assert call["url"] == "https://app.example/auth-webhook"
        # Headers carry signature/timestamp/nonce
        assert "X-Bridge-Signature" in call["headers"]
        assert "X-Bridge-Timestamp" in call["headers"]
        assert "X-Bridge-Nonce" in call["headers"]
        # Signature must verify with the configured secret
        ok = verify(
            "shared-secret",
            call["content"],
            timestamp=int(call["headers"]["X-Bridge-Timestamp"]),
            nonce=call["headers"]["X-Bridge-Nonce"],
            signature_hex=call["headers"]["X-Bridge-Signature"],
        )
        assert ok, "HMAC must verify"

        # The DB UPDATE must scrub token_cleartext + set status='delivered'
        sqls = [c.args[0] for c in conn.execute.await_args_list]
        delivered_sql = next((s for s in sqls if "'delivered'" in s), None)
        assert delivered_sql is not None
        assert "token_cleartext = NULL" in delivered_sql

    @pytest.mark.asyncio
    async def test_4xx_marks_failed_no_retry(self, configured_webhook):
        row = _make_row(app_id="werking-report", attempts=0)
        pool, conn = _make_pool(claim_row=row)
        client = _HttpxClientStub(responses=[_HttpxResponseStub(400, '{"error":"bad-hmac"}')])

        status = await wd.process_one_pending(pool, client)
        assert status == "failed"

        sqls = [c.args[0] for c in conn.execute.await_args_list]
        failed_sql = next((s for s in sqls if "'failed'" in s), None)
        assert failed_sql is not None
        # Bridge does NOT schedule a retry on 4xx — next_retry_at is NULLed
        assert "next_retry_at   = NULL" in failed_sql
        assert "token_cleartext = NULL" in failed_sql

    @pytest.mark.asyncio
    async def test_5xx_schedules_retry_with_backoff(self, configured_webhook):
        row = _make_row(attempts=0)  # first attempt
        pool, conn = _make_pool(claim_row=row)
        client = _HttpxClientStub(responses=[_HttpxResponseStub(503, "down")])

        status = await wd.process_one_pending(pool, client)
        assert status == "pending"

        sqls_with_args = list(conn.execute.await_args_list)
        retry_call = next(
            (c for c in sqls_with_args
             if "'pending'" in c.args[0] and "next_retry_at" in c.args[0]),
            None,
        )
        assert retry_call is not None
        # attempts param is first positional — after the first failure it's 1.
        attempts_arg = retry_call.args[1]
        delay_seconds = retry_call.args[2]
        assert attempts_arg == 1
        assert delay_seconds == "60"  # 1-min backoff after first failure
        # Cleartext is NOT scrubbed while still pending (CHECK invariant).
        assert "token_cleartext" not in retry_call.args[0]

    @pytest.mark.asyncio
    async def test_5xx_after_max_attempts_abandons_and_scrubs(self, configured_webhook):
        # The dispatcher increments attempts BEFORE the abandon-check, so
        # `attempts=MAX_ATTEMPTS - 1` is the last-allowed retry.
        row = _make_row(attempts=wd.MAX_ATTEMPTS - 1)
        pool, conn = _make_pool(claim_row=row)
        client = _HttpxClientStub(responses=[_HttpxResponseStub(500, "still down")])

        status = await wd.process_one_pending(pool, client)
        assert status == "abandoned"

        sqls = [c.args[0] for c in conn.execute.await_args_list]
        abandoned_sql = next((s for s in sqls if "'abandoned'" in s), None)
        assert abandoned_sql is not None
        assert "token_cleartext = NULL" in abandoned_sql

    @pytest.mark.asyncio
    async def test_transport_error_treated_as_retry(self, configured_webhook):
        """A network error (ConnectError, timeout) must NOT be classified as 4xx."""
        row = _make_row(attempts=1)
        pool, conn = _make_pool(claim_row=row)
        client = _HttpxClientStub(responses=[ConnectionError("connection refused")])

        status = await wd.process_one_pending(pool, client)
        assert status == "pending"

        retry_call = next(
            (c for c in conn.execute.await_args_list
             if "'pending'" in c.args[0] and "next_retry_at" in c.args[0]),
            None,
        )
        assert retry_call is not None
        attempts_arg = retry_call.args[1]
        assert attempts_arg == 2  # was 1, now 2
        # Body-to-store carries the transport_error repr
        assert "transport_error" in str(retry_call.args[4])

    @pytest.mark.asyncio
    async def test_missing_webhook_config_leaves_pending(self, configured_webhook, monkeypatch):
        """If config drift produces an app_id without webhook config at runtime,
        DO NOT burn an attempt against a wrong URL — leave the row pending and
        log loudly. Boot-time validation should have caught it."""
        # Force a config lookup miss for this app_id
        row = _make_row(app_id="werking-report")
        pool, conn = _make_pool(claim_row=row)
        client = _HttpxClientStub(responses=[])

        # Sabotage get_webhook_config to raise LookupError
        from src.identity import webhook_dispatcher
        monkeypatch.setattr(
            webhook_dispatcher, "get_webhook_config",
            MagicMock(side_effect=LookupError("config gone")),
        )

        status = await wd.process_one_pending(pool, client)
        assert status == "pending"

        # Did NOT POST anything
        assert client.calls == []
        # Did NOT UPDATE the row (still pending, untouched)
        assert not any(
            "UPDATE auth_token_webhook_deliveries" in (c.args[0] if c.args else "")
            for c in conn.execute.await_args_list
        )


# ---------------------------------------------------------------------------
# Apply-outcome direct unit tests (delegated SQL shape)
# ---------------------------------------------------------------------------

class TestApplyOutcome:
    @pytest.mark.asyncio
    async def test_delivered_increments_attempts(self, configured_webhook):
        conn = AsyncMock()
        conn.execute = AsyncMock(return_value=None)
        result = await wd._apply_outcome(
            conn,
            delivery_id=str(uuid.uuid4()),
            token_id=str(uuid.uuid4()),
            app_id="werking-report",
            kind="reset",
            attempts_before=2,
            status_code=200,
            response_body="ok",
            transport_error=None,
        )
        assert result == "delivered"
        call = conn.execute.await_args_list[0]
        assert call.args[1] == 3  # attempts post-increment
        assert call.args[2] == 200


class TestSigningInputBoundary:
    @pytest.mark.asyncio
    async def test_body_used_for_signature_is_exactly_what_hits_the_wire(
        self, configured_webhook
    ):
        """Re-sign the captured bytes; result must match the X-Bridge-Signature
        header. This guards against any double-serialisation bug."""
        row = _make_row(app_id="werking-report", email="x@y.z")
        pool, _conn = _make_pool(claim_row=row)
        client = _HttpxClientStub(responses=[_HttpxResponseStub(204, "")])

        await wd.process_one_pending(pool, client)

        call = client.calls[0]
        # Body should be JSON-serialisable; the contract is "the bytes we
        # signed are the bytes we sent".
        parsed = json.loads(call["content"].decode("utf-8"))
        assert set(parsed.keys()) == {"token", "kind", "email", "expiresAt", "userId"}
        ok = verify(
            "shared-secret",
            call["content"],
            timestamp=int(call["headers"]["X-Bridge-Timestamp"]),
            nonce=call["headers"]["X-Bridge-Nonce"],
            signature_hex=call["headers"]["X-Bridge-Signature"],
        )
        assert ok
