"""
Regression: a MALFORMED billing identity must fail CLOSED at the budget gate.

Until 2026-08-03 the gate caught every identity-resolution failure in one
`except (ValueError, ...)` and let the call through. That lumped two different
classes together:

  • infra/data conditions (DB hiccup, an email with no Bridge user) — the call
    is well-formed and outside the caller's control → fail-open is deliberate;
  • a caller sending an identity that is not even well-formed → a BUG that
    silently costs money.

The public check funnel sent the marker string `anonymous:public-check-funnel`,
hit the second case, and was waved through unbudgeted: 177 calls / 43,94 € on
the morning of 2026-08-03, while a *registered* customer was stopped at 5 €.

See spec-check-eigene-lizenz-20260803.md §1.3.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from src.budget.gate import enforce_budget
from src.budget.plans import PlanConfig
from src.identity.user_resolver import (
    MalformedUserIdentity,
    UnknownUserIdentity,
    UnresolvableUserIdentity,
)

_UID = uuid.UUID("11111111-2222-3333-4444-555555555555")

_MONTHLY_PLAN = PlanConfig(
    id="report-standard", app_id="werking-report", name="Report Standard",
    price=100.0, interval="month", api_budget_eur=100.0, description="",
)


@pytest.mark.asyncio
async def test_marker_identity_is_rejected_not_waved_through():
    """The exact string that opened the hole must now be refused."""
    with patch("src.budget.gate.find_plan_for_app", return_value=_MONTHLY_PLAN), \
         patch("src.budget.gate.resolve_user_id",
               AsyncMock(side_effect=MalformedUserIdentity("neither uuid nor email"))), \
         patch("src.budget.gate.evaluate_budget", AsyncMock()) as mock_eval:
        with pytest.raises(HTTPException) as exc:
            await enforce_budget("anonymous:public-check-funnel", "werking-report", 1.0)

    # 400, not 402 — the call is malformed, not out of money. Telling the user
    # to top up would be a lie.
    assert exc.value.status_code == 400
    assert exc.value.detail["error"] == "malformed_billing_identity"
    # And it must never have reached the budget evaluation at all.
    mock_eval.assert_not_called()


@pytest.mark.asyncio
async def test_unknown_email_identity_still_fails_open():
    """A well-formed email with no Bridge user keeps the pre-existing policy.

    Blocking an unlicensed-but-well-formed address is a business decision,
    not an architectural one — this change must not quietly make it.
    """
    with patch("src.budget.gate.find_plan_for_app", return_value=_MONTHLY_PLAN), \
         patch("src.budget.gate.resolve_user_id",
               AsyncMock(side_effect=UnknownUserIdentity("no Bridge user"))):
        # Must NOT raise.
        await enforce_budget("someone@example.com", "werking-report", 1.0)


@pytest.mark.asyncio
async def test_resolvable_identity_is_unaffected():
    """The happy path stays exactly as it was."""
    with patch("src.budget.gate.find_plan_for_app", return_value=_MONTHLY_PLAN), \
         patch("src.budget.gate.resolve_user_id", AsyncMock(return_value=_UID)), \
         patch("src.budget.gate.evaluate_budget",
               AsyncMock(return_value={"allowed": True})):
        await enforce_budget(str(_UID), "werking-report", 1.0)


def test_malformed_and_unknown_share_the_legacy_base_class():
    """Existing handlers catch UnresolvableUserIdentity — both kinds must match.

    sandbox/routes.py and routing/user_provider_override.py still catch the
    base class; splitting the exception must not slip past them.
    """
    assert issubclass(MalformedUserIdentity, UnresolvableUserIdentity)
    assert issubclass(UnknownUserIdentity, UnresolvableUserIdentity)
    assert issubclass(UnresolvableUserIdentity, ValueError)
