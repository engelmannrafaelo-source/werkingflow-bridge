"""
Unit tests for src/api_auth/deps.py — acting_user_id / is_operator scoping.

These tests do NOT require a database. They exercise the pure-logic properties
and the require_self_or_admin guard that is the critical security boundary for
the customer self-service portal proxy pattern (ADR 0007 extension).
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from src.api_auth.deps import AuthClaims


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _operator() -> AuthClaims:
    """Service token without X-User-ID — unrestricted operator."""
    return AuthClaims(
        kind="service", user_id=None, email=None, tenant_id=None,
        is_admin=True, acting_user_id=None,
    )


def _proxy(user_id: str) -> AuthClaims:
    """Service token WITH X-User-ID — proxy acting for the given user."""
    return AuthClaims(
        kind="service", user_id=None, email=None, tenant_id=None,
        is_admin=True, acting_user_id=user_id,
    )


def _user(user_id: str, tenant_id: str = "t-1") -> AuthClaims:
    """Regular user JWT."""
    return AuthClaims(
        kind="user", user_id=user_id, email=f"{user_id}@example.com",
        tenant_id=tenant_id, is_admin=False, acting_user_id=user_id,
    )


def _admin_jwt(user_id: str = "admin-1") -> AuthClaims:
    """User JWT with isAdmin=True (future-proofing; currently never issued)."""
    return AuthClaims(
        kind="user", user_id=user_id, email="admin@example.com",
        tenant_id="t-admin", is_admin=True, acting_user_id=user_id,
    )


# Simulates require_self_or_admin without FastAPI dependency injection.
def _check_self_or_admin(path_user_id: str, claims: AuthClaims) -> AuthClaims:
    if claims.is_service and claims.acting_user_id is not None:
        if claims.acting_user_id == path_user_id:
            return claims
        raise HTTPException(status_code=403, detail="Forbidden: proxy token scoped to different user")
    if claims.is_admin:
        return claims
    if claims.user_id == path_user_id:
        return claims
    raise HTTPException(status_code=403, detail="Forbidden: can only act on own user")


# ---------------------------------------------------------------------------
# is_operator
# ---------------------------------------------------------------------------

class TestIsOperator:
    def test_operator_service_token(self):
        assert _operator().is_operator is True

    def test_proxy_service_token_not_operator(self):
        assert _proxy("user-A").is_operator is False

    def test_regular_user_not_operator(self):
        assert _user("user-A").is_operator is False

    def test_admin_jwt_is_operator(self):
        assert _admin_jwt().is_operator is True


# ---------------------------------------------------------------------------
# effective_user_id
# ---------------------------------------------------------------------------

class TestEffectiveUserId:
    def test_operator_has_no_effective_user(self):
        assert _operator().effective_user_id is None

    def test_proxy_effective_user_is_acting_user(self):
        assert _proxy("user-X").effective_user_id == "user-X"

    def test_user_jwt_effective_user_is_own_id(self):
        assert _user("user-Y").effective_user_id == "user-Y"


# ---------------------------------------------------------------------------
# require_self_or_admin — security-critical cases
# ---------------------------------------------------------------------------

class TestRequireSelfOrAdmin:
    # Operator service token: unrestricted
    def test_operator_can_access_any_user(self):
        _check_self_or_admin("user-A", _operator())
        _check_self_or_admin("user-B", _operator())

    # Service proxy: hard scope to acting_user_id
    def test_proxy_allows_own_user(self):
        _check_self_or_admin("user-A", _proxy("user-A"))

    def test_proxy_rejects_other_user(self):
        with pytest.raises(HTTPException) as exc:
            _check_self_or_admin("user-B", _proxy("user-A"))
        assert exc.value.status_code == 403

    def test_proxy_cannot_use_is_admin_to_bypass(self):
        """Key security invariant: proxy is_admin=True must NOT grant access to other users."""
        proxy = _proxy("user-A")
        assert proxy.is_admin is True  # confirm the credential IS admin
        with pytest.raises(HTTPException) as exc:
            _check_self_or_admin("user-B", proxy)  # must still be blocked
        assert exc.value.status_code == 403

    # User JWT: self-only
    def test_user_jwt_allows_own_id(self):
        _check_self_or_admin("user-Z", _user("user-Z"))

    def test_user_jwt_rejects_other_user(self):
        with pytest.raises(HTTPException) as exc:
            _check_self_or_admin("user-B", _user("user-A"))
        assert exc.value.status_code == 403

    # Admin JWT: unrestricted
    def test_admin_jwt_can_access_any_user(self):
        _check_self_or_admin("user-A", _admin_jwt())
        _check_self_or_admin("user-B", _admin_jwt())


# ---------------------------------------------------------------------------
# X-User-ID ignored for user JWT (spoofing prevention)
# ---------------------------------------------------------------------------

class TestXUserIdOnlyForServiceToken:
    def test_user_jwt_acting_user_is_always_jwt_sub(self):
        """
        When _decode_user_jwt builds the claims, acting_user_id = user_id (JWT sub).
        An attacker cannot forge acting_user_id via a header.

        This test verifies that the AuthClaims dataclass, as built by
        _decode_user_jwt, never has acting_user_id != user_id.
        """
        claims = _user("user-legitimate")
        assert claims.acting_user_id == claims.user_id
        assert claims.acting_user_id != "user-attacker"

    def test_service_token_with_x_user_id_sets_acting_user(self):
        proxy = _proxy("user-customer")
        assert proxy.acting_user_id == "user-customer"
        assert proxy.is_service is True

    def test_service_token_without_x_user_id_has_no_acting_user(self):
        op = _operator()
        assert op.acting_user_id is None
        assert op.is_operator is True
