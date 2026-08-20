"""Tests for the email-identity leaf of src/identity/user_resolver.py
(ADR-0009 Schritt 2b, C1).

Covers the three-stage resolution — cache, platform-api, direct-DB fallback —
and the two contract decisions that carry risk: an unreachable platform-api
must NOT look like "unknown identity" (that would reject a paying caller), and
the lookup must opt into the bounded retry because it is a pure read.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

import src.identity.user_resolver as ur
from src.identity.user_resolver import (
    UnknownUserIdentity,
    resolve_user_id,
)
from src.platform_client import PlatformResponse, PlatformUnavailable


@pytest.fixture(autouse=True)
def _clear_cache():
    ur.invalidate_email_cache()
    yield
    ur.invalidate_email_cache()


@pytest.mark.asyncio
async def test_uuid_identity_never_touches_platform_api():
    """A UUID identity is parsed locally — no hop, no DB, as before."""
    uid = uuid.uuid4()
    with patch.object(ur, "call_platform", new=AsyncMock()) as called:
        got = await resolve_user_id(str(uid))
    assert got == uid
    called.assert_not_awaited()


@pytest.mark.asyncio
async def test_email_resolved_via_platform_api():
    uid = uuid.uuid4()
    resp = PlatformResponse(status_code=200, json={"id": str(uid)})
    with patch.object(ur, "call_platform", new=AsyncMock(return_value=resp)):
        got = await resolve_user_id("kunde@example.tld")
    assert got == uid


@pytest.mark.asyncio
async def test_email_lookup_opts_into_one_retry():
    """Pure read → safe to replay. Guards the opt-in from being dropped."""
    uid = uuid.uuid4()
    resp = PlatformResponse(status_code=200, json={"id": str(uid)})
    with patch.object(ur, "call_platform", new=AsyncMock(return_value=resp)) as called:
        await resolve_user_id("kunde@example.tld")
    assert called.await_args.kwargs["retries"] == 1


@pytest.mark.asyncio
async def test_404_means_unknown_identity():
    resp = PlatformResponse(status_code=200, json={"id": None})
    with patch.object(ur, "call_platform", new=AsyncMock(return_value=resp)):
        with pytest.raises(UnknownUserIdentity):
            await resolve_user_id("niemand@example.tld")


@pytest.mark.asyncio
async def test_platform_unavailable_falls_back_to_direct_db():
    uid = uuid.uuid4()
    with patch.object(ur, "call_platform", new=AsyncMock(side_effect=PlatformUnavailable("down"))), \
         patch.object(ur, "lookup_user_id_by_email", new=AsyncMock(return_value=uid)):
        got = await resolve_user_id("kunde@example.tld")
    assert got == uid


@pytest.mark.asyncio
async def test_unexpected_contract_falls_back_instead_of_rejecting():
    """A malformed platform-api answer must not be read as "no such user" —
    that would refuse a legitimate paying caller over a platform-api bug."""
    uid = uuid.uuid4()
    weird = PlatformResponse(status_code=200, json={"unexpected": "shape"})
    with patch.object(ur, "call_platform", new=AsyncMock(return_value=weird)), \
         patch.object(ur, "lookup_user_id_by_email", new=AsyncMock(return_value=uid)) as db:
        got = await resolve_user_id("kunde@example.tld")
    assert got == uid
    db.assert_awaited_once()


@pytest.mark.asyncio
async def test_result_is_cached_so_a_burst_costs_one_lookup():
    uid = uuid.uuid4()
    resp = PlatformResponse(status_code=200, json={"id": str(uid)})
    with patch.object(ur, "call_platform", new=AsyncMock(return_value=resp)) as called:
        for _ in range(5):
            assert await resolve_user_id("kunde@example.tld") == uid
    assert called.await_count == 1


@pytest.mark.asyncio
async def test_unknown_identity_is_cached_too():
    """A flood of unknown identities must not amplify into a lookup storm."""
    resp = PlatformResponse(status_code=200, json={"id": None})
    with patch.object(ur, "call_platform", new=AsyncMock(return_value=resp)) as called:
        for _ in range(3):
            with pytest.raises(UnknownUserIdentity):
                await resolve_user_id("niemand@example.tld")
    assert called.await_count == 1


@pytest.mark.asyncio
async def test_404_is_a_missing_route_and_falls_back_to_the_db():
    """REGRESSION (2026-08-20, auf der dev-Bridge gemessen): ein noch nicht
    deploytes platform-api antwortet mit 404. Das als "unbekannte Identitaet"
    zu lesen machte jeden Engelmann-Aufrufer unaufloesbar — still, weil 404
    kein PlatformUnavailable ausloest und damit nie den Rueckfall erreicht."""
    uid = uuid.uuid4()
    missing_route = PlatformResponse(status_code=404, json={"detail": "Not Found"})
    with patch.object(ur, "call_platform", new=AsyncMock(return_value=missing_route)), \
         patch.object(ur, "lookup_user_id_by_email", new=AsyncMock(return_value=uid)) as db:
        got = await resolve_user_id("kunde@example.tld")
    assert got == uid
    db.assert_awaited_once()
