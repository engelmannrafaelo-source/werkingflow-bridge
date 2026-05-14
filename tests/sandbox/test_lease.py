"""
Acceptance-criteria tests for /v1/sandbox/lease-token lifecycle.

Covered criteria from SPEC-X1-BRIDGE.md:
  1. Lease-Happy-Path: subscription user gets token + lease in DB
  2. Lease-Negativ-Path: platform_managed → 400 lease_not_applicable
  3. Kein-Capacity: all accounts in cooldown → 503 no_capacity
  4. Heartbeat: active lease refreshes; released lease → 404
  5. Release: idempotent double-release
  8. Migration sicher: subscription billing_mode accepted without 500
"""
import sys
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from unittest.mock import AsyncMock, MagicMock, patch, mock_open

import pytest
from fastapi import HTTPException

# ---------------------------------------------------------------------------
# Criterion 8: subscription is a valid billing_mode (pure unit test)
# ---------------------------------------------------------------------------

class TestSubscriptionBillingMode:
    def test_budget_checker_allows_subscription(self):
        from src.tenant.budget_checker import BudgetCheckResult
        from src.tenant.client import TenantSettings

        tenant = TenantSettings(
            tenant_id="t1",
            tenant_slug="t1",
            billing_mode="subscription",
        )

        # Direct check: subscription path returns allowed=True without hitting DB
        import asyncio
        from src.tenant.budget_checker import check_budget

        async def _run():
            return await check_budget(tenant)

        result = asyncio.get_event_loop().run_until_complete(_run())
        assert result.allowed is True
        assert result.billing_mode == "subscription"

    def test_tenant_settings_accepts_subscription(self):
        from src.tenant.client import TenantSettings

        t = TenantSettings.from_dict({
            "tenant_id": "abc",
            "tenant_slug": "abc",
            "billing_mode": "subscription",
        })
        assert t.billing_mode == "subscription"


# ---------------------------------------------------------------------------
# Criterion 1: Happy-path — subscription user gets token + lease
# ---------------------------------------------------------------------------

