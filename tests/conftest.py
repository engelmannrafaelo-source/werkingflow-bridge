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


@pytest.fixture(autouse=True)
def _no_ambient_research_library(monkeypatch):
    """Kein Test spricht mit dem echten Bibliotheks-Bucket.

    Die Recherche-Pfade loesen die kuratierte Bibliothek aus RESEARCH_LIBRARY_*
    auf. Auf einer Entwickler-Shell sind diese Variablen geladen (Infisical),
    also wuerde ein Unit-Test ueber _execute_research_impl echte S3-Aufrufe
    machen und je nach Maschine anders ausgehen — genau die Sorte
    Umgebungsabhaengigkeit, die einen gruenen Lauf wertlos macht.

    Tests, die die Bibliothek brauchen, setzen sie ausdruecklich selbst
    (tests/research_cloud/test_library_pool.py uebergibt ihre LibraryConfig
    direkt und mockt die S3-Naht).
    """
    for var in (
        "RESEARCH_LIBRARY_ENABLED",
        "RESEARCH_LIBRARY_S3_ENDPOINT_URL",
        "RESEARCH_LIBRARY_S3_BUCKET",
        "RESEARCH_LIBRARY_S3_ACCESS_KEY_ID",
        "RESEARCH_LIBRARY_S3_SECRET_ACCESS_KEY",
        "RESEARCH_LIBRARY_S3_REGION",
        "RESEARCH_LIBRARY_S3_PREFIX",
        "RESEARCH_LIBRARY_MIRROR_DIR",
        # Same reason for Perplexity: a worker env with the flag on would arm
        # the tool in every unrelated executor test.
        "RESEARCH_PERPLEXITY_ENABLED",
        "RESEARCH_PERPLEXITY_PRESET",
        "PERPLEXITY_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)


# --- *_prozessende modules run on their own (BRUMBAU2R P2-5) -----------------
# They need the REAL claude_code_sdk and real process ends. In a session that
# also collects modules which replace the SDK in sys.modules (tests/agentischer_lauf
# imports the real one first, so their own skip guard no longer fires), they
# run in a polluted state and fail (FileNotFoundError .../pids): a whole run is
# then six red tests noisier and the next real red goes unseen. So a mixed run
# leaves them out — loudly, with the call that runs them — and a run of only
# these modules runs them: scripts/test-prozessende.sh, one process per module.
PROZESSENDE_SUFFIX = "_prozessende.py"
PROZESSENDE_AUFRUF = "scripts/test-prozessende.sh"
_PROZESSENDE_KEY = pytest.StashKey()


def pytest_collection_modifyitems(config, items):
    eigene = [i for i in items if i.path.name.endswith(PROZESSENDE_SUFFIX)]
    if not eigene or len(eigene) == len(items):
        return
    items[:] = [i for i in items if not i.path.name.endswith(PROZESSENDE_SUFFIX)]
    config.hook.pytest_deselected(items=eigene)
    module = sorted({i.path.name for i in eigene})
    config.stash[_PROZESSENDE_KEY] = module, len(eigene)


def pytest_terminal_summary(terminalreporter, config):
    abgetrennt = config.stash.get(_PROZESSENDE_KEY, None)
    if abgetrennt:
        module, anzahl = abgetrennt
        terminalreporter.write_line(
            f"PROZESSENDE NICHT GELAUFEN: {anzahl} Tests in {', '.join(module)} laufen "
            f"nur in eigenem Prozess — {PROZESSENDE_AUFRUF}",
            yellow=True, bold=True,
        )
