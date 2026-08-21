"""Tests for the two per-user read leaves moved to platform-api (ADR-0009
Schritt 2, worker-DB-free reads): the operator provider pin and user→tenant.

Both are fail-CLOSED, and that is what these tests are really about. The
dangerous failure is not an exception — it is a plausible-looking None:

  * A provider pin read as absent silently routes a Bedrock/EU-pinned customer
    onto the default backend. A 200 comes back, nothing looks wrong, and the
    data residency promise is broken. So: an unanswerable lookup must RAISE,
    including when platform-api is merely undeployed (404 on the route).
  * A tenant read as absent turns an outage into "user has no tenant_id" — a
    400 that blames the caller's data, and, for anything reading None as "not
    tenant-scoped", a write across the isolation boundary.

The one legitimate None is a process with no configured channel to the user
pool at all: no users there, hence no pins.
"""
from __future__ import annotations

import contextlib
import uuid
from unittest.mock import AsyncMock, patch

import pytest

import src.api_auth.tenant_resolver as tr
import src.routing.user_provider_override as upo
from src.platform_client import PlatformResponse, PlatformUnavailable

BEDROCK_PIN = {"provider": "bedrock", "region": "eu-central-1"}


def _resp(status, body):
    return PlatformResponse(status_code=status, json=body)


@contextlib.contextmanager
def _direct_db(enabled: bool):
    """Pin whether a direct connection exists for this process.

    Patched rather than driven through BRIDGE_DB_URL because other test modules
    in the same session leave that variable set; the fallback branch these tests
    exercise is exactly the one that variable selects, so it has to be pinned
    explicitly or the assertion silently tests the other path.
    """
    with patch("src.db.client.is_db_enabled", return_value=enabled):
        yield


@pytest.fixture(autouse=True)
def _clear_caches(monkeypatch):
    upo.invalidate_cache()
    tr.invalidate_tenant_cache()
    # A configured platform-api channel by default; individual tests override.
    monkeypatch.setenv("BRIDGE_SERVICE_TOKEN", "test-token")
    yield
    upo.invalidate_cache()
    tr.invalidate_tenant_cache()


# ── provider pin ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pin_comes_back_from_platform_api():
    uid = str(uuid.uuid4())
    with patch.object(upo, "call_platform",
                      new=AsyncMock(return_value=_resp(200, {"providerConfig": BEDROCK_PIN}))):
        got = await upo.get_user_provider_config(uid)
    assert got == BEDROCK_PIN


@pytest.mark.asyncio
async def test_pin_null_is_a_real_answer_not_an_error():
    uid = str(uuid.uuid4())
    with patch.object(upo, "call_platform",
                      new=AsyncMock(return_value=_resp(200, {"providerConfig": None}))):
        got = await upo.get_user_provider_config(uid)
    assert got is None


@pytest.mark.asyncio
async def test_pin_unavailable_without_db_raises_instead_of_unpinning():
    """THE compliance guard: no answer must never read as 'not pinned'."""
    uid = str(uuid.uuid4())
    with _direct_db(False), patch.object(upo, "call_platform",
                                         new=AsyncMock(side_effect=PlatformUnavailable("down"))):
        with pytest.raises(upo.UserProviderOverrideError):
            await upo.get_user_provider_config(uid)


@pytest.mark.asyncio
async def test_pin_undeployed_route_404_also_raises():
    """An undeployed platform-api answers 404 for the whole route. Reading that
    as 'no pin' would make a missing deployment a silent re-routing."""
    uid = str(uuid.uuid4())
    with _direct_db(False), patch.object(upo, "call_platform",
                                         new=AsyncMock(return_value=_resp(404, {"detail": "Not Found"}))):
        with pytest.raises(upo.UserProviderOverrideError):
            await upo.get_user_provider_config(uid)


@pytest.mark.asyncio
async def test_pin_falls_back_to_direct_db_while_bridge_db_url_exists():
    uid = str(uuid.uuid4())
    with _direct_db(True), \
         patch.object(upo, "call_platform",
                      new=AsyncMock(side_effect=PlatformUnavailable("down"))), \
         patch.object(upo, "fetch_provider_config_from_db",
                      new=AsyncMock(return_value=BEDROCK_PIN)) as direct:
        got = await upo.get_user_provider_config(uid)
    assert got == BEDROCK_PIN
    assert direct.await_count == 1


