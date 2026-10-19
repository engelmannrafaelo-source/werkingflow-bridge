"""R14: close the two surviving R13 mutants.

may_use_app scope line and is_operator with a mixed admin scope.
"""

import pytest
from fastapi import HTTPException

from src import principals
from src.api_auth.deps import AuthClaims, require_admin


def _row(**kw):
    base = dict(
        id="p",
        name="p",
        allowed_apps=["werking-energy"],
        allowed_paths=["*"],
        monthly_cap_eur=None,
        is_legacy=False,
        scopes=["app:werking-energy"],
    )
    base.update(kw)
    return principals.Principal(**base)


@pytest.mark.parametrize(
    "principal,app,expected",
    [
        (_row(), "werking-energy", True),
        (_row(), "werking-report", False),
        # The scope line is the only barrier for legacy-shaped rows
        # without a scope grant.
        (_row(scopes=[]), "werking-energy", False),
        (_row(scopes=[], allowed_apps=["*"]), "werking-energy", False),
        (
            _row(scopes=[], allowed_apps=["werking-energy", "werking-report"]),
            "werking-energy",
            False,
        ),
        (
            _row(scopes=["app:werking-energy"], allowed_apps=["*"]),
            "werking-report",
            False,
        ),
        (
            _row(
                scopes=["app:werking-energy"],
                allowed_apps=["werking-energy", "werking-report"],
            ),
            "werking-report",
            False,
        ),
        (
            _row(scopes=["app:werking-report"], allowed_apps=["werking-energy"]),
            "werking-energy",
            False,
        ),
        (_row(scopes=["bridge:admin"], allowed_apps=[]), "werking-energy", False),
        (_row(scopes=["app:werking-energy", "bridge:admin"]), "werking-energy", False),
        # Only the synthetic legacy principal keeps its allowlist semantics.
        (_row(is_legacy=True, scopes=[], allowed_apps=["*"]), "werking-energy", True),
        (
            _row(is_legacy=True, scopes=[], allowed_apps=["werking-report"]),
            "werking-energy",
            False,
        ),
        (_row(is_legacy=True, scopes=[], allowed_apps=["*"]), None, True),
        (_row(), None, False),
    ],
)
def test_may_use_app_requires_exactly_the_app_scope(principal, app, expected):
    assert principal.may_use_app(app) is expected


def _claims(scopes, allowed_apps=(), acting_user_id=None, kind="service"):
    return AuthClaims(
        kind=kind,
        user_id=None if kind == "service" else "u",
        email=None,
        tenant_id=None,
        is_admin=scopes == ("bridge:admin",),
        acting_user_id=acting_user_id,
        scopes=scopes,
        allowed_apps=allowed_apps,
    )


@pytest.mark.parametrize(
    "claims",
    [
        _claims(("bridge:admin", "app:werking-energy"), ("werking-energy",)),
        _claims(("app:werking-energy", "bridge:admin"), ("werking-energy",)),
        _claims(("bridge:admin", "app:werking-energy")),
        _claims(("bridge:admin",), ("werking-energy",)),
        _claims(("bridge:admin", "bridge:internal")),
        _claims(("bridge:admin",), (), acting_user_id="synthetic-user"),
        _claims(("app:werking-energy",), ("werking-energy",)),
        _claims(("bridge:admin",), kind="user"),
    ],
)
def test_mixed_or_scoped_admin_grant_is_not_operator(claims):
    assert claims.is_operator is False
    with pytest.raises(HTTPException) as exc:
        require_admin(claims)
    assert exc.value.status_code == 403


def test_only_the_isolated_admin_grant_is_operator():
    assert _claims(("bridge:admin",)).is_operator is True
    assert require_admin(_claims(("bridge:admin",))).is_operator is True
