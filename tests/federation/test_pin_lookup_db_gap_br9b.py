"""BR9b (BR8R2 MUSS 1): platform-api AND its database gone at once is a gap,
not an answer.

The window BR8b exists for: postgres-prod is recreated on server2. The local
platform-api then answers 5xx (its database is gone), the lookup falls back to
the direct DB connection, and that hits the same missing database. Before BR9b
the DB error fell through to the generic handler and came out as the BASE
class UserProviderOverrideError, which BR9 pins to retryable:false — Energy
would stop at once where it should wait.

What must hold now:
- DB unreachable (connection refused/reset, timeout, server shutting down or
  starting) after a temporary platform-api failure → ProviderConfigTemporarilyUnavailable;
- same for the identity resolution's own DB fallback in front of the pin;
- any other DB error (an answer, a SQL defect) stays final;
- fail-closed stays: nothing is guessed in either case.
"""
from __future__ import annotations

import os

os.environ.setdefault("BRIDGE_JWT_SECRET", "test-secret-for-unit-tests")
os.environ.setdefault("BRIDGE_SERVICE_TOKEN", "test-service-token")

from unittest.mock import AsyncMock

import asyncpg
import pytest

from src.federation import set_request_origin
from src.identity import user_resolver
from src.platform_client import PlatformResponse, PlatformTemporarilyUnavailable
from src.routing import user_provider_override as upo

UID = "12a312e3-0000-4000-8000-000000000001"

DB_GONE = [
    pytest.param(ConnectionRefusedError(111, "Connection refused"), id="refused"),
    pytest.param(TimeoutError(), id="timeout"),
    pytest.param(asyncpg.exceptions.CannotConnectNowError("the database system is starting up"),
                 id="starting-up"),
    pytest.param(asyncpg.exceptions.AdminShutdownError("terminating connection"),
                 id="admin-shutdown"),
    pytest.param(asyncpg.exceptions.ConnectionDoesNotExistError("connection was closed"),
                 id="closed-mid-query"),
]

DB_ANSWERED = [
    pytest.param(asyncpg.exceptions.UndefinedColumnError("column does not exist"),
                 id="sql-defect"),
    pytest.param(ValueError("Expecting value"), id="bad-json"),
]


@pytest.fixture(autouse=True)
def _local_worker_with_db(monkeypatch):
    """A worker with both channels configured (platform.env: BRIDGE_DB_URL),
    serving a LOCAL request — the only case with a direct-DB fallback."""
    monkeypatch.setenv("BRIDGE_DB_URL", "postgresql://unused")
    monkeypatch.setenv("BRIDGE_SERVICE_TOKEN", "prod-token")
    set_request_origin(None)
    upo.invalidate_cache()
    user_resolver.invalidate_email_cache()
    yield
    upo.invalidate_cache()
    user_resolver.invalidate_email_cache()


def _platform_down(monkeypatch, module):
    monkeypatch.setattr(module, "call_platform", AsyncMock(
        side_effect=PlatformTemporarilyUnavailable("platform-api 503")))


@pytest.mark.parametrize("db_error", DB_GONE)
async def test_platform_and_db_gone_is_retryable(monkeypatch, db_error):
    _platform_down(monkeypatch, upo)
    monkeypatch.setattr(upo, "fetch_provider_config_from_db",
                        AsyncMock(side_effect=db_error))
    with pytest.raises(upo.ProviderConfigTemporarilyUnavailable) as caught:
        await upo.get_user_provider_config(UID)
    assert caught.value.__cause__ is db_error


@pytest.mark.parametrize("db_error", DB_GONE)
async def test_db_gone_after_unexpected_platform_answer_is_retryable(monkeypatch, db_error):
    """platform-api answered, but not the contract (e.g. 404 before its deploy):
    the DB is asked, and it is not there either. Still unanswered."""
    monkeypatch.setattr(upo, "call_platform", AsyncMock(
        return_value=PlatformResponse(status_code=404, json={"detail": "Not Found"})))
    monkeypatch.setattr(upo, "fetch_provider_config_from_db",
                        AsyncMock(side_effect=db_error))
    with pytest.raises(upo.ProviderConfigTemporarilyUnavailable):
        await upo.get_user_provider_config(UID)


@pytest.mark.parametrize("db_error", DB_ANSWERED)
async def test_other_db_errors_stay_final(monkeypatch, db_error):
    _platform_down(monkeypatch, upo)
    monkeypatch.setattr(upo, "fetch_provider_config_from_db",
                        AsyncMock(side_effect=db_error))
    with pytest.raises(upo.UserProviderOverrideError) as caught:
        await upo.get_user_provider_config(UID)
    assert not isinstance(caught.value, upo.ProviderConfigTemporarilyUnavailable)


@pytest.mark.parametrize("db_error", DB_GONE)
async def test_identity_fallback_with_db_gone_is_retryable(monkeypatch, db_error):
    """Email identity: resolve_user_id falls back to the DB first, before the pin."""
    _platform_down(monkeypatch, user_resolver)
    monkeypatch.setattr(user_resolver, "lookup_user_id_by_email",
                        AsyncMock(side_effect=db_error))
    with pytest.raises(upo.ProviderConfigTemporarilyUnavailable):
        await upo.get_user_provider_config("kunde@example.com")


@pytest.mark.parametrize("db_error", DB_ANSWERED)
async def test_identity_fallback_other_db_errors_stay_final(monkeypatch, db_error):
    _platform_down(monkeypatch, user_resolver)
    monkeypatch.setattr(user_resolver, "lookup_user_id_by_email",
                        AsyncMock(side_effect=db_error))
    with pytest.raises(upo.UserProviderOverrideError) as caught:
        await upo.get_user_provider_config("kunde@example.com")
    assert not isinstance(caught.value, upo.ProviderConfigTemporarilyUnavailable)


async def test_db_answer_after_platform_gap_is_used(monkeypatch):
    """The fallback itself still works: the DB answers, the pin is enforced."""
    _platform_down(monkeypatch, upo)
    monkeypatch.setattr(upo, "fetch_provider_config_from_db",
                        AsyncMock(return_value={"provider": "bedrock"}))
    assert await upo.get_user_provider_config(UID) == {"provider": "bedrock"}


def test_unreachable_classifier():
    from src.db.client import is_db_unreachable

    assert is_db_unreachable(ConnectionResetError())
    assert is_db_unreachable(asyncpg.exceptions.TooManyConnectionsError("too many"))
    assert not is_db_unreachable(RuntimeError("DB pool not initialized"))
    assert not is_db_unreachable(asyncpg.exceptions.UndefinedTableError("no table"))