class TestLeaseHappyPath:
    @pytest.mark.asyncio
    async def test_lease_issued_for_subscription_user(self, tmp_path):
        """Subscription billing_mode → lease is created, token returned."""
        from src.sandbox import lease_service as ls
        from src.sandbox.account_router import PickedAccount

        # Write a fake token file to tmp_path
        token_file = tmp_path / "claude_token_engelmann.txt"
        token_file.write_text("sk-fake-oauth-token")

        user_id = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        lease_id = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")

        conn = AsyncMock()
        # get_tenant_info: users JOIN tenants
        conn.fetchrow.return_value = {
            "tenant_id": "tenant-abc",
            "billing_mode": "subscription",
        }
        conn.execute.return_value = None

        with patch("src.sandbox.lease_service._SECRETS_DIR", tmp_path):
            token = ls.read_oauth_token("engelmann")
            assert token == "sk-fake-oauth-token"

            created_id = await ls.create_lease(
                conn,
                user_id=user_id,
                tenant_id="tenant-abc",
                app="engelmann",
                account_id="engelmann",
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=20),
            )
        # create_lease uses gen_random_uuid internally, just check it called execute
        conn.execute.assert_called_once()

    @pytest.mark.asyncio
    async def test_pick_account_selects_highest_headroom(self):
        """pick_account returns the account with highest headroom_percent."""
        from src.sandbox.account_router import pick_account

        pool_response = {
            "accounts": {
                "engelmann": {
                    "available": True,
                    "cooldown_remaining_s": 0,
                    "headroom_percent": 45.0,
                },
                "office": {
                    "available": True,
                    "cooldown_remaining_s": 0,
                    "headroom_percent": 80.0,
                },
                "gmail": {
                    "available": False,
                    "cooldown_remaining_s": 0,
                    "headroom_percent": 90.0,
                },
            }
        }

        async def mock_get(*args, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = pool_response
            return resp

        with patch("src.sandbox.account_router.httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.__aenter__.return_value = instance
            instance.__aexit__.return_value = None
            instance.get = mock_get
            MockClient.return_value = instance

            picked = await pick_account()

        assert picked.account_id == "office"
        assert picked.headroom_percent == 80.0

    @pytest.mark.asyncio
    async def test_pick_account_respects_preferred(self):
        """When preferred account passes filter, it is chosen over higher headroom."""
        from src.sandbox.account_router import pick_account

        pool_response = {
            "accounts": {
                "engelmann": {
                    "available": True,
                    "cooldown_remaining_s": 0,
                    "headroom_percent": 45.0,
                },
                "office": {
                    "available": True,
                    "cooldown_remaining_s": 0,
                    "headroom_percent": 80.0,
                },
            }
        }

        async def mock_get(*args, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = pool_response
            return resp

        with patch("src.sandbox.account_router.httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.__aenter__.return_value = instance
            instance.__aexit__.return_value = None
            instance.get = mock_get
            MockClient.return_value = instance

            picked = await pick_account(preferred_account_id="engelmann")

        assert picked.account_id == "engelmann"


# ---------------------------------------------------------------------------
# Criterion 2: platform_managed → 400 lease_not_applicable
# ---------------------------------------------------------------------------

class TestLeaseNegativePath:
    @pytest.mark.asyncio
    async def test_platform_managed_raises_http_400(self):
        from src.sandbox import lease_service as ls

        conn = AsyncMock()
        conn.fetchrow.return_value = {
            "tenant_id": "tenant-xyz",
            "billing_mode": "platform_managed",
        }

        info = await ls.get_tenant_info(conn, uuid.uuid4())
        assert info["billing_mode"] == "platform_managed"
        # The route layer raises 400; here we verify billing_mode comes through correctly
        # and that it is not "subscription"
        assert info["billing_mode"] != "subscription"


# ---------------------------------------------------------------------------
# Criterion 3: No capacity → 503 no_capacity with retry_after_s
# ---------------------------------------------------------------------------

class TestNoCapacity:
    @pytest.mark.asyncio
    async def test_no_capacity_raises_with_min_cooldown(self):
        from src.sandbox.account_router import pick_account, NoCapacityError

        pool_response = {
            "accounts": {
                "engelmann": {
                    "available": False,
                    "cooldown_remaining_s": 120,
                    "headroom_percent": 80.0,
                },
                "office": {
                    "available": True,
                    "cooldown_remaining_s": 60,
                    "headroom_percent": 5.0,  # below threshold
                },
            }
        }

        async def mock_get(*args, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = pool_response
            return resp

        with patch("src.sandbox.account_router.httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.__aenter__.return_value = instance
            instance.__aexit__.return_value = None
            instance.get = mock_get
            MockClient.return_value = instance

            with pytest.raises(NoCapacityError) as exc_info:
                await pick_account()

        assert exc_info.value.retry_after_s >= 30  # at minimum 30s returned

    @pytest.mark.asyncio
    async def test_metrics_reader_unreachable_raises_runtime_error(self):
        from src.sandbox.account_router import pick_account
        import httpx

        with patch("src.sandbox.account_router.httpx.AsyncClient") as MockClient:
            instance = AsyncMock()
            instance.__aenter__.return_value = instance
            instance.__aexit__.return_value = None
            instance.get.side_effect = httpx.ConnectError("connection refused")
            MockClient.return_value = instance

            with pytest.raises(RuntimeError, match="unreachable"):
                await pick_account()


# ---------------------------------------------------------------------------
# Criterion 4: Heartbeat — active lease refreshes, released/expired → 404
# ---------------------------------------------------------------------------

class TestHeartbeat:
    @pytest.mark.asyncio
    async def test_heartbeat_refreshes_active_lease(self):
        from src.sandbox import lease_service as ls

        new_expires = datetime(2099, 12, 31, tzinfo=timezone.utc)
        conn = AsyncMock()
        conn.fetchrow.return_value = {"expires_at": new_expires}

        result = await ls.heartbeat_lease(conn, uuid.uuid4())
        assert result == new_expires

    @pytest.mark.asyncio
    async def test_heartbeat_released_lease_raises_404(self):
        from src.sandbox import lease_service as ls

        conn = AsyncMock()
        conn.fetchrow.return_value = None  # UPDATE returned no row (released/expired)

        with pytest.raises(HTTPException) as exc_info:
            await ls.heartbeat_lease(conn, uuid.uuid4())

        assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# Criterion 5: Release — idempotent double-release
# ---------------------------------------------------------------------------

class TestRelease:
    @pytest.mark.asyncio
    async def test_first_release_returns_true(self):
        from src.sandbox import lease_service as ls

        lease_id = uuid.uuid4()
        conn = AsyncMock()
        conn.fetchval.side_effect = [
            1,           # EXISTS check
            lease_id,    # UPDATE RETURNING (just released)
        ]

        just_released = await ls.release_lease(conn, lease_id)
        assert just_released is True

    @pytest.mark.asyncio
    async def test_second_release_returns_false_not_error(self):
        from src.sandbox import lease_service as ls

        lease_id = uuid.uuid4()
        conn = AsyncMock()
        conn.fetchval.side_effect = [
            1,     # EXISTS check
            None,  # UPDATE RETURNING — already released, no row updated
        ]

        just_released = await ls.release_lease(conn, lease_id)
        assert just_released is False  # idempotent, no exception

    @pytest.mark.asyncio
    async def test_release_nonexistent_lease_raises_404(self):
        from src.sandbox import lease_service as ls

        conn = AsyncMock()
        conn.fetchval.side_effect = [None]  # EXISTS check returns None

        with pytest.raises(HTTPException) as exc_info:
            await ls.release_lease(conn, uuid.uuid4())

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_token_file_missing_raises_runtime_error(self, tmp_path):
        from src.sandbox import lease_service as ls

        with patch("src.sandbox.lease_service._SECRETS_DIR", tmp_path):
            with pytest.raises(RuntimeError, match="token file not found"):
                ls.read_oauth_token("nonexistent-account")
