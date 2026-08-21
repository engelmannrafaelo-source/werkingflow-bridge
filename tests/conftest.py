"""Shared fixtures.

Currently one: `ledger_seam`, the fake for the worker→platform-api money seam
introduced in ADR-0009 Schritt 2c.

Why a fixture and not a mocked asyncpg connection (which is what these tests
used before): the worker no longer HAS a connection. Its money path now states
facts over HTTP, so the honest test double is the seam — "what did the worker
ask platform-api to write", not "what SQL did it emit". Tests that still want
to check the SQL itself now have a home one layer down, against
src/activity/ledger_db.py, where the statements actually live.
"""
from __future__ import annotations

from contextlib import ExitStack
from typing import Any, Dict, List, Optional
from unittest.mock import patch

import pytest

from src.activity import ledger_client


class LedgerSeamFake:
    """Records what the worker sends to platform-api, and can fail on demand.

    `explode_on` simulates an unanswerable call — the HTTP equivalent of the DB
    outage the previous version of these tests simulated. It raises
    PlatformUnavailable, which is precisely what call_platform raises on a
    timeout, a connection error or a 5xx, so the worker sees the real thing.

    Stages: "context" (the users ⋈ tenants read) and "ledger" (the write).
    """

    def __init__(self) -> None:
        self.context: Optional[Dict[str, Any]] = {
            "tenantId": None,  # set per test
            "billingMode": "subscription",
        }
        self.ledger_calls: List[Dict[str, Any]] = []
        self.context_calls: List[str] = []
        self.outcome = "written"
        self.explode_on: Optional[str] = None
        self.anonymous_present = True

    def _maybe_explode(self, stage: str) -> None:
        if self.explode_on == stage:
            from src.platform_client import PlatformUnavailable

            raise PlatformUnavailable(f"platform-api unreachable on {stage} (test)")

    async def anonymous_identity_present(self) -> bool:
        self._maybe_explode("anonymous")
        return self.anonymous_present

    async def load_billing_context(self, user_id: str) -> Optional[Dict[str, Any]]:
        self._maybe_explode("context")
        self.context_calls.append(user_id)
        return self.context

    async def write_ai_call(self, payload: Dict[str, Any]) -> str:
        self._maybe_explode("ledger")
        self.ledger_calls.append(payload)
        return self.outcome

    # ── read helpers ────────────────────────────────────────────────────
    @property
    def row(self) -> Dict[str, Any]:
        """The single money row the worker asked for. Fails loudly when there
        is none — 'no row' is the bug these tests exist to catch, and it should
        not surface as a confusing IndexError deep in an assertion."""
        assert self.ledger_calls, "no usage_events row was requested"
        return self.ledger_calls[0]


@pytest.fixture
def ledger_seam():
    seam = LedgerSeamFake()
    with ExitStack() as stack:
        for name in (
            "anonymous_identity_present",
            "load_billing_context",
            "write_ai_call",
        ):
            stack.enter_context(patch.object(ledger_client, name, getattr(seam, name)))
        yield seam