@pytest.mark.asyncio
async def test_pin_no_configured_channel_means_no_users_hence_no_pin(monkeypatch):
    """A process with neither channel has no user pool at all — a static
    deployment fact, not a failure. This is the ONE legitimate None."""
    monkeypatch.delenv("BRIDGE_SERVICE_TOKEN", raising=False)
    called = AsyncMock()
    with _direct_db(False), patch.object(upo, "call_platform", new=called):
        got = await upo.get_user_provider_config(str(uuid.uuid4()))
    assert got is None
    assert called.await_count == 0


@pytest.mark.asyncio
async def test_pin_read_opts_into_the_bounded_retry():
    uid = str(uuid.uuid4())
    call = AsyncMock(return_value=_resp(200, {"providerConfig": None}))
    with patch.object(upo, "call_platform", new=call):
        await upo.get_user_provider_config(uid)
    assert call.await_args.kwargs["retries"] == 1


@pytest.mark.asyncio
async def test_pin_is_cached_so_the_hot_path_is_a_dict_lookup():
    uid = str(uuid.uuid4())
    call = AsyncMock(return_value=_resp(200, {"providerConfig": BEDROCK_PIN}))
    with patch.object(upo, "call_platform", new=call):
        await upo.get_user_provider_config(uid)
        await upo.get_user_provider_config(uid)
    assert call.await_count == 1


# ── user → tenant ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tenant_comes_back_from_platform_api():
    uid = str(uuid.uuid4())
    with patch.object(tr, "call_platform",
                      new=AsyncMock(return_value=_resp(200, {"found": True, "tenantId": "t-1"}))):
        assert await tr.get_tenant_of_user(uid) == "t-1"


@pytest.mark.asyncio
async def test_tenant_unknown_user_and_no_tenant_stay_distinguishable():
    uid = str(uuid.uuid4())
    with patch.object(tr, "call_platform",
                      new=AsyncMock(return_value=_resp(200, {"found": False, "tenantId": None}))):
        with pytest.raises(Exception) as unknown:
            await tr.resolve_tenant_for_user(uid)
    assert "Unknown user" in str(unknown.value.detail)

    tr.invalidate_tenant_cache()
    with patch.object(tr, "call_platform",
                      new=AsyncMock(return_value=_resp(200, {"found": True, "tenantId": None}))):
        with pytest.raises(Exception) as no_tenant:
            await tr.resolve_tenant_for_user(uid)
    assert "has no tenant_id" in str(no_tenant.value.detail)


@pytest.mark.asyncio
async def test_tenant_unavailable_raises_and_never_reads_as_no_tenant():
    """The isolation guard: an outage must not become a verdict about the user."""
    uid = str(uuid.uuid4())
    with _direct_db(False), patch.object(tr, "call_platform",
                                         new=AsyncMock(side_effect=PlatformUnavailable("down"))):
        with pytest.raises(tr.TenantLookupUnavailable):
            await tr.get_tenant_of_user(uid)


@pytest.mark.asyncio
async def test_tenant_falls_back_to_direct_db_while_bridge_db_url_exists():
    uid = str(uuid.uuid4())
    with _direct_db(True), \
         patch.object(tr, "call_platform",
                      new=AsyncMock(side_effect=PlatformUnavailable("down"))), \
         patch.object(tr, "fetch_user_tenant_row",
                      new=AsyncMock(return_value={"tenantId": "t-9"})) as direct:
        assert await tr.get_tenant_of_user(uid) == "t-9"
    assert direct.await_count == 1


@pytest.mark.asyncio
async def test_tenant_misses_are_not_cached():
    """A user provisioned a second before its tenant must not stay broken for a
    whole TTL — the reason this cache is positive-only."""
    uid = str(uuid.uuid4())
    call = AsyncMock(side_effect=[
        _resp(200, {"found": True, "tenantId": None}),
        _resp(200, {"found": True, "tenantId": "t-late"}),
    ])
    with patch.object(tr, "call_platform", new=call):
        assert await tr.get_tenant_of_user(uid) is None
        assert await tr.get_tenant_of_user(uid) == "t-late"
    assert call.await_count == 2


@pytest.mark.asyncio
async def test_tenant_hit_is_cached_and_invalidation_drops_it():
    uid = str(uuid.uuid4())
    call = AsyncMock(return_value=_resp(200, {"found": True, "tenantId": "t-1"}))
    with patch.object(tr, "call_platform", new=call):
        await tr.get_tenant_of_user(uid)
        await tr.get_tenant_of_user(uid)
        assert call.await_count == 1
        tr.invalidate_tenant_cache(uid)
        await tr.get_tenant_of_user(uid)
    assert call.await_count == 2
